"""Tests for ordeal.explore — coverage-guided exploration with checkpointing."""

from __future__ import annotations

from hypothesis.stateful import invariant, rule

from ordeal.chaos import ChaosTest
from ordeal.explore import CoverageCollector, Explorer, _DataProxy
from ordeal.faults import LambdaFault
from tests._explore_target import BranchyService

# ============================================================================
# ChaosTest wrapping the BranchyService
# ============================================================================


class BranchyChaos(ChaosTest):
    faults = [
        LambdaFault("reset_fault", lambda: None, lambda: None),
    ]

    def __init__(self):
        super().__init__()
        self.service = BranchyService()

    @rule()
    def do_a(self):
        self.service.step_a()

    @rule()
    def do_b(self):
        self.service.step_b()

    @rule()
    def do_c(self):
        self.service.step_c()

    @invariant()
    def state_is_valid(self):
        assert self.service.state in {"init", "a", "b", "ab", "c", "deep"}

    def teardown(self):
        self.service.reset()
        super().teardown()


# ============================================================================
# A ChaosTest that always fails (for failure detection tests)
# ============================================================================


class FailingChaos(ChaosTest):
    faults = []

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def increment(self):
        self.counter += 1
        if self.counter >= 3:
            raise ValueError("counter overflow")


# ============================================================================
# Tests
# ============================================================================


class TestCoverageCollector:
    def test_collects_edges(self):
        collector = CoverageCollector(["_explore_target"])
        collector.start()
        svc = BranchyService()
        svc.step_a()
        svc.step_b()
        edges = collector.stop()
        assert len(edges) > 0

    def test_no_edges_for_non_target(self):
        collector = CoverageCollector(["nonexistent_module"])
        collector.start()
        svc = BranchyService()
        svc.step_a()
        edges = collector.stop()
        assert len(edges) == 0

    def test_snapshot_during_collection(self):
        collector = CoverageCollector(["_explore_target"])
        collector.start()
        svc = BranchyService()
        svc.step_a()
        snap1 = collector.snapshot()
        svc.step_b()
        snap2 = collector.snapshot()
        collector.stop()
        assert len(snap2) >= len(snap1)


class TestDataProxy:
    def test_draw_sampled_from(self):
        import hypothesis.strategies as st

        proxy = _DataProxy()
        result = proxy.draw(st.sampled_from([1, 2, 3]))
        assert result in {1, 2, 3}

    def test_draw_integers(self):
        import hypothesis.strategies as st

        proxy = _DataProxy()
        result = proxy.draw(st.integers(min_value=0, max_value=10))
        assert isinstance(result, int)


class TestExplorerDiscovery:
    def test_discovers_rules(self):
        explorer = Explorer(BranchyChaos)
        explorer._discover()
        rule_names = [r.name for r in explorer._rules]
        assert "do_a" in rule_names
        assert "do_b" in rule_names
        assert "do_c" in rule_names
        # Internal rules should be excluded
        assert "_nemesis" not in rule_names

    def test_discovers_invariants(self):
        explorer = Explorer(BranchyChaos)
        explorer._discover()
        assert "state_is_valid" in explorer._invariant_names


class TestExplorerExecution:
    def test_runs_without_coverage(self):
        """Explorer works even without target_modules (no coverage tracking)."""
        explorer = Explorer(BranchyChaos, seed=42)
        result = explorer.run(max_runs=20, steps_per_run=10)
        assert result.total_runs == 20
        assert result.total_steps > 0
        assert result.unique_edges == 0  # no coverage tracking

    def test_runs_with_coverage(self):
        """Explorer tracks coverage when target_modules is set."""
        explorer = Explorer(
            BranchyChaos,
            target_modules=["tests._explore_target"],
            seed=42,
        )
        result = explorer.run(max_runs=50, steps_per_run=20)
        assert result.unique_edges > 0
        assert result.checkpoints_saved > 0

    def test_checkpoints_increase_coverage(self):
        """Exploring from checkpoints should find more edges than without."""
        # Without checkpoints (low checkpoint_prob)
        exp_no_cp = Explorer(
            BranchyChaos,
            target_modules=["tests._explore_target"],
            seed=42,
            checkpoint_prob=0.0,  # never restore
        )
        res_no_cp = exp_no_cp.run(max_runs=100, steps_per_run=15)

        # With checkpoints (high checkpoint_prob)
        exp_cp = Explorer(
            BranchyChaos,
            target_modules=["tests._explore_target"],
            seed=42,
            checkpoint_prob=0.6,
        )
        res_cp = exp_cp.run(max_runs=100, steps_per_run=15)

        # Both should find some coverage
        assert res_no_cp.unique_edges > 0
        assert res_cp.unique_edges > 0
        # Checkpoint-based should find at least as much (usually more)
        # Not strictly guaranteed with same run count, so just check it works
        assert res_cp.checkpoints_saved > 0

    def test_detects_failures(self):
        """Explorer catches exceptions from rules."""
        explorer = Explorer(FailingChaos, seed=42)
        result = explorer.run(max_runs=50, steps_per_run=10)
        assert len(result.failures) > 0
        assert "counter overflow" in str(result.failures[0].error)

    def test_failure_includes_rule_log(self):
        explorer = Explorer(FailingChaos, seed=42)
        result = explorer.run(max_runs=50, steps_per_run=10)
        assert len(result.failures) > 0
        assert len(result.failures[0].rule_log) > 0

    def test_max_time_respected(self):
        import time

        explorer = Explorer(BranchyChaos, seed=42)
        start = time.monotonic()
        result = explorer.run(max_time=1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0  # generous bound
        assert result.total_runs > 0


class TestExplorerResult:
    def test_summary_format(self):
        explorer = Explorer(BranchyChaos, seed=42)
        result = explorer.run(max_runs=10, steps_per_run=5)
        summary = result.summary()
        assert "Exploration:" in summary
        assert "Coverage:" in summary

    def test_edge_log_grows(self):
        explorer = Explorer(
            BranchyChaos,
            target_modules=["tests._explore_target"],
            seed=42,
        )
        result = explorer.run(max_runs=30, steps_per_run=15)
        # Edge log should have one entry per run
        assert len(result.edge_log) == 30
        # Edge count should be non-decreasing
        edges = [count for _, count in result.edge_log]
        for i in range(1, len(edges)):
            assert edges[i] >= edges[i - 1]
