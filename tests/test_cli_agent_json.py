"""Agent-facing JSON contract tests for the CLI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import ordeal.audit as audit_mod
import ordeal.mine as mine_mod
import ordeal.mutations as mutations_mod
import ordeal.state as ordeal_state
import ordeal.trace as trace_mod
from ordeal.audit import CoverageMeasurement, CoverageResult, FunctionAudit, ModuleAudit, Status
from ordeal.cli import main
from ordeal.mine import MinedProperty, MineResult
from ordeal.mutations import Mutant, MutationResult, NoTestsFoundError
from ordeal.trace import Trace, TraceStep


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
        assert payload["suggested_commands"][0] == "ordeal audit ordeal.demo --show-generated"
        assert "Function evidence:" in payload["summary"]
        assert payload["raw_details"]["function_audits"][0]["module"] == "ordeal.demo"
        assert (
            payload["raw_details"]["report"]["extra_sections"][0][0] == "Function-Level Evidence"
        )

    def test_audit_json_write_gaps_outputs_gap_stub_artifacts(
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
                )
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
        assert payload["raw_details"]["gap_stub_files"] == [
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.value",
                "path": str(gap_dir / "test_ordeal_demo_value_gaps.py"),
                "source": "mutation_gap",
            },
            {
                "module": "ordeal.demo",
                "target": "ordeal.demo.parse",
                "path": str(gap_dir / "test_ordeal_demo_parse_gaps.py"),
                "source": "function_audit",
                "status": "uncovered",
                "epistemic": "none",
            },
        ]
        assert any(artifact["kind"] == "gap-stub" for artifact in payload["artifacts"])
        assert any(
            artifact["kind"] == "gap-stub"
            and artifact["uri"] == str(gap_dir / "test_ordeal_demo_value_gaps.py").replace(
                "\\", "/"
            )
            for artifact in payload["artifacts"]
        )

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
