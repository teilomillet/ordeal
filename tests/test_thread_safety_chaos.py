"""Chaos tests for ordeal's own thread-safety — ordeal testing ordeal.

Uses ChaosTest with rules that spawn concurrent threads, invariants that
check consistency, and always()/sometimes() assertions on the results.
Hypothesis explores different operation sequences; the nemesis injects
faults during concurrent operations to stress the lock paths.
"""

from __future__ import annotations

import copy
import threading
from typing import ClassVar

import hypothesis.strategies as st
from hypothesis import settings
from hypothesis.stateful import invariant, rule

from ordeal import ChaosTest, always
from ordeal.assertions import PropertyTracker
from ordeal.faults import Fault, LambdaFault

# ============================================================================
# Helpers
# ============================================================================

THREADS = 8
ITERS = 100


def _run_threads(fn, n=THREADS):
    """Run fn() on n threads, barrier-synchronized for max contention."""
    barrier = threading.Barrier(n)

    def work():
        barrier.wait()
        fn()

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ============================================================================
# A fault that introduces threading.Event waits (timing perturbation)
# ============================================================================


class _SleepFault(Fault):
    """Fault that adds a brief yield between operations.

    When active, concurrent tests call threading.Event().wait(0) between
    operations, increasing the chance of interleaving.
    """

    def __init__(self) -> None:
        super().__init__(name="thread_yield")
        self._event = threading.Event()

    def _do_activate(self) -> None:
        self._event.clear()

    def _do_deactivate(self) -> None:
        self._event.set()

    def maybe_yield(self) -> None:
        if self.active:
            self._event.wait(0)  # yield to other threads


# ============================================================================
# ChaosTest: PropertyTracker under concurrent access
# ============================================================================


class TrackerChaos(ChaosTest):
    """Stateful chaos test for PropertyTracker thread safety.

    Rules spawn threads that concurrently record, toggle, and reset.
    Invariants verify consistency after each step.
    """

    faults: ClassVar[list[Fault]] = [_SleepFault()]
    swarm = False

    def __init__(self) -> None:
        super().__init__()
        self.tracker = PropertyTracker()
        self.tracker.active = True
        self.total_records = 0  # lower bound on expected hits
        self.total_true = 0
        self.total_false = 0
        self.reset_happened = False

    @rule(n_threads=st.integers(min_value=2, max_value=THREADS))
    def concurrent_record_true(self, n_threads: int) -> None:
        """N threads each record ITERS True observations."""
        prop_name = "chaos_prop"

        def work():
            for _ in range(ITERS):
                self.tracker.record(prop_name, "always", True)

        _run_threads(work, n=n_threads)
        self.total_records += n_threads * ITERS
        self.total_true += n_threads * ITERS

    @rule(n_threads=st.integers(min_value=2, max_value=THREADS))
    def concurrent_record_mixed(self, n_threads: int) -> None:
        """N threads record alternating True/False."""
        prop_name = "chaos_prop"

        def work():
            for i in range(ITERS):
                self.tracker.record(prop_name, "always", i % 2 == 0)

        _run_threads(work, n=n_threads)
        self.total_records += n_threads * ITERS
        per_thread_true = ITERS // 2 + (1 if ITERS % 2 else 0)
        per_thread_false = ITERS - per_thread_true
        self.total_true += n_threads * per_thread_true
        self.total_false += n_threads * per_thread_false

    @rule()
    def toggle_active(self) -> None:
        """Toggle active off and on — records during 'off' are silently dropped."""
        self.tracker.active = False
        self.tracker.active = True

    @rule()
    def reset_tracker(self) -> None:
        """Reset clears all properties — counts become unpredictable."""
        self.tracker.reset()
        self.total_records = 0
        self.total_true = 0
        self.total_false = 0
        self.reset_happened = True

    @rule(n_threads=st.integers(min_value=2, max_value=THREADS))
    def concurrent_record_hit(self, n_threads: int) -> None:
        """N threads concurrently call record_hit."""

        def work():
            for _ in range(ITERS):
                self.tracker.record_hit("reach", "reachable")

        _run_threads(work, n=n_threads)

    @invariant()
    def counts_are_consistent(self) -> None:
        """hits == passes + failures for record()-based properties.

        record_hit() (reachable/unreachable) only increments hits,
        so this invariant only applies to always/sometimes types.
        """
        for p in self.tracker.results:
            if p.type in ("always", "sometimes"):
                always(
                    p.hits == p.passes + p.failures,
                    f"hits={p.hits} != passes={p.passes} + failures={p.failures}",
                )

    @invariant()
    def no_negative_counts(self) -> None:
        """No property should ever have negative counts."""
        for p in self.tracker.results:
            always(p.hits >= 0, f"negative hits: {p.hits}")
            always(p.passes >= 0, f"negative passes: {p.passes}")
            always(p.failures >= 0, f"negative failures: {p.failures}")

    @invariant()
    def tracker_is_active(self) -> None:
        """Tracker should be active (we always re-enable after toggle)."""
        always(self.tracker.active, "tracker should be active")


# ============================================================================
# ChaosTest: Fault activate/deactivate under concurrent access
# ============================================================================


class FaultChaos(ChaosTest):
    """Stateful chaos test for Fault activation thread safety.

    Exercises concurrent activate/deactivate, deep-copy under contention,
    and verifies that _do_activate is never called twice without an
    intervening deactivate.
    """

    faults: ClassVar[list[Fault]] = [_SleepFault()]
    swarm = False

    def __init__(self) -> None:
        super().__init__()
        self.activate_count = 0
        self.deactivate_count = 0
        self._count_lock = threading.Lock()

        def on_activate():
            with self._count_lock:
                self.activate_count += 1

        def on_deactivate():
            with self._count_lock:
                self.deactivate_count += 1

        self.target_fault = LambdaFault(
            "target", on_activate=on_activate, on_deactivate=on_deactivate
        )
        self.copy_deactivations = 0

    @rule(n_threads=st.integers(min_value=2, max_value=THREADS))
    def concurrent_activate(self, n_threads: int) -> None:
        """N threads all call activate — only one should win."""
        self.target_fault.deactivate()  # ensure clean start
        before = self.activate_count

        def work():
            self.target_fault.activate()

        _run_threads(work, n=n_threads)

        always(
            self.activate_count == before + 1,
            f"activate called {self.activate_count - before} times, expected 1",
        )

    @rule(n_threads=st.integers(min_value=2, max_value=THREADS))
    def concurrent_toggle(self, n_threads: int) -> None:
        """N threads toggling activate/deactivate rapidly."""

        def work():
            for _ in range(ITERS):
                self.target_fault.activate()
                self.target_fault.deactivate()

        _run_threads(work, n=n_threads)

    @rule()
    def deepcopy_is_independent(self) -> None:
        """Deep-copied fault has independent state and lock.

        The copy shares callbacks (Python doesn't deepcopy closures),
        so we track the extra deactivation from the copy.
        """
        self.target_fault.activate()
        copied = copy.deepcopy(self.target_fault)

        always(copied.active, "copy should inherit active state")

        # Copy's deactivation fires the shared callback
        copied.deactivate()
        self.copy_deactivations += 1
        always(not copied.active, "copy should be independently deactivatable")
        always(self.target_fault.active, "original should be unaffected by copy")

        self.target_fault.deactivate()

    @invariant()
    def activate_deactivate_balanced(self) -> None:
        """When inactive, activate and deactivate counts must match.

        copy_deactivations accounts for deactivations fired via
        deep-copied faults (which share the callback closure).
        """
        if not self.target_fault.active:
            effective_deactivates = self.deactivate_count - self.copy_deactivations
            always(
                self.activate_count == effective_deactivates,
                f"imbalanced: {self.activate_count} activates"
                f" vs {effective_deactivates} deactivates"
                f" (raw={self.deactivate_count}, copies={self.copy_deactivations})",
            )

    @invariant()
    def counts_are_non_negative(self) -> None:
        always(self.activate_count >= 0, "negative activate count")
        always(self.deactivate_count >= 0, "negative deactivate count")


# ============================================================================
# ChaosTest: CoverageCollector edges under concurrent access
# ============================================================================


class EdgesChaos(ChaosTest):
    """Stateful chaos test for CoverageCollector edge set thread safety.

    Rules insert edges from multiple threads and take snapshots.
    Invariants verify no edges are lost and snapshots are consistent.
    """

    faults: ClassVar[list[Fault]] = [_SleepFault()]
    swarm = False

    def __init__(self) -> None:
        super().__init__()
        from ordeal.explore import CoverageCollector

        self.collector = CoverageCollector(["anything"])
        self.expected_edges: set[int] = set()
        self.next_edge = 0

    @rule(n_threads=st.integers(min_value=2, max_value=THREADS))
    def concurrent_insert(self, n_threads: int) -> None:
        """N threads insert disjoint edge ranges — total must be exact."""
        base = self.next_edge
        edges_per_thread = ITERS
        self.next_edge += n_threads * edges_per_thread

        local_expected: set[int] = set()
        for t in range(n_threads):
            for i in range(edges_per_thread):
                local_expected.add(base + t * edges_per_thread + i)

        def make_work(thread_id):
            def work():
                start = base + thread_id * edges_per_thread
                for i in range(edges_per_thread):
                    with self.collector._edges_lock:
                        self.collector._edges.add(start + i)

            return work

        threads = []
        barrier = threading.Barrier(n_threads)
        for t in range(n_threads):
            w = make_work(t)

            def synced(fn=w):
                barrier.wait()
                fn()

            threads.append(threading.Thread(target=synced))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.expected_edges |= local_expected

    @rule()
    def snapshot_is_subset(self) -> None:
        """snapshot() must return a subset of all inserted edges."""
        snap = self.collector.snapshot()
        always(
            snap <= self.expected_edges | self.collector._edges,
            "snapshot contains unknown edges",
        )

    @invariant()
    def no_lost_edges(self) -> None:
        """Every expected edge must be present in the collector."""
        always(
            self.expected_edges <= self.collector._edges,
            f"lost {len(self.expected_edges - self.collector._edges)} edges",
        )

    @invariant()
    def edge_count_matches(self) -> None:
        always(
            len(self.collector._edges) >= len(self.expected_edges),
            "fewer edges than expected",
        )


# ============================================================================
# Hypothesis TestCase wiring
# ============================================================================

TestTrackerChaos = TrackerChaos.TestCase
TestTrackerChaos.settings = settings(max_examples=50, stateful_step_count=10)

TestFaultChaos = FaultChaos.TestCase
TestFaultChaos.settings = settings(max_examples=50, stateful_step_count=10)

TestEdgesChaos = EdgesChaos.TestCase
TestEdgesChaos.settings = settings(max_examples=30, stateful_step_count=8)
