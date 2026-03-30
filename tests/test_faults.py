"""Tests for ordeal.faults — fault injection primitives."""

import math

import pytest

from ordeal.faults import Fault, LambdaFault, PatchFault

# -- Helpers ----------------------------------------------------------------

# A simple module-level function to patch
_call_log: list[str] = []


def sample_function(x: int) -> int:
    _call_log.append(f"called({x})")
    return x * 2


# -- Tests ------------------------------------------------------------------


class TestFaultBase:
    def test_starts_inactive(self):
        class F(Fault):
            def _do_activate(self):
                pass

            def _do_deactivate(self):
                pass

        f = F(name="test")
        assert not f.active

    def test_activate_deactivate_cycle(self):
        activated = []

        class F(Fault):
            def _do_activate(self):
                activated.append("on")

            def _do_deactivate(self):
                activated.append("off")

        f = F()
        f.activate()
        assert f.active
        assert activated == ["on"]
        f.deactivate()
        assert not f.active
        assert activated == ["on", "off"]

    def test_double_activate_is_noop(self):
        count = [0]

        class F(Fault):
            def _do_activate(self):
                count[0] += 1

            def _do_deactivate(self):
                pass

        f = F()
        f.activate()
        f.activate()
        assert count[0] == 1

    def test_reset_deactivates(self):
        class F(Fault):
            def _do_activate(self):
                pass

            def _do_deactivate(self):
                pass

        f = F()
        f.activate()
        f.reset()
        assert not f.active

    def test_repr(self):
        class F(Fault):
            def _do_activate(self):
                pass

            def _do_deactivate(self):
                pass

        f = F(name="my_fault")
        assert "my_fault" in repr(f)
        assert "OFF" in repr(f)
        f.activate()
        assert "ON" in repr(f)


class TestLambdaFault:
    def test_calls_callbacks(self):
        log = []
        f = LambdaFault("test", lambda: log.append("on"), lambda: log.append("off"))
        f.activate()
        assert log == ["on"]
        f.deactivate()
        assert log == ["on", "off"]


class TestPatchFault:
    def setup_method(self):
        _call_log.clear()

    def test_patches_function_when_active(self):
        def make_wrapper(original):
            def wrapper(*args, **kwargs):
                return -1

            return wrapper

        target = f"{__name__}.sample_function"
        fault = PatchFault(target, make_wrapper, name="test_patch")

        # Before activation: normal behavior
        assert sample_function(5) == 10

        # Activate: patched behavior
        fault.activate()
        assert sample_function(5) == -1

        # Deactivate: restored
        fault.deactivate()
        assert sample_function(5) == 10

    def test_reset_clears_resolved_state(self):
        fault = PatchFault(
            f"{__name__}.sample_function",
            lambda orig: lambda *a, **k: 999,
        )
        fault.activate()
        assert sample_function(1) == 999
        fault.reset()
        assert sample_function(1) == 2
        # Can re-activate after reset
        fault.activate()
        assert sample_function(1) == 999
        fault.deactivate()


class TestIOFaults:
    def test_error_on_call(self):
        from ordeal.faults.io import error_on_call

        fault = error_on_call(f"{__name__}.sample_function", ValueError, "boom")
        fault.activate()
        with pytest.raises(ValueError, match="boom"):
            sample_function(1)
        fault.deactivate()
        assert sample_function(1) == 2

    def test_return_empty(self):
        from ordeal.faults.io import return_empty

        fault = return_empty(f"{__name__}.sample_function")
        fault.activate()
        assert sample_function(1) is None
        fault.deactivate()
        assert sample_function(1) == 2

    def test_truncate_output(self):
        from ordeal.faults.io import truncate_output

        def string_fn() -> str:
            return "hello world"

        # Patch at module level
        import tests.test_faults as mod

        mod.string_fn = string_fn

        fault = truncate_output("tests.test_faults.string_fn", fraction=0.5)
        fault.activate()
        result = mod.string_fn()
        assert len(result) == 5  # half of 11 = 5
        fault.deactivate()


# Make string_fn accessible at module level for the test above
def string_fn() -> str:
    return "hello world"


class TestNumericalFaults:
    def test_nan_injection(self):
        from ordeal.faults.numerical import nan_injection

        def predict() -> float:
            return 0.95

        import tests.test_faults as mod

        mod._predict = predict

        fault = nan_injection("tests.test_faults._predict")
        fault.activate()
        result = mod._predict()
        assert math.isnan(result)
        fault.deactivate()
        assert mod._predict() == 0.95

    def test_inf_injection(self):
        from ordeal.faults.numerical import inf_injection

        def score() -> float:
            return 1.0

        import tests.test_faults as mod

        mod._score = score

        fault = inf_injection("tests.test_faults._score")
        fault.activate()
        assert math.isinf(mod._score())
        fault.deactivate()


# Module-level helpers for patching
def _predict() -> float:
    return 0.95


def _score() -> float:
    return 1.0


class TestTimingFaults:
    def test_timeout_raises(self):
        from ordeal.faults.timing import timeout

        fault = timeout(f"{__name__}.sample_function", delay=5.0)
        fault.activate()
        with pytest.raises(TimeoutError, match="Simulated timeout"):
            sample_function(1)
        fault.deactivate()

    def test_intermittent_crash(self):
        from ordeal.faults.timing import intermittent_crash

        fault = intermittent_crash(f"{__name__}.sample_function", every_n=2)
        fault.activate()
        assert sample_function(1) == 2  # call 1: ok
        with pytest.raises(RuntimeError, match="Simulated crash"):
            sample_function(2)  # call 2: crash
        assert sample_function(3) == 6  # call 3: ok
        fault.deactivate()

    def test_intermittent_crash_resets_counter(self):
        from ordeal.faults.timing import intermittent_crash

        fault = intermittent_crash(f"{__name__}.sample_function", every_n=2)
        fault.activate()
        sample_function(1)
        fault.reset()
        fault.activate()
        # Counter should be reset, so call 1 again is ok
        assert sample_function(1) == 2
