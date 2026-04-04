"""Agent-facing JSON contract tests for the CLI."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import ordeal.audit as audit_mod
import ordeal.auto as ordeal_auto
import ordeal.mine as mine_mod
import ordeal.mutations as mutations_mod
import ordeal.state as ordeal_state
import ordeal.trace as trace_mod
from ordeal.audit import CoverageMeasurement, CoverageResult, FunctionAudit, ModuleAudit, Status
from ordeal.cli import main
from ordeal.mine import MinedProperty, MineResult
from ordeal.mutations import Mutant, MutationResult, NoTestsFoundError
from ordeal.trace import Trace, TraceStep


def _make_target_listing_module(name: str = "tests._cli_targets") -> types.ModuleType:
    """Create a small module with callable-surface variety for JSON listing tests."""
    mod = types.ModuleType(name)
    exec(
        "class Env:\n"
        "    def __init__(self):\n"
        "        self.prefix = 'env'\n"
        "\n"
        "    def build_env_vars(self, path: str) -> str:\n"
        "        return f'{self.prefix}:{path}'\n"
        "\n"
        "    async def post_sandbox_setup(self) -> str:\n"
        "        return 'ready'\n"
        "\n"
        "def direct(x: int) -> int:\n"
        "    return x\n"
        "\n"
        "def no_hints(x):\n"
        "    return x\n",
        mod.__dict__,
    )
    return mod


class TestCLIAgentJson:
    def test_scan_json_outputs_agent_envelope(self, monkeypatch, capsys):
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.63,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 5},
            tree=SimpleNamespace(size=1),
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
                    "counterexample": {"input": {"xs": [9, 8, 7]}},
                }
            ],
        )
        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--json", "-n", "10"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "scan"
        assert payload["status"] == "findings"
        assert payload["target"] == "pkg.mod"
        assert payload["suggested_test_file"] == "tests/test_ordeal_regressions.py"
        assert payload["confidence"] == 0.63
        assert payload["findings"][0]["target"] == "pkg.mod.normalize"

    def test_scan_json_marks_unreplayed_crashes_as_speculative(self, monkeypatch, capsys):
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.31,
            functions={"flaky": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 1},
            tree=SimpleNamespace(size=0),
            findings=[],
            frontier={"flaky": ["crash not replayed"]},
            finding_details=[
                {
                    "kind": "crash",
                    "category": "speculative_crash",
                    "function": "flaky",
                    "summary": "flaky: unreplayed crash on random inputs",
                    "error": "boom",
                    "failing_args": {"x": 0},
                    "replayable": False,
                    "replay_attempts": 2,
                    "replay_matches": 0,
                }
            ],
        )
        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--json", "-n", "10"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "exploratory"
        assert payload["findings"][0]["kind"] == "crash"
        assert payload["findings"][0]["details"]["category"] == "speculative_crash"
        assert payload["findings"][0]["details"]["replayable"] is False

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
                        "input": {"xs": [1, 2, 3]},
                        "output": [0.1, 0.2, 0.3],
                        "replayed": [0.2, 0.3, 0.5],
                    },
                )
            ],
        )
        monkeypatch.setattr(mine_mod, "mine", lambda *args, **kwargs: result)

        rc = main(["mine", "ordeal.demo.normalize", "--json", "-n", "10"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "mine"
        assert payload["status"] == "findings"
        assert payload["target"] == "ordeal.demo.normalize"
        assert payload["suggested_test_file"] == "tests/test_ordeal_regressions.py"
        assert payload["confidence"] == 0.9
        assert "regression test" in payload["recommended_action"].lower()

    def test_audit_json_outputs_agent_envelope(self, monkeypatch, capsys):
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

        def fake_audit(*args, **kwargs):
            return ModuleAudit(
                module="ordeal.demo",
                current_test_count=4,
                current_test_lines=40,
                current_coverage=verified,
                migrated_test_count=3,
                migrated_lines=30,
                migrated_coverage=verified,
                mutation_score="8/10 (80%)",
                validation_mode="fast",
                gap_functions=["normalize"],
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
                                "detail": "covered indirectly by test_ordeal_demo_score_roundtrip",
                            }
                        ],
                    ),
                    FunctionAudit(
                        name="score",
                        status="exercised",
                        epistemic="verified",
                        covered_body_lines=2,
                        total_body_lines=2,
                    ),
                ],
                suggestions=["L42 in normalize(): test when x < 0"],
            )

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        rc = main(["audit", "ordeal.demo", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "audit"
        assert payload["status"] == "findings"
        assert payload["findings"]
        assert payload["confidence"] == 1.0
        kinds = {item["kind"] for item in payload["findings"]}
        assert "function_gap" in kinds
        function_gaps = [item for item in payload["findings"] if item["kind"] == "function_gap"]
        assert len(function_gaps) == 1
        assert function_gaps[0]["summary"] == "normalize has no effective tests yet"
        assert payload["suggested_commands"][0] == "ordeal audit ordeal.demo --show-generated"
        assert "Function evidence:" in payload["summary"]
        assert "hidden by default" in payload["summary"]
        assert payload["raw_details"]["function_audits"][0]["module"] == "ordeal.demo"
        assert {item["status"] for item in payload["raw_details"]["function_audits"]} == {
            "uncovered",
            "exploratory",
            "exercised",
        }
        assert (
            payload["raw_details"]["report"]["extra_sections"][0][0] == "Function-Level Evidence"
        )

    def test_scan_json_list_targets_outputs_callable_metadata(
        self, monkeypatch, tmp_path, capsys
    ):
        mod = _make_target_listing_module()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
        monkeypatch.setitem(
            ordeal_auto._REGISTERED_OBJECT_FACTORIES,
            "tests._cli_targets:Env",
            lambda: mod.Env(),
        )

        rc = main(["scan", mod.__name__, "--json", "--list-targets"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "scan"
        assert payload["status"] == "exploratory"
        rows = payload["raw_details"]["targets"]
        env_row = next(row for row in rows if row["name"] == "Env.build_env_vars")
        assert env_row["kind"] == "instance"
        assert env_row["factory_required"] is True
        assert env_row["factory_configured"] is True
        assert env_row["selected"] is True

    def test_audit_json_list_targets_outputs_callable_metadata(
        self, monkeypatch, tmp_path, capsys
    ):
        mod = _make_target_listing_module()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
        monkeypatch.setitem(
            ordeal_auto._REGISTERED_OBJECT_FACTORIES,
            "tests._cli_targets:Env",
            lambda: mod.Env(),
        )
        monkeypatch.setitem(
            ordeal_auto._REGISTERED_OBJECT_SETUPS,
            "tests._cli_targets:Env",
            lambda instance: instance,
        )
        monkeypatch.setitem(
            ordeal_auto._REGISTERED_OBJECT_SCENARIOS,
            "tests._cli_targets:Env",
            lambda instance: instance,
        )

        rc = main(["audit", mod.__name__, "--json", "--list-targets"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "audit"
        assert payload["status"] == "exploratory"
        assert payload["raw_details"]["target_groups"][0]["module"] == mod.__name__
        rows = payload["raw_details"]["targets"]
        env_row = next(row for row in rows if row["name"] == "Env.build_env_vars")
        assert env_row["kind"] == "instance"
        assert env_row["factory_required"] is True
        assert env_row["factory_configured"] is True
        assert env_row["setup_configured"] is True
        assert env_row["scenario_count"] == 1
        assert env_row["runnable"] is True
        assert any(
            row["name"] == "no_hints" and row["skip_reason"] == "missing inferable strategies"
            for row in rows
        )

    def test_audit_json_preserves_method_level_function_names(self, monkeypatch, capsys):
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

        def fake_audit(*args, **kwargs):
            return ModuleAudit(
                module="demo_pkg.envs",
                current_test_count=4,
                current_test_lines=40,
                current_coverage=verified,
                migrated_test_count=3,
                migrated_lines=30,
                migrated_coverage=verified,
                validation_mode="fast",
                function_audits=[
                    FunctionAudit(
                        name="Env.build_env_vars",
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

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        rc = main(["audit", "demo_pkg.envs", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["raw_details"]["function_audits"][0]["name"] == "Env.build_env_vars"
        function_gaps = [item for item in payload["findings"] if item["kind"] == "function_gap"]
        assert function_gaps[0]["function"] == "Env.build_env_vars"
        assert function_gaps[0]["details"]["status"] == "uncovered"

    def test_audit_json_require_direct_tests_blocks_on_non_exercised_functions(
        self, monkeypatch, capsys
    ):
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
            module="ordeal.demo",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            function_audits=[
                FunctionAudit(name="parse", status="uncovered", epistemic="none"),
                FunctionAudit(name="score", status="exploratory", epistemic="inferred"),
            ],
        )
        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        rc = main(["audit", "ordeal.demo", "--json", "--require-direct-tests"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "blocked"
        assert payload["blocking_reason"] == "direct tests required for 2 function(s)"
        assert "Direct test gate: FAIL (1 exploratory, 1 uncovered)" in payload["summary"]
        assert payload["raw_details"]["direct_test_gate"] == {
            "required": True,
            "passed": False,
            "exploratory": ["ordeal.demo.score"],
            "uncovered": ["ordeal.demo.parse"],
        }

    def test_audit_json_uses_config_defaults_and_gate(self, monkeypatch, tmp_path, capsys):
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
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(
            """
[audit]
modules = ["ordeal.demo"]
max_examples = 27
workers = 3
validation_mode = "deep"
include_exploratory_function_gaps = true
require_direct_tests = true
""",
            encoding="utf-8",
        )

        calls: dict[str, object] = {}

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
                function_audits=[
                    FunctionAudit(name="parse", status="uncovered", epistemic="none"),
                    FunctionAudit(name="score", status="exploratory", epistemic="inferred"),
                ],
            )

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        rc = main(["audit", "--json", "--config", str(config_path)])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert calls == {
            "module": "ordeal.demo",
            "test_dir": "tests",
            "max_examples": 27,
            "workers": 3,
            "validation_mode": "deep",
        }
        assert payload["status"] == "blocked"
        assert payload["blocking_reason"] == "direct tests required for 2 function(s)"
        function_gap_summaries = {
            item["summary"] for item in payload["findings"] if item["kind"] == "function_gap"
        }
        assert function_gap_summaries == {
            "parse has no effective tests yet",
            "score is only indirectly exercised by current tests",
        }
        assert payload["raw_details"]["direct_test_gate"] == {
            "required": True,
            "passed": False,
            "exploratory": ["ordeal.demo.score"],
            "uncovered": ["ordeal.demo.parse"],
        }

    def test_audit_json_write_gaps_outputs_gap_stub_artifacts(self, monkeypatch, tmp_path, capsys):
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
            module="ordeal.demo",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            mutation_score="8/10 (80%)",
            validation_mode="fast",
            mutation_gap_stubs=[
                {
                    "target": "ordeal.demo.value",
                    "content": '"""Draft review stubs for audit gaps.\n"""',
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
                            "detail": "covered indirectly by test_ordeal_demo_score_roundtrip",
                        }
                    ],
                ),
            ],
            suggestions=["L42 in normalize(): test when x < 0"],
        )
        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        gap_dir = tmp_path / "audit-gaps"
        rc = main(["audit", "ordeal.demo", "--json", "--write-gaps", str(gap_dir)])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert (gap_dir / "test_ordeal_demo_value_gaps.py").exists()
        assert (gap_dir / "test_ordeal_demo_parse_gaps.py").exists()
        normalized_gap_stub_files = [
            {
                **item,
                "path": Path(str(item["path"])).as_posix(),
            }
            for item in payload["raw_details"]["gap_stub_files"]
        ]
        assert normalized_gap_stub_files == [
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.value",
                "path": (gap_dir / "test_ordeal_demo_value_gaps.py").as_posix(),
                "source": "mutation_gap",
            },
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.parse",
                "path": (gap_dir / "test_ordeal_demo_parse_gaps.py").as_posix(),
                "source": "function_audit",
                "status": "uncovered",
                "epistemic": "none",
            },
        ]
        assert not (gap_dir / "test_ordeal_demo_score_gaps.py").exists()
        assert "hidden by default" in payload["summary"]
        assert any(artifact["kind"] == "gap-stub" for artifact in payload["artifacts"])
        assert any(
            artifact["kind"] == "gap-stub"
            and Path(str(artifact["uri"])).as_posix()
            == (gap_dir / "test_ordeal_demo_value_gaps.py").as_posix()
            for artifact in payload["artifacts"]
        )

    def test_audit_json_uses_configured_gap_output_dir(self, monkeypatch, tmp_path, capsys):
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
        config_path = tmp_path / "ordeal.toml"
        gap_dir = tmp_path / "audit-gaps"
        config_path.write_text(
            f"""
[audit]
modules = ["ordeal.demo"]
write_gaps_dir = "{gap_dir.as_posix()}"
""",
            encoding="utf-8",
        )

        result = ModuleAudit(
            module="ordeal.demo",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            mutation_score="8/10 (80%)",
            validation_mode="fast",
            mutation_gap_stubs=[
                {
                    "target": "ordeal.demo.value",
                    "content": '"""Draft review stubs for audit gaps.\n"""',
                }
            ],
            function_audits=[
                FunctionAudit(name="parse", status="uncovered", epistemic="none"),
            ],
        )
        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        rc = main(["audit", "--json", "--config", str(config_path)])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert (gap_dir / "test_ordeal_demo_value_gaps.py").exists()
        assert (gap_dir / "test_ordeal_demo_parse_gaps.py").exists()
        normalized_gap_stub_files = [
            {
                **item,
                "path": Path(str(item["path"])).as_posix(),
            }
            for item in payload["raw_details"]["gap_stub_files"]
        ]
        assert normalized_gap_stub_files == [
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.value",
                "path": (gap_dir / "test_ordeal_demo_value_gaps.py").as_posix(),
                "source": "mutation_gap",
            },
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.parse",
                "path": (gap_dir / "test_ordeal_demo_parse_gaps.py").as_posix(),
                "source": "function_audit",
                "status": "uncovered",
                "epistemic": "none",
            },
        ]

    def test_audit_json_include_exploratory_function_gaps_surfaces_details(
        self, monkeypatch, tmp_path, capsys
    ):
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
            module="ordeal.demo",
            current_test_count=4,
            current_test_lines=40,
            current_coverage=verified,
            migrated_test_count=3,
            migrated_lines=30,
            migrated_coverage=verified,
            mutation_score="8/10 (80%)",
            validation_mode="fast",
            mutation_gap_stubs=[
                {
                    "target": "ordeal.demo.value",
                    "content": '"""Draft review stubs for audit gaps.\n"""',
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
                            "detail": "covered indirectly by test_ordeal_demo_score_roundtrip",
                        }
                    ],
                ),
            ],
            suggestions=["L42 in normalize(): test when x < 0"],
        )
        monkeypatch.setattr(audit_mod, "audit", lambda *args, **kwargs: result)

        gap_dir = tmp_path / "audit-gaps"
        rc = main(
            [
                "audit",
                "ordeal.demo",
                "--json",
                "--include-exploratory-function-gaps",
                "--write-gaps",
                str(gap_dir),
            ]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        function_gaps = [item for item in payload["findings"] if item["kind"] == "function_gap"]
        assert {item["summary"] for item in function_gaps} == {
            "parse has no effective tests yet",
            "score is only indirectly exercised by current tests",
        }
        normalized_gap_stub_files = [
            {
                **item,
                "path": Path(str(item["path"])).as_posix(),
            }
            for item in payload["raw_details"]["gap_stub_files"]
        ]
        assert normalized_gap_stub_files == [
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.value",
                "path": (gap_dir / "test_ordeal_demo_value_gaps.py").as_posix(),
                "source": "mutation_gap",
            },
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.parse",
                "path": (gap_dir / "test_ordeal_demo_parse_gaps.py").as_posix(),
                "source": "function_audit",
                "status": "uncovered",
                "epistemic": "none",
            },
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.score",
                "path": (gap_dir / "test_ordeal_demo_score_gaps.py").as_posix(),
                "source": "function_audit",
                "status": "exploratory",
                "epistemic": "inferred",
            },
        ]
        assert (gap_dir / "test_ordeal_demo_score_gaps.py").exists()
        assert "hidden by default" not in payload["summary"]

    def test_mutate_json_outputs_agent_envelope(self, monkeypatch, capsys):
        result = MutationResult(
            target="ordeal.demo.normalize",
            mutants=[
                Mutant(
                    operator="arithmetic",
                    description="+ -> -",
                    line=12,
                    col=4,
                    killed=False,
                    source_line="return a + b",
                )
            ],
        )
        monkeypatch.setattr(mutations_mod, "mutate", lambda *args, **kwargs: result)

        rc = main(["mutate", "ordeal.demo.normalize", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "mutate"
        assert payload["status"] == "findings"
        assert payload["suggested_test_file"] == "tests/test_ordeal_regressions.py"
        assert payload["findings"][0]["location"] == "L12:4"
        assert payload["raw_details"]["overall_score"] == 0.0

    def test_mutate_json_reports_blocking_reason_when_no_tests_found(self, monkeypatch, capsys):
        def fake_mutate(*args, **kwargs):
            raise NoTestsFoundError(
                "no tests found",
                target="pkg.mod.compute",
                suggested_file="tests/test_pkg_mod.py",
            )

        monkeypatch.setattr(mutations_mod, "mutate", fake_mutate)
        starter_fn = lambda target: "def test_x():\n    pass\n"  # noqa: E731
        monkeypatch.setattr(mutations_mod, "generate_starter_tests", starter_fn)

        rc = main(["mutate", "pkg.mod.compute", "--json"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "mutate"
        assert payload["status"] == "blocked"
        assert payload["blocking_reason"] == "No tests found for pkg.mod.compute"
        assert payload["suggested_test_file"] == "tests/test_pkg_mod.py"

    def test_replay_json_outputs_agent_envelope(self, monkeypatch, capsys):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="pkg.tests:Chaos",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="step", params={})],
        )
        monkeypatch.setattr(trace_mod.Trace, "load", lambda path: trace)
        monkeypatch.setattr(trace_mod, "replay", lambda loaded: ValueError("boom"))

        rc = main(["replay", "trace.json", "--json"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "replay"
        assert payload["status"] == "reproduced"
        assert payload["confidence"] == 1.0
        assert "shrink" in payload["recommended_action"].lower()
