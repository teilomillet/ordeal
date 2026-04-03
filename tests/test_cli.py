"""Tests for ordeal.cli — CLI entry point."""

from __future__ import annotations

from types import SimpleNamespace

import hypothesis.strategies as st
from hypothesis import settings as hsettings

import ordeal.cli as cli
import ordeal.scaling as scaling
from ordeal import ChaosTest, always, invariant, rule
from ordeal.assertions import tracker
from ordeal.cli import main
from ordeal.explore import ProgressSnapshot
from ordeal.quickcheck import quickcheck


class TestCLI:
    def test_no_command_returns_0(self):
        assert main([]) == 0

    def test_explore_missing_config(self):
        assert main(["explore", "--config", "/nonexistent.toml"]) == 1

    def test_replay_missing_file(self):
        assert main(["replay", "/nonexistent/trace.json"]) == 1

    # -- ordeal mine --

    def test_mine_single_function(self, capsys):
        assert main(["mine", "ordeal.invariants.bounded", "-n", "30"]) == 0
        out = capsys.readouterr().out
        assert "mine(bounded)" in out

    def test_mine_module(self, capsys):
        assert main(["mine", "ordeal.invariants", "-n", "30"]) == 0
        out = capsys.readouterr().out
        assert "mine(" in out

    def test_mine_bad_import(self):
        assert main(["mine", "nonexistent.module.func"]) == 1

    def test_mine_bad_dotted_path(self):
        assert main(["mine", "nodot"]) == 1

    def test_mine_default_examples(self, capsys):
        """Default -n is 500 — just verify the flag is wired correctly."""
        assert main(["mine", "ordeal.invariants.bounded", "-n", "10"]) == 0
        out = capsys.readouterr().out
        assert "mine(bounded)" in out

    # -- ordeal mine-pair --

    def test_mine_pair_roundtrip(self, capsys):
        target = "tests._mutation_target.add"
        assert main(["mine-pair", target, target, "-n", "30"]) == 0
        out = capsys.readouterr().out
        assert "add" in out

    def test_mine_pair_bad_target(self):
        assert main(["mine-pair", "nodot", "json.loads"]) == 1

    def test_explore_with_real_config(self, tmp_path):
        """End-to-end: write a config, run explore, check exit code."""
        config = tmp_path / "ordeal.toml"
        # Use forward slashes in TOML — backslashes are escape sequences
        report_path = str(tmp_path / "report.json").replace("\\", "/")
        config.write_text(
            """
[explorer]
target_modules = ["tests._explore_target"]
max_time = 2
seed = 42
steps_per_run = 10

[[tests]]
class = "tests.test_explore:BranchyChaos"

[report]
format = "json"
output = "{output}"
verbose = false
""".format(output=report_path)
        )

        code = main(["explore", "--config", str(config), "--no-shrink"])
        # May or may not find failures — just verify it runs
        assert code in (0, 1)
        # JSON report should exist
        assert (tmp_path / "report.json").exists()

    def test_benchmark_reports_anytime_signal(self, monkeypatch, capsys):
        class _FakeTestCfg:
            class_path = "tests.fake:Chaos"

            def resolve(self):
                return object

        cfg = SimpleNamespace(
            tests=[_FakeTestCfg()],
            explorer=SimpleNamespace(
                target_modules=["tests._explore_target"],
                seed=42,
                max_checkpoints=32,
                checkpoint_prob=0.4,
                checkpoint_strategy="energy",
                fault_toggle_prob=0.3,
                ngram=1,
                steps_per_run=10,
            ),
        )

        class _FakeExplorer:
            def __init__(self, *args, **kwargs):
                self.workers = kwargs["workers"]

            def run(self, max_time, steps_per_run, progress=None):
                if progress is not None:
                    progress(
                        ProgressSnapshot(
                            elapsed=6.0,
                            total_runs=12,
                            total_steps=120,
                            unique_edges=8,
                            checkpoints=3,
                            failures=0,
                            runs_per_second=2.0,
                        )
                    )
                    progress(
                        ProgressSnapshot(
                            elapsed=11.0,
                            total_runs=20,
                            total_steps=220,
                            unique_edges=11,
                            checkpoints=4,
                            failures=1,
                            runs_per_second=1.8,
                        )
                    )
                return SimpleNamespace(
                    total_runs=24 * self.workers,
                    total_steps=240 * self.workers,
                    unique_edges=12 * self.workers,
                    checkpoints_saved=5 * self.workers,
                    failures=[object()] if self.workers == 1 else [],
                    duration_seconds=max_time,
                )

        monkeypatch.setattr(cli, "load_config", lambda path: cfg)
        monkeypatch.setattr(cli, "Explorer", _FakeExplorer)

        rc = main(["benchmark", "--config", "ignored.toml", "--max-workers", "2", "--time", "1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Anytime Signal (N=1 Baseline)" in out
        assert "5s: runs=12, steps=120, edges=8, checkpoints=3, failures=0" in out
        assert "10s: runs=20, steps=220, edges=11, checkpoints=4, failures=1" in out

    def test_benchmark_mutation_mode(self, monkeypatch, capsys):
        calls: dict[str, object] = {}

        class _FakeSuite:
            def summary(self) -> str:
                return "Mutation Benchmark\npkg.mod.compute"

        def fake_benchmark(*args, **kwargs):
            calls.update(kwargs)
            return _FakeSuite()

        monkeypatch.setattr(scaling, "benchmark", fake_benchmark)

        rc = main(
            [
                "benchmark",
                "--mutate",
                "pkg.mod.compute",
                "--repeat",
                "3",
                "--workers",
                "2",
                "--preset",
                "essential",
                "--no-filter-equivalent",
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "Mutation Benchmark" in out
        assert calls["mutate_targets"] == ["pkg.mod.compute"]
        assert calls["repeats"] == 3
        assert calls["workers"] == 2
        assert calls["preset"] == "essential"
        assert calls["filter_equivalent"] is False


# ============================================================================
# ordeal-powered tests for `ordeal mine` CLI
# ============================================================================

# Valid dotted paths that should succeed
_VALID_TARGETS = st.sampled_from(
    [
        "ordeal.invariants.bounded",
        "ordeal.invariants.unique",
        "ordeal.invariants",
    ]
)

# Invalid paths that should fail with exit code 1
_INVALID_TARGETS = st.sampled_from(
    [
        "nodot",
        "nonexistent.module.func",
        "also.nonexistent",
    ]
)


@quickcheck
def test_qc_mine_valid_always_succeeds(target: str):
    """Valid targets must always return exit code 0."""
    # Constrain to known-good targets only
    if target not in ("ordeal.invariants.bounded", "ordeal.invariants.unique"):
        return
    code = main(["mine", target, "-n", "10"])
    assert code == 0


@quickcheck
def test_qc_mine_invalid_always_fails(target: str):
    """Bare words (no dot) must always return exit code 1."""
    if "." in target or not target or target.startswith("-"):
        return  # skip strings that look like flags or contain dots
    code = main(["mine", target])
    assert code == 1


def test_always_mine_exit_contract():
    """Use always() to verify the exit code contract over several targets."""
    tracker.active = True
    tracker.reset()
    try:
        for target in ["ordeal.invariants.bounded", "ordeal.invariants.unique"]:
            code = main(["mine", target, "-n", "10"])
            always(code == 0, "valid target returns 0")

        for target in ["nodot", "nonexistent.mod.fn"]:
            code = main(["mine", target])
            always(code == 1, "invalid target returns 1")

        ok = next(r for r in tracker.results if r.name == "valid target returns 0")
        assert ok.passes == 2 and ok.failures == 0
        bad = next(r for r in tracker.results if r.name == "invalid target returns 1")
        assert bad.passes == 2 and bad.failures == 0
    finally:
        tracker.active = False


class MineCLIBattle(ChaosTest):
    """Stateful test: interleave valid and invalid mine calls."""

    faults = []

    def __init__(self):
        super().__init__()
        self.valid_runs = 0
        self.invalid_runs = 0

    @rule()
    def mine_valid(self):
        code = main(["mine", "ordeal.invariants.bounded", "-n", "10"])
        assert code == 0
        self.valid_runs += 1

    @rule()
    def mine_invalid_nodot(self):
        code = main(["mine", "nodot"])
        assert code == 1
        self.invalid_runs += 1

    @rule()
    def mine_invalid_import(self):
        code = main(["mine", "fake.module.func"])
        assert code == 1
        self.invalid_runs += 1

    @invariant()
    def runs_tracked(self):
        assert self.valid_runs >= 0
        assert self.invalid_runs >= 0

    def teardown(self):
        super().teardown()


TestMineCLIBattle = MineCLIBattle.TestCase
TestMineCLIBattle.settings = hsettings(max_examples=10, stateful_step_count=6)
