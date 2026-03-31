"""Tests for ordeal's API chaos testing — fault toggling, tracker activation, reset.

These tests exercise the ordeal-specific logic without requiring a running API server.
"""

from __future__ import annotations

from ordeal.assertions import tracker
from ordeal.faults import LambdaFault
from ordeal.integrations.openapi import with_chaos


def _make_faults(n: int = 3) -> list[LambdaFault]:
    """Create n trackable faults."""
    log: list[str] = []
    faults = []
    for i in range(n):
        f = LambdaFault(
            f"fault_{i}",
            on_activate=lambda i=i: log.append(f"on_{i}"),
            on_deactivate=lambda i=i: log.append(f"off_{i}"),
        )
        f._test_log = log
        faults.append(f)
    return faults


# ============================================================================
# with_chaos decorator
# ============================================================================


class TestWithChaos:
    def test_activates_tracker(self):
        tracker.active = False
        faults = _make_faults(1)

        @with_chaos(faults, seed=42)
        def fn():
            return tracker.active

        assert fn() is True
        tracker.active = False

    def test_toggles_faults(self):
        faults = _make_faults(3)

        call_count = 0

        @with_chaos(faults, fault_probability=1.0, seed=42)
        def fn():
            nonlocal call_count
            call_count += 1
            # All faults should be active (prob=1.0)
            assert all(f.active for f in faults)

        fn()
        assert call_count == 1

    def test_resets_faults_after_call(self):
        faults = _make_faults(2)

        @with_chaos(faults, fault_probability=1.0, seed=42)
        def fn():
            pass

        fn()
        # After call, all faults should be deactivated
        assert all(not f.active for f in faults)

    def test_resets_faults_on_exception(self):
        faults = _make_faults(2)

        @with_chaos(faults, fault_probability=1.0, seed=42)
        def fn():
            raise ValueError("boom")

        try:
            fn()
        except ValueError:
            pass
        # Even on exception, faults should be reset
        assert all(not f.active for f in faults)

    def test_zero_probability_no_activation(self):
        faults = _make_faults(3)

        @with_chaos(faults, fault_probability=0.0, seed=42)
        def fn():
            assert all(not f.active for f in faults)

        fn()

    def test_seed_reproducibility(self):
        """Same seed should produce same fault activation pattern."""
        faults1 = _make_faults(5)
        faults2 = _make_faults(5)
        patterns1 = []
        patterns2 = []

        @with_chaos(faults1, fault_probability=0.5, seed=123)
        def fn1():
            patterns1.append(tuple(f.active for f in faults1))

        @with_chaos(faults2, fault_probability=0.5, seed=123)
        def fn2():
            patterns2.append(tuple(f.active for f in faults2))

        for _ in range(10):
            fn1()
            fn2()

        assert patterns1 == patterns2

    def test_swarm_uses_subset(self):
        """Swarm mode should only toggle a subset of faults."""
        faults = _make_faults(10)
        ever_active: set[str] = set()

        @with_chaos(faults, fault_probability=1.0, seed=42, swarm=True)
        def fn():
            for f in faults:
                if f.active:
                    ever_active.add(f.name)

        for _ in range(20):
            fn()
        # Swarm picks a subset — not all 10 should be eligible
        assert 0 < len(ever_active) < 10
