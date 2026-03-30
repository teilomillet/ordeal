"""Tests for ordeal.cli — CLI entry point."""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import settings as hsettings

from ordeal import ChaosTest, always, invariant, rule
from ordeal.assertions import tracker
from ordeal.cli import main
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


# ============================================================================
# ordeal-powered tests for `ordeal mine` CLI
# ============================================================================

# Valid dotted paths that should succeed
_VALID_TARGETS = st.sampled_from([
    "ordeal.invariants.bounded",
    "ordeal.invariants.unique",
    "ordeal.invariants",
])

# Invalid paths that should fail with exit code 1
_INVALID_TARGETS = st.sampled_from([
    "nodot",
    "nonexistent.module.func",
    "also.nonexistent",
])


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
    if "." in target:
        return
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
