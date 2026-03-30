"""Tests for ordeal.cli — CLI entry point."""
from __future__ import annotations

from ordeal.cli import main


class TestCLI:
    def test_no_command_returns_0(self):
        assert main([]) == 0

    def test_explore_missing_config(self):
        assert main(["explore", "--config", "/nonexistent.toml"]) == 1

    def test_replay_missing_file(self):
        assert main(["replay", "/nonexistent/trace.json"]) == 1

    def test_explore_with_real_config(self, tmp_path):
        """End-to-end: write a config, run explore, check exit code."""
        config = tmp_path / "ordeal.toml"
        config.write_text("""
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
""".format(output=str(tmp_path / "report.json")))

        code = main(["explore", "--config", str(config), "--no-shrink"])
        # May or may not find failures — just verify it runs
        assert code in (0, 1)
        # JSON report should exist
        assert (tmp_path / "report.json").exists()
