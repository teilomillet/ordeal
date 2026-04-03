"""Tests for ordeal.cli — CLI entry point."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import hypothesis.strategies as st
import pytest
from hypothesis import settings as hsettings

import ordeal.cli as cli
import ordeal.scaling as scaling
import ordeal.state as ordeal_state
from ordeal import ChaosTest, always, invariant, rule
from ordeal.assertions import tracker
from ordeal.cli import main
from ordeal.explore import ProgressSnapshot
from ordeal.mine import MinedProperty, MineResult
from ordeal.quickcheck import quickcheck


class TestCLI:
    def test_no_command_returns_0(self):
        assert main([]) == 0

    def test_top_level_help_mentions_report_examples(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "ordeal scan mymod --save-artifacts" in out
        assert "ordeal scan mymod --report-file report.md" in out
        assert "shareable bug report" in out
        assert "ordeal scan mymod --write-regression" in out
        assert "ordeal mine mymod.func --write-regression" in out

    def test_python_module_entrypoint_runs(self):
        proc = subprocess.run(
            [sys.executable, "-m", "ordeal.cli", "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "usage:" in proc.stdout

    def test_audit_help_mentions_validation_mode(self, capsys):
        with pytest.raises(SystemExit):
            main(["audit", "--help"])
        out = capsys.readouterr().out
        assert "--validation-mode {fast,deep}" in out
        assert "fast replay" in out

    def test_benchmark_help_mentions_perf_contract_quality(self, capsys):
        with pytest.raises(SystemExit):
            main(["benchmark", "--help"])
        out = capsys.readouterr().out
        assert "--perf-contract PERF_CONTRACT" in out
        assert "--output-json PATH" in out
        assert "score-gap budget" in out

    def test_audit_forwards_validation_mode(self, monkeypatch, capsys):
        import ordeal.audit as audit_mod

        calls: dict[str, object] = {}

        def fake_audit_report(modules, **kwargs):
            calls["modules"] = modules
            calls.update(kwargs)
            return "ordeal audit"

        monkeypatch.setattr(audit_mod, "audit_report", fake_audit_report)

        rc = main(["audit", "ordeal.demo", "--validation-mode", "deep"])

        assert rc == 0
        assert calls["modules"] == ["ordeal.demo"]
        assert calls["validation_mode"] == "deep"
        assert "ordeal audit" in capsys.readouterr().out

    def test_catalog_mentions_report_file(self, capsys):
        assert main(["catalog"]) == 0
        out = capsys.readouterr().out
        assert "--report-file report.md" in out
        assert "--write-regression" in out
        assert "tests/test_ordeal_regressions.py" in out
        assert "--save-artifacts" in out
        assert ".ordeal/findings/mymod.md" in out
        assert ".ordeal/findings/index.json" in out

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

    def test_mine_help_mentions_shareable_report(self, capsys):
        with pytest.raises(SystemExit):
            main(["mine", "--help"])
        out = capsys.readouterr().out
        assert "shareable Markdown finding report" in out
        assert "runnable pytest regressions" in out
        assert "--report-file PATH" in out
        assert "--write-regression" in out
        assert "default: tests/test_ordeal_regressions.py" in out

    def test_mine_shows_report_hint_when_findings_exist(self, monkeypatch, capsys):
        result = MineResult(
            function="normalize",
            examples=20,
            properties=[
                MinedProperty(
                    "idempotent",
                    18,
                    20,
                    {
                        "input": {"xs": [1, 2, 3]},
                        "output": [0.1, 0.2, 0.3],
                        "replayed": [0.2, 0.3, 0.5],
                    },
                )
            ],
        )

        import ordeal.mine as ordeal_mine

        monkeypatch.setattr(ordeal_mine, "mine", lambda *args, **kwargs: result)

        assert main(["mine", "ordeal.demo.normalize", "-n", "10"]) == 0
        out = capsys.readouterr().out
        assert "--write-regression (tests/test_ordeal_regressions.py)" in out

    def test_scan_help_mentions_shareable_report(self, capsys):
        with pytest.raises(SystemExit):
            main(["scan", "--help"])
        out = capsys.readouterr().out
        assert "shareable Markdown bug report" in out
        assert "runnable pytest regressions" in out
        assert "--save-artifacts" in out
        assert ".ordeal/findings/mymod.md" in out
        assert "--report-file PATH" in out
        assert "--write-regression" in out
        assert "default: tests/test_ordeal_regressions.py" in out

    def test_scan_suppresses_inner_noise_and_formats_summary(self, monkeypatch, capsys):
        class _FakeTree:
            size = 3

        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"a": object(), "b": object()},
            supervisor_info={"trajectory_steps": 5},
            tree=_FakeTree(),
            findings=["normalize: idempotent (92%)"],
            frontier={"score": ["mutation score 67%", "1 unhardened survivor(s)"]},
        )

        def fake_explore(*args, **kwargs):
            print("INNER STDOUT NOISE")
            sys.stderr.write("INNER STDERR NOISE\n")
            return state

        monkeypatch.setattr(ordeal_state, "explore", fake_explore)

        rc = main(["scan", "pkg.mod", "-n", "10"])
        captured = capsys.readouterr()

        assert rc == 1
        assert "INNER STDOUT NOISE" not in captured.out
        assert "INNER STDERR NOISE" not in captured.err
        assert "ordeal scan: pkg.mod" in captured.out
        assert "status: findings found" in captured.out
        assert "gaps to close:" in captured.out
        assert "--save-artifacts" in captured.out

    def test_scan_no_findings_returns_zero(self, monkeypatch, capsys):
        state = SimpleNamespace(
            module="pkg.clean",
            confidence=0.91,
            functions={"a": object()},
            supervisor_info={},
            tree=SimpleNamespace(size=0),
            findings=[],
            frontier={},
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.clean", "-n", "10"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "status: no findings yet" in out
        assert "findings: none" in out

    def test_scan_report_file_writes_markdown(self, monkeypatch, tmp_path, capsys):
        report_path = tmp_path / "scan-report.md"
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 5},
            tree=SimpleNamespace(size=3),
            findings=["normalize: idempotent (87%)"],
            frontier={"normalize": ["property: idempotent (87%)"]},
            finding_details=[
                {
                    "kind": "property",
                    "function": "normalize",
                    "name": "idempotent",
                    "summary": "idempotent (87%)",
                    "confidence": 0.87,
                    "holds": 26,
                    "total": 30,
                    "counterexample": {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                }
            ],
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--report-file", str(report_path), "-n", "10"])
        captured = capsys.readouterr()

        assert rc == 1
        assert report_path.exists()
        report = report_path.read_text()
        assert "# Ordeal Finding Report" in report
        assert "Target: `pkg.mod`" in report
        assert "### 1. `pkg.mod.normalize`" in report
        assert '`ordeal check pkg.mod.normalize -p "idempotent" -n 200`' in report
        assert "Counterexample:" in report
        assert '"... +2 more item(s)"' in report
        assert "Regression test stub:" in report
        assert "replay_args['xs'] = first" in report
        assert "Scan report saved:" in captured.err

    def test_scan_write_regression_writes_pytest_file(self, monkeypatch, tmp_path, capsys):
        regression_path = tmp_path / "test_ordeal_regressions.py"
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 5},
            tree=SimpleNamespace(size=3),
            findings=["normalize: idempotent (87%)"],
            frontier={"normalize": ["property: idempotent (87%)"]},
            finding_details=[
                {
                    "kind": "property",
                    "function": "normalize",
                    "name": "idempotent",
                    "summary": "idempotent (87%)",
                    "confidence": 0.87,
                    "holds": 26,
                    "total": 30,
                    "counterexample": {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                }
            ],
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--write-regression", str(regression_path), "-n", "10"])
        captured = capsys.readouterr()

        assert rc == 1
        assert regression_path.exists()
        regression = regression_path.read_text()
        assert "Generated by `ordeal scan --write-regression`" in regression
        assert "from pkg.mod import normalize" in regression
        assert "def test_normalize_idempotent_regression() -> None:" in regression
        assert "replay_args['xs'] = first" in regression
        assert "... +2 more item(s)" not in regression
        assert "Regression tests written:" in captured.err
        assert f"Run: uv run pytest {regression_path} -q" in captured.err

    def test_scan_save_artifacts_writes_default_report_and_regression(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 5},
            tree=SimpleNamespace(size=3),
            findings=["normalize: idempotent (87%)"],
            frontier={"normalize": ["property: idempotent (87%)"]},
            finding_details=[
                {
                    "kind": "property",
                    "function": "normalize",
                    "name": "idempotent",
                    "summary": "idempotent (87%)",
                    "confidence": 0.87,
                    "holds": 26,
                    "total": 30,
                    "counterexample": {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                }
            ],
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--save-artifacts", "-n", "10"])
        captured = capsys.readouterr()

        report_path = tmp_path / ".ordeal" / "findings" / "pkg" / "mod.md"
        index_path = tmp_path / ".ordeal" / "findings" / "index.json"
        regression_path = tmp_path / "tests" / "test_ordeal_regressions.py"
        assert rc == 1
        assert report_path.exists()
        assert index_path.exists()
        assert regression_path.exists()
        assert "Target: `pkg.mod`" in report_path.read_text()
        assert "def test_normalize_idempotent_regression() -> None:" in regression_path.read_text()
        artifact_index = json.loads(index_path.read_text())
        assert artifact_index["version"] == 1
        assert len(artifact_index["entries"]) == 1
        assert artifact_index["entries"][0]["module"] == "pkg.mod"
        assert artifact_index["entries"][0]["artifacts"]["report"] == ".ordeal/findings/pkg/mod.md"
        assert (
            artifact_index["entries"][0]["artifacts"]["regression"]
            == "tests/test_ordeal_regressions.py"
        )
        assert (
            artifact_index["entries"][0]["commands"]["pytest"]
            == "uv run pytest tests/test_ordeal_regressions.py -q"
        )
        assert (
            artifact_index["entries"][0]["commands"]["rescan"]
            == "uv run ordeal scan pkg.mod --save-artifacts"
        )
        assert "artifacts:" in captured.out
        assert "report: .ordeal/findings/pkg/mod.md" in captured.out
        assert "regression: tests/test_ordeal_regressions.py" in captured.out
        assert "index: .ordeal/findings/index.json" in captured.out
        assert "next:" in captured.out
        assert "run: uv run pytest tests/test_ordeal_regressions.py -q" in captured.out
        assert "after fix: uv run ordeal scan pkg.mod --save-artifacts" in captured.out
        assert "Scan report saved:" in captured.err
        assert "Regression tests written:" in captured.err
        assert "Artifact index updated:" in captured.err

    def test_scan_save_artifacts_skips_writes_without_findings(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = SimpleNamespace(
            module="pkg.clean",
            confidence=0.91,
            functions={"a": object()},
            supervisor_info={},
            tree=SimpleNamespace(size=0),
            findings=[],
            frontier={},
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.clean", "--save-artifacts", "-n", "10"])
        captured = capsys.readouterr()

        assert rc == 0
        assert not (tmp_path / ".ordeal").exists()
        assert not (tmp_path / "tests").exists()
        assert "artifacts:" not in captured.out
        assert "No findings yet; no artifacts written." in captured.err

    def test_scan_save_artifacts_keeps_report_history_without_regression_stub(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.58,
            functions={"score": object()},
            supervisor_info={"seed": 42},
            tree=SimpleNamespace(size=1),
            findings=["score: mutation score 67%, 1 unhardened survivor(s)"],
            frontier={"score": ["mutation score 67%, 1 unhardened survivor(s)"]},
            finding_details=[
                {
                    "kind": "mutation",
                    "function": "score",
                    "summary": "mutation score 67%, 1 unhardened survivor(s)",
                    "mutation_score": 0.67,
                    "survived_mutants": 1,
                }
            ],
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--save-artifacts", "-n", "10"])
        captured = capsys.readouterr()

        report_path = tmp_path / ".ordeal" / "findings" / "pkg" / "mod.md"
        index_path = tmp_path / ".ordeal" / "findings" / "index.json"
        regression_path = tmp_path / "tests" / "test_ordeal_regressions.py"
        assert rc == 1
        assert report_path.exists()
        assert index_path.exists()
        assert not regression_path.exists()
        artifact_index = json.loads(index_path.read_text())
        assert artifact_index["entries"][0]["artifacts"]["regression"] is None
        assert artifact_index["entries"][0]["commands"]["pytest"] is None
        assert "No concrete regression tests could be generated" in captured.err
        assert "Artifact index updated:" in captured.err
        assert "regression: not generated from current findings" in captured.out
        assert "after fix: uv run ordeal scan pkg.mod --save-artifacts" in captured.out

    def test_scan_save_artifacts_appends_index_history(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 5},
            tree=SimpleNamespace(size=3),
            findings=["normalize: idempotent (87%)"],
            frontier={"normalize": ["property: idempotent (87%)"]},
            finding_details=[
                {
                    "kind": "property",
                    "function": "normalize",
                    "name": "idempotent",
                    "summary": "idempotent (87%)",
                    "confidence": 0.87,
                    "holds": 26,
                    "total": 30,
                    "counterexample": {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                }
            ],
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        assert main(["scan", "pkg.mod", "--save-artifacts", "-n", "10"]) == 1
        capsys.readouterr()
        assert main(["scan", "pkg.mod", "--save-artifacts", "-n", "10"]) == 1
        capsys.readouterr()

        artifact_index = json.loads((tmp_path / ".ordeal" / "findings" / "index.json").read_text())
        assert len(artifact_index["entries"]) == 2

    def test_scan_write_regression_defaults_and_dedupes(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 5},
            tree=SimpleNamespace(size=3),
            findings=["normalize: idempotent (87%)"],
            frontier={"normalize": ["property: idempotent (87%)"]},
            finding_details=[
                {
                    "kind": "property",
                    "function": "normalize",
                    "name": "idempotent",
                    "summary": "idempotent (87%)",
                    "confidence": 0.87,
                    "holds": 26,
                    "total": 30,
                    "counterexample": {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                }
            ],
        )

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        assert main(["scan", "pkg.mod", "--write-regression", "-n", "10"]) == 1
        first = capsys.readouterr()
        regression_path = tmp_path / "tests" / "test_ordeal_regressions.py"
        assert regression_path.exists()
        assert "Regression tests written:" in first.err

        assert main(["scan", "pkg.mod", "--write-regression", "-n", "10"]) == 1
        second = capsys.readouterr()
        regression = regression_path.read_text()
        assert regression.count("def test_normalize_idempotent_regression() -> None:") == 1
        assert "Regression tests already present:" in second.err
        assert "Skipped 1 existing regression" in second.err

    def test_mine_report_file_writes_markdown(self, monkeypatch, tmp_path, capsys):
        report_path = tmp_path / "mine-report.md"
        result = MineResult(
            function="normalize",
            examples=20,
            properties=[
                MinedProperty(
                    "idempotent",
                    18,
                    20,
                    {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                )
            ],
            not_checked=["state mutation and side effects"],
        )

        import ordeal.mine as ordeal_mine

        monkeypatch.setattr(ordeal_mine, "mine", lambda *args, **kwargs: result)

        rc = main(["mine", "ordeal.demo.normalize", "--report-file", str(report_path), "-n", "10"])
        captured = capsys.readouterr()

        assert rc == 0
        assert report_path.exists()
        report = report_path.read_text()
        assert "Tool: `ordeal mine`" in report
        assert "Target: `ordeal.demo.normalize`" in report
        assert "### 1. `ordeal.demo.normalize`" in report
        assert "Regression test stub:" in report
        assert "`ordeal mutate ordeal.demo.normalize`" in report
        assert "## What Mine Did Not Check" in report
        assert "Mine report saved:" in captured.err

    def test_mine_write_regression_writes_pytest_file(self, monkeypatch, tmp_path, capsys):
        regression_path = tmp_path / "test_ordeal_regressions.py"
        result = MineResult(
            function="normalize",
            examples=20,
            properties=[
                MinedProperty(
                    "idempotent",
                    18,
                    20,
                    {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                )
            ],
        )

        import ordeal.mine as ordeal_mine

        monkeypatch.setattr(ordeal_mine, "mine", lambda *args, **kwargs: result)

        rc = main(
            [
                "mine",
                "ordeal.demo.normalize",
                "--write-regression",
                str(regression_path),
                "-n",
                "10",
            ]
        )
        captured = capsys.readouterr()

        assert rc == 0
        assert regression_path.exists()
        regression = regression_path.read_text()
        assert "Generated by `ordeal mine --write-regression`" in regression
        assert "from ordeal.demo import normalize" in regression
        assert "def test_normalize_idempotent_regression() -> None:" in regression
        assert "replay_args['xs'] = first" in regression
        assert "... +2 more item(s)" not in regression
        assert "Regression tests written:" in captured.err
        assert f"Run: uv run pytest {regression_path} -q" in captured.err

    def test_mine_write_regression_defaults_and_dedupes(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        result = MineResult(
            function="normalize",
            examples=20,
            properties=[
                MinedProperty(
                    "idempotent",
                    18,
                    20,
                    {
                        "input": {"xs": [9, 8, 7, 6, 5, 4, 3, 2]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                )
            ],
        )

        import ordeal.mine as ordeal_mine

        monkeypatch.setattr(ordeal_mine, "mine", lambda *args, **kwargs: result)

        assert main(["mine", "ordeal.demo.normalize", "--write-regression", "-n", "10"]) == 0
        first = capsys.readouterr()
        regression_path = tmp_path / "tests" / "test_ordeal_regressions.py"
        assert regression_path.exists()
        assert "Regression tests written:" in first.err

        assert main(["mine", "ordeal.demo.normalize", "--write-regression", "-n", "10"]) == 0
        second = capsys.readouterr()
        regression = regression_path.read_text()
        assert regression.count("def test_normalize_idempotent_regression() -> None:") == 1
        assert "Regression tests already present:" in second.err
        assert "Skipped 1 existing regression" in second.err

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

    def test_benchmark_perf_contract_mode(self, monkeypatch, capsys):
        calls: dict[str, object] = {}

        class _FakeSuite:
            passed = True

            def summary(self) -> str:
                return "Performance Contract [PASS]"

            def to_json(self) -> str:
                return '{"passed": true}'

        def fake_benchmark_perf_contract(path, **kwargs):
            calls["path"] = path
            calls.update(kwargs)
            return _FakeSuite()

        monkeypatch.setattr(scaling, "benchmark_perf_contract", fake_benchmark_perf_contract)

        rc = main(["benchmark", "--perf-contract", "ordeal.perf.toml"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Performance Contract [PASS]" in out
        assert calls["path"] == "ordeal.perf.toml"

    def test_benchmark_perf_contract_writes_json(self, monkeypatch, tmp_path, capsys):
        class _FakeSuite:
            passed = True

            def summary(self) -> str:
                return "Performance Contract [PASS]"

            def to_json(self) -> str:
                return '{"passed": true, "case_count": 1}'

        monkeypatch.setattr(
            scaling,
            "benchmark_perf_contract",
            lambda *args, **kwargs: _FakeSuite(),
        )

        out_path = tmp_path / "perf.json"
        rc = main(
            [
                "benchmark",
                "--perf-contract",
                "ordeal.perf.toml",
                "--output-json",
                str(out_path),
            ]
        )

        assert rc == 0
        assert out_path.read_text(encoding="utf-8").strip() == '{"passed": true, "case_count": 1}'
        out = capsys.readouterr().out
        assert "Performance Contract [PASS]" in out

    def test_benchmark_perf_contract_check_fails(self, monkeypatch, capsys):
        class _FakeSuite:
            passed = False

            def summary(self) -> str:
                return "Performance Contract [FAIL]"

            def to_json(self) -> str:
                return '{"passed": false}'

        def fake(*args, **kwargs):
            return _FakeSuite()

        monkeypatch.setattr(scaling, "benchmark_perf_contract", fake)

        rc = main(["benchmark", "--perf-contract", "ordeal.perf.toml", "--check"])

        assert rc == 1
        out = capsys.readouterr().out
        assert "Performance Contract [FAIL]" in out

    def test_benchmark_output_json_requires_perf_contract(self, capsys):
        rc = main(["benchmark", "--output-json", "perf.json", "--mutate", "pkg.mod.compute"])

        assert rc == 2
        err = capsys.readouterr().err
        assert "--output-json requires --perf-contract" in err


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
