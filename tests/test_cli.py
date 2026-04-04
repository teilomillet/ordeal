"""Tests for ordeal.cli — CLI entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import hypothesis.strategies as st
import pytest
from hypothesis import settings as hsettings

import ordeal.auto as ordeal_auto
import ordeal.cli as cli
import ordeal.scaling as scaling
import ordeal.state as ordeal_state
from ordeal import ChaosTest, always, invariant, rule
from ordeal.assertions import tracker
from ordeal.cli import main
from ordeal.explore import ProgressSnapshot
from ordeal.mine import MinedProperty, MineResult
from ordeal.quickcheck import quickcheck


def _write_verify_fixture(
    tmp_path: Path,
    *,
    finding_id: str = "fnd_testverify01",
    regression_path: str | None = "tests/test_ordeal_regressions.py",
    regression_test: str | None = "test_normalize_idempotent_regression",
) -> tuple[Path, Path, str]:
    report_path = tmp_path / ".ordeal" / "findings" / "pkg" / "mod.md"
    bundle_path = tmp_path / ".ordeal" / "findings" / "pkg" / "mod.json"
    index_path = tmp_path / ".ordeal" / "findings" / "index.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")
    if regression_path is not None:
        regression_file = tmp_path / regression_path
        regression_file.parent.mkdir(parents=True, exist_ok=True)
        stub = "def test_normalize_idempotent_regression() -> None:\n    pass\n"
        regression_file.write_text(stub)

    bundle = {
        "version": 1,
        "saved_at": "2026-04-04T00:00:00Z",
        "tool": "scan",
        "target": "pkg.mod",
        "workspace": str(tmp_path),
        "status": "findings found",
        "confidence": 0.63,
        "seed": 42,
        "summary": ["Checked: 1 function"],
        "gaps": ["`pkg.mod.normalize`: property: idempotent (87%)"],
        "finding_count": 1,
        "findings": [
            {
                "finding_id": finding_id,
                "fingerprint": "a" * 64,
                "qualname": "pkg.mod.normalize",
                "kind": "property",
                "function": "normalize",
                "name": "idempotent",
                "summary": "idempotent (87%)",
                "status": "open",
                "regression_test": regression_test,
            }
        ],
        "artifacts": {
            "report": ".ordeal/findings/pkg/mod.md",
            "bundle": ".ordeal/findings/pkg/mod.json",
            "regression": regression_path,
            "index": ".ordeal/findings/index.json",
        },
        "commands": {
            "pytest": "uv run pytest tests/test_ordeal_regressions.py -q"
            if regression_path
            else None,
            "rescan": "uv run ordeal scan pkg.mod --save-artifacts",
        },
    }
    index = {
        "version": 1,
        "entries": [
            {
                "kind": "scan",
                "created_at": bundle["saved_at"],
                "module": "pkg.mod",
                "workspace": str(tmp_path),
                "status": "findings found",
                "confidence": 0.63,
                "seed": 42,
                "finding_count": 1,
                "finding_ids": [finding_id],
                "findings": [
                    {
                        "finding_id": finding_id,
                        "fingerprint": "a" * 64,
                        "qualname": "pkg.mod.normalize",
                        "kind": "property",
                        "name": "idempotent",
                        "summary": "idempotent (87%)",
                    }
                ],
                "artifacts": dict(bundle["artifacts"]),
                "commands": dict(bundle["commands"]),
            }
        ],
    }

    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return index_path, bundle_path, finding_id


class TestCLI:
    def test_no_command_returns_0(self):
        assert main([]) == 0

    def test_top_level_help_points_to_live_help(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "ordeal scan <module>" in out
        assert "ordeal init [package]" in out
        assert "ordeal skill" in out
        assert "ordeal <command> --help" in out
        assert "ordeal catalog" in out
        assert "catalog()" in out

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
        assert "--config CONFIG" in out
        assert "--validation-mode {fast,deep}" in out
        assert "fast replay" in out
        assert "--write-gaps PATH" in out
        assert "--include-exploratory-function-gaps" in out
        assert "--require-direct-tests" in out

    def test_benchmark_help_mentions_perf_contract_quality(self, capsys):
        with pytest.raises(SystemExit):
            main(["benchmark", "--help"])
        out = capsys.readouterr().out
        assert "--perf-contract PERF_CONTRACT" in out
        assert "--json" in out
        assert "--output-json PATH" in out
        assert "score-gap budget" in out

    def test_audit_forwards_validation_mode(self, monkeypatch, capsys):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        calls: dict[str, object] = {}

        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )

        def fake_audit(module, **kwargs):
            calls["module"] = module
            calls.update(kwargs)
            return ModuleAudit(
                module=module,
                current_test_count=4,
                current_test_lines=40,
                current_coverage=verified,
                migrated_test_count=3,
                migrated_lines=30,
                migrated_coverage=verified,
                mutation_score="8/10 (80%)",
                validation_mode=kwargs["validation_mode"],
                function_audits=[
                    FunctionAudit(
                        name="score",
                        status="exercised",
                        epistemic="verified",
                        covered_body_lines=2,
                        total_body_lines=2,
                    )
                ],
            )

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        rc = main(["audit", "ordeal.demo", "--validation-mode", "deep"])

        assert rc == 0
        assert calls["module"] == "ordeal.demo"
        assert calls["validation_mode"] == "deep"
        assert "ordeal audit" in capsys.readouterr().out

    def test_audit_uses_config_defaults_when_modules_omitted(
        self, monkeypatch, tmp_path, capsys
    ):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(
            """
[audit]
modules = ["ordeal.demo"]
test_dir = "spec"
max_examples = 31
workers = 3
validation_mode = "deep"
""",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        calls: dict[str, object] = {}
        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )

        def fake_audit(module, **kwargs):
            calls["module"] = module
            calls.update(kwargs)
            return ModuleAudit(
                module=module,
                current_test_count=4,
                current_test_lines=40,
                current_coverage=verified,
                migrated_test_count=3,
                migrated_lines=30,
                migrated_coverage=verified,
                validation_mode=kwargs["validation_mode"],
                function_audits=[
                    FunctionAudit(
                        name="score",
                        status="exercised",
                        epistemic="verified",
                        covered_body_lines=2,
                        total_body_lines=2,
                    )
                ],
            )

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        rc = main(["audit"])

        assert rc == 0
        assert calls["module"] == "ordeal.demo"
        assert calls["test_dir"] == "spec"
        assert calls["max_examples"] == 31
        assert calls["workers"] == 3
        assert calls["validation_mode"] == "deep"
        assert "ordeal audit" in capsys.readouterr().out

    def test_audit_cli_can_disable_configured_direct_test_gate(
        self, monkeypatch, tmp_path, capsys
    ):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(
            """
[audit]
modules = ["pkg.mod"]
require_direct_tests = true
""",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )
        result = ModuleAudit(
            module="pkg.mod",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            function_audits=[FunctionAudit(name="parse", status="uncovered", epistemic="none")],
        )
        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        rc = main(["audit", "--no-require-direct-tests"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "Direct test gate:" not in captured.out

    def test_audit_write_gaps_writes_draft_stubs(self, monkeypatch, tmp_path, capsys):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )
        result = ModuleAudit(
            module="pkg.mod",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            mutation_score="8/10 (80%)",
            validation_mode="fast",
            gap_functions=["normalize"],
            mutation_gap_stubs=[
                {
                    "target": "pkg.mod.value",
                    "content": (
                        '"""Draft review stubs for mutation gaps in pkg.mod.value.\n\n'
                        "Generated by ordeal.\n"
                        "These are review notes, not runnable regressions yet.\n"
                        "Reviewed signature: value() -> int\n"
                        '"""\n\n'
                        "from __future__ import annotations\n\n"
                        "import pkg.mod as _ordeal_target\n\n"
                        "# Evidence:\n"
                        "# - mutant: arithmetic: + -> - at L1:0\n"
                        "\n"
                        "# Suggested starting point:\n"
                        "# def test_pkg_value_gap() -> None:\n"
                        "#     # TODO: replace placeholder with concrete inputs.\n"
                        "#     result = _ordeal_target.value()\n"
                        "#     assert ...\n"
                    ),
                }
            ],
            function_audits=[
                FunctionAudit(
                    name="parse",
                    status="uncovered",
                    epistemic="none",
                    evidence=[
                        {
                            "kind": "no_tests",
                            "detail": "no matching pytest files or collected nodeids",
                        }
                    ],
                ),
                FunctionAudit(
                    name="score",
                    status="exploratory",
                    epistemic="inferred",
                    covered_body_lines=4,
                    total_body_lines=12,
                    evidence=[
                        {
                            "kind": "nodeids",
                            "detail": "covered indirectly by test_pkg_mod_score_roundtrip",
                        }
                    ],
                ),
            ],
            suggestions=["L42 in normalize(): test when x < 0"],
        )

        def fake_audit(*args, **kwargs):
            return result

        monkeypatch.setattr(audit_mod, "audit", fake_audit)
        monkeypatch.setattr(
            audit_mod,
            "audit_report",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("audit_report should not run when --write-gaps is set")
            ),
        )

        gap_dir = tmp_path / "draft-gaps"
        rc = main(["audit", "pkg.mod", "--write-gaps", str(gap_dir)])
        captured = capsys.readouterr()

        assert rc == 0
        assert (gap_dir / "test_pkg_mod_value_gaps.py").exists()
        assert (gap_dir / "test_pkg_mod_parse_gaps.py").exists()
        assert not (gap_dir / "test_pkg_mod_score_gaps.py").exists()
        parse_stub = (gap_dir / "test_pkg_mod_parse_gaps.py").read_text(encoding="utf-8")
        assert "Reviewed signature: parse(...)" in parse_stub
        assert "Epistemic status: uncovered [none]" in parse_stub
        assert "# - write a direct regression for pkg.mod.parse" in parse_stub
        assert "# - keep the assertion small, specific, and reviewable" in parse_stub
        assert (
            "# TODO: call parse(...) with a concrete input that matches the evidence."
            in parse_stub
        )
        assert (
            "# TODO: assert the intended contract, not just that the call succeeds." in parse_stub
        )
        assert "exploratory gaps hidden by default" in captured.out
        assert "score is only indirectly exercised" not in captured.out
        assert "Wrote 2 draft gap stub file(s) to" in captured.err

    def test_init_uses_config_defaults_and_audit_settings(self, monkeypatch, tmp_path, capsys):
        import ordeal.audit as audit_mod
        import ordeal.mutations as mutations
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(
            """
[audit]
max_examples = 33
workers = 5
validation_mode = "deep"
include_exploratory_function_gaps = true

[init]
target = "pkg"
output_dir = "qa"
close_gaps = true
gap_output_dir = "qa/gaps"
scan_max_examples = 12
""",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        calls: dict[str, object] = {}
        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )

        def fake_init_project(*, target, output_dir, dry_run):
            calls["init_project"] = {
                "target": target,
                "output_dir": output_dir,
                "dry_run": dry_run,
            }
            return [
                {
                    "module": "pkg.mod",
                    "status": "generated",
                    "path": str(tmp_path / output_dir / "test_pkg_mod.py"),
                    "content": (
                        "def test_pkg_mod_value_pinned() -> None:\n"
                        "    assert 1 == 1\n"
                    ),
                }
            ]

        def fake_audit(module, **kwargs):
            calls["audit"] = {"module": module, **kwargs}
            return ModuleAudit(
                module=module,
                current_test_count=4,
                current_test_lines=40,
                current_coverage=verified,
                migrated_test_count=3,
                migrated_lines=30,
                migrated_coverage=verified,
                mutation_score="8/10 (80%)",
                validation_mode=kwargs["validation_mode"],
                function_audits=[
                    FunctionAudit(
                        name="score",
                        status="exploratory",
                        epistemic="inferred",
                        evidence=[{"kind": "nodeids", "detail": "indirect"}],
                    )
                ],
            )

        def fake_run_init_scan(modules, *, max_examples=10):
            calls["init_scan"] = {"modules": list(modules), "max_examples": max_examples}
            return {
                "status": "no findings",
                "modules": list(modules),
                "functions_checked": 1,
                "skipped_functions": 0,
                "findings": [],
                "errors": [],
                "max_examples": max_examples,
                "available_commands": [],
            }

        monkeypatch.setattr(mutations, "init_project", fake_init_project)
        monkeypatch.setattr(audit_mod, "audit", fake_audit)
        monkeypatch.setattr(cli, "_run_init_scan", fake_run_init_scan)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        rc = main(["init", "--config", str(config_path)])
        report = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert calls["init_project"] == {"target": "pkg", "output_dir": "qa", "dry_run": False}
        assert calls["audit"] == {
            "module": "pkg.mod",
            "test_dir": "qa",
            "max_examples": 33,
            "workers": 5,
            "validation_mode": "deep",
        }
        assert calls["init_scan"] == {"modules": ["pkg.mod"], "max_examples": 12}
        assert report["close_gaps"] is True
        assert report["gap_stub_files"][0]["path"] == "qa/gaps/test_pkg_mod_score_gaps.py"
        assert Path(tmp_path / "qa" / "gaps" / "test_pkg_mod_score_gaps.py").exists()

    def test_audit_require_direct_tests_fails_on_exploratory_coverage(self, monkeypatch, capsys):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )
        result = ModuleAudit(
            module="pkg.mod",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            mutation_score="8/10 (80%)",
            validation_mode="fast",
            function_audits=[
                FunctionAudit(
                    name="score",
                    status="exploratory",
                    epistemic="inferred",
                    covered_body_lines=4,
                    total_body_lines=12,
                    evidence=[
                        {
                            "kind": "nodeids",
                            "detail": "covered indirectly by test_pkg_mod_score_roundtrip",
                        }
                    ],
                )
            ],
        )

        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        rc = main(["audit", "pkg.mod", "--require-direct-tests"])
        captured = capsys.readouterr()

        assert rc == 1
        assert "exploratory gaps hidden by default" in captured.out
        assert "Direct test gate: FAIL (1 exploratory)" in captured.out
        assert "Direct tests required: fail (1 exploratory)" in captured.err

    def test_audit_require_direct_tests_fails_on_uncovered_coverage(self, monkeypatch, capsys):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        verified = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=82.0,
                total_statements=100,
                missing_count=18,
                missing_lines=frozenset({10, 11}),
                source="coverage.py",
            ),
        )
        result = ModuleAudit(
            module="pkg.mod",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            function_audits=[
                FunctionAudit(
                    name="parse",
                    status="uncovered",
                    epistemic="none",
                    evidence=[
                        {
                            "kind": "no_tests",
                            "detail": "no matching pytest files or collected nodeids",
                        }
                    ],
                )
            ],
        )

        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        rc = main(["audit", "pkg.mod", "--require-direct-tests"])
        captured = capsys.readouterr()

        assert rc == 1
        assert "Direct test gate: FAIL (1 uncovered)" in captured.out
        assert "Direct tests required: fail (1 uncovered)" in captured.err

    def test_catalog_lists_live_cli_commands(self, capsys):
        assert main(["catalog"]) == 0
        out = capsys.readouterr().out
        assert "Run 'ordeal --help' for the full live CLI surface." in out
        assert "Run 'ordeal <command> --help' for command-specific options." in out
        assert "Key CLI entrypoints: scan, init, audit, mutate, verify, skill." in out
        assert "Run 'ordeal skill' or 'ordeal init --install-skill'" in out
        for entry in cli.command_catalog():
            assert entry["name"] in out
            assert entry["doc"] in out

    def test_command_catalog_matches_parser_choices(self):
        parser = cli._build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        assert sorted(subparsers.choices) == [entry["name"] for entry in cli.command_catalog()]

    def test_all_subparsers_bind_a_handler(self):
        parser = cli._build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        for subparser in subparsers.choices.values():
            assert callable(subparser.get_default("_handler"))

    def test_command_catalog_exposes_rich_argument_metadata(self):
        entries = {entry["name"]: entry for entry in cli.command_catalog()}

        scan = entries["scan"]
        assert scan["schema_version"] == cli.CLI_CATALOG_SCHEMA_VERSION
        seed = next(arg for arg in scan["arguments"] if arg["name"] == "seed")
        assert seed["value_type"] == "int"
        assert seed["accepts_value"] is True
        assert seed["semantics"] == "value"

        save_artifacts = next(arg for arg in scan["arguments"] if arg["name"] == "save_artifacts")
        assert save_artifacts["value_type"] == "bool"
        assert save_artifacts["kind"] == "flag"
        assert save_artifacts["semantics"] == "flag"

        benchmark = entries["benchmark"]
        args = benchmark["arguments"]
        mutate_target = next(a for a in args if a["name"] == "mutate_targets")
        assert mutate_target["repeatable"] is True
        assert mutate_target["semantics"] == "repeatable"

        mutate = entries["mutate"]
        targets = next(arg for arg in mutate["arguments"] if arg["name"] == "targets")
        assert targets["variadic"] is True
        assert targets["semantics"] == "variadic"

    def test_explore_missing_config(self):
        assert main(["explore", "--config", "/nonexistent.toml"]) == 1

    def test_explore_runs_scan_entries_when_config_has_no_tests(
        self,
        monkeypatch,
        tmp_path,
        capsys,
    ):
        config = tmp_path / "ordeal.toml"
        config.write_text(
            "[[scan]]\n"
            'module = "pkg.mod"\n'
            "max_examples = 7\n"
            'ignore_relations = ["commutative_composition"]\n'
            'relation_overrides = { normalize = ["roundtrip"] }\n',
            encoding="utf-8",
        )

        import ordeal.auto as auto_mod

        calls: dict[str, object] = {}

        class _FakeScanResult:
            passed = True

            def summary(self) -> str:
                return "scan_module('pkg.mod'): 1 functions, 0 failed"

        def fake_scan_module(module: str, **kwargs):
            calls["module"] = module
            calls.update(kwargs)
            return _FakeScanResult()

        monkeypatch.setattr(auto_mod, "scan_module", fake_scan_module)

        rc = main(["explore", "--config", str(config)])

        assert rc == 0
        assert calls["module"] == "pkg.mod"
        assert calls["max_examples"] == 7
        assert calls["ignore_relations"] == ["commutative_composition"]
        assert calls["relation_overrides"] == {"normalize": ["roundtrip"]}
        assert "scan_module('pkg.mod')" in capsys.readouterr().out

    def test_scan_uses_fixture_registries_and_scan_config(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "conftest.py").write_text(
            "import hypothesis.strategies as st\n"
            "from ordeal.auto import register_fixture\n"
            "register_fixture('model', st.just('fixture-model'))\n",
            encoding="utf-8",
        )
        (tmp_path / "shared_registry.py").write_text(
            "import hypothesis.strategies as st\n"
            "from ordeal.auto import register_fixture\n"
            "register_fixture('shared_mode', st.just('shared-mode'))\n",
            encoding="utf-8",
        )
        (tmp_path / "scan_registry.py").write_text(
            "import hypothesis.strategies as st\n"
            "from ordeal.auto import register_fixture\n"
            "register_fixture('scan_mode', st.just('scan-mode'))\n",
            encoding="utf-8",
        )
        (tmp_path / "ordeal.toml").write_text(
            "[fixtures]\n"
            'registries = ["shared_registry"]\n'
            "\n"
            "[[scan]]\n"
            'module = "pkg.mod"\n'
            "max_examples = 7\n"
            'expected_failures = ["known_bad"]\n'
            'fixture_registries = ["scan_registry"]\n'
            'ignore_properties = ["commutative"]\n'
            'ignore_relations = ["commutative_composition"]\n'
            'property_overrides = { normalize = ["idempotent"] }\n'
            'relation_overrides = { normalize = ["roundtrip"] }\n'
            'fixtures = { mode = "fast,slow" }\n',
            encoding="utf-8",
        )

        original_strategies = dict(ordeal_auto._REGISTERED_STRATEGIES)
        original_modules = set(ordeal_auto._FIXTURE_REGISTRY_MODULES)
        ordeal_auto._REGISTERED_STRATEGIES.clear()
        ordeal_auto._FIXTURE_REGISTRY_MODULES.clear()

        calls: dict[str, object] = {}

        def fake_explore(module: str, **kwargs):
            calls["module"] = module
            calls.update(kwargs)
            return SimpleNamespace(
                module=module,
                confidence=0.9,
                functions={"ok": object()},
                supervisor_info={},
                tree=SimpleNamespace(size=0),
                findings=[],
                frontier={},
                skipped=[],
            )

        monkeypatch.setattr(ordeal_state, "explore", fake_explore)

        try:
            rc = main(
                [
                    "scan",
                    "pkg.mod",
                    "--ignore-property",
                    "associative",
                    "--ignore-relation",
                    "distributive",
                    "--property-override",
                    "normalize=bounded",
                    "--relation-override",
                    "normalize=dual",
                ]
            )
            loaded_fixture_names = set(ordeal_auto._REGISTERED_STRATEGIES)
        finally:
            ordeal_auto._REGISTERED_STRATEGIES.clear()
            ordeal_auto._REGISTERED_STRATEGIES.update(original_strategies)
            ordeal_auto._FIXTURE_REGISTRY_MODULES.clear()
            ordeal_auto._FIXTURE_REGISTRY_MODULES.update(original_modules)

        assert rc == 0
        assert calls["module"] == "pkg.mod"
        assert calls["max_examples"] == 7
        assert calls["scan_expected_failures"] == ["known_bad"]
        assert calls["scan_ignore_properties"] == ["commutative", "associative"]
        assert calls["scan_ignore_relations"] == [
            "commutative_composition",
            "distributive",
        ]
        assert calls["scan_property_overrides"] == {"normalize": ["idempotent", "bounded"]}
        assert calls["scan_relation_overrides"] == {"normalize": ["roundtrip", "dual"]}
        assert "mode" in calls["scan_fixtures"]
        assert {"model", "shared_mode", "scan_mode"} <= loaded_fixture_names
        assert "status: no findings yet" in capsys.readouterr().out

    def test_scan_warns_when_shared_fixture_registry_missing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ordeal.toml").write_text(
            '[fixtures]\nregistries = ["missing_registry"]\n\n[[scan]]\nmodule = "pkg.mod"\n',
            encoding="utf-8",
        )

        original_strategies = dict(ordeal_auto._REGISTERED_STRATEGIES)
        original_modules = set(ordeal_auto._FIXTURE_REGISTRY_MODULES)
        ordeal_auto._REGISTERED_STRATEGIES.clear()
        ordeal_auto._FIXTURE_REGISTRY_MODULES.clear()

        monkeypatch.setattr(
            ordeal_state,
            "explore",
            lambda *args, **kwargs: SimpleNamespace(
                module="pkg.mod",
                confidence=0.0,
                functions={},
                supervisor_info={},
                tree=SimpleNamespace(size=0),
                findings=[],
                frontier={},
                skipped=[],
            ),
        )

        try:
            rc = main(["scan", "pkg.mod"])
        finally:
            ordeal_auto._REGISTERED_STRATEGIES.clear()
            ordeal_auto._REGISTERED_STRATEGIES.update(original_strategies)
            ordeal_auto._FIXTURE_REGISTRY_MODULES.clear()
            ordeal_auto._FIXTURE_REGISTRY_MODULES.update(original_modules)

        assert rc == 0
        err = capsys.readouterr().err
        assert "warning: fixture registry load failed for missing_registry" in err

    def test_replay_missing_file(self):
        assert main(["replay", "/nonexistent/trace.json"]) == 1

    def test_replay_json_missing_file(self, capsys):
        rc = main(["replay", "/nonexistent/trace.json", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert payload["tool"] == "replay"
        assert payload["status"] == "blocked"
        assert payload["blocking_reason"]

    def test_scan_json_outputs_agent_envelope(self, monkeypatch, capsys):
        state = ordeal_state.ExplorationState("pkg.mod")
        fn = state.function("normalize")
        fn.mined = True
        fn.properties = [{"name": "idempotent", "universal": False}]
        fn.property_violations = ["idempotent (87%)"]
        fn.property_violation_details = [
            {
                "name": "idempotent",
                "summary": "idempotent (87%)",
                "confidence": 0.87,
                "holds": 26,
                "total": 30,
                "counterexample": {
                    "input": {"xs": [9, 8, 7, 6]},
                    "output": [1.0, 0.5, 0.0],
                    "replayed": [0.66, 0.33, 0.0],
                },
            }
        ]
        fn.scanned = True
        fn.crash_free = True
        fn.mutated = True
        fn.mutation_score = 0.8
        state.supervisor_info = {"seed": 42, "trajectory_steps": 5}

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["tool"] == "scan"
        assert payload["status"] == "exploratory"
        assert payload["confidence"] == pytest.approx(state.confidence)
        assert payload["findings"][0]["kind"] == "property"
        assert payload["findings"][0]["details"]["category"] == "speculative_property"
        assert "evidence_dimensions" in payload["raw_details"]
        assert payload["raw_details"]["state"]["supervisor_info"]["seed"] == 42

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

    def test_mine_json_outputs_agent_envelope(self, monkeypatch, capsys):
        result = MineResult(
            function="normalize",
            examples=20,
            properties=[
                MinedProperty(
                    "idempotent",
                    18,
                    20,
                    {
                        "input": {"xs": [9, 8, 7, 6]},
                        "output": [1.0, 0.5, 0.0],
                        "replayed": [0.66, 0.33, 0.0],
                    },
                )
            ],
            not_checked=["state mutation and side effects"],
        )

        import ordeal.mine as ordeal_mine

        monkeypatch.setattr(ordeal_mine, "mine", lambda *args, **kwargs: result)

        rc = main(["mine", "ordeal.demo.normalize", "--json", "-n", "10"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["tool"] == "mine"
        assert payload["status"] == "findings"
        assert payload["suggested_test_file"] == "tests/test_ordeal_regressions.py"
        assert payload["findings"][0]["summary"] == "idempotent (90%)"
        assert payload["raw_details"]["results"][0]["function"] == "normalize"

    def test_audit_json_outputs_agent_envelope(self, monkeypatch, capsys):
        import ordeal.audit as audit_mod
        from ordeal.audit import (
            CoverageMeasurement,
            CoverageResult,
            FunctionAudit,
            ModuleAudit,
            Status,
        )

        result = ModuleAudit(
            module="pkg.mod",
            mutation_score="3/4 (75%)",
            function_audits=[
                FunctionAudit(
                    name="normalize",
                    status="uncovered",
                    epistemic="none",
                    evidence=[
                        {
                            "kind": "no_tests",
                            "detail": "no matching pytest files or collected nodeids",
                        }
                    ],
                )
            ],
            suggestions=["L10 in normalize(): test when x < 0"],
        )
        result.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=90.0,
                total_statements=10,
                missing_count=1,
                missing_lines=frozenset({10}),
                source="coverage.py API",
            ),
        )
        result.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=92.0,
                total_statements=10,
                missing_count=0,
                missing_lines=frozenset(),
                source="coverage.py API",
            ),
        )

        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        rc = main(["audit", "pkg.mod", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["tool"] == "audit"
        assert payload["status"] == "findings"
        assert payload["confidence"] == pytest.approx(1.0)
        kinds = {item["kind"] for item in payload["findings"]}
        assert {"coverage_gap", "mutation_gap", "function_gap"} <= kinds
        assert payload["findings"][0]["details"]["category"] == "test_strength_gap"
        assert "Function evidence:" in payload["summary"]
        assert "evidence_dimensions" in payload["raw_details"]
        assert payload["raw_details"]["modules"][0]["module"] == "pkg.mod"

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
        assert "ordeal verify <finding-id>" in out
        assert "--save-artifacts" in out
        assert ".ordeal/findings/mymod.md" in out
        assert ".ordeal/findings/mymod.json" in out
        assert "--report-file PATH" in out
        assert "--write-regression" in out
        assert "default: tests/test_ordeal_regressions.py" in out

    def test_verify_help_mentions_finding_id(self, capsys):
        with pytest.raises(SystemExit):
            main(["verify", "--help"])
        out = capsys.readouterr().out
        assert "stable `finding_id`" in out
        assert "--index PATH" in out
        assert ".ordeal/findings/index.json" in out

    def test_init_help_mentions_opt_in_flags(self, capsys):
        with pytest.raises(SystemExit):
            main(["init", "--help"])
        out = capsys.readouterr().out
        assert "--install-skill" in out
        assert "--close-gaps" in out
        assert "lightweight read-only scan" in out
        assert "summary" in out

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
        bundle_path = tmp_path / ".ordeal" / "findings" / "pkg" / "mod.json"
        index_path = tmp_path / ".ordeal" / "findings" / "index.json"
        regression_path = tmp_path / "tests" / "test_ordeal_regressions.py"
        assert rc == 1
        assert report_path.exists()
        assert bundle_path.exists()
        assert index_path.exists()
        assert regression_path.exists()
        assert "Target: `pkg.mod`" in report_path.read_text()
        assert "def test_normalize_idempotent_regression() -> None:" in regression_path.read_text()
        bundle = json.loads(bundle_path.read_text())
        assert bundle["tool"] == "scan"
        assert bundle["target"] == "pkg.mod"
        assert bundle["saved_at"]
        assert bundle["workspace"] == str(tmp_path)
        assert bundle["artifacts"]["report"] == ".ordeal/findings/pkg/mod.md"
        assert bundle["artifacts"]["bundle"] == ".ordeal/findings/pkg/mod.json"
        assert bundle["artifacts"]["regression"] == "tests/test_ordeal_regressions.py"
        assert bundle["artifacts"]["index"] == ".ordeal/findings/index.json"
        assert bundle["findings"][0]["finding_id"].startswith("fnd_")
        assert len(bundle["findings"][0]["fingerprint"]) == 64
        assert bundle["findings"][0]["status"] == "open"
        assert bundle["findings"][0]["regression_test"] == "test_normalize_idempotent_regression"
        artifact_index = json.loads(index_path.read_text())
        assert artifact_index["version"] == 1
        assert len(artifact_index["entries"]) == 1
        assert artifact_index["entries"][0]["kind"] == "scan"
        assert artifact_index["entries"][0]["module"] == "pkg.mod"
        assert artifact_index["entries"][0]["workspace"] == str(tmp_path)
        assert artifact_index["entries"][0]["created_at"] == bundle["saved_at"]
        assert artifact_index["entries"][0]["artifacts"]["report"] == ".ordeal/findings/pkg/mod.md"
        assert (
            artifact_index["entries"][0]["artifacts"]["bundle"] == ".ordeal/findings/pkg/mod.json"
        )
        assert (
            artifact_index["entries"][0]["artifacts"]["regression"]
            == "tests/test_ordeal_regressions.py"
        )
        assert artifact_index["entries"][0]["artifacts"]["index"] == ".ordeal/findings/index.json"
        assert artifact_index["entries"][0]["finding_ids"] == [bundle["findings"][0]["finding_id"]]
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
        assert "bundle: .ordeal/findings/pkg/mod.json" in captured.out
        assert "regression: tests/test_ordeal_regressions.py" in captured.out
        assert "index: .ordeal/findings/index.json" in captured.out
        assert "available:" in captured.out
        fid = bundle["findings"][0]["finding_id"]
        assert f"verify: uv run ordeal verify {fid}" in captured.out
        assert "pytest: uv run pytest tests/test_ordeal_regressions.py -q" in captured.out
        assert "rescan: uv run ordeal scan pkg.mod --save-artifacts" in captured.out
        assert "Scan report saved:" in captured.err
        assert "Scan bundle saved:" in captured.err
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
        bundle_path = tmp_path / ".ordeal" / "findings" / "pkg" / "mod.json"
        index_path = tmp_path / ".ordeal" / "findings" / "index.json"
        regression_path = tmp_path / "tests" / "test_ordeal_regressions.py"
        assert rc == 1
        assert report_path.exists()
        assert bundle_path.exists()
        assert index_path.exists()
        assert not regression_path.exists()
        bundle = json.loads(bundle_path.read_text())
        assert bundle["artifacts"]["regression"] is None
        assert bundle["artifacts"]["index"] == ".ordeal/findings/index.json"
        assert bundle["commands"]["pytest"] is None
        assert bundle["findings"][0]["finding_id"].startswith("fnd_")
        assert bundle["findings"][0]["regression_test"] is None
        artifact_index = json.loads(index_path.read_text())
        assert artifact_index["entries"][0]["kind"] == "scan"
        assert (
            artifact_index["entries"][0]["artifacts"]["bundle"] == ".ordeal/findings/pkg/mod.json"
        )
        assert artifact_index["entries"][0]["artifacts"]["regression"] is None
        assert artifact_index["entries"][0]["artifacts"]["index"] == ".ordeal/findings/index.json"
        assert artifact_index["entries"][0]["commands"]["pytest"] is None
        assert "No concrete regression tests could be generated" in captured.err
        assert "Scan bundle saved:" in captured.err
        assert "Artifact index updated:" in captured.err
        assert "bundle: .ordeal/findings/pkg/mod.json" in captured.out
        assert "verify:" not in captured.out
        assert "regression: not generated from current findings" in captured.out
        assert "rescan: uv run ordeal scan pkg.mod --save-artifacts" in captured.out

    def test_scan_save_artifacts_appends_index_history(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        first_state = SimpleNamespace(
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

        second_state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.61,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 7},
            tree=SimpleNamespace(size=4),
            findings=["normalize: idempotent (83%)"],
            frontier={"normalize": ["property: idempotent (83%)"]},
            finding_details=[
                {
                    "kind": "property",
                    "function": "normalize",
                    "name": "idempotent",
                    "summary": "idempotent (83%)",
                    "confidence": 0.83,
                    "holds": 25,
                    "total": 30,
                    "counterexample": {
                        "input": {"xs": [1, 2, 3]},
                        "output": [0.5, 0.25, 0.25],
                        "replayed": [0.4, 0.3, 0.3],
                    },
                }
            ],
        )

        states = iter([first_state, second_state])
        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: next(states))

        assert main(["scan", "pkg.mod", "--save-artifacts", "-n", "10"]) == 1
        capsys.readouterr()
        assert main(["scan", "pkg.mod", "--save-artifacts", "-n", "10"]) == 1
        capsys.readouterr()

        artifact_index = json.loads((tmp_path / ".ordeal" / "findings" / "index.json").read_text())
        assert len(artifact_index["entries"]) == 2
        assert artifact_index["entries"][0]["findings"][0]["summary"] == "idempotent (87%)"
        assert artifact_index["entries"][1]["findings"][0]["summary"] == "idempotent (83%)"
        assert (
            artifact_index["entries"][0]["findings"][0]["finding_id"]
            == artifact_index["entries"][1]["findings"][0]["finding_id"]
        )
        assert (
            artifact_index["entries"][0]["findings"][0]["fingerprint"]
            == artifact_index["entries"][1]["findings"][0]["fingerprint"]
        )

    def test_verify_marks_finding_verified(self, monkeypatch, tmp_path, capsys):
        index_path, bundle_path, finding_id = _write_verify_fixture(tmp_path)
        calls: dict[str, object] = {}

        def fake_run(cmd, cwd=None, text=None, capture_output=None, check=None):
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        rc = main(["verify", finding_id, "--index", str(index_path)])
        captured = capsys.readouterr()

        assert rc == 0
        assert calls["cmd"] == [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_ordeal_regressions.py::test_normalize_idempotent_regression",
            "-q",
        ]
        assert calls["cwd"] == str(tmp_path)
        bundle = json.loads(bundle_path.read_text())
        assert bundle["findings"][0]["status"] == "verified"
        assert bundle["verification"]["finding_id"] == finding_id
        assert bundle["verification"]["status"] == "verified"
        assert bundle["verification"]["exit_code"] == 0
        artifact_index = json.loads(index_path.read_text())
        assert len(artifact_index["entries"]) == 2
        assert artifact_index["entries"][1]["kind"] == "verification"
        assert artifact_index["entries"][1]["finding_id"] == finding_id
        assert artifact_index["entries"][1]["status"] == "verified"
        assert "verify:" in captured.out
        assert "status: verified" in captured.out
        expected = "tests/test_ordeal_regressions.py::test_normalize_idempotent_regression"
        assert expected in captured.out

    def test_verify_marks_finding_reproduced(self, monkeypatch, tmp_path, capsys):
        index_path, bundle_path, finding_id = _write_verify_fixture(tmp_path)

        def fake_run(cmd, cwd=None, text=None, capture_output=None, check=None):
            return SimpleNamespace(returncode=1, stdout="F\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        rc = main(["verify", finding_id, "--index", str(index_path)])
        captured = capsys.readouterr()

        assert rc == 1
        bundle = json.loads(bundle_path.read_text())
        assert bundle["findings"][0]["status"] == "reproduced"
        assert bundle["verification"]["status"] == "reproduced"
        assert bundle["verification"]["exit_code"] == 1
        artifact_index = json.loads(index_path.read_text())
        assert artifact_index["entries"][1]["kind"] == "verification"
        assert artifact_index["entries"][1]["status"] == "reproduced"
        assert "status: reproduced" in captured.out

    def test_verify_requires_runnable_regression(self, monkeypatch, tmp_path, capsys):
        index_path, bundle_path, finding_id = _write_verify_fixture(
            tmp_path,
            regression_path=None,
            regression_test=None,
        )

        called = False

        def fake_run(cmd, cwd=None, text=None, capture_output=None, check=None):
            nonlocal called
            called = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        rc = main(["verify", finding_id, "--index", str(index_path)])
        captured = capsys.readouterr()

        assert rc == 2
        assert not called
        bundle = json.loads(bundle_path.read_text())
        assert "verification" not in bundle
        artifact_index = json.loads(index_path.read_text())
        assert len(artifact_index["entries"]) == 1
        assert "No runnable regression is recorded" in captured.err

    def test_init_default_is_minimal(self, monkeypatch, tmp_path, capsys):
        import ordeal.mutations as mutations
        from ordeal.auto import FunctionResult, ScanResult

        monkeypatch.chdir(tmp_path)
        test_file = tmp_path / "tests" / "test_pkg.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        original = "def test_pkg_value_pinned() -> None:\n    assert 1 == 1\n"
        test_file.write_text(original, encoding="utf-8")

        monkeypatch.setattr(
            mutations,
            "init_project",
            lambda target=None, output_dir="tests", dry_run=False: [
                {
                    "module": "pkg",
                    "status": "generated",
                    "path": str(test_file),
                    "content": original,
                }
            ],
        )

        skill_called = False

        def fake_install_skill(*, dry_run=False):
            nonlocal skill_called
            skill_called = True
            return ".claude/skills/ordeal/SKILL.md"

        monkeypatch.setattr(cli, "_install_skill", fake_install_skill)

        scan_calls: list[tuple[str, int]] = []

        def fake_scan_module(
            module,
            *,
            max_examples=50,
            check_return_type=True,
            fixtures=None,
            expected_failures=None,
        ):
            scan_calls.append((str(module), max_examples))
            return ScanResult(
                module=str(module),
                functions=[FunctionResult(name="value", passed=True)],
            )

        monkeypatch.setattr(ordeal_auto, "scan_module", fake_scan_module)

        mutate_scripts: list[str] = []

        def fake_run(cmd, capture_output=None, text=None, env=None, cwd=None, check=False):
            if cmd[:3] == [sys.executable, "-m", "pytest"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[:2] == [sys.executable, "-c"]:
                script = cmd[2]
                if "'mutate'" in script:
                    mutate_scripts.append(script)
                    assert "--generate-stubs" not in script
                    return SimpleNamespace(returncode=0, stdout="Score: 1/2 (50%)\n", stderr="")
                if "'explore'" in script:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        rc = main(["init", "pkg"])
        captured = capsys.readouterr()

        assert rc == 0
        assert not skill_called
        assert mutate_scripts
        assert scan_calls == [("pkg", 10)]
        assert test_file.read_text(encoding="utf-8") == original
        assert not (tmp_path / ".claude").exists()
        assert "Gaps:       report-only" in captured.err
        assert "Initial scan: no findings yet (1 function(s) checked)" in captured.err

        report = json.loads(captured.out)
        assert report["install_skill"] is False
        assert report["close_gaps"] is False
        assert report["skill"] is None
        assert report["initial_scan"]["status"] == "no findings yet"
        assert report["initial_scan"]["functions_checked"] == 1
        assert report["initial_scan"]["available_commands"] == ["ordeal scan pkg --save-artifacts"]
        assert ".claude/skills/ordeal/SKILL.md" not in report["files"]

    def test_init_opt_in_installs_skill_and_writes_audit_gap_stubs(
        self, monkeypatch, tmp_path, capsys
    ):
        import ordeal.audit as audit_mod
        import ordeal.mutations as mutations
        from ordeal.audit import ModuleAudit
        from ordeal.auto import FunctionResult, ScanResult

        monkeypatch.chdir(tmp_path)
        test_file = tmp_path / "tests" / "test_pkg.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        original = "def test_pkg_value_pinned() -> None:\n    assert 1 == 1\n"
        test_file.write_text(original, encoding="utf-8")

        monkeypatch.setattr(
            mutations,
            "init_project",
            lambda target=None, output_dir="tests", dry_run=False: [
                {
                    "module": "pkg",
                    "status": "generated",
                    "path": str(test_file),
                    "content": original,
                }
            ],
        )

        def fake_install_skill(*, dry_run=False):
            skill_path = tmp_path / ".claude" / "skills" / "ordeal" / "SKILL.md"
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text("skill", encoding="utf-8")
            return ".claude/skills/ordeal/SKILL.md"

        monkeypatch.setattr(cli, "_install_skill", fake_install_skill)

        monkeypatch.setattr(
            ordeal_auto,
            "scan_module",
            lambda module, **kwargs: ScanResult(
                module=str(module), functions=[FunctionResult(name="value", passed=True)]
            ),
        )
        gap_stub = (
            '"""Draft review stubs for mutation gaps in pkg.value.\n\n'
            "Generated by ordeal.\n"
            "These are review notes, not runnable regressions yet.\n"
            "Reviewed signature: value() -> int\n"
            '"""\n\n'
            "from __future__ import annotations\n\n"
            "import pkg as _ordeal_target\n\n"
            "def test_pkg_value_gap() -> None:\n"
            "    # Review this draft before pinning it as a regression.\n"
            "    # Mutant: arithmetic: + -> - at L1:0\n"
            "    # Source: return 1\n"
            "    # Fix idea: Add a test that distinguishes the original from: + -> -\n"
            "    result = _ordeal_target.value()\n"
            "    # Pinned behavior candidate. Replace this placeholder once reviewed.\n"
            "    # assert result == ...\n"
        )
        function_gap_stub = (
            '"""Draft review stubs for audit gaps in pkg.parse.\n\n'
            "Generated by ordeal.\n"
            "These are review notes, not runnable regressions yet.\n"
            "Epistemic status: uncovered [none]\n"
            "Reviewed signature: parse(...)\n"
            '"""\n\n'
            "from __future__ import annotations\n\n"
            "import pkg as _ordeal_target\n\n"
            "# Evidence summary:\n"
            "# - no_tests: no matching pytest files or collected nodeids\n\n"
            "# Why this exists:\n"
            "# - write a direct regression for pkg.parse\n"
            "# - there is no effective test coverage yet\n"
            "# - keep the assertion small, specific, and reviewable\n\n"
            "# Suggested starting point:\n"
            "# def test_pkg_parse_gap() -> None:\n"
            "#     # TODO: call parse(...) with a concrete input that matches the evidence.\n"
            "#     result = _ordeal_target.parse(...)\n"
            "#     # TODO: assert the intended contract, not just that the call succeeds.\n"
            "#     assert ...\n"
        )

        def fake_run(cmd, capture_output=None, text=None, env=None, cwd=None, check=False):
            if cmd[:3] == [sys.executable, "-m", "pytest"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(
            audit_mod,
            "audit",
            lambda module, **kwargs: ModuleAudit(
                module=str(module),
                mutation_score="1/2 (50%)",
                mutation_gap_stubs=[{"target": "pkg.value", "content": gap_stub}],
                function_audits=[
                    audit_mod.FunctionAudit(
                        name="parse",
                        status="uncovered",
                        epistemic="none",
                        evidence=[
                            {
                                "kind": "no_tests",
                                "detail": "no matching pytest files or collected nodeids",
                            }
                        ],
                    )
                ],
                weakest_tests=[{"test": "tests/test_pkg.py::test_pkg_value_pinned", "kills": 1}],
            ),
        )

        rc = main(["init", "pkg", "--install-skill", "--close-gaps"])
        captured = capsys.readouterr()

        assert rc == 0
        assert test_file.read_text(encoding="utf-8") == original
        gap_file = tmp_path / "tests" / "test_pkg_value_gaps.py"
        assert gap_file.read_text(encoding="utf-8") == gap_stub.rstrip() + "\n"
        function_gap_file = tmp_path / "tests" / "test_pkg_parse_gaps.py"
        assert function_gap_file.read_text(encoding="utf-8") == function_gap_stub.rstrip() + "\n"
        assert (tmp_path / ".claude" / "skills" / "ordeal" / "SKILL.md").exists()
        assert "Gaps:       report-only" not in captured.err
        assert "Gaps:       wrote 2 draft stub file(s) from audit" in captured.err
        assert "Weakest:" in captured.err

        def _normalize_gap_stub_file(item: dict[str, object]) -> dict[str, object]:
            normalized = dict(item)
            normalized["path"] = Path(str(normalized["path"])).as_posix()
            return normalized

        report = json.loads(captured.out)
        assert report["install_skill"] is True
        assert report["close_gaps"] is True
        assert report["skill"] == ".claude/skills/ordeal/SKILL.md"
        assert report["initial_scan"]["status"] == "no findings yet"
        assert [_normalize_gap_stub_file(item) for item in report["gap_stub_files"]] == [
            {
                "module": "pkg",
                "target": "pkg.value",
                "path": Path("tests/test_pkg_value_gaps.py").as_posix(),
                "source": "mutation_gap",
            },
            {
                "module": "pkg",
                "target": "pkg.parse",
                "path": Path("tests/test_pkg_parse_gaps.py").as_posix(),
                "source": "function_audit",
                "status": "uncovered",
                "epistemic": "none",
            },
        ]
        assert report["weakest_tests"] == [
            {
                "module": "pkg",
                "test": "tests/test_pkg.py::test_pkg_value_pinned",
                "kills": 1,
            }
        ]
        assert ".claude/skills/ordeal/SKILL.md" in report["files"]
        assert "tests/test_pkg_value_gaps.py" in report["files"]
        assert "tests/test_pkg_parse_gaps.py" in report["files"]

    def test_init_dry_run_skips_validation_scan(self, monkeypatch, tmp_path, capsys):
        import ordeal.mutations as mutations

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            mutations,
            "init_project",
            lambda target=None, output_dir="tests", dry_run=False: [
                {
                    "module": "pkg",
                    "status": "generated",
                    "path": str(tmp_path / "tests" / "test_pkg.py"),
                    "content": "def test_pkg_value_pinned() -> None:\n    assert 1 == 1\n",
                }
            ],
        )

        def fail_scan(*args, **kwargs):
            raise AssertionError("scan_module should not run during --dry-run")

        monkeypatch.setattr(ordeal_auto, "scan_module", fail_scan)

        rc = main(["init", "pkg", "--dry-run"])
        captured = capsys.readouterr()

        assert rc == 0
        assert "DRY RUN" in captured.err
        assert "Initial scan:" not in captured.err

    def test_init_reports_lightweight_scan_findings(self, monkeypatch, tmp_path, capsys):
        import ordeal.mutations as mutations
        from ordeal.auto import FunctionResult, ScanResult

        monkeypatch.chdir(tmp_path)
        test_file = tmp_path / "tests" / "test_pkg.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        original = "def test_pkg_value_pinned() -> None:\n    assert 1 == 1\n"
        test_file.write_text(original, encoding="utf-8")

        monkeypatch.setattr(
            mutations,
            "init_project",
            lambda target=None, output_dir="tests", dry_run=False: [
                {
                    "module": "pkg",
                    "status": "generated",
                    "path": str(test_file),
                    "content": original,
                }
            ],
        )
        monkeypatch.setattr(cli, "_install_skill", lambda *, dry_run=False: None)
        monkeypatch.setattr(
            ordeal_auto,
            "scan_module",
            lambda module, **kwargs: ScanResult(
                module=str(module),
                functions=[
                    FunctionResult(
                        name="normalize",
                        passed=True,
                        property_violations=["idempotent (92%)"],
                    ),
                    FunctionResult(
                        name="score",
                        passed=False,
                        error="boom",
                        failing_args={"x": -1},
                        replayable=True,
                        replay_attempts=2,
                        replay_matches=2,
                    ),
                ],
            ),
        )

        def fake_run(cmd, capture_output=None, text=None, env=None, cwd=None, check=False):
            if cmd[:3] == [sys.executable, "-m", "pytest"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[:2] == [sys.executable, "-c"] and "'mutate'" in cmd[2]:
                return SimpleNamespace(returncode=0, stdout="Score: 2/2 (100%)\n", stderr="")
            raise AssertionError(f"Unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        rc = main(["init", "pkg"])
        captured = capsys.readouterr()

        assert rc == 0
        assert "Initial scan: 2 finding(s) across 1 module(s)" in captured.err
        assert "pkg.normalize: idempotent (92%)" in captured.err
        assert "pkg.score: crash safety failed" in captured.err

        report = json.loads(captured.out)
        assert report["initial_scan"]["status"] == "findings found"
        finding = report["initial_scan"]["findings"][0]["summary"]
        assert finding == "pkg.normalize: idempotent (92%)"
        assert report["initial_scan"]["findings"][1]["error"] == "boom"
        assert report["initial_scan"]["available_commands"] == ["ordeal scan pkg --save-artifacts"]

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

    def test_mutate_json_outputs_agent_envelope(self, monkeypatch, capsys):
        import ordeal.mutations as mutations_mod
        from ordeal.mutations import Mutant, MutationResult

        result = MutationResult(target="pkg.mod.normalize")
        result.mutants.append(
            Mutant(
                operator="comparison",
                description="== -> !=",
                line=10,
                col=4,
                source_line="if x == y:",
            )
        )

        monkeypatch.setattr(mutations_mod, "mutate", lambda *args, **kwargs: result)

        rc = main(["mutate", "pkg.mod.normalize", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["tool"] == "mutate"
        assert payload["status"] == "findings"
        assert payload["findings"][0]["kind"] == "mutation"
        assert payload["raw_details"]["targets"][0]["target"] == "pkg.mod.normalize"

    def test_mutate_json_blocked_when_no_tests_found(self, monkeypatch, capsys):
        import ordeal.mutations as mutations_mod
        from ordeal.mutations import NoTestsFoundError

        def fake_mutate(*args, **kwargs):
            raise NoTestsFoundError(
                "no tests",
                target="pkg.mod.normalize",
                suggested_file="tests/test_pkg_mod.py",
            )

        monkeypatch.setattr(mutations_mod, "mutate", fake_mutate)
        monkeypatch.setattr(
            mutations_mod,
            "generate_starter_tests",
            lambda target: "def test_pkg_mod() -> None:\n    pass\n",
        )

        rc = main(["mutate", "pkg.mod.normalize", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert payload["tool"] == "mutate"
        assert payload["status"] == "blocked"
        assert payload["suggested_test_file"] == "tests/test_pkg_mod.py"
        starters = payload["raw_details"]["blockers"][0]["starter_tests"]
        assert starters.startswith("def test_pkg_mod")


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
