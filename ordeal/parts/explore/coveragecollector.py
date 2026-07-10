from __future__ import annotations
# ruff: noqa
import copy
import hmac
import importlib
import multiprocessing as mp
import os
import pickle
import random
import secrets
import signal
import struct
import sys
import threading
import time as _time
import traceback as _traceback
import warnings
import zlib
from collections import Counter, deque
from dataclasses import dataclass, field
from itertools import combinations
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

    Uses AFL-style edge hashing to capture control-flow *transitions*,
    not just line visits.

    **N-gram coverage** (configurable via the ``ngram`` parameter):

    With ``ngram=1`` (the default), hashing uses a single previous location:
    ``prev_loc XOR cur_loc``.  This is the classic AFL edge model — fast and
    effective, but blind to *path context*.  The same edge A->B looks
    identical regardless of whether we arrived via X->A->B or Y->A->B.

    With ``ngram=2+``, the collector maintains a ring buffer of the last N
    locations and hashes all of them together with the current location.
    This captures deeper path patterns: the edge A->B reached via X->A->B
    produces a different hash than Y->A->B.  This is the same idea as
    AFL++'s ``NGRAM`` instrumentation (``-fsanitize-coverage=trace-pc-guard``
    with N-gram context), adapted for Python's ``sys.settrace`` collector.

    **Why ngram=2 is the sweet spot for Python:**

    AFL++ defaults to NGRAM-4 for compiled C/C++ where basic blocks are
    tiny and paths diverge rapidly.  Python's line-level tracing is much
    coarser — each "location" is a full source line, not a machine-code
    basic block.  Empirically, ngram=2 captures the important path context
    (which branch led to this edge) without the exponential hash-space
    explosion that makes ngram=4 produce mostly unique, never-repeated
    hashes in Python.  The memory cost is minimal: one extra ``int`` per
    thread (the deque), and the hash computation adds a ``tuple()`` call
    per traced line.

    **Memory/performance tradeoff:**

    - ``ngram=1``: One ``int`` per thread (``prev_loc``).  XOR + shift per
      traced line.  Identical to classic AFL.
    - ``ngram=2``: Two-element deque per thread.  ``hash(tuple(...)) ^ loc``
      per traced line.  ~10-15% slower than ngram=1 in microbenchmarks,
      negligible in real exploration runs (I/O and strategy generation
      dominate).
    - ``ngram=3+``: Diminishing returns.  The hash space grows
      exponentially, so most N-gram hashes are seen only once, reducing
      the signal-to-noise ratio for checkpoint energy scheduling.
      Not recommended for Python unless profiling shows a specific need.

    Optimizations over naive per-line locking:

    - **Filename cache**: ``_is_target`` result is cached per filename so
      the path-segment check runs at most once per unique file.
    - **Thread-local edge buffer**: Edges accumulate in a per-thread list
      and are flushed to the shared set every 256 edges, reducing lock
      acquisitions by ~256x.
    - **Snapshot caching**: ``snapshot()`` returns a cached ``frozenset``
      when no new edges have arrived since the last call, avoiding
      repeated O(n) construction on steps that don't discover new paths.

    Thread-safe for free-threaded Python 3.13+: per-thread location state
    (``prev_loc`` or ``prev_locs``) and edge buffer are thread-local, and
    ``_edges`` is lock-protected.
    """

    _FLUSH_THRESHOLD = 256

    def __init__(self, target_paths: list[str], *, ngram: int = 1) -> None:
        if ngram < 1:
            raise ValueError(f"ngram must be >= 1, got {ngram}")
        self._targets = target_paths
        self._ngram = ngram
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
        self._lines_hit: dict[str, set[int]] = {}  # filename -> set of line numbers

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

        # Track line-level coverage for gap reporting
        lineno = frame.f_lineno
        lines = self._lines_hit.get(fn)
        if lines is None:
            lines = set()
            self._lines_hit[fn] = lines
        lines.add(lineno)

        loc = hash((fn, lineno)) & 0xFFFF

        if self._ngram == 1:
            # Classic AFL single-edge: prev_loc XOR cur_loc.
            # Identical to the original implementation for backward compat.
            prev = getattr(self._tls, "prev_loc", 0)
            self._tls.prev_loc = loc >> 1
            edge = prev ^ loc
        else:
            # N-gram coverage: hash the last N locations together with cur_loc.
            # This captures path context — the same edge reached via different
            # paths produces a different hash.  Mirrors AFL++'s NGRAM mode.
            prev_locs = getattr(self._tls, "prev_locs", None)
            if prev_locs is None:
                prev_locs = deque([0] * self._ngram, maxlen=self._ngram)
                self._tls.prev_locs = prev_locs
            edge = hash(tuple(prev_locs)) ^ loc
            prev_locs.append(loc >> 1)

        buf = getattr(self._tls, "edge_buf", None)
        if buf is None:
            buf = []
            self._tls.edge_buf = buf
        buf.append(edge)
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
        if self._ngram == 1:
            self._tls.prev_loc = 0
        else:
            self._tls.prev_locs = deque([0] * self._ngram, maxlen=self._ngram)
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

    @property
    def lines_hit(self) -> dict[str, set[int]]:
        """Mapping of filename -> set of line numbers visited."""
        return dict(self._lines_hit)
def _find_branch_lines(source: str) -> list[tuple[int, str]]:
    """Find branch and control-flow statement lines in Python source via AST.

    Returns ``[(lineno, code_snippet), ...]`` for ``if``, ``for``,
    ``while``, ``try``, ``except``, ``match``, ``assert``, and ``raise``.
    """
    import ast
    import textwrap

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Branch/control-flow node types
    branch_types: tuple[type, ...] = (
        ast.If,
        ast.For,
        ast.While,
        ast.Try,
        ast.ExceptHandler,
        ast.Assert,
        ast.Raise,
    )
    # Python 3.10+ match/case
    if hasattr(ast, "Match"):
        branch_types = (*branch_types, ast.Match, ast.match_case)

    src_lines = source.splitlines()
    branches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue
        if isinstance(node, branch_types):
            code = src_lines[lineno - 1].strip() if lineno <= len(src_lines) else ""
            branches.append((lineno, textwrap.shorten(code, 80)))
    return branches
def _compute_coverage_gaps(
    lines_hit: dict[str, set[int]],
    target_modules: list[str],
    total_runs: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    """Compare lines_hit against branch lines in target modules.

    Returns ``(gaps, lines_covered, lines_total)`` where gaps is a list
    of ``{file, line, code}`` dicts for branch/control-flow statements
    not reached during exploration.

    **Epistemic note**: "not reached" means the explorer did not
    execute this line in ``total_runs`` runs.  It does NOT mean the
    code is unreachable — a longer run or different fault schedule
    might reach it.
    """
    import ast
    import importlib
    import inspect

    all_branch_lines: list[tuple[str, int, str]] = []
    all_executable: set[tuple[str, int]] = set()

    for mod_name in target_modules:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        try:
            src = inspect.getsource(mod)
            src_file = inspect.getfile(mod)
        except (OSError, TypeError):
            continue

        branches = _find_branch_lines(src)
        for lineno, code in branches:
            all_branch_lines.append((src_file, lineno, code))

        # Count executable lines via AST — only lines that contain
        # actual statements (skips docstrings, comments, blank lines).
        try:
            tree = ast.parse(Path(src_file).read_text(encoding="utf-8"))
        except Exception:
            try:
                tree = ast.parse(src)
            except Exception:
                continue
        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", None)
            if lineno is not None and isinstance(node, ast.stmt):
                # Skip pure docstring expressions (string-only Expr nodes)
                if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant,)):
                    if isinstance(node.value.value, str):
                        continue
                all_executable.add((src_file, lineno))

    covered = set()
    for fn, lines in lines_hit.items():
        for ln in lines:
            covered.add((fn, ln))

    lines_total = len(all_executable)
    lines_covered = len(all_executable & covered)

    gaps = []
    for src_file, lineno, code in all_branch_lines:
        if (src_file, lineno) not in covered:
            try:
                rel = str(Path(src_file).relative_to(Path.cwd()))
            except ValueError:
                rel = src_file
            gaps.append({"file": rel, "line": lineno, "code": code})

    return gaps, lines_covered, lines_total
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
_CHECKPOINT_FEEDBACK_WINDOW = 32
_CHECKPOINT_FEEDBACK_MIN_SAMPLES = 16
_HIGH_RESTORE_SHARE = 0.20
_MIN_ADAPTIVE_CHECKPOINT_PROB = 0.10
# Seed mutation: when branching from a checkpoint that has productive seeds,
# mutate one of them instead of generating fresh via strategy.example().
# This is the AFL closed-loop adapted for typed stateful testing.
# See module docstring for the full rationale and literature references.
_SEED_MUTATION_PROB = 0.25  # 25% of rule executions use mutation (mine.py uses same ratio)
_MAX_SEEDS_PER_CHECKPOINT = 16  # bounded to prevent memory growth
_AUTO_WORKER_CAP = 8  # auto mode stays conservative; explicit workers can exceed this
# Swarm configuration constants (Groce et al., ISSTA 2012)
_SWARM_ENERGY_REWARD = 2.0  # energy boost when a config leads to new edges
_SWARM_ENERGY_DECAY = 0.9  # decay per run for configs that don't find new edges
_SWARM_ENERGY_MIN = 0.1  # floor — no config is fully excluded
_SWARM_WARMUP_RUNS = 20  # pure coin-flip before switching to energy-weighted
@dataclass
class SwarmConfig:
    """A joint rule+fault configuration for one exploration run.

    Each configuration determines which rules are callable AND which
    faults the nemesis can toggle.  This is the paper's model (Groce
    et al., ISSTA 2012): a "configuration" is the full feature set.

    The ``energy`` field enables adaptive scheduling (MOpt pattern):
    configurations that led to new coverage get higher selection
    probability in future runs.
    """

    active_rules: list[str]  # rule names included in this config
    active_faults: list[str]  # fault names the nemesis can toggle
    energy: float = 1.0
    times_used: int = 0
    edges_found: int = 0
    runs_with_new_edges: int = 0
    failure_count: int = 0
    property_hits: int = 0

    @property
    def key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Hashable identity for dedup and lookup."""
        return (tuple(sorted(self.active_rules)), tuple(sorted(self.active_faults)))
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
_POOL_AUTH_TAG_SIZE = 32
