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
import os
import pickle
import random
import struct
import sys
import threading
import time as _time
import warnings
import zlib
from dataclasses import dataclass, field
from pathlib import Path
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

    Optimizations over naive per-line locking:

    - **Filename cache**: ``_is_target`` result is cached per filename so
      the path-segment check runs at most once per unique file.
    - **Thread-local edge buffer**: Edges accumulate in a per-thread list
      and are flushed to the shared set every 256 edges, reducing lock
      acquisitions by ~256x.
    - **Snapshot caching**: ``snapshot()`` returns a cached ``frozenset``
      when no new edges have arrived since the last call, avoiding
      repeated O(n) construction on steps that don't discover new paths.

    Thread-safe for free-threaded Python 3.13+: ``_prev_loc`` and the
    edge buffer are per-thread, and ``_edges`` is lock-protected.
    """

    _FLUSH_THRESHOLD = 256

    def __init__(self, target_paths: list[str]) -> None:
        self._targets = target_paths
        # Pre-split target paths into tuples of segments once at init.
        # Avoids repeated string splitting on every _is_target call.
        self._target_tuples: list[tuple[str, ...]] = [
            tuple(t.replace(".", "/").split("/")) for t in target_paths
        ]
        self._edges: set[int] = set()
        self._edges_lock = threading.Lock()
        self._tls = threading.local()
        self._target_cache: dict[str, bool] = {}
        self._snapshot_cache: frozenset[int] | None = None
        self._dirty = False
        self._prev_trace: Any = None
        self._coverage_cov: Any = None

    def _is_target(self, filename: str) -> bool:
        """Check if *filename* belongs to one of the target modules.

        Uses path-segment matching so ``"app"`` matches ``app/foo.py``
        but not ``myapp/foo.py``.  Handles both directory segments
        and filename segments (stripping ``.py`` extension).

        Target paths are pre-split into tuples at ``__init__`` time
        so this method only splits the filename (once per unique file,
        cached by the caller).
        """
        normalized = filename.replace("\\", "/")
        segments = normalized.split("/")
        bare_segments = [s.removesuffix(".py") if s.endswith(".py") else s for s in segments]
        for target_parts in self._target_tuples:
            n = len(target_parts)
            for i in range(len(bare_segments) - n + 1):
                if tuple(bare_segments[i : i + n]) == target_parts:
                    return True
        return False

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        if event != "line":
            return self._trace
        fn = frame.f_code.co_filename
        is_target = self._target_cache.get(fn)
        if is_target is None:
            is_target = self._is_target(fn)
            self._target_cache[fn] = is_target
        if not is_target:
            return self._trace

        loc = hash((fn, frame.f_lineno)) & 0xFFFF
        prev = getattr(self._tls, "prev_loc", 0)
        self._tls.prev_loc = loc >> 1

        buf = getattr(self._tls, "edge_buf", None)
        if buf is None:
            buf = []
            self._tls.edge_buf = buf
        buf.append(prev ^ loc)
        if len(buf) >= self._FLUSH_THRESHOLD:
            with self._edges_lock:
                self._edges.update(buf)
                self._dirty = True
            buf.clear()
        return self._trace

    def _flush_local(self) -> None:
        """Flush the calling thread's edge buffer into the shared set."""
        buf = getattr(self._tls, "edge_buf", None)
        if buf:
            with self._edges_lock:
                self._edges.update(buf)
                self._dirty = True
            buf.clear()

    def start(self) -> None:
        """Reset state and begin collecting edge coverage via ``sys.settrace``."""
        self._tls.prev_loc = 0
        self._tls.edge_buf = []
        self._target_cache.clear()
        self._snapshot_cache = None
        self._dirty = False
        with self._edges_lock:
            self._edges.clear()
        # Pause coverage.py's collector if active so we can install our
        # tracer without permanently clobbering its C-level trace function.
        self._coverage_cov = None
        try:
            import coverage

            cov = coverage.Coverage.current()
            if cov is not None and cov._collector is not None:
                cov._collector.pause()
                self._coverage_cov = cov
        except Exception:
            pass
        self._prev_trace = sys.gettrace()
        sys.settrace(self._trace)

    def stop(self) -> frozenset[int]:
        """Stop collection and restore the previous trace function."""
        sys.settrace(self._prev_trace)
        # Resume coverage.py's collector — this reinstalls its C tracer.
        if self._coverage_cov is not None:
            try:
                self._coverage_cov._collector.resume()
            except Exception:
                pass
            self._coverage_cov = None
        self._flush_local()
        with self._edges_lock:
            self._snapshot_cache = frozenset(self._edges)
            self._dirty = False
            return self._snapshot_cache

    def snapshot(self) -> frozenset[int]:
        """Current edges without stopping collection.

        Returns a cached frozenset when no new edges have been
        flushed since the last call, avoiding repeated construction.
        """
        self._flush_local()
        with self._edges_lock:
            if not self._dirty and self._snapshot_cache is not None:
                return self._snapshot_cache
            self._snapshot_cache = frozenset(self._edges)
            self._dirty = False
            return self._snapshot_cache


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
_ENERGY_DECAY = 0.8
_ENERGY_MIN = 0.01

# Shared-memory edge bitmap: one byte per 16-bit edge hash.
# Single-byte writes are atomic — no locks needed.
_EDGE_BITMAP_SIZE = 65536

# Shared-memory state bitmap: same pattern as edges, for global state dedup.
# Workers skip states already visited by any worker.
_STATE_BITMAP_SIZE = 65536

# Shared-memory ring buffer for checkpoint exchange.
#
# Design: each worker owns a contiguous slice of slots (no write contention).
# Readers scan all slots and skip their own.  A CRC32 checksum guards against
# torn reads — if a reader sees a partially-written slot, the checksum won't
# match and the slot is silently skipped until the next poll.
#
# Energy propagation: any worker can update a slot's energy field.  When
# worker B selects a checkpoint published by worker A and discovers new
# edges, B writes the updated energy back to the slot.  All workers see
# the update on their next poll, so the global energy landscape converges
# without locks.
_POOL_NUM_SLOTS = 256
_POOL_SLOT_SIZE = 16384  # 16 KB per slot
_POOL_HEADER_SIZE = 64
_POOL_RING_SIZE = _POOL_HEADER_SIZE + _POOL_NUM_SLOTS * _POOL_SLOT_SIZE

# Slot binary layout (32-byte header + data):
#   [0:4]   sequence   uint32  — 0 = empty, >0 = valid (set LAST by writer)
#   [4:6]   writer_id  uint16
#   [6:8]   _pad       uint16
#   [8:12]  energy     float32 — writable by any worker (propagation)
#   [12:16] data_len   uint32
#   [16:20] checksum   uint32  — CRC32 of data bytes
#   [20:24] new_edges  uint32
#   [24:28] step       uint32
#   [28:32] _pad       4 bytes
#   [32:]   data       pickled _MachineSnapshot payload
_POOL_SLOT_HDR_SIZE = 32
_POOL_SLOT_DATA_MAX = _POOL_SLOT_SIZE - _POOL_SLOT_HDR_SIZE


def _ring_write(
    buf: memoryview,
    slot: int,
    seq: int,
    writer_id: int,
    energy: float,
    data: bytes,
    new_edges: int,
    step: int,
) -> bool:
    """Write a serialized checkpoint into a ring buffer slot.

    Writes data first, then the header, then sequence *last*.
    The sequence field is the "publish" signal — readers ignore
    slots where sequence == 0 or hasn't changed.

    Returns False if data exceeds the slot capacity.
    """
    if len(data) > _POOL_SLOT_DATA_MAX:
        return False
    base = _POOL_HEADER_SIZE + slot * _POOL_SLOT_SIZE
    # 1. Write data bytes
    d_start = base + _POOL_SLOT_HDR_SIZE
    buf[d_start : d_start + len(data)] = data
    # 2. Write header fields (except sequence)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    struct.pack_into("<HH", buf, base + 4, writer_id, 0)
    struct.pack_into("<f", buf, base + 8, energy)
    struct.pack_into("<I", buf, base + 12, len(data))
    struct.pack_into("<I", buf, base + 16, crc)
    struct.pack_into("<I", buf, base + 20, new_edges)
    struct.pack_into("<I", buf, base + 24, step)
    # 3. Sequence LAST — signals "slot is ready"
    struct.pack_into("<I", buf, base, seq)
    return True


def _ring_read(buf: memoryview, slot: int) -> dict[str, Any] | None:
    """Read a checkpoint from a ring buffer slot.

    Returns None for empty slots, oversized data, or checksum mismatches
    (torn reads).  Callers retry on the next poll cycle.
    """
    base = _POOL_HEADER_SIZE + slot * _POOL_SLOT_SIZE
    seq = struct.unpack_from("<I", buf, base)[0]
    if seq == 0:
        return None
    writer_id = struct.unpack_from("<H", buf, base + 4)[0]
    energy = struct.unpack_from("<f", buf, base + 8)[0]
    data_len = struct.unpack_from("<I", buf, base + 12)[0]
    checksum = struct.unpack_from("<I", buf, base + 16)[0]
    new_edges = struct.unpack_from("<I", buf, base + 20)[0]
    step_val = struct.unpack_from("<I", buf, base + 24)[0]
    if data_len == 0 or data_len > _POOL_SLOT_DATA_MAX:
        return None
    d_start = base + _POOL_SLOT_HDR_SIZE
    data = bytes(buf[d_start : d_start + data_len])
    if (zlib.crc32(data) & 0xFFFFFFFF) != checksum:
        return None  # torn read — skip until next poll
    return {
        "sequence": seq,
        "writer_id": writer_id,
        "energy": energy,
        "data": data,
        "new_edge_count": new_edges,
        "step": step_val,
        "slot": slot,
    }


def _ring_update_energy(buf: memoryview, slot: int, energy: float) -> None:
    """Propagate an energy update to a ring buffer slot.

    Any worker can call this.  Relaxed consistency: other workers
    see the update on their next poll, no barriers needed.
    """
    base = _POOL_HEADER_SIZE + slot * _POOL_SLOT_SIZE
    struct.pack_into("<f", buf, base + 8, energy)


@dataclass
class _MachineSnapshot:
    """Lightweight snapshot: user state dict + fault active flags.

    Avoids deep-copying Fault objects (which carry locks, compiled
    patterns, and monkeypatched references).  Restore by creating a
    fresh machine and overlaying the saved state.
    """

    state_dict: dict[str, Any]
    fault_active: dict[str, bool]


@dataclass
class Checkpoint:
    """A saved machine state with energy-based scheduling weight."""

    snapshot: _MachineSnapshot
    new_edge_count: int
    step: int
    run_id: int
    energy: float = 1.0
    times_selected: int = 0
    _pool_slot: int = -1  # ring buffer slot (-1 = local checkpoint)


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
    skipped_steps: int = 0
    unique_edges: int = 0
    checkpoints_saved: int = 0
    failures: list[Failure] = field(default_factory=list)
    duration_seconds: float = 0.0
    edge_log: list[tuple[int, int]] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)
    last_new_edge_run: int = 0
    runs_since_new_edge: int = 0
    saturated: bool = False
    stopped_reason: str = ""
    adaptation_phase: int = 0
    unique_states: int = 0
    properties_satisfied: int = 0
    mutations_total: int = 0
    mutations_killed: int = 0

    def summary(self) -> str:
        """Human-readable exploration summary."""
        steps_info = f"{self.total_steps} steps"
        if self.skipped_steps > 0:
            steps_info += f" ({self.skipped_steps} skipped — strategy generation failed)"
        lines = [
            f"Exploration: {self.total_runs} runs, {steps_info}, {self.duration_seconds:.1f}s",
            f"Coverage: {self.unique_edges} edges, {self.checkpoints_saved} checkpoints",
        ]
        if self.unique_states > 0:
            lines.append(f"States: {self.unique_states} unique state hashes")
        if self.properties_satisfied > 0:
            lines.append(f"Properties: {self.properties_satisfied} sometimes-properties satisfied")
        if self.mutations_total > 0:
            survived = self.mutations_total - self.mutations_killed
            lines.append(
                f"Mutations: {self.mutations_killed}/{self.mutations_total} killed"
                f" ({survived} survived)"
            )
        if self.adaptation_phase > 0:
            lines.append(f"Adapted: {self.adaptation_phase} phase(s) of escalation")
        if self.unique_edges > 0 and self.total_runs > 0:
            if self.saturated:
                lines.append(
                    f"Saturated: no new edges for {self.runs_since_new_edge} runs "
                    f"(last discovery at run {self.last_new_edge_run})"
                )
            elif self.runs_since_new_edge > self.total_runs * 0.5:
                lines.append(
                    f"Coverage stale: {self.runs_since_new_edge} runs since last new edge"
                )
        if self.failures:
            lines.append(f"Failures found: {len(self.failures)}")
            for f in self.failures[:5]:
                lines.append(f"  {f}")
        elif self.saturated:
            lines.append("No failures found \u2014 all reachable paths explored.")
        else:
            lines.append("No failures found.")
        if self.stopped_reason:
            lines.append(f"Stopped: {self.stopped_reason}")

        # Structured capabilities — what was active vs not.
        caps = self.capabilities_used
        unused = [k for k, v in caps.items() if not v]
        if unused:
            lines.append(f"Unused capabilities: {', '.join(unused)}")

        return "\n".join(lines)

    @property
    def capabilities_used(self) -> dict[str, bool]:
        """Which exploration capabilities were active for this run.

        Exposes structured metadata so tooling (or an AI assistant) can
        identify what's available but wasn't exercised, and decide
        whether to suggest it based on context.
        """
        return {
            "state_hash": self.unique_states > 0,
            "mutations": self.mutations_total > 0,
            "checkpoints": self.checkpoints_saved > 0,
            "sometimes_properties": self.properties_satisfied > 0,
        }


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
        share_edges: bool = True,
        share_checkpoints: bool = True,
        mutation_targets: list[str] | None = None,
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
            workers: Number of parallel worker processes. ``0`` means auto
                (uses ``os.cpu_count()``). Default ``1`` (sequential).
            mutation_targets: Dotted paths to functions to mutate
                (e.g. ``["myapp.service.process"]``).  Mutations become
                faults that the nemesis toggles during exploration.
                Killed mutants = your tests catch the bug.  Surviving
                mutants = test gap found.
            share_edges: When ``workers > 1``, use a shared-memory edge
                bitmap so workers skip edges already found by others.
                AFL-style: one byte per edge hash, single-byte atomic
                writes, zero locks.  Default ``True``.
            share_checkpoints: When ``workers > 1``, share checkpoints
                between workers via a shared-memory ring buffer.  Workers
                publish discoveries and subscribe to others' finds with
                global energy propagation — a checkpoint that leads to
                new edges for any worker gets higher priority for all.
                Default ``True``.
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
        self.workers = (os.cpu_count() or 1) if workers <= 0 else workers
        self.share_edges = share_edges
        self.share_checkpoints = share_checkpoints
        self.mutation_targets = mutation_targets or []

        # Shared-memory edge bitmap (set by _run_parallel / _worker_fn)
        # 65536 bytes — one byte per possible 16-bit edge hash.
        # Single-byte writes are atomic on all architectures.
        self._shared_bitmap: memoryview | None = None

        # Shared-memory state bitmap (set by _run_parallel / _worker_fn)
        self._shared_state_bitmap: memoryview | None = None

        # Shared-memory ring buffer for checkpoint exchange
        self._pool_ring: memoryview | None = None
        self._worker_id: int = 0
        self._pool_num_workers: int = 1
        self._pool_slots_per_worker: int = _POOL_NUM_SLOTS
        self._pool_next_slot: int = 0  # next slot to write (within our range)
        self._pool_write_seq: int = 0  # per-worker monotonic sequence
        self._pool_seen_seq: dict[int, int] = {}  # slot → last seen sequence
        self._pool_last_sync: float = 0.0
        self._pool_sync_interval: float = 0.5  # 500ms (was 2s for file-based)

        # Internal state
        self._total_edges: set[int] = set()
        self._total_states: set[int] = set()
        self._satisfied_properties: set[str] = set()
        self._checkpoints: list[Checkpoint] = []
        self._rules: list[_RuleInfo] = []
        self._invariant_names: list[str] = []

    # -- Snapshot / restore -------------------------------------------------

    def _snapshot_machine(self, machine: ChaosTest) -> _MachineSnapshot:
        """Create a lightweight snapshot, skipping Fault objects."""
        state: dict[str, Any] = {}
        for k, v in machine.__dict__.items():
            if k == "_faults":
                continue
            try:
                state[k] = copy.deepcopy(v)
            except Exception:
                pass
        fault_active = {f.name: f.active for f in machine._faults}
        return _MachineSnapshot(state_dict=state, fault_active=fault_active)

    def _restore_machine(self, snapshot: _MachineSnapshot) -> ChaosTest:
        """Restore a fresh machine from a snapshot."""
        machine = self.test_class()
        for k, v in snapshot.state_dict.items():
            try:
                machine.__dict__[k] = copy.deepcopy(v)
            except Exception:
                machine.__dict__[k] = v
        for f in machine._faults:
            was_active = snapshot.fault_active.get(f.name, False)
            if was_active and not f.active:
                f.activate()
            elif not was_active and f.active:
                f.deactivate()
        return machine

    # -- Resumable state persistence ----------------------------------------

    def save_state(self, path: str | Path) -> None:
        """Save exploration state to disk for later resumption.

        Persists the checkpoint corpus, discovered edges, state hashes,
        satisfied properties, and RNG state.  The file is a pickle — not
        intended for cross-version portability, but reliable for
        resume-after-interrupt on the same codebase.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        # Filter checkpoints to picklable snapshots
        cp_data: list[dict[str, Any]] = []
        for cp in self._checkpoints:
            try:
                # Filter state_dict to only picklable values
                safe_state: dict[str, Any] = {}
                for k, v in cp.snapshot.state_dict.items():
                    try:
                        pickle.dumps(v)
                        safe_state[k] = v
                    except Exception:
                        pass
                if not safe_state:
                    continue
                cp_data.append(
                    {
                        "state_dict": safe_state,
                        "fault_active": cp.snapshot.fault_active,
                        "new_edge_count": cp.new_edge_count,
                        "step": cp.step,
                        "run_id": cp.run_id,
                        "energy": cp.energy,
                        "times_selected": cp.times_selected,
                    }
                )
            except Exception:
                continue

        payload = {
            "version": 1,
            "total_edges": self._total_edges,
            "total_states": self._total_states,
            "satisfied_properties": self._satisfied_properties,
            "checkpoints": cp_data,
            "rng_state": self.rng.getstate(),
            "seed": self.seed,
        }

        tmp = p.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(payload, f)
        tmp.rename(p)  # atomic on POSIX

    def load_state(self, path: str | Path) -> dict[str, Any]:
        """Load saved exploration state, restoring checkpoints and edges.

        Returns a dict of counters (``total_runs``, ``total_steps``, etc.)
        that the caller should seed into the ``ExplorationResult``.
        """
        with open(path, "rb") as f:
            payload = pickle.load(f)

        self._total_edges = set(payload.get("total_edges", set()))
        self._total_states = set(payload.get("total_states", set()))
        self._satisfied_properties = set(payload.get("satisfied_properties", set()))

        rng_state = payload.get("rng_state")
        if rng_state is not None:
            self.rng.setstate(rng_state)

        # Reconstruct checkpoints
        self._checkpoints.clear()
        for cpd in payload.get("checkpoints", []):
            snap = _MachineSnapshot(
                state_dict=cpd["state_dict"],
                fault_active=cpd.get("fault_active", {}),
            )
            self._checkpoints.append(
                Checkpoint(
                    snapshot=snap,
                    new_edge_count=cpd.get("new_edge_count", 0),
                    step=cpd.get("step", 0),
                    run_id=cpd.get("run_id", 0),
                    energy=cpd.get("energy", 1.0),
                    times_selected=cpd.get("times_selected", 0),
                )
            )

        return {
            "total_edges": len(self._total_edges),
            "checkpoints": len(self._checkpoints),
        }

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
        """Execute a rule, drawing parameters from strategies.

        Returns drawn params.  If a required strategy fails to generate,
        skips the rule entirely (returns empty dict) rather than calling
        the rule with missing arguments — prevents "spinning" where the
        explorer counts thousands of no-op runs.
        """
        params: dict[str, Any] = {}
        required_count = 0

        for param_name, strategy in rule.strategies.items():
            if param_name == "data" or rule.has_data and param_name == "data":
                params["data"] = _DataProxy()
            else:
                required_count += 1
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    try:
                        params[param_name] = strategy.example()
                    except Exception:
                        pass  # strategy failed — tracked below

        # If any required strategy failed, skip the rule entirely.
        # This prevents spinning: calling rules with missing arguments
        # that return immediately and inflate run counts.
        generated = len(params) - (1 if "data" in params else 0)
        if required_count > 0 and generated < required_count:
            return params  # caller sees incomplete params

        try:
            getattr(machine, rule.name)(**params)
        except TypeError:
            # Fallback: call with no args (rule may have defaults)
            try:
                getattr(machine, rule.name)()
            except TypeError:
                pass  # rule genuinely can't execute — skip

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
        """Energy-weighted selection with recency and exploration bonuses.

        Combines three signals to balance exploitation and exploration:
        - **Energy**: checkpoints that found new edges get higher weight
        - **Recency**: newer checkpoints (frontier) get a sqrt bonus
        - **Exploration**: over-selected checkpoints are penalized
        """
        weights = [
            cp.energy * (1 + i) ** 0.5 / (1 + cp.times_selected) ** 0.5
            for i, cp in enumerate(self._checkpoints)
        ]
        (cp,) = self.rng.choices(self._checkpoints, weights=weights, k=1)
        cp.times_selected += 1
        return cp

    def _select_recent(self) -> Checkpoint:
        """Favor recently-created checkpoints."""
        n = len(self._checkpoints)
        weights = list(range(1, n + 1))
        return self.rng.choices(self._checkpoints, weights=weights, k=1)[0]

    def _update_checkpoint_energy(self, cp: Checkpoint, new_edges: int) -> None:
        """Reward checkpoints that led to new discoveries, decay others.

        When the checkpoint came from the shared ring buffer, propagate
        the energy update back so all workers see it on their next poll.
        """
        if new_edges > 0:
            cp.energy += new_edges * _ENERGY_REWARD
        else:
            cp.energy = max(_ENERGY_MIN, cp.energy * _ENERGY_DECAY)
        # Propagate to ring buffer — other workers see the updated energy
        if cp._pool_slot >= 0 and self._pool_ring is not None:
            _ring_update_energy(self._pool_ring, cp._pool_slot, cp.energy)

    def _pool_publish(self, machine: ChaosTest, new_count: int, step: int, run_id: int) -> None:
        """Publish a checkpoint to the shared-memory ring buffer.

        Each worker owns a contiguous slice of slots and writes to them
        in round-robin order.  The ring buffer uses per-worker monotonic
        sequence numbers so readers can detect new writes without locks.
        """
        if self._pool_ring is None:
            return
        try:
            snap = self._snapshot_machine(machine)
            picklable_state: dict[str, Any] = {}
            for k, v in snap.state_dict.items():
                try:
                    pickle.dumps(v)
                    picklable_state[k] = v
                except Exception:
                    pass
            payload = {"state_dict": picklable_state, "fault_active": snap.fault_active}
            data = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
            if len(data) > _POOL_SLOT_DATA_MAX:
                return  # checkpoint too large for a slot — skip

            # Claim our next slot (round-robin within our owned range)
            base_slot = self._worker_id * self._pool_slots_per_worker
            slot = base_slot + (self._pool_next_slot % self._pool_slots_per_worker)
            self._pool_next_slot += 1
            self._pool_write_seq += 1

            energy = 1.0 + new_count * _ENERGY_REWARD
            _ring_write(
                self._pool_ring,
                slot,
                self._pool_write_seq,
                self._worker_id,
                energy,
                data,
                new_count,
                step,
            )
        except Exception:
            pass

    def _pool_subscribe(self) -> None:
        """Import new checkpoints from other workers via the ring buffer.

        Scans all slots outside our owned range.  Skips slots already
        seen (via per-slot sequence tracking) and torn reads (CRC32
        mismatch).  Imported checkpoints carry the energy from the
        ring buffer, reflecting global energy propagation.
        """
        if self._pool_ring is None:
            return
        now = _time.monotonic()
        if now - self._pool_last_sync < self._pool_sync_interval:
            return
        self._pool_last_sync = now
        try:
            my_base = self._worker_id * self._pool_slots_per_worker
            my_end = my_base + self._pool_slots_per_worker
            for slot in range(_POOL_NUM_SLOTS):
                if my_base <= slot < my_end:
                    continue  # skip our own slots
                entry = _ring_read(self._pool_ring, slot)
                if entry is None:
                    continue
                seq = entry["sequence"]
                if seq <= self._pool_seen_seq.get(slot, 0):
                    continue  # already imported
                self._pool_seen_seq[slot] = seq
                try:
                    payload = pickle.loads(entry["data"])
                    snap = _MachineSnapshot(
                        state_dict=payload.get("state_dict", payload),
                        fault_active=payload.get("fault_active", {}),
                    )
                    self._checkpoints.append(
                        Checkpoint(
                            snapshot=snap,
                            new_edge_count=entry["new_edge_count"],
                            step=entry["step"],
                            run_id=-1,
                            energy=entry["energy"],
                            _pool_slot=slot,
                        )
                    )
                except Exception:
                    continue
        except Exception:
            pass

    # -- Step execution helpers (extracted from run() for readability) -----

    def _execute_step(
        self,
        machine: ChaosTest,
        rule_log: list[str],
        trace_steps: list[TraceStep],
        ts_offset: float,
        new_edges_this_run: int,
    ) -> bool:
        """Execute one exploration step: either a fault toggle or a rule.

        Returns ``True`` if the step executed, ``False`` if it was
        skipped (strategy generation failed for required parameters).
        """
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
            return True
        else:
            rule_info = self.rng.choice(self._rules)
            params = self._execute_rule(machine, rule_info)
            # Detect skipped rules: required params missing means strategy
            # generation failed. Don't log as a real step — prevents the
            # "spinning" problem where run counts inflate with no-op calls.
            required = sum(1 for p in rule_info.strategies if p != "data")
            generated = sum(
                1 for k, v in params.items() if k != "data" and not isinstance(v, _DataProxy)
            )
            if required > 0 and generated < required:
                return False  # skip — strategy generation failed
            rule_log.append(rule_info.name)

            serializable_params = {
                k: v for k, v in params.items() if not isinstance(v, _DataProxy)
            }
            # active_faults omitted on rule steps (derivable from
            # fault_toggle sequence, saves ~70% of list comprehensions)
            trace_steps.append(
                TraceStep(
                    kind="rule",
                    name=rule_info.name,
                    params=serializable_params,
                    edge_count=len(self._total_edges) + new_edges_this_run,
                    timestamp_offset=ts_offset,
                )
            )
        return True

    def _process_coverage(
        self,
        machine: ChaosTest,
        collector: CoverageCollector | None,
        step: int,
        run_id: int,
        new_edges_this_run: int,
        result: ExplorationResult,
        use_coverage: bool,
        _assertions: Any,
    ) -> int:
        """Check for new edges, states, and properties after a step.

        Returns the updated ``new_edges_this_run`` count.
        """
        # Edge coverage
        if collector:
            edges = collector.snapshot()
            new = edges - self._total_edges
            if new and self._shared_bitmap is not None:
                new = {e for e in new if not self._shared_bitmap[e]}
            if new:
                new_edges_this_run += len(new)
                self._total_edges |= new
                if self._shared_bitmap is not None:
                    for e in new:
                        self._shared_bitmap[e] = 1
                self._save_checkpoint(machine, len(new), step, run_id)
                self._pool_publish(machine, len(new), step, run_id)
                result.checkpoints_saved += 1

        # State-aware coverage (with global dedup via shared state bitmap)
        if hasattr(machine, "state_hash"):
            sh = machine.state_hash()
            if sh and sh not in self._total_states:
                # Global dedup: skip states another worker already explored
                sh16 = sh & 0xFFFF
                if self._shared_state_bitmap is not None and self._shared_state_bitmap[sh16]:
                    pass  # another worker already found this state
                else:
                    self._total_states.add(sh)
                    if self._shared_state_bitmap is not None:
                        self._shared_state_bitmap[sh16] = 1
                    new_edges_this_run += 1
                    if use_coverage:
                        self._save_checkpoint(machine, 1, step, run_id)
                        self._pool_publish(machine, 1, step, run_id)
                        result.checkpoints_saved += 1

        # Property-guided search
        for p in _assertions.tracker.results:
            if p.type == "sometimes" and p.passes > 0 and p.name not in self._satisfied_properties:
                self._satisfied_properties.add(p.name)
                result.properties_satisfied += 1
                new_edges_this_run += 1
                if use_coverage:
                    self._save_checkpoint(machine, 1, step, run_id)
                    result.checkpoints_saved += 1

        return new_edges_this_run

    def _record_failure(
        self,
        e: Exception,
        run_id: int,
        step: int,
        trace_steps: list[TraceStep],
        rule_log: list[str],
        machine: ChaosTest,
        source_cp: Checkpoint | None,
        new_edges_this_run: int,
        run_start: float,
        class_name: str,
        result: ExplorationResult,
        _mutation_pairs: list[tuple],
    ) -> Trace:
        """Record a failure into the result and return the trace."""
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
        # Check if any mutation faults are active (killed mutants)
        _active_names = {f.name for f in machine.active_faults}
        for _mutant, _mfault in _mutation_pairs:
            if _mfault.name in _active_names and not _mutant.killed:
                _mutant.killed = True
                _mutant.error = str(e)[:200]
                result.mutations_killed += 1

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
        return trace

    # -- Checkpoint scheduling ----------------------------------------------

    def _find_min_energy_idx(self) -> int:
        """Find the index of the lowest-energy checkpoint."""
        min_e = self._checkpoints[0].energy
        min_i = 0
        for i in range(1, len(self._checkpoints)):
            if self._checkpoints[i].energy < min_e:
                min_e = self._checkpoints[i].energy
                min_i = i
        return min_i

    def _save_checkpoint(self, machine: ChaosTest, new_count: int, step: int, run_id: int) -> None:
        """Save a checkpoint. Evicts lowest-energy checkpoint if at capacity."""
        if self.max_checkpoints <= 0:
            return
        if len(self._checkpoints) >= self.max_checkpoints:
            if self.checkpoint_strategy == "energy":
                self._checkpoints.pop(self._find_min_energy_idx())
            else:
                idx = self.rng.randint(0, max(0, len(self._checkpoints) - 2))
                self._checkpoints.pop(idx)

        self._checkpoints.append(
            Checkpoint(
                snapshot=self._snapshot_machine(machine),
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
        patience: int = 0,
        progress: Callable[[ProgressSnapshot], None] | None = None,
        resume_from: str | Path | None = None,
        save_state_to: str | Path | None = None,
    ) -> ExplorationResult:
        """Run the coverage-guided exploration loop.

        Args:
            max_time: Wall-clock time limit in seconds.
            max_runs: Maximum number of runs (or ``None`` for time-only).
            steps_per_run: Max rule steps per run.
            shrink: If True, shrink failing traces after exploration.
            max_shrink_time: Time limit for shrinking each failure.
            patience: Stop after N consecutive runs without new edges. 0=disabled.
            progress: Optional callback for live progress updates.
            resume_from: Path to a saved state file from a previous run.
                Restores checkpoints, edges, and RNG state so exploration
                continues where it left off.
            save_state_to: Path to save exploration state on completion
                (and on interrupt).  Use with ``resume_from`` on the next
                run to continue exploration across sessions.
        """
        if self.workers > 1:
            return self._run_parallel(
                max_time=max_time,
                max_runs=max_runs,
                steps_per_run=steps_per_run,
                shrink=shrink,
                max_shrink_time=max_shrink_time,
                patience=patience,
                progress=progress,
            )

        self._discover()

        # Activate property tracker for property-guided search
        from ordeal import assertions as _assertions

        _tracker_was_active = _assertions.tracker.active
        _assertions.tracker.active = True
        if not self._rules:
            raise ValueError(f"No callable rules found on {self.test_class.__name__}")

        result = ExplorationResult()

        # Resume from saved state if provided
        if resume_from is not None:
            restored = self.load_state(resume_from)
            result.unique_edges = restored["total_edges"]
            result.checkpoints_saved = restored["checkpoints"]

        use_coverage = bool(self.target_paths)
        start = _time.monotonic()
        class_name = _qualified_name(self.test_class)

        # Generate mutation faults from target functions
        _mutation_pairs: list[tuple] = []
        _original_test_class = self.test_class
        if self.mutation_targets:
            from ordeal.mutations import mutation_faults as _gen_mutation_faults

            for mt in self.mutation_targets:
                try:
                    _mutation_pairs.extend(_gen_mutation_faults(mt))
                except Exception:
                    pass
            if _mutation_pairs:
                _mfaults = [f for _, f in _mutation_pairs]

                class _MutatedTest(self.test_class):
                    faults = list(self.test_class.faults) + _mfaults

                self.test_class = _MutatedTest
                result.mutations_total = len(_mutation_pairs)

        _runs_since_new: int = 0
        _adapt_phase: int = 0
        _orig_fault_prob = self.fault_toggle_prob
        _orig_cp_strategy = self.checkpoint_strategy

        while True:
            elapsed = _time.monotonic() - start
            if elapsed >= max_time:
                result.stopped_reason = "time"
                break
            if max_runs is not None and result.total_runs >= max_runs:
                result.stopped_reason = "max_runs"
                break
            if patience > 0 and _runs_since_new >= patience and use_coverage:
                if _adapt_phase < 3:
                    # Escalate: go deeper before giving up
                    _adapt_phase += 1
                    _runs_since_new = 0
                    steps_per_run = min(steps_per_run * 2, 500)
                    self.fault_toggle_prob = min(0.5, self.fault_toggle_prob + 0.1)
                    if _adapt_phase == 2:
                        self.checkpoint_strategy = "uniform"
                    result.adaptation_phase = _adapt_phase
                else:
                    result.saturated = True
                    result.stopped_reason = "saturated"
                    break

            # Pull checkpoints from other workers
            self._pool_subscribe()

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
                machine = self._restore_machine(source_cp.snapshot)
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

                    executed = self._execute_step(
                        machine, rule_log, trace_steps, ts_offset, new_edges_this_run
                    )
                    if not executed:
                        result.skipped_steps += 1
                        continue
                    self._check_invariants(machine)
                    new_edges_this_run = self._process_coverage(
                        machine,
                        collector,
                        step,
                        run_id,
                        new_edges_this_run,
                        result,
                        use_coverage,
                        _assertions,
                    )

            except Exception as e:
                trace = self._record_failure(
                    e,
                    run_id,
                    step,
                    trace_steps,
                    rule_log,
                    machine,
                    source_cp,
                    new_edges_this_run,
                    run_start,
                    class_name,
                    result,
                    _mutation_pairs,
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

            # Saturation tracking
            if new_edges_this_run > 0:
                _runs_since_new = 0
                result.last_new_edge_run = run_id
            else:
                _runs_since_new += 1
            result.runs_since_new_edge = _runs_since_new

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

        # Restore test class and original params after exploration
        self.test_class = _original_test_class
        _assertions.tracker.active = _tracker_was_active
        self.fault_toggle_prob = _orig_fault_prob
        self.checkpoint_strategy = _orig_cp_strategy
        result.unique_states = len(self._total_states)

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

        # Save state for future resumption
        if save_state_to is not None:
            self.save_state(save_state_to)

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
        patience: int,
        progress: Callable[[ProgressSnapshot], None] | None,
    ) -> ExplorationResult:
        """Run exploration across multiple worker processes.

        Each worker gets a unique seed (base + i*7919) for independent
        state-space exploration.  When ``share_edges`` is True, workers
        communicate via a shared-memory edge bitmap (AFL-style): one
        byte per 16-bit edge hash, single-byte atomic writes, zero locks.

        Results are aggregated: runs/steps summed, edges unioned.
        """
        from multiprocessing.shared_memory import SharedMemory

        start = _time.monotonic()
        class_path = f"{self.test_class.__module__}.{self.test_class.__qualname__}"

        # Create shared edge bitmap (65536 bytes, one per edge hash)
        shm: SharedMemory | None = None
        shm_name: str | None = None
        if self.share_edges:
            shm = SharedMemory(create=True, size=_EDGE_BITMAP_SIZE)
            shm.buf[:] = b"\x00" * _EDGE_BITMAP_SIZE
            shm_name = shm.name

        # Shared state bitmap (same pattern as edges, for global state dedup)
        state_shm: SharedMemory | None = None
        state_shm_name: str | None = None
        if self.share_edges:
            state_shm = SharedMemory(create=True, size=_STATE_BITMAP_SIZE)
            state_shm.buf[:] = b"\x00" * _STATE_BITMAP_SIZE
            state_shm_name = state_shm.name

        # Shared ring buffer for checkpoint exchange + energy propagation
        ring_shm: SharedMemory | None = None
        ring_shm_name: str | None = None
        if self.share_checkpoints:
            ring_shm = SharedMemory(create=True, size=_POOL_RING_SIZE)
            # SharedMemory is zeroed on creation (POSIX shm_open + ftruncate)
            ring_shm_name = ring_shm.name

        slots_per_worker = _POOL_NUM_SLOTS // max(self.workers, 1)

        try:
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
                        "patience": patience,
                        "shared_edges_name": shm_name,
                        "shared_state_name": state_shm_name,
                        "ring_shm_name": ring_shm_name,
                        "worker_id": i,
                        "num_workers": self.workers,
                        "slots_per_worker": slots_per_worker,
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
        finally:
            if shm is not None:
                shm.close()
                shm.unlink()
            if state_shm is not None:
                state_shm.close()
                state_shm.unlink()
            if ring_shm is not None:
                ring_shm.close()
                ring_shm.unlink()


def _worker_fn(args: dict[str, Any]) -> dict[str, Any]:
    """Worker process: import test class, run single-worker Explorer, return results.

    Defined at module level so it can be pickled by multiprocessing.
    If ``shared_edges_name`` is set, attaches to the shared-memory edge
    bitmap for cross-worker deduplication.
    """
    from multiprocessing.shared_memory import SharedMemory

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

    # Attach to shared edge bitmap
    shm: SharedMemory | None = None
    shm_name = args.get("shared_edges_name")
    if shm_name:
        shm = SharedMemory(name=shm_name, create=False)
        explorer._shared_bitmap = shm.buf

    # Attach to shared state bitmap
    state_shm: SharedMemory | None = None
    state_name = args.get("shared_state_name")
    if state_name:
        state_shm = SharedMemory(name=state_name, create=False)
        explorer._shared_state_bitmap = state_shm.buf

    # Attach to shared ring buffer for checkpoint exchange
    ring_shm: SharedMemory | None = None
    ring_name = args.get("ring_shm_name")
    if ring_name:
        ring_shm = SharedMemory(name=ring_name, create=False)
        explorer._pool_ring = ring_shm.buf
        explorer._worker_id = args.get("worker_id", 0)
        explorer._pool_num_workers = args.get("num_workers", 1)
        explorer._pool_slots_per_worker = args.get("slots_per_worker", _POOL_NUM_SLOTS)

    try:
        result = explorer.run(
            max_time=args["max_time"],
            max_runs=args.get("max_runs"),
            steps_per_run=args["steps_per_run"],
            shrink=args.get("shrink", True),
            max_shrink_time=args.get("max_shrink_time", 30.0),
            patience=args.get("patience", 0),
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
    finally:
        if shm is not None:
            shm.close()
        if state_shm is not None:
            state_shm.close()
        if ring_shm is not None:
            ring_shm.close()
