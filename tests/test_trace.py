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


class _ComplexBomb(ChaosTest):
    """Fails after a specific sequence: 5x tick_a, then 2x tick_b, then tick_c.

    Used to test shrinking on complex traces with many irrelevant steps
    and fault toggles. Only 8 specific steps (out of potentially 100+)
    are needed to trigger the failure.
    """

    faults = []

    def __init__(self):
        super().__init__()
        self.a_count = 0
        self.b_count = 0
        self.ready = False

    @rule()
    def tick_a(self):
        self.a_count += 1

    @rule()
    def tick_b(self):
        if self.a_count >= 5:
            self.b_count += 1

    @rule()
    def tick_c(self):
        if self.b_count >= 2:
            raise ValueError("complex boom")

    @rule()
    def noop(self):
        """Noise step — does nothing useful."""
        pass


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


# ============================================================================
# Shrink stress tests — large traces with noise
# ============================================================================


class TestShrinkStress:
    """Verify shrinking quality on realistic multi-step, multi-fault traces.

    These tests use _ComplexBomb which requires exactly 8 essential steps
    (5x tick_a + 2x tick_b + 1x tick_c) but is buried in 60-80+ step
    traces filled with noise (noop steps, irrelevant fault toggles).
    """

    def test_60_steps_shrinks_to_8(self):
        """60-step trace with noise shrinks to the 8 essential steps.

        Before shrinking (60 steps):
            noop, +fault_a, noop, tick_a, noop, -fault_a, tick_a, noop,
            +fault_b, tick_a, noop, tick_a, noop, -fault_b, tick_a,
            noop, tick_b, noop, tick_b, +fault_c, -fault_c, tick_c, ...

        After shrinking (8 steps):
            tick_a, tick_a, tick_a, tick_a, tick_a, tick_b, tick_b, tick_c
        """
        import random

        rng = random.Random(99)
        steps: list[TraceStep] = []

        # Build the essential sequence buried in noise
        essential = (
            [TraceStep(kind="rule", name="tick_a") for _ in range(5)]
            + [TraceStep(kind="rule", name="tick_b") for _ in range(2)]
            + [TraceStep(kind="rule", name="tick_c")]
        )
        noise_rules = [
            TraceStep(kind="rule", name="noop"),
        ]
        noise_faults = [
            TraceStep(kind="fault_toggle", name="+noise_fault_a"),
            TraceStep(kind="fault_toggle", name="-noise_fault_a"),
            TraceStep(kind="fault_toggle", name="+noise_fault_b"),
            TraceStep(kind="fault_toggle", name="-noise_fault_b"),
        ]

        # Interleave essential steps with ~50 noise steps
        for es in essential:
            # Add 3-7 noise steps before each essential step
            for _ in range(rng.randint(3, 7)):
                steps.append(rng.choice(noise_rules + noise_faults))
            steps.append(es)

        assert len(steps) >= 50, f"Trace too short: {len(steps)}"

        trace = Trace(
            run_id=1,
            seed=99,
            test_class="tests.test_trace:_ComplexBomb",
            from_checkpoint=None,
            steps=steps,
            failure=TraceFailure(
                error_type="ValueError",
                error_message="complex boom",
                step=len(steps) - 1,
            ),
        )

        # Verify the trace reproduces before shrinking
        error = replay(trace, _ComplexBomb)
        assert error is not None, "Trace must reproduce before shrinking"

        shrunk = shrink(trace, _ComplexBomb, max_time=15.0)

        # Verify shrunk trace still reproduces
        error = replay(shrunk, _ComplexBomb)
        assert error is not None, "Shrunk trace must still reproduce"

        # Core assertion: shrinks to exactly the 8 essential steps
        assert len(shrunk.steps) == 8, (
            f"Expected 8 steps, got {len(shrunk.steps)}: {[s.name for s in shrunk.steps]}"
        )

        # Verify all noise is gone
        assert all(s.kind == "rule" for s in shrunk.steps), "No fault toggles should remain"
        names = [s.name for s in shrunk.steps]
        assert names.count("tick_a") == 5
        assert names.count("tick_b") == 2
        assert names.count("tick_c") == 1
        assert "noop" not in names

    def test_shrink_ratio_exceeds_80_percent(self):
        """Shrink ratio on a 80-step trace should exceed 80%."""
        import random

        rng = random.Random(77)
        steps: list[TraceStep] = []

        # 3 essential tick steps buried in 80+ noise
        essential = [TraceStep(kind="rule", name="tick") for _ in range(3)]
        for es in essential:
            for _ in range(rng.randint(20, 30)):
                steps.append(TraceStep(kind="rule", name="tick"))
            steps.append(es)

        original_len = len(steps)
        assert original_len >= 60

        trace = Trace(
            run_id=1,
            seed=77,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=steps,
            failure=TraceFailure(
                error_type="ValueError", error_message="boom", step=original_len - 1
            ),
        )
        shrunk = shrink(trace, _BombAt3, max_time=10.0)

        ratio = 1.0 - len(shrunk.steps) / original_len
        assert ratio >= 0.8, (
            f"Shrink ratio {ratio:.0%} < 80% ({original_len} -> {len(shrunk.steps)})"
        )
        assert len(shrunk.steps) == 3

    def test_shrunk_trace_format(self, tmp_path):
        """Verify what a shrunk trace looks like when serialized."""
        trace = Trace(
            run_id=42,
            seed=12345,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="tick") for _ in range(50)],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=49),
        )
        shrunk = shrink(trace, _BombAt3, max_time=10.0)

        # Save and reload
        path = tmp_path / "shrunk.json"
        shrunk.save(path)
        loaded = Trace.load(path)

        # Verify structure
        assert loaded.run_id == 42
        assert loaded.seed == 12345
        assert len(loaded.steps) == 3
        assert all(s.kind == "rule" and s.name == "tick" for s in loaded.steps)
        assert loaded.failure.error_type == "ValueError"
