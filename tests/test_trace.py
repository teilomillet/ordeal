"""Tests for ordeal.trace — serialization, replay, and shrinking."""

from __future__ import annotations

from hypothesis.stateful import rule

from ordeal.chaos import ChaosTest
from ordeal.trace import Trace, TraceFailure, TraceStep, replay, shrink

# ============================================================================
# Test fixture: a ChaosTest that fails at step N
# ============================================================================


class _BombAt3(ChaosTest):
    """Fails when counter reaches 3."""

    faults = []

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 3:
            raise ValueError("boom")


class _BombWithFault(ChaosTest):
    """Fails only when a specific fault is active."""

    faults = []  # no real faults — we test replay of fault_toggle steps

    def __init__(self):
        super().__init__()
        self.counter = 0
        self.fault_active = False

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 2 and self.fault_active:
            raise ValueError("fault-triggered boom")


# ============================================================================
# Serialization
# ============================================================================


class TestTraceSerialization:
    def test_round_trip(self, tmp_path):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="tick", params={}, active_faults=[], edge_count=5),
                TraceStep(kind="rule", name="tick", params={}, active_faults=[], edge_count=10),
                TraceStep(kind="rule", name="tick", params={}, active_faults=[], edge_count=12),
            ],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=2),
            edges_discovered=12,
            duration=0.05,
        )
        path = tmp_path / "trace.json"
        trace.save(path)
        loaded = Trace.load(path)
        assert loaded.run_id == 1
        assert loaded.seed == 42
        assert len(loaded.steps) == 3
        assert loaded.failure.error_type == "ValueError"

    def test_to_dict(self):
        trace = Trace(run_id=1, seed=0, test_class="x:Y", from_checkpoint=None)
        d = trace.to_dict()
        assert d["run_id"] == 1

    def test_bytes_serialization(self, tmp_path):
        """Params with bytes should survive JSON round-trip."""
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="x:Y",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="f", params={"data": b"\x00\xff"})],
        )
        path = tmp_path / "trace.json"
        trace.save(path)
        loaded = Trace.load(path)
        assert loaded.steps[0].params["data"] == b"\x00\xff"


# ============================================================================
# Replay
# ============================================================================


class TestReplay:
    def test_reproduces_failure(self):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="rule", name="tick"),
            ],
        )
        error = replay(trace)
        assert error is not None
        assert "boom" in str(error)

    def test_no_failure_with_fewer_steps(self):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="rule", name="tick"),
            ],
        )
        error = replay(trace)
        assert error is None

    def test_replay_with_explicit_class(self):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="bogus:Bogus",  # wrong class — override
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="rule", name="tick"),
            ],
        )
        error = replay(trace, test_class=_BombAt3)
        assert error is not None


# ============================================================================
# Shrinking
# ============================================================================


class TestShrink:
    def test_shrinks_to_minimum(self):
        """A 10-step trace where only 3 steps are needed should shrink to 3."""
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="tick") for _ in range(10)],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=9),
        )
        shrunk = shrink(trace, _BombAt3, max_time=10.0)
        assert len(shrunk.steps) == 3  # minimum to trigger boom

    def test_shrink_preserves_failure(self):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="tick") for _ in range(20)],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=19),
        )
        shrunk = shrink(trace, _BombAt3, max_time=10.0)
        # Replaying the shrunk trace should still fail
        error = replay(shrunk, _BombAt3)
        assert error is not None

    def test_shrink_removes_irrelevant_fault_toggles(self):
        """Fault toggles not needed for the failure should be removed."""
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="fault_toggle", name="+noop_fault"),
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="fault_toggle", name="-noop_fault"),
                TraceStep(kind="rule", name="tick"),
                TraceStep(kind="rule", name="tick"),
            ],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=4),
        )
        shrunk = shrink(trace, _BombAt3, max_time=10.0)
        # Fault toggles should be removed since they're irrelevant
        fault_steps = [s for s in shrunk.steps if s.kind == "fault_toggle"]
        assert len(fault_steps) == 0
        assert len(shrunk.steps) == 3  # just the 3 ticks
