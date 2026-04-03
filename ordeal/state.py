"""Unified exploration state — what ordeal knows about your code.

Every ordeal tool (mine, mutate, scan, chaos_for, fuzz, explore)
explores one dimension of the state space.  ``ExplorationState``
accumulates their results into a single, persistent, queryable
picture.  AI assistants read this to decide what to explore next.

Quick start — explore everything in one pass::

    from ordeal.state import explore

    state = explore("myapp.scoring")
    print(state.confidence)       # 0.72
    print(state.frontier)         # what's unexplored
    print(state.findings)         # bugs and anomalies

Resume exploration (state persists)::

    state = explore("myapp.scoring", state=state)
    print(state.confidence)       # 0.89 — growing

Use tools individually — they enrich the same state::

    from ordeal import mine, mutate
    state = ExplorationState("myapp.scoring")
    state = explore_mine(state)
    state = explore_mutate(state)
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, dataclass, field
from typing import Any


def _source_hash(func: Any) -> str | None:
    """Hash a function's source code.  Returns ``None`` if unavailable."""
    try:
        source = inspect.getsource(func)
        return hashlib.sha256(source.encode()).hexdigest()[:16]
    except (OSError, TypeError):
        return None


@dataclass
class FunctionState:
    """Exploration state for a single function."""

    name: str
    source_hash: str | None = None

    # mine() results
    mined: bool = False
    properties: list[dict[str, Any]] = field(default_factory=list)
    property_violations: list[str] = field(default_factory=list)
    edges_discovered: int = 0
    saturated: bool = False

    # mutate() results
    mutated: bool = False
    mutation_score: float | None = None
    survived_mutants: int = 0
    killed_mutants: int = 0

    # harden() results — verified tests that close mutation gaps
    hardened: bool = False
    hardened_kills: int = 0

    # scan/fuzz results
    scanned: bool = False
    crash_free: bool | None = None
    fuzz_examples: int = 0

    # chaos testing
    chaos_tested: bool = False
    faults_tested: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Per-function exploration confidence [0, 1].

        Combines coverage across dimensions. Each dimension contributes
        independently — more exploration = higher confidence.
        """
        scores: list[float] = []
        if self.mined:
            # High confidence if many properties hold universally
            total = len(self.properties)
            universal = sum(1 for p in self.properties if p.get("universal", False))
            scores.append(universal / total if total > 0 else 0.5)
        if self.mutated and self.mutation_score is not None:
            # Hardening boosts effective mutation score: verified kills
            # close gaps that the original test suite missed.
            effective = self.mutation_score
            if self.hardened and self.survived_mutants > 0:
                total = self.killed_mutants + self.survived_mutants
                effective = (self.killed_mutants + self.hardened_kills) / total
            scores.append(min(effective, 1.0))
        if self.scanned:
            scores.append(1.0 if self.crash_free else 0.0)
        if self.chaos_tested:
            scores.append(min(1.0, len(self.faults_tested) / 3))
        return sum(scores) / max(len(scores), 1)

    def reset(self) -> None:
        """Clear all exploration results.  Called when source changes."""
        self.source_hash = None
        self.mined = False
        self.properties = []
        self.property_violations = []
        self.edges_discovered = 0
        self.saturated = False
        self.mutated = False
        self.mutation_score = None
        self.survived_mutants = 0
        self.killed_mutants = 0
        self.hardened = False
        self.hardened_kills = 0
        self.scanned = False
        self.crash_free = None
        self.fuzz_examples = 0
        self.chaos_tested = False
        self.faults_tested = []

    @property
    def frontier(self) -> list[str]:
        """What's unexplored for this function."""
        gaps: list[str] = []
        if not self.mined:
            gaps.append("not mined")
        elif self.saturated:
            gaps.append(f"mining saturated ({self.edges_discovered} edges)")
        if not self.mutated:
            gaps.append("not mutation-tested")
        elif self.mutation_score is not None and self.mutation_score < 0.8:
            gaps.append(f"mutation score {self.mutation_score:.0%}")
            unhardened = self.survived_mutants - self.hardened_kills
            if unhardened > 0:
                gaps.append(f"{unhardened} unhardened survivor(s)")
        if not self.scanned:
            gaps.append("not scanned")
        if not self.chaos_tested:
            gaps.append("no chaos testing")
        for v in self.property_violations:
            gaps.append(f"property: {v}")
        return gaps


@dataclass
class ExplorationState:
    """Unified exploration state for a module.

    Accumulates results from all ordeal tools. JSON-serializable
    for persistence across sessions.
    """

    module: str
    functions: dict[str, FunctionState] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    edge_coverage: float | None = None
    exploration_time: float = 0.0
    supervisor_info: dict[str, Any] = field(default_factory=dict)
    tree: Any = field(default=None, repr=False)

    def function(self, name: str) -> FunctionState:
        """Get or create state for a function."""
        if name not in self.functions:
            self.functions[name] = FunctionState(name=name)
        return self.functions[name]

    def refresh(self) -> list[str]:
        """Invalidate functions whose source code changed since last exploration.

        Compares stored ``source_hash`` on each function against the
        current source.  Changed functions are :meth:`reset` — all
        prior results are discarded so the next exploration redoes them
        from scratch.  Fresh source hashes are stamped on every
        function that can be resolved.

        Returns the names of functions that were invalidated.
        """
        from ordeal.auto import _get_public_functions, _resolve_module

        invalidated: list[str] = []
        try:
            mod = _resolve_module(self.module)
            current = {name: func for name, func in _get_public_functions(mod)}
        except Exception:
            return invalidated

        for name, fs in list(self.functions.items()):
            func = current.get(name)
            if func is None:
                # Function removed — reset so frontier shows gaps.
                fs.reset()
                invalidated.append(name)
                continue
            h = _source_hash(func)
            if fs.source_hash is not None and h != fs.source_hash:
                fs.reset()
                invalidated.append(name)
            # Stamp fresh hash regardless (covers first-time and post-reset).
            fs.source_hash = h

        return invalidated

    @property
    def confidence(self) -> float:
        """Aggregate confidence across all functions."""
        if not self.functions:
            return 0.0
        return sum(f.confidence for f in self.functions.values()) / len(self.functions)

    @property
    def frontier(self) -> dict[str, list[str]]:
        """Per-function gaps — what's unexplored."""
        return {name: fs.frontier for name, fs in self.functions.items() if fs.frontier}

    @property
    def findings(self) -> list[str]:
        """All bugs and anomalies found."""
        results: list[str] = []
        for name, fs in self.functions.items():
            if fs.crash_free is False:
                results.append(f"{name}: crashes on random inputs")
            for v in fs.property_violations:
                results.append(f"{name}: {v}")
            if fs.mutation_score is not None and fs.mutation_score < 0.5:
                results.append(f"{name}: mutation score {fs.mutation_score:.0%}")
        return results

    def summary(self) -> str:
        """Human-readable exploration report."""
        lines = [f"Exploration: {self.module}"]
        lines.append(f"  confidence: {self.confidence:.0%}")
        lines.append(f"  functions:  {len(self.functions)}")
        if self.skipped:
            lines.append(f"  skipped:    {len(self.skipped)} functions")
            for name, reason in self.skipped:
                lines.append(f"    {name}: {reason}")
        if self.supervisor_info:
            seed = self.supervisor_info.get("seed", "?")
            traj = self.supervisor_info.get("trajectory_steps", 0)
            states = self.supervisor_info.get("unique_states", 0)
            lines.append(f"  seed: {seed} ({traj} transitions, {states} states)")
        if self.tree and self.tree.size > 0:
            lines.append(
                f"  state tree: {self.tree.size} checkpoints, depth {self.tree.max_depth}"
            )
        if self.findings:
            lines.append(f"  findings:   {len(self.findings)}")
            for f in self.findings:
                lines.append(f"    - {f}")
        frontier = self.frontier
        if frontier:
            lines.append(f"  frontier:   {sum(len(v) for v in frontier.values())} gaps")
            for name, gaps in frontier.items():
                lines.append(f"    {name}: {', '.join(gaps)}")
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize to JSON for persistence.

        The state tree's snapshots are excluded (not JSON-serializable).
        The tree structure is preserved via ``tree.to_json()``.
        """
        from ordeal.suggest import suggest

        data: dict[str, Any] = {
            "module": self.module,
            "confidence": round(self.confidence, 4),
            "functions": {name: asdict(fs) for name, fs in self.functions.items()},
            "findings": self.findings,
            "frontier": self.frontier,
            "suggestions": suggest(self),
            "skipped": self.skipped,
            "exploration_time": round(self.exploration_time, 2),
            "seed": self.supervisor_info.get("seed"),
        }
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, data: str) -> ExplorationState:
        """Deserialize from JSON."""
        raw = json.loads(data)
        state = cls(module=raw["module"])
        state.edge_coverage = raw.get("edge_coverage")
        state.exploration_time = raw.get("exploration_time", 0.0)
        state.supervisor_info = raw.get("supervisor_info", {})
        for name, fdata in raw.get("functions", {}).items():
            fs = FunctionState(**fdata)
            state.functions[name] = fs
        return state


# ============================================================================
# Exploration steps — each enriches the state from one dimension
# ============================================================================


def explore_mine(
    state: ExplorationState,
    *,
    max_examples: int = 50,
    include_private: bool = False,
) -> ExplorationState:
    """Mine all functions in the module and update state."""

    from ordeal.auto import _get_public_functions, _infer_strategies, _resolve_module
    from ordeal.mine import _is_suspicious_property, mine

    mod = _resolve_module(state.module)
    funcs = _get_public_functions(mod, include_private=include_private)

    # Track skipped functions with reasons
    for name, func in funcs:
        strats = _infer_strategies(func, None)
        if strats is None:
            state.skipped.append((name, "can't infer strategies (Optional/complex params)"))

    for name, func in funcs:
        try:
            result = mine(func, max_examples=max_examples)
        except Exception:
            continue
        fs = state.function(name)
        fs.source_hash = _source_hash(func)
        fs.mined = True
        fs.properties = [
            {
                "name": p.name,
                "confidence": p.confidence,
                "universal": p.universal,
                "holds": p.holds,
                "total": p.total,
            }
            for p in result.properties
            if p.total > 0
        ]
        fs.edges_discovered = result.edges_discovered
        fs.saturated = result.saturated
        # Flag suspicious properties
        fs.property_violations = [
            f"{p.name} ({p.confidence:.0%})"
            for p in result.properties
            if _is_suspicious_property(p)
        ]
    return state


def explore_scan(state: ExplorationState, *, max_examples: int = 30) -> ExplorationState:
    """Scan module for crashes and update state."""
    from ordeal.auto import scan_module

    result = scan_module(state.module, max_examples=max_examples)
    for fr in result.functions:
        fs = state.function(fr.name)
        fs.scanned = True
        fs.crash_free = fr.passed
        if fr.property_violations:
            # Merge, don't duplicate
            existing = set(fs.property_violations)
            for v in fr.property_violations:
                if v not in existing:
                    fs.property_violations.append(v)
    return state


def explore_mutate(
    state: ExplorationState,
    *,
    workers: int = 1,
    extra_mutants: dict[str, list[str | tuple[str, str]]] | None = None,
    concern: str | None = None,
    llm: Any | None = None,
) -> ExplorationState:
    """Mutation-test all mined functions and update state.

    Scales with *workers*: more CPUs = more mutants tested in parallel.

    Args:
        state: Exploration state to enrich.
        workers: Parallel workers for mutation testing.
        extra_mutants: Per-function extra mutant source strings, keyed by
            function name.  Written by the AI assistant or human.
        concern: Free-text concern for targeted mutation generation.
        llm: Optional LLM callable for automated mutant generation.
    """
    from ordeal.mutations import mutate

    for name, fs in list(state.functions.items()):
        if fs.mutated:
            continue
        target = f"{state.module}.{name}"
        fn_extras = (extra_mutants or {}).get(name)
        try:
            result = mutate(
                target,
                preset="essential",
                workers=workers,
                extra_mutants=fn_extras,
                concern=concern,
                llm=llm,
            )
        except Exception:
            continue
        fs.mutated = True
        fs.mutation_score = result.score
        fs.killed_mutants = sum(1 for m in result.mutants if m.killed)
        fs.survived_mutants = sum(1 for m in result.mutants if not m.killed)
    return state


def explore_harden(
    state: ExplorationState,
    extra_tests: dict[str, list[str]],
) -> ExplorationState:
    """Verify tests against surviving mutants and update state (Meta ACH pattern).

    For each function in *extra_tests*, re-runs mutation testing to get
    surviving mutants, then verifies each test with the three-assurance
    loop: buildable, valid regression, kills mutant.

    This is the step where an AI assistant closes the loop: it reads
    ``state.frontier`` to find unhardened survivors, writes tests, and
    calls ``explore_harden`` to verify them.

    Args:
        state: Exploration state with prior mutation results.
        extra_tests: Per-function test source strings, keyed by function
            name.  Each test should import the target and assert behavior.

    Returns:
        Updated state with hardening results.
    """
    from ordeal.mutations import mutate

    for name, tests in extra_tests.items():
        fs = state.function(name)
        if not fs.mutated or fs.survived_mutants == 0:
            continue
        target = f"{state.module}.{name}"
        try:
            result = mutate(target, preset="essential")
        except Exception:
            continue
        if not result.survived:
            continue
        hardened = result.harden(tests)
        if hardened.verified:
            fs.hardened = True
            fs.hardened_kills += hardened.total_kills
    return state


def explore_chaos(state: ExplorationState, *, max_examples: int = 10) -> ExplorationState:
    """Auto-generate and run chaos tests, update state."""
    from ordeal.auto import chaos_for

    try:
        TestCase = chaos_for(state.module, max_examples=max_examples, stateful_step_count=10)
        test = TestCase("runTest")
        test.runTest()
    except Exception:
        pass

    # Mark all functions as chaos-tested
    for fs in state.functions.values():
        fs.chaos_tested = True
    return state


def explore(
    module: str,
    *,
    state: ExplorationState | None = None,
    time_limit: float | None = None,
    workers: int = 1,
    max_examples: int = 50,
    seed: int = 42,
    patch_io: bool = False,
    include_private: bool = False,
) -> ExplorationState:
    """Run all exploration strategies on a module.

    Assembles mine → scan → mutate → chaos into one pass.
    Each step enriches the shared ``ExplorationState``.  The entire
    exploration runs inside a ``DeterministicSupervisor`` for
    reproducibility, and checkpoints into a ``StateTree`` so the
    AI can navigate the exploration trajectory.

    Scales with compute: more *workers* → more mutations tested
    in parallel, more *max_examples* → more input space sampled.
    Confidence grows with both.

    Deterministic: same *seed* + same code = same exploration.
    Different seeds explore different regions of the state space.
    The trajectory is logged in ``state.supervisor`` and the
    state tree is in ``state.tree``.

    The AI assistant can also run steps individually via
    ``explore_mine``, ``explore_scan``, ``explore_mutate``,
    ``explore_harden``, ``explore_chaos`` for finer control.

    Args:
        module: Dotted module path.
        state: Resume from a previous exploration. ``None`` starts fresh.
        time_limit: Optional time budget in seconds (soft limit).
        workers: Parallel workers for mutation testing. More CPUs = more
            state space explored per unit time.
        max_examples: Hypothesis examples for mining and scanning. More
            examples = more input space sampled = higher confidence.
        seed: RNG seed for deterministic exploration. Same seed = same
            trajectory. Default 42.
        patch_io: If ``True``, enable deterministic file/network/subprocess
            I/O inside the supervisor while exploring.
    """
    import time as _time

    from ordeal.supervisor import DeterministicSupervisor, StateTree

    if state is None:
        state = ExplorationState(module=module)
    else:
        # Resuming — invalidate any functions whose source changed so
        # the pipeline redoes them from scratch instead of skipping.
        state.refreshed = state.refresh()

    # Initialize supervisor and state tree if not already present
    if not hasattr(state, "supervisor") or state.supervisor is None:
        state.supervisor = None  # set below inside context
    if not hasattr(state, "tree") or state.tree is None:
        state.tree = StateTree()

    sup = DeterministicSupervisor(seed=seed, patch_io=patch_io)
    sup.__enter__()

    try:
        start = _time.monotonic()

        # Checkpoint: initial state
        state_hash = hash(("init", module, seed))
        state.tree.checkpoint(state_hash, snapshot=state, action="start", seed=seed)
        sup.log_transition("explore_start", state_hash=state_hash)

        # Step 1: Mine properties
        state = explore_mine(state, max_examples=max_examples, include_private=include_private)
        mine_hash = hash(("mined", len(state.functions), state.confidence))
        state.tree.checkpoint(
            mine_hash,
            parent=state_hash,
            action="mine",
            snapshot=None,
            edges=sum(f.edges_discovered for f in state.functions.values()),
            seed=seed,
        )
        sup.log_transition("explore_mine", state_hash=mine_hash)
        prev_hash = mine_hash

        # Step 2: Crash safety
        if time_limit is None or (_time.monotonic() - start) < time_limit:
            state = explore_scan(state, max_examples=max_examples)
            scan_hash = hash(("scanned", state.confidence))
            state.tree.checkpoint(
                scan_hash,
                parent=prev_hash,
                action="scan",
                seed=seed,
            )
            sup.log_transition("explore_scan", state_hash=scan_hash)
            prev_hash = scan_hash

        # Step 3: Mutation testing
        if time_limit is None or (_time.monotonic() - start) < time_limit:
            state = explore_mutate(state, workers=workers)
            mutate_hash = hash(("mutated", state.confidence))
            state.tree.checkpoint(
                mutate_hash,
                parent=prev_hash,
                action="mutate",
                seed=seed,
            )
            sup.log_transition("explore_mutate", state_hash=mutate_hash)
            prev_hash = mutate_hash

        # Step 4: Chaos testing
        if time_limit is None or (_time.monotonic() - start) < time_limit:
            state = explore_chaos(state, max_examples=max_examples)
            chaos_hash = hash(("chaos", state.confidence))
            state.tree.checkpoint(
                chaos_hash,
                parent=prev_hash,
                action="chaos",
                seed=seed,
            )
            sup.log_transition("explore_chaos", state_hash=chaos_hash)

        state.exploration_time += _time.monotonic() - start

    finally:
        # Store supervisor info on the state for inspection
        state.supervisor_info = sup.reproduction_info()
        state.supervisor_info["trajectory_steps"] = len(sup.trajectory)
        state.supervisor_info["unique_states"] = len(sup.visited_states)
        sup.__exit__(None, None, None)

    return state
