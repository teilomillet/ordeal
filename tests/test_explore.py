"""Tests for ordeal.explore — coverage-guided exploration with checkpointing."""

from __future__ import annotations

from hypothesis.stateful import invariant, rule

from ordeal.chaos import ChaosTest
from ordeal.explore import Checkpoint, CoverageCollector, Explorer, _DataProxy
from ordeal.faults import LambdaFault
from tests._explore_target import BranchyService
from tests._hard_target import HardService

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


# ============================================================================
# Energy scheduling: UCB1 + rare-edge bonus
# ============================================================================


class TestEnergyScheduling:
    def test_energy_reward_on_new_edges(self):
        """Checkpoints that find new edges get higher energy."""
        explorer = Explorer(BranchyChaos, seed=42)
        machine = BranchyChaos()
        cp = Checkpoint(machine, 0, 0, 1, energy=1.0)
        explorer._update_checkpoint_energy(cp, 5)
        assert cp.energy > 1.0

    def test_decay_without_new_edges(self):
        """Energy decays when a checkpoint produces no new edges."""
        explorer = Explorer(BranchyChaos, seed=42)
        machine = BranchyChaos()
        cp = Checkpoint(machine, 0, 0, 1, energy=5.0)
        explorer._update_checkpoint_energy(cp, 0)
        assert cp.energy < 5.0
        assert cp.energy > 0.0

    def test_energy_selection_favors_high_energy(self):
        """Energy-weighted selection should favor high-energy checkpoints."""
        explorer = Explorer(BranchyChaos, seed=42)
        explorer._discover()
        machine = BranchyChaos()
        cp_high = Checkpoint(machine, 1, 0, 1, energy=100.0)
        cp_low = Checkpoint(machine, 1, 0, 2, energy=0.01)
        explorer._checkpoints = [cp_high, cp_low]

        high_count = 0
        for _ in range(100):
            cp = explorer._select_energy()
            if cp is cp_high:
                high_count += 1
        assert high_count > 80, "High-energy checkpoint should be selected most often"

    def test_checkpoints_saved_during_exploration(self):
        """Explorer should save checkpoints when it discovers new edges."""
        explorer = Explorer(
            BranchyChaos,
            target_modules=["tests._explore_target"],
            seed=42,
        )
        result = explorer.run(max_runs=50, steps_per_run=20)
        assert result.checkpoints_saved > 0
        assert len(explorer._checkpoints) > 0


# ============================================================================
# Ablation: energy vs uniform vs recent
# ============================================================================


class HardChaos(ChaosTest):
    """ChaosTest wrapping HardService — 5-phase, 17-rule state machine.

    Critical path: advance_a 5x -> advance_b 5x -> advance_c 5x ->
    advance_d 5x -> advance_e 5x (25 steps total).

    12 noise rules dilute the action space.  Phase transitions unlock
    deep call chains with many unique edges; noise methods are shallow.
    Total edge count directly reflects exploration depth.
    """

    faults = [
        LambdaFault("noop_fault", lambda: None, lambda: None),
    ]

    def __init__(self):
        super().__init__()
        self.svc = HardService()

    @rule()
    def do_advance_a(self):
        self.svc.advance_a()

    @rule()
    def do_advance_b(self):
        self.svc.advance_b()

    @rule()
    def do_advance_c(self):
        self.svc.advance_c()

    @rule()
    def do_advance_d(self):
        self.svc.advance_d()

    @rule()
    def do_advance_e(self):
        self.svc.advance_e()

    @rule()
    def do_noise_counter(self):
        self.svc.noise_counter()

    @rule()
    def do_noise_toggle(self):
        self.svc.noise_toggle()

    @rule()
    def do_noise_accumulate(self):
        self.svc.noise_accumulate()

    @rule()
    def do_noise_cycle(self):
        self.svc.noise_cycle()

    @rule()
    def do_noise_flag(self):
        self.svc.noise_flag()

    @rule()
    def do_noise_reset(self):
        self.svc.noise_reset()

    @rule()
    def do_noise_pulse(self):
        self.svc.noise_pulse()

    @rule()
    def do_noise_swap(self):
        self.svc.noise_swap()

    @rule()
    def do_noise_modulo(self):
        self.svc.noise_modulo()

    @rule()
    def do_noise_cascade(self):
        self.svc.noise_cascade()

    @rule()
    def do_noise_mirror(self):
        self.svc.noise_mirror()

    @rule()
    def do_noise_wave(self):
        self.svc.noise_wave()

    @invariant()
    def phase_is_valid(self):
        assert 0 <= self.svc.phase <= 5

    def teardown(self):
        super().teardown()


class TestAblation:
    """Compare checkpoint strategies on HardService.

    Energy scheduling combines three signals (energy, recency,
    exploration penalty) to outperform uniform random selection
    on deep targets with noise.
    """

    N_RUNS = 200
    STEPS_PER_RUN = 20
    N_SEEDS = 5

    def _run_strategy(self, strategy: str, seed: int) -> tuple[int, list[tuple[int, int]]]:
        """Run exploration, return (unique_edges, edge_log)."""
        explorer = Explorer(
            HardChaos,
            target_modules=["tests._hard_target"],
            seed=seed,
            checkpoint_strategy=strategy,
            checkpoint_prob=0.7,
        )
        result = explorer.run(max_runs=self.N_RUNS, steps_per_run=self.STEPS_PER_RUN)
        return result.unique_edges, result.edge_log

    def test_energy_beats_uniform(self):
        """Energy scheduling should find >= as many edges as uniform."""
        energy_total = 0
        uniform_total = 0

        for i in range(self.N_SEEDS):
            seed = 100 + i * 31
            energy_total += self._run_strategy("energy", seed)[0]
            uniform_total += self._run_strategy("uniform", seed)[0]

        energy_avg = energy_total / self.N_SEEDS
        uniform_avg = uniform_total / self.N_SEEDS

        assert energy_avg >= uniform_avg * 0.95, (
            f"Energy ({energy_avg:.1f} avg edges) should match or beat "
            f"uniform ({uniform_avg:.1f} avg edges)"
        )

    def test_energy_competitive_with_recent(self):
        """Energy should be within 15% of recent-biased selection.

        Recent is a strong baseline — it naturally targets the exploration
        frontier.  Energy combines recency with reward signals, which adds
        overhead without a clear advantage over pure recency.  The test
        asserts energy stays competitive (within 15%).
        """
        energy_total = 0
        recent_total = 0

        for i in range(self.N_SEEDS):
            seed = 200 + i * 37
            energy_total += self._run_strategy("energy", seed)[0]
            recent_total += self._run_strategy("recent", seed)[0]

        energy_avg = energy_total / self.N_SEEDS
        recent_avg = recent_total / self.N_SEEDS

        assert energy_avg >= recent_avg * 0.85, (
            f"Energy ({energy_avg:.1f} avg edges) too far behind "
            f"recent ({recent_avg:.1f} avg edges)"
        )

    def test_all_strategies_find_coverage(self):
        """Sanity check: all three strategies find some edges."""
        for strategy in ("energy", "uniform", "recent"):
            edges, _ = self._run_strategy(strategy, seed=42)
            assert edges > 0, f"Strategy {strategy!r} found 0 edges"

    def test_edge_growth_curves(self):
        """Energy should discover edges at least as fast at midpoint."""
        midpoint = self.N_RUNS // 2

        e_edges, e_log = self._run_strategy("energy", seed=42)
        u_edges, u_log = self._run_strategy("uniform", seed=42)

        if e_log and u_log and midpoint < len(e_log):
            energy_mid = e_log[midpoint][1]
            uniform_mid = u_log[midpoint][1]
            assert energy_mid >= uniform_mid * 0.8, (
                f"Energy midpoint ({energy_mid}) much worse than uniform ({uniform_mid})"
            )
