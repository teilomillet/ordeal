"""Tests for ordeal.faults.concurrency — concurrency fault injections."""

import threading
import time

import pytest

from ordeal.faults.concurrency import (
    contended_call,
    delayed_release,
    stale_state,
    thread_boundary,
)

# -- Helpers ----------------------------------------------------------------


def fast_function(x: int) -> int:
    return x * 2


class _StatefulService:
    def __init__(self):
        self.config = {"mode": "production", "retries": 3}
        self.cache = [1, 2, 3]


# -- Tests ------------------------------------------------------------------


class TestContendedCall:
    def test_serializes_calls(self):
        fault = contended_call(f"{__name__}.fast_function", contention=0.0)
        fault.activate()
        # Still returns correct result
        assert fast_function(5) == 10
        fault.deactivate()

    def test_real_mode_adds_delay(self):
        fault = contended_call(f"{__name__}.fast_function", contention=0.05, mode="real")
        fault.activate()
        start = time.time()
        fast_function(1)
        elapsed = time.time() - start
        # Should have some delay (at least part of 0.05s)
        assert elapsed >= 0.01
        fault.deactivate()

    def test_concurrent_access_serialized(self):
        fault = contended_call(f"{__name__}.fast_function", contention=0.0, mode="simulate")
        fault.activate()
        results = []
        errors = []

        def worker(val):
            try:
                results.append(fast_function(val))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors
        assert sorted(results) == [i * 2 for i in range(10)]
        fault.deactivate()


class TestDelayedRelease:
    def test_returns_correct_result(self):
        fault = delayed_release(f"{__name__}.fast_function", delay=0.0)
        fault.activate()
        assert fast_function(7) == 14
        fault.deactivate()

    def test_real_mode_adds_post_delay(self):
        fault = delayed_release(f"{__name__}.fast_function", delay=0.05, mode="real")
        fault.activate()
        start = time.time()
        result = fast_function(3)
        elapsed = time.time() - start
        assert result == 6
        assert elapsed >= 0.01
        fault.deactivate()


class TestThreadBoundary:
    def test_executes_on_different_thread(self):
        main_thread = threading.current_thread().ident
        observed_threads = []

        def track_thread(x: int) -> int:
            observed_threads.append(threading.current_thread().ident)
            return x * 2

        import tests.test_faults_concurrency as mod

        mod._track_thread = track_thread

        fault = thread_boundary("tests.test_faults_concurrency._track_thread")
        fault.activate()
        result = mod._track_thread(5)
        assert result == 10
        assert observed_threads[0] != main_thread
        fault.deactivate()

    def test_propagates_exceptions(self):
        def failing_fn() -> None:
            raise ValueError("boom")

        import tests.test_faults_concurrency as mod

        mod._failing_fn = failing_fn

        fault = thread_boundary("tests.test_faults_concurrency._failing_fn")
        fault.activate()
        with pytest.raises(ValueError, match="boom"):
            mod._failing_fn()
        fault.deactivate()


# Module-level helpers for patching
def _track_thread(x: int) -> int:
    return x * 2


def _failing_fn() -> None:
    raise ValueError("boom")


class TestStaleState:
    def test_injects_stale_value(self):
        svc = _StatefulService()
        assert svc.config["mode"] == "production"

        fault = stale_state(svc, "config", {"mode": "degraded", "retries": 0})
        fault.activate()
        assert svc.config["mode"] == "degraded"
        assert svc.config["retries"] == 0

        fault.deactivate()
        assert svc.config["mode"] == "production"
        assert svc.config["retries"] == 3

    def test_reset_restores(self):
        svc = _StatefulService()
        fault = stale_state(svc, "cache", [])
        fault.activate()
        assert svc.cache == []
        fault.reset()
        assert svc.cache == [1, 2, 3]
