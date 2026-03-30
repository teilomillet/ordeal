"""Coverage-guided exploration engine with checkpointing.

This is ordeal's answer to Antithesis's exploration engine.  It:

1. Executes ChaosTest rule sequences (including parameterized rules)
2. Tracks edge coverage of the system under test (AFL-style)
3. **Checkpoints** interesting states when new coverage is found
4. **Branches** from checkpoints — exploring many different actions
   from the same rare state
5. **Shrinks** failing traces to the minimal reproducing sequence
6. **Records traces** for replay and post-hoc analysis

    from ordeal.explore import Explorer

    explorer = Explorer(
        MyServiceChaos,
        target_modules=["myapp"],
    )
    result = explorer.run(max_time=60)
    print(result.summary())
"""

from __future__ import annotations

import copy
import importlib
import multiprocessing as mp
import random
import sys
import threading
import time as _time
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import hypothesis.strategies as st

from ordeal.trace import Trace, TraceFailure, TraceStep
from ordeal.trace import shrink as _shrink_trace

if TYPE_CHECKING:
    from ordeal.chaos import ChaosTest


# ============================================================================
# Coverage collection (AFL-style edge hashing)
# ============================================================================


class CoverageCollector:
    """Track edge coverage via ``sys.settrace``.

    Uses AFL-style edge hashing: ``hash(prev_location XOR cur_location)``.
    This captures control-flow *transitions*, not just line visits.

    Thread-safe for free-threaded Python 3.13+: ``_prev_loc`` is per-thread
    (each thread traces its own call stack), and ``_edges`` is lock-protected.
    """

    def __init__(self, target_paths: list[str]) -> None:
        self._targets = target_paths
        self._edges: set[int] = set()
        self._edges_lock = threading.Lock()
        self._tls = threading.local()

    def _is_target(self, filename: str) -> bool:
        """Check if *filename* belongs to one of the target modules.

        Uses path-segment matching so ``"app"`` matches ``app/foo.py``
        but not ``myapp/foo.py``.  Handles both directory segments
        and filename segments (stripping ``.py`` extension).
        """
        normalized = filename.replace("\\", "/")
        # Build segments, stripping .py from the last one
        segments = normalized.split("/")
        bare_segments = [s.removesuffix(".py") if s.endswith(".py") else s for s in segments]
        for target in self._targets:
            # Convert dotted module path to slash-separated segments
            target_parts = target.replace(".", "/").split("/")
            n = len(target_parts)
            # Check if target_parts appear as a contiguous subsequence
            for i in range(len(bare_segments) - n + 1):
                if bare_segments[i : i + n] == target_parts:
                    return True
        return False

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        if event == "line" and self._is_target(frame.f_code.co_filename):
            loc = hash((frame.f_code.co_filename, frame.f_lineno)) & 0xFFFF
            prev = getattr(self._tls, "prev_loc", 0)
            with self._edges_lock:
                self._edges.add(prev ^ loc)
            self._tls.prev_loc = loc >> 1
        return self._trace

    def start(self) -> None:
        """Reset state and begin collecting edge coverage via ``sys.settrace``."""
        self._tls.prev_loc = 0
        with self._edges_lock:
            self._edges.clear()
        sys.settrace(self._trace)

    def stop(self) -> frozenset[int]:
        """Stop collection and return the set of observed edges."""
        sys.settrace(None)
        with self._edges_lock:
            return frozenset(self._edges)

    def snapshot(self) -> frozenset[int]:
        """Current edges without stopping collection."""
        with self._edges_lock:
            return frozenset(self._edges)


# ============================================================================
# Rule introspection
# ============================================================================


@dataclass
class _RuleInfo:
    """Metadata about a single @rule method."""

    name: str
    strategies: dict[str, st.SearchStrategy]  # param_name -> SearchStrategy (from Hypothesis)
    has_data: bool = False  # True if one param is data=st.data()


# ============================================================================
# Data proxy — lets the explorer call @rule(data=st.data()) methods
# ============================================================================


class _DataProxy:
    """Stand-in for Hypothesis's ``data`` object.

    Records every draw for trace replay.
    """

    def __init__(self) -> None:
        self.draws: list[tuple[str, Any]] = []

    def draw(self, strategy: st.SearchStrategy[Any], label: str | None = None) -> Any:
        """Draw a value from a Hypothesis strategy."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            value = strategy.example()
        self.draws.append((label or "", value))
        return value


# ============================================================================
# Checkpoint with energy
# ============================================================================

_ENERGY_REWARD = 2.0
_ENERGY_DECAY = 0.95
_ENERGY_MIN = 0.01


@dataclass
class Checkpoint:
    """A saved machine state with energy-based scheduling weight."""

    machine_copy: ChaosTest
    new_edge_count: int
    step: int
    run_id: int
    energy: float = 1.0
    times_selected: int = 0


# ============================================================================
# Progress reporting
# ============================================================================


@dataclass
class ProgressSnapshot:
    """Live stats emitted during exploration."""

    elapsed: float
    total_runs: int
    total_steps: int
    unique_edges: int
    checkpoints: int
    failures: int
    runs_per_second: float


# ============================================================================
# Results
# ============================================================================


@dataclass
class Failure:
    """A failure found during exploration, with optional trace for replay."""

    error: Exception
    step: int
    run_id: int
    active_faults: list[str]
    rule_log: list[str]
    trace: Trace | None = None

    def __str__(self) -> str:
        faults = ", ".join(self.active_faults) or "none"
        last_rules = " -> ".join(self.rule_log[-10:])
        shrunk = ""
        if self.trace:
            shrunk = f" (shrunk to {len(self.trace.steps)} steps)"
        return (
            f"Run {self.run_id}, step {self.step}: "
            f"{type(self.error).__name__}: {self.error}{shrunk}\n"
            f"  Active faults: {faults}\n"
            f"  Sequence: {last_rules}"
        )


@dataclass
class ExplorationResult:
    """Aggregated results from an exploration run."""

    total_runs: int = 0
    total_steps: int = 0
    unique_edges: int = 0
    checkpoints_saved: int = 0
    failures: list[Failure] = field(default_factory=list)
    duration_seconds: float = 0.0
    edge_log: list[tuple[int, int]] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable exploration summary."""
        lines = [
            f"Exploration: {self.total_runs} runs, "
            f"{self.total_steps} steps, {self.duration_seconds:.1f}s",
            f"Coverage: {self.unique_edges} edges, {self.checkpoints_saved} checkpoints",
        ]
        if self.failures:
            lines.append(f"Failures found: {len(self.failures)}")
            for f in self.failures[:5]:
                lines.append(f"  {f}")
        else:
            lines.append("No failures found.")
        return "\n".join(lines)


# ============================================================================
# Explorer
# ============================================================================


def _qualified_name(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


class Explorer:
    """Coverage-guided exploration engine for ChaosTest.

    Compared to Hypothesis (random search + shrinking), the Explorer uses
    coverage feedback and energy-based checkpoint scheduling to find bugs
    at the intersection of features — the class of bugs that random testing
    almost never reaches.
    """

    def __init__(
        self,
        test_class: type,
        *,
        target_modules: list[str] | None = None,
        seed: int = 42,
        max_checkpoints: int = 256,
        checkpoint_prob: float = 0.4,
        checkpoint_strategy: str = "energy",
        fault_toggle_prob: float = 0.3,
        record_traces: bool = False,
        workers: int = 1,
    ) -> None:
        """Initialize the exploration engine.

        Args:
            test_class: A ChaosTest subclass to explore.
            target_modules: Dotted module names for coverage (e.g. ``["myapp"]``).
            seed: RNG seed for reproducible runs.
            max_checkpoints: Checkpoint corpus size limit.
            checkpoint_prob: Probability of starting from a checkpoint.
            checkpoint_strategy: ``"energy"`` | ``"uniform"`` | ``"recent"``.
            fault_toggle_prob: Probability of nemesis action per step.
            record_traces: If True, keep full traces in the result.
            workers: Number of parallel worker processes. Each gets a unique
                seed for independent state-space exploration. Default 1
                (sequential). Use ``os.cpu_count()`` for full utilization.
        """
        self.test_class = test_class
        self.target_paths = [m.replace(".", "/") for m in (target_modules or [])]
        self.target_modules = target_modules
        self.rng = random.Random(seed)
        self.seed = seed
        self.max_checkpoints = max_checkpoints
        self.checkpoint_prob = checkpoint_prob
        self.checkpoint_strategy = checkpoint_strategy
        self.fault_toggle_prob = fault_toggle_prob
        self.record_traces = record_traces
        self.workers = max(1, workers)

        # Internal state
        self._total_edges: set[int] = set()
        self._checkpoints: list[Checkpoint] = []
        self._rules: list[_RuleInfo] = []
        self._invariant_names: list[str] = []

    # -- Discovery ----------------------------------------------------------

    def _discover(self) -> None:
        """Introspect the test class for rules (including parameterized) and invariants."""
        self._rules.clear()
        self._invariant_names.clear()
        skip = {"_nemesis", "_swarm_init"}

        for name in dir(self.test_class):
            attr = getattr(self.test_class, name, None)
            if attr is None:
                continue

            # Rules — read strategy info from Hypothesis metadata
            rule_meta = getattr(attr, "hypothesis_stateful_rule", None)
            if rule_meta is not None and name not in skip:
                strategies: dict[str, Any] = {}
                has_data = False

                if hasattr(rule_meta, "arguments_strategies"):
                    strategies = dict(rule_meta.arguments_strategies)
                elif hasattr(rule_meta, "arguments"):
                    strategies = dict(rule_meta.arguments)

                # Detect data=st.data() parameter
                for param_name, strat in strategies.items():
                    strat_repr = repr(strat).lower()
                    is_data = "dataobject" in strat_repr or "data()" in strat_repr
                    if param_name == "data" or is_data:
                        has_data = True

                # Skip Bundle-consuming rules (can't execute outside Hypothesis)
                if hasattr(rule_meta, "bundles") and rule_meta.bundles:
                    continue

                self._rules.append(
                    _RuleInfo(
                        name=name,
                        strategies=strategies,
                        has_data=has_data,
                    )
                )

            # Invariants
            if hasattr(attr, "hypothesis_stateful_invariant"):
                self._invariant_names.append(name)

    # -- Execution ----------------------------------------------------------

    def _execute_rule(self, machine: ChaosTest, rule: _RuleInfo) -> dict[str, Any]:
        """Execute a rule, drawing parameters from strategies. Returns drawn params."""
        params: dict[str, Any] = {}

        for param_name, strategy in rule.strategies.items():
            if param_name == "data" or rule.has_data and param_name == "data":
                params["data"] = _DataProxy()
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    try:
                        params[param_name] = strategy.example()
                    except Exception:
                        pass  # skip unresolvable params

        try:
            getattr(machine, rule.name)(**params)
        except TypeError:
            # Fallback: call with no args (rule may have defaults)
            getattr(machine, rule.name)()

        return params

    def _toggle_fault(self, machine: ChaosTest) -> str:
        """Toggle a random fault. Returns signed name like ``+name`` or ``-name``."""
        fault = self.rng.choice(machine._faults)
        if fault.active:
            fault.deactivate()
            return f"-{fault.name}"
        fault.activate()
        return f"+{fault.name}"

    def _check_invariants(self, machine: ChaosTest) -> None:
        """Run all @invariant methods."""
        for name in self._invariant_names:
            getattr(machine, name)()

    # -- Checkpoint scheduling ----------------------------------------------

    def _select_checkpoint(self) -> Checkpoint:
        """Select a checkpoint using the configured strategy."""
        if self.checkpoint_strategy == "energy":
            return self._select_energy()
        elif self.checkpoint_strategy == "recent":
            return self._select_recent()
        return self.rng.choice(self._checkpoints)  # uniform

    def _select_energy(self) -> Checkpoint:
        """Energy-weighted selection: favor checkpoints that led to discoveries."""
        total = sum(cp.energy for cp in self._checkpoints)
        r = self.rng.random() * total
        cumulative = 0.0
        for cp in self._checkpoints:
            cumulative += cp.energy
            if cumulative >= r:
                cp.times_selected += 1
                return cp
        return self._checkpoints[-1]

    def _select_recent(self) -> Checkpoint:
        """Favor recently-created checkpoints."""
        n = len(self._checkpoints)
        weights = list(range(1, n + 1))
        return self.rng.choices(self._checkpoints, weights=weights, k=1)[0]

    def _update_checkpoint_energy(self, cp: Checkpoint, new_edges: int) -> None:
        """Reward checkpoints that led to new discoveries, decay others."""
        if new_edges > 0:
            cp.energy += new_edges * _ENERGY_REWARD
        else:
            cp.energy = max(_ENERGY_MIN, cp.energy * _ENERGY_DECAY)

    def _save_checkpoint(self, machine: ChaosTest, new_count: int, step: int, run_id: int) -> None:
        """Save a checkpoint. Evicts lowest-energy checkpoint if at capacity."""
        if len(self._checkpoints) >= self.max_checkpoints:
            if self.checkpoint_strategy == "energy":
                # Evict lowest energy
                min_idx = min(
                    range(len(self._checkpoints)), key=lambda i: self._checkpoints[i].energy
                )
                self._checkpoints.pop(min_idx)
            else:
                idx = self.rng.randint(0, max(0, len(self._checkpoints) - 2))
                self._checkpoints.pop(idx)

        self._checkpoints.append(
            Checkpoint(
                machine_copy=copy.deepcopy(machine),
                new_edge_count=new_count,
                step=step,
                run_id=run_id,
            )
        )

    # -- Main loop ----------------------------------------------------------

    def run(
        self,
        *,
        max_time: float = 60.0,
        max_runs: int | None = None,
        steps_per_run: int = 50,
        shrink: bool = True,
        max_shrink_time: float = 30.0,
        progress: Callable[[ProgressSnapshot], None] | None = None,
    ) -> ExplorationResult:
        """Run the coverage-guided exploration loop.

        Args:
            max_time: Wall-clock time limit in seconds.
            max_runs: Maximum number of runs (or ``None`` for time-only).
            steps_per_run: Max rule steps per run.
            shrink: If True, shrink failing traces after exploration.
            max_shrink_time: Time limit for shrinking each failure.
            progress: Optional callback for live progress updates.
        """
        if self.workers > 1:
            return self._run_parallel(
                max_time=max_time,
                max_runs=max_runs,
                steps_per_run=steps_per_run,
                shrink=shrink,
                max_shrink_time=max_shrink_time,
                progress=progress,
            )

        self._discover()
        if not self._rules:
            raise ValueError(f"No callable rules found on {self.test_class.__name__}")

        result = ExplorationResult()
        use_coverage = bool(self.target_paths)
        start = _time.monotonic()
        class_name = _qualified_name(self.test_class)

        while True:
            elapsed = _time.monotonic() - start
            if elapsed >= max_time:
                break
            if max_runs is not None and result.total_runs >= max_runs:
                break

            result.total_runs += 1
            run_id = result.total_runs
            rule_log: list[str] = []
            trace_steps: list[TraceStep] = []
            run_start = _time.monotonic()
            source_cp: Checkpoint | None = None

            # -- Start: fresh or from checkpoint --
            from_cp = self._checkpoints and self.rng.random() < self.checkpoint_prob
            if from_cp:
                source_cp = self._select_checkpoint()
                machine = copy.deepcopy(source_cp.machine_copy)
                rule_log.append(f"[checkpoint r{source_cp.run_id}s{source_cp.step}]")
            else:
                machine = self.test_class()

            n_steps = self.rng.randint(1, steps_per_run)
            collector = CoverageCollector(self.target_paths) if use_coverage else None
            if collector:
                collector.start()

            step = 0
            new_edges_this_run = 0
            try:
                for step in range(n_steps):
                    result.total_steps += 1
                    ts_offset = _time.monotonic() - run_start

                    # Nemesis or rule
                    if machine._faults and self.rng.random() < self.fault_toggle_prob:
                        toggle_name = self._toggle_fault(machine)
                        rule_log.append(toggle_name)
                        trace_steps.append(
                            TraceStep(
                                kind="fault_toggle",
                                name=toggle_name,
                                active_faults=[f.name for f in machine.active_faults],
                                edge_count=len(self._total_edges) + new_edges_this_run,
                                timestamp_offset=ts_offset,
                            )
                        )
                    else:
                        rule_info = self.rng.choice(self._rules)
                        params = self._execute_rule(machine, rule_info)
                        rule_log.append(rule_info.name)

                        # Record serializable params (skip _DataProxy)
                        serializable_params = {
                            k: v for k, v in params.items() if not isinstance(v, _DataProxy)
                        }
                        trace_steps.append(
                            TraceStep(
                                kind="rule",
                                name=rule_info.name,
                                params=serializable_params,
                                active_faults=[f.name for f in machine.active_faults],
                                edge_count=len(self._total_edges) + new_edges_this_run,
                                timestamp_offset=ts_offset,
                            )
                        )

                    # Invariants
                    self._check_invariants(machine)

                    # Coverage
                    if collector:
                        edges = collector.snapshot()
                        new = edges - self._total_edges
                        if new:
                            new_edges_this_run += len(new)
                            self._total_edges |= new
                            self._save_checkpoint(machine, len(new), step, run_id)
                            result.checkpoints_saved += 1

            except Exception as e:
                trace = Trace(
                    run_id=run_id,
                    seed=self.seed,
                    test_class=class_name,
                    from_checkpoint=source_cp.run_id if source_cp else None,
                    steps=trace_steps,
                    failure=TraceFailure(
                        error_type=type(e).__name__,
                        error_message=str(e)[:500],
                        step=step,
                    ),
                    edges_discovered=new_edges_this_run,
                    duration=_time.monotonic() - run_start,
                )
                result.failures.append(
                    Failure(
                        error=e,
                        step=step,
                        run_id=run_id,
                        active_faults=[f.name for f in machine.active_faults],
                        rule_log=rule_log,
                        trace=trace,
                    )
                )
                if self.record_traces:
                    result.traces.append(trace)

            else:
                if self.record_traces:
                    result.traces.append(
                        Trace(
                            run_id=run_id,
                            seed=self.seed,
                            test_class=class_name,
                            from_checkpoint=source_cp.run_id if source_cp else None,
                            steps=trace_steps,
                            edges_discovered=new_edges_this_run,
                            duration=_time.monotonic() - run_start,
                        )
                    )
            finally:
                if collector:
                    collector.stop()
                machine.teardown()

            # Update checkpoint energy
            if source_cp is not None:
                self._update_checkpoint_energy(source_cp, new_edges_this_run)

            result.edge_log.append((run_id, len(self._total_edges)))

            # Progress callback
            if progress:
                elapsed_now = _time.monotonic() - start
                progress(
                    ProgressSnapshot(
                        elapsed=elapsed_now,
                        total_runs=result.total_runs,
                        total_steps=result.total_steps,
                        unique_edges=len(self._total_edges),
                        checkpoints=len(self._checkpoints),
                        failures=len(result.failures),
                        runs_per_second=result.total_runs / max(elapsed_now, 0.001),
                    )
                )

        # -- Post-exploration: shrink failures --
        if shrink:
            for failure in result.failures:
                if failure.trace and failure.trace.steps:
                    failure.trace = _shrink_trace(
                        failure.trace,
                        self.test_class,
                        max_time=max_shrink_time,
                    )

        result.unique_edges = len(self._total_edges)
        result.duration_seconds = _time.monotonic() - start
        return result

    # -- Parallel execution -------------------------------------------------

    def _run_parallel(
        self,
        *,
        max_time: float,
        max_runs: int | None,
        steps_per_run: int,
        shrink: bool,
        max_shrink_time: float,
        progress: Callable[[ProgressSnapshot], None] | None,
    ) -> ExplorationResult:
        """Run exploration across multiple worker processes.

        Each worker gets a unique seed (base + i*7919) for independent
        state-space exploration.  Results are aggregated: runs/steps are
        summed, edges are unioned for true unique count.
        """
        start = _time.monotonic()

        class_path = f"{self.test_class.__module__}.{self.test_class.__qualname__}"

        worker_args = []
        for i in range(self.workers):
            worker_args.append(
                {
                    "class_path": class_path,
                    "target_modules": self.target_modules,
                    "seed": self.seed + i * 7919,
                    "max_time": max_time,
                    "max_runs": max_runs,
                    "steps_per_run": steps_per_run,
                    "max_checkpoints": self.max_checkpoints,
                    "checkpoint_prob": self.checkpoint_prob,
                    "checkpoint_strategy": self.checkpoint_strategy,
                    "fault_toggle_prob": self.fault_toggle_prob,
                    "record_traces": self.record_traces,
                    "shrink": shrink,
                    "max_shrink_time": max_shrink_time,
                }
            )

        ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
        with ctx.Pool(self.workers) as pool:
            worker_results = pool.map(_worker_fn, worker_args)

        # Aggregate results
        result = ExplorationResult()
        all_edges: set[int] = set()

        for wr in worker_results:
            result.total_runs += wr["total_runs"]
            result.total_steps += wr["total_steps"]
            result.checkpoints_saved += wr["checkpoints_saved"]
            result.edge_log.extend(wr["edge_log"])
            all_edges.update(wr["edges"])

            # Reconstruct minimal Failure objects from serialized data
            for finfo in wr["failures"]:
                result.failures.append(
                    Failure(
                        error=RuntimeError(finfo["error_message"]),
                        step=finfo["step"],
                        run_id=finfo["run_id"],
                        active_faults=finfo["active_faults"],
                        rule_log=finfo["rule_log"],
                    )
                )

        result.unique_edges = len(all_edges)
        self._total_edges = all_edges
        result.duration_seconds = _time.monotonic() - start
        return result


def _worker_fn(args: dict[str, Any]) -> dict[str, Any]:
    """Worker process: import test class, run single-worker Explorer, return results.

    Defined at module level so it can be pickled by multiprocessing.
    """
    class_path = args["class_path"]
    module_path, _, class_name = class_path.rpartition(".")
    mod = importlib.import_module(module_path)
    test_class = getattr(mod, class_name)

    explorer = Explorer(
        test_class,
        target_modules=args.get("target_modules"),
        seed=args["seed"],
        max_checkpoints=args["max_checkpoints"],
        checkpoint_prob=args["checkpoint_prob"],
        checkpoint_strategy=args["checkpoint_strategy"],
        fault_toggle_prob=args["fault_toggle_prob"],
        record_traces=args.get("record_traces", False),
        workers=1,  # each worker runs sequentially
    )

    result = explorer.run(
        max_time=args["max_time"],
        max_runs=args.get("max_runs"),
        steps_per_run=args["steps_per_run"],
        shrink=args.get("shrink", True),
        max_shrink_time=args.get("max_shrink_time", 30.0),
    )

    # Serialize — exceptions and traces don't pickle cleanly across processes
    serialized_failures = []
    for f in result.failures:
        serialized_failures.append(
            {
                "error_message": str(f.error)[:500],
                "step": f.step,
                "run_id": f.run_id,
                "active_faults": f.active_faults,
                "rule_log": f.rule_log,
            }
        )

    return {
        "total_runs": result.total_runs,
        "total_steps": result.total_steps,
        "unique_edges": result.unique_edges,
        "checkpoints_saved": result.checkpoints_saved,
        "duration_seconds": result.duration_seconds,
        "failures": serialized_failures,
        "edge_log": result.edge_log,
        "edges": list(explorer._total_edges),
    }
