"""Tests for fault ablation — determine which faults are necessary for a failure."""

from __future__ import annotations

from hypothesis.stateful import rule

from ordeal.chaos import ChaosTest
from ordeal.faults import LambdaFault
from ordeal.trace import Trace, TraceFailure, TraceStep, ablate_faults, replay

# ============================================================================
# Test fixtures: ChaosTests with faults
# ============================================================================

# Thread-safe state flags for faults (per-class to avoid cross-test leakage)
_fault_a_flag = False
_fault_b_flag = False


def _make_fault_a():
    """Fault that sets a global flag when active."""

    def _on():
        global _fault_a_flag
        _fault_a_flag = True

    def _off():
        global _fault_a_flag
        _fault_a_flag = False

    return LambdaFault("fault_a", on_activate=_on, on_deactivate=_off)


def _make_fault_b():
    """Fault that sets a global flag when active."""

    def _on():
        global _fault_b_flag
        _fault_b_flag = True

    def _off():
        global _fault_b_flag
        _fault_b_flag = False

    return LambdaFault("fault_b", on_activate=_on, on_deactivate=_off)


class _NeedsFaultA(ChaosTest):
    """Only fails when fault_a is active."""

    faults = [_make_fault_a(), _make_fault_b()]

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 2 and _fault_a_flag:
            raise ValueError("fault_a caused this")


class _NeedsBothFaults(ChaosTest):
    """Fails only when BOTH fault_a AND fault_b are active."""

    faults = [_make_fault_a(), _make_fault_b()]

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 2 and _fault_a_flag and _fault_b_flag:
            raise ValueError("both faults caused this")


class _NeedsNoFaults(ChaosTest):
    """Always fails regardless of faults."""

    faults = [_make_fault_a(), _make_fault_b()]

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 2:
            raise ValueError("faults don't matter")


# ============================================================================
# Helpers
# ============================================================================


def _class_path(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _make_trace_with_faults(
    test_class: type,
    active_faults: list[str],
) -> Trace:
    """Build a trace that activates the given faults, then ticks twice."""
    steps: list[TraceStep] = []
    for fname in active_faults:
        steps.append(TraceStep(kind="fault_toggle", name=f"+{fname}"))
    steps.append(TraceStep(kind="rule", name="tick", params={}))
    steps.append(TraceStep(kind="rule", name="tick", params={}))
    return Trace(
        run_id=1,
        seed=42,
        test_class=_class_path(test_class),
        from_checkpoint=None,
        steps=steps,
        failure=TraceFailure(error_type="ValueError", error_message="boom", step=1),
    )


# ============================================================================
# Tests
# ============================================================================


class TestAblation:
    def setup_method(self):
        """Reset fault flags before each test."""
        global _fault_a_flag, _fault_b_flag
        _fault_a_flag = False
        _fault_b_flag = False

    def test_single_fault_necessary(self):
        """fault_a is necessary, fault_b is not."""
        trace = _make_trace_with_faults(_NeedsFaultA, ["fault_a", "fault_b"])
        # Verify it reproduces
        err = replay(trace, _NeedsFaultA)
        assert err is not None

        result = ablate_faults(trace, _NeedsFaultA)
        assert result["fault_a"] is True  # necessary
        assert result["fault_b"] is False  # unnecessary

    def test_both_faults_necessary(self):
        """Both faults are necessary."""
        trace = _make_trace_with_faults(_NeedsBothFaults, ["fault_a", "fault_b"])
        err = replay(trace, _NeedsBothFaults)
        assert err is not None

        result = ablate_faults(trace, _NeedsBothFaults)
        assert result["fault_a"] is True
        assert result["fault_b"] is True

    def test_no_faults_necessary(self):
        """Bug reproduces without any faults."""
        trace = _make_trace_with_faults(_NeedsNoFaults, ["fault_a", "fault_b"])
        err = replay(trace, _NeedsNoFaults)
        assert err is not None

        result = ablate_faults(trace, _NeedsNoFaults)
        assert result["fault_a"] is False
        assert result["fault_b"] is False

    def test_no_fault_toggles_returns_empty(self):
        """Trace with no fault toggles returns empty dict."""
        trace = Trace(
            run_id=1,
            seed=42,
            test_class=_class_path(_NeedsNoFaults),
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="tick", params={}),
                TraceStep(kind="rule", name="tick", params={}),
            ],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=1),
        )
        result = ablate_faults(trace, _NeedsNoFaults)
        assert result == {}

    def test_ablation_in_failure_str(self):
        """Failure.__str__ includes necessary faults when ablation is set."""
        from ordeal.explore import Failure

        f = Failure(
            error=ValueError("boom"),
            step=1,
            run_id=1,
            active_faults=["fault_a", "fault_b"],
            rule_log=["tick", "tick"],
            necessary_faults={"fault_a": True, "fault_b": False},
        )
        s = str(f)
        assert "Necessary faults: fault_a" in s
        assert "fault_b" not in s.split("Necessary faults:")[1]
