"""Tests for exploration telemetry: behavior coverage, swarm stats, boundaries."""

from __future__ import annotations

import signal
import subprocess

from hypothesis.stateful import rule

from ordeal import sometimes
from ordeal.chaos import ChaosTest
from ordeal.explore import Explorer
from ordeal.faults import LambdaFault


class _BehaviorTelemetryChaos(ChaosTest):
    faults = []

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        sometimes(True, "tick observed")


class _BoundaryTelemetryChaos(ChaosTest):
    faults = []

    @rule()
    def crash_child(self):
        raise subprocess.CalledProcessError(
            -signal.SIGTERM,
            ["worker", "--once"],
            output="partial output",
            stderr="segfault",
        )


class _SwarmTelemetryChaos(ChaosTest):
    faults = [
        LambdaFault("f1", lambda: None, lambda: None),
        LambdaFault("f2", lambda: None, lambda: None),
        LambdaFault("f3", lambda: None, lambda: None),
    ]

    @rule()
    def tick(self):
        pass


class TestExploreTelemetry:
    def test_behavior_coverage_records_rule_fault_property_tuples(self):
        explorer = Explorer(
            _BehaviorTelemetryChaos,
            seed=42,
            record_traces=True,
        )
        result = explorer.run(max_runs=4, steps_per_run=2, shrink=False)

        assert result.rule_fault_coverage["tick"]["none"] >= 1
        assert result.behavior_coverage["tick"]["none"] == ["tick observed"]
        assert result.property_stress["tick observed"]["none"] >= 1
        assert any(
            "tick observed" in step.properties_observed
            for trace in result.traces
            for step in trace.steps
            if step.kind == "rule"
        )

    def test_native_boundary_findings_are_structured(self):
        explorer = Explorer(
            _BoundaryTelemetryChaos,
            seed=42,
            record_traces=True,
        )
        result = explorer.run(max_runs=1, steps_per_run=1, shrink=False)

        assert result.failures
        failure = result.failures[0]
        assert failure.native_boundary is not None
        assert failure.native_boundary["mode"] == "signal"
        assert failure.native_boundary["signal_name"] == "SIGTERM"
        assert result.native_boundary_findings[0]["mode"] == "signal"
        assert failure.trace is not None
        assert failure.trace.failure is not None
        assert failure.trace.failure.native_boundary is not None
        assert "Native boundary:" in result.summary()

    def test_rule_swarm_reports_pairwise_coverage(self):
        explorer = Explorer(
            _SwarmTelemetryChaos,
            rule_swarm=True,
            seed=42,
        )
        result = explorer.run(max_runs=40, steps_per_run=3, shrink=False)

        assert result.rule_swarm_runs > 0
        assert result.swarm_stats
        assert len(result.fault_pair_coverage) == 3
        assert all("times_used" in row for row in result.swarm_stats)
        assert result.uncovered_fault_pairs == []
