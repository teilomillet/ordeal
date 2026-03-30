"""Concurrent thread-safety tests for free-threaded Python 3.13/3.14.

These tests verify correctness under contention, not just absence of crashes.
Each test spawns multiple threads hitting shared state and checks that
invariants hold on the final result.
"""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from ordeal.assertions import PropertyTracker, always, unreachable
from ordeal.faults import LambdaFault
from ordeal.faults.timing import intermittent_crash, jitter

# ============================================================================
# Helpers
# ============================================================================

THREADS = 16
ITERS = 500


def _run_concurrent(fn, n_threads=THREADS):
    """Run fn(thread_id) in n_threads threads, return list of results."""
    results = []
    errors = []
    barrier = threading.Barrier(n_threads)

    def worker(tid):
        barrier.wait()  # maximize contention by starting together
        try:
            return fn(tid)
        except Exception as e:
            errors.append(e)
            raise

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(worker, i) for i in range(n_threads)]
        for f in as_completed(futures):
            results.append(f.result())

    assert not errors, f"Thread errors: {errors}"
    return results


# ============================================================================
# PropertyTracker
# ============================================================================


class TestPropertyTrackerThreadSafety:
    def test_concurrent_record_total_hits(self):
        """N threads each record M hits -> total hits == N*M."""
        tracker = PropertyTracker()
        tracker.active = True

        def work(tid):
            for _ in range(ITERS):
                tracker.record("prop", "always", True)

        _run_concurrent(work)

        results = tracker.results
        assert len(results) == 1
        p = results[0]
        assert p.hits == THREADS * ITERS
        assert p.passes == THREADS * ITERS
        assert p.failures == 0

    def test_concurrent_record_mixed_pass_fail(self):
        """Even threads pass, odd threads fail -> counts are exact."""
        tracker = PropertyTracker()
        tracker.active = True

        def work(tid):
            condition = tid % 2 == 0
            for _ in range(ITERS):
                tracker.record("mixed", "always", condition)

        _run_concurrent(work)

        p = tracker.results[0]
        assert p.hits == THREADS * ITERS
        even_threads = THREADS // 2
        odd_threads = THREADS - even_threads
        assert p.passes == even_threads * ITERS
        assert p.failures == odd_threads * ITERS

    def test_concurrent_record_multiple_properties(self):
        """Each thread records to its own property -> no cross-contamination."""
        tracker = PropertyTracker()
        tracker.active = True

        def work(tid):
            name = f"prop_{tid}"
            for _ in range(ITERS):
                tracker.record(name, "sometimes", True)

        _run_concurrent(work)

        results = {p.name: p for p in tracker.results}
        assert len(results) == THREADS
        for tid in range(THREADS):
            p = results[f"prop_{tid}"]
            assert p.hits == ITERS
            assert p.passes == ITERS

    def test_concurrent_record_hit(self):
        """record_hit from N threads -> hits == N*M."""
        tracker = PropertyTracker()
        tracker.active = True

        def work(tid):
            for _ in range(ITERS):
                tracker.record_hit("reach", "reachable")

        _run_concurrent(work)

        p = tracker.results[0]
        assert p.hits == THREADS * ITERS

    def test_active_toggle_during_recording(self):
        """Toggling active while recording shouldn't corrupt state."""
        tracker = PropertyTracker()
        tracker.active = True

        def recorder(tid):
            for _ in range(ITERS):
                tracker.record("prop", "always", True)

        def toggler(tid):
            for _ in range(ITERS):
                tracker.active = False
                tracker.active = True

        barrier = threading.Barrier(THREADS + 2)

        def run_recorder():
            barrier.wait()
            for _ in range(ITERS):
                tracker.record("prop", "always", True)

        def run_toggler():
            barrier.wait()
            for _ in range(ITERS):
                tracker.active = False
                tracker.active = True

        threads = []
        for _ in range(THREADS):
            threads.append(threading.Thread(target=run_recorder))
        threads.append(threading.Thread(target=run_toggler))
        threads.append(threading.Thread(target=run_toggler))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # We can't predict exact count (some records skipped when inactive),
        # but state must be consistent: no negative counts, no corruption
        results = tracker.results
        if results:
            p = results[0]
            assert p.hits >= 0
            assert p.passes >= 0
            assert p.failures == 0
            assert p.passes == p.hits  # all conditions were True

    def test_reset_during_recording(self):
        """reset() while recording shouldn't crash or corrupt."""
        tracker = PropertyTracker()
        tracker.active = True

        barrier = threading.Barrier(THREADS + 1)

        def recorder():
            barrier.wait()
            for _ in range(ITERS):
                tracker.record("prop", "always", True)

        def resetter():
            barrier.wait()
            for _ in range(ITERS // 10):
                tracker.reset()

        threads = [threading.Thread(target=recorder) for _ in range(THREADS)]
        threads.append(threading.Thread(target=resetter))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # After concurrent reset + record, state should be consistent
        for p in tracker.results:
            assert p.hits >= 0
            assert p.passes >= 0


    def test_record_returns_active_state(self):
        """record() return value reflects the active state atomically."""
        tracker = PropertyTracker()

        assert tracker.record("x", "always", True) is False  # inactive
        tracker.active = True
        assert tracker.record("x", "always", True) is True  # active

        assert tracker.record_hit("y", "reachable") is True
        tracker.active = False
        assert tracker.record_hit("y", "reachable") is False

    def test_always_no_toctou(self):
        """always() uses atomic active check — no raise after deactivation.

        The record() return value determines whether to raise, not a
        separate read of tracker.active. This prevents a TOCTOU race
        where active is toggled between record() and the raise check.
        """
        import ordeal.assertions as _mod

        tracker = PropertyTracker()
        old_tracker = _mod.tracker
        _mod.tracker = tracker

        try:
            # When inactive: record returns False, no raise even on violation
            tracker.active = False
            always(False, "should_not_raise")  # must not raise

            # When active: record returns True, raise on violation
            tracker.active = True
            with pytest.raises(AssertionError, match="always violated"):
                always(False, "should_raise")
        finally:
            _mod.tracker = old_tracker

    def test_unreachable_no_toctou(self):
        """unreachable() uses atomic active check — no raise after deactivation."""
        import ordeal.assertions as _mod

        tracker = PropertyTracker()
        old_tracker = _mod.tracker
        _mod.tracker = tracker

        try:
            tracker.active = False
            unreachable("should_not_raise")  # must not raise

            tracker.active = True
            with pytest.raises(AssertionError, match="unreachable code reached"):
                unreachable("should_raise")
        finally:
            _mod.tracker = old_tracker


# ============================================================================
# Fault base class — activate/deactivate
# ============================================================================


class TestFaultThreadSafety:
    def test_concurrent_activate_deactivate(self):
        """Rapid activate/deactivate from many threads shouldn't double-patch."""
        activate_count = 0
        deactivate_count = 0
        count_lock = threading.Lock()

        def on_activate():
            nonlocal activate_count
            with count_lock:
                activate_count += 1

        def on_deactivate():
            nonlocal deactivate_count
            with count_lock:
                deactivate_count += 1

        fault = LambdaFault("test", on_activate=on_activate, on_deactivate=on_deactivate)

        def work(tid):
            for _ in range(ITERS):
                fault.activate()
                fault.deactivate()

        _run_concurrent(work)

        # activate and deactivate must have been called the same number of times
        assert activate_count == deactivate_count
        assert not fault.active

    def test_concurrent_activate_only(self):
        """Many threads calling activate -> _do_activate called exactly once."""
        call_count = 0
        count_lock = threading.Lock()

        def on_activate():
            nonlocal call_count
            with count_lock:
                call_count += 1

        fault = LambdaFault("test", on_activate=on_activate, on_deactivate=lambda: None)

        def work(tid):
            fault.activate()

        _run_concurrent(work)

        assert call_count == 1
        assert fault.active

    def test_deepcopy_produces_independent_locks(self):
        """Deep-copied fault has its own lock, independent state."""
        fault = LambdaFault("test", on_activate=lambda: None, on_deactivate=lambda: None)
        fault.activate()

        copied = copy.deepcopy(fault)

        # Copied fault should have same active state
        assert copied.active
        # But independent lock — deactivating copy doesn't affect original
        copied.deactivate()
        assert not copied.active
        assert fault.active  # original unchanged

    def test_deepcopy_lock_is_functional(self):
        """Deep-copied fault's lock actually works under contention."""
        fault = LambdaFault("test", on_activate=lambda: None, on_deactivate=lambda: None)
        copied = copy.deepcopy(fault)

        call_count = 0
        count_lock = threading.Lock()

        # Replace the on_activate to count calls
        def counting_activate():
            nonlocal call_count
            with count_lock:
                call_count += 1

        copied._on_activate = counting_activate

        def work(tid):
            copied.activate()

        _run_concurrent(work)

        # Only one activate should have gone through
        assert call_count == 1


# ============================================================================
# Fault counters — intermittent_crash and jitter
# ============================================================================

# Target function that the faults will patch
_target_call_count = 0


def _dummy_target():
    """Dummy function to be patched by faults."""
    global _target_call_count
    _target_call_count += 1
    return 42.0


class TestFaultCounterThreadSafety:
    def test_intermittent_crash_counter_accuracy(self):
        """Concurrent calls -> crash count matches expected ratio."""
        fault = intermittent_crash(f"{__name__}._dummy_target", every_n=5)
        fault.activate()

        crash_count = 0
        success_count = 0
        crash_lock = threading.Lock()

        def work(tid):
            nonlocal crash_count, success_count
            local_crashes = 0
            local_successes = 0
            for _ in range(ITERS):
                try:
                    _dummy_target()
                    local_successes += 1
                except RuntimeError:
                    local_crashes += 1
            with crash_lock:
                crash_count += local_crashes
                success_count += local_successes

        _run_concurrent(work)
        fault.deactivate()

        total = crash_count + success_count
        assert total == THREADS * ITERS

        # Every 5th call should crash. With lock, counter is exact.
        expected_crashes = total // 5
        assert crash_count == expected_crashes, (
            f"Expected {expected_crashes} crashes from {total} calls, got {crash_count}"
        )

    def test_jitter_counter_accuracy(self):
        """Concurrent jitter calls -> counter tracks correctly."""
        fault = jitter(f"{__name__}._dummy_target", magnitude=0.1)
        fault.activate()

        results = []
        results_lock = threading.Lock()

        def work(tid):
            local_results = []
            for _ in range(ITERS):
                val = _dummy_target()
                local_results.append(val)
            with results_lock:
                results.extend(local_results)

        _run_concurrent(work)
        fault.deactivate()

        total = len(results)
        assert total == THREADS * ITERS

        # Every result should be jittered (not the original 42.0)
        # Even-count calls add, odd-count calls subtract
        non_original = [r for r in results if r != 42.0]
        assert len(non_original) == total, "All calls should be jittered when fault is active"

        # The jittered values should be 42.0 +/- 0.1 * 42.0 = 42.0 +/- 4.2
        for r in results:
            assert abs(r - 42.0) == pytest.approx(0.1 * 42.0, abs=0.01)


# ============================================================================
# CoverageCollector — edges set
# ============================================================================


class TestCoverageCollectorThreadSafety:
    def test_concurrent_edge_insertion(self):
        """Direct concurrent adds to _edges -> no lost edges."""
        from ordeal.explore import CoverageCollector

        collector = CoverageCollector(["anything"])

        # Bypass sys.settrace — directly test the _edges set + lock
        all_edges = set()
        per_thread_edges = {}

        def work(tid):
            local = set()
            for i in range(ITERS):
                edge = tid * ITERS + i
                local.add(edge)
                with collector._edges_lock:
                    collector._edges.add(edge)
            per_thread_edges[tid] = local

        _run_concurrent(work)

        # Union of all per-thread edges
        for s in per_thread_edges.values():
            all_edges |= s

        assert len(collector._edges) == len(all_edges)
        assert collector._edges == all_edges

    def test_snapshot_during_insertion(self):
        """snapshot() during concurrent inserts returns a consistent frozen set."""
        from ordeal.explore import CoverageCollector

        collector = CoverageCollector(["anything"])
        snapshots = []
        snap_lock = threading.Lock()

        barrier = threading.Barrier(THREADS + 1)

        def inserter():
            barrier.wait()
            for i in range(ITERS):
                with collector._edges_lock:
                    collector._edges.add(i)

        def snapshotter():
            barrier.wait()
            for _ in range(ITERS // 5):
                snap = collector.snapshot()
                with snap_lock:
                    snapshots.append(snap)

        threads = [threading.Thread(target=inserter) for _ in range(THREADS)]
        threads.append(threading.Thread(target=snapshotter))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each snapshot should be a subset of the final state
        final = collector._edges
        for snap in snapshots:
            assert snap <= final, "Snapshot should never contain edges not in final set"

        # Snapshots should be monotonically non-decreasing in size
        # (not strictly, since snapshot() is a point-in-time read)
        # But no snapshot should be empty if inserts already started
        # We can't guarantee order, but we can check consistency
        for snap in snapshots:
            assert isinstance(snap, frozenset)

    def test_prev_loc_is_per_thread(self):
        """Each thread gets its own _prev_loc via threading.local."""
        from ordeal.explore import CoverageCollector

        collector = CoverageCollector(["anything"])
        collector._tls.prev_loc = 999

        seen = {}
        barrier = threading.Barrier(THREADS)

        def work():
            barrier.wait()
            tid = threading.current_thread().ident
            # New thread should NOT see main thread's prev_loc
            val = getattr(collector._tls, "prev_loc", -1)
            seen[tid] = val
            # Set our own
            collector._tls.prev_loc = tid

        threads = [threading.Thread(target=work) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Worker threads should have gotten the default (-1), not 999
        for tid, val in seen.items():
            assert val == -1, f"Thread {tid} saw prev_loc={val}, expected -1 (isolation failure)"

        # Main thread's value should be untouched
        assert collector._tls.prev_loc == 999
