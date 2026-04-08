"""Agent-facing JSON contract tests for the CLI."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import ordeal.audit as audit_mod
import ordeal.auto as ordeal_auto
import ordeal.cli as cli
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


def _write_bootstrap_target_project(tmp_path: Path) -> str:
    """Write a tiny package whose class target needs audit bootstrap scaffolding."""
    sys.modules.pop("bootstrappkg.envs", None)
    sys.modules.pop("bootstrappkg", None)
    pkg = tmp_path / "bootstrappkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "envs.py").write_text(
        "class ComposableEnv:\n"
        "    def __init__(self, prefix: str = 'demo'):\n"
        "        self.prefix = prefix\n"
        "        self.upload_content = None\n"
        "\n"
        "    def build_env_vars(self, path: str, system_prompt: str | None = None) -> str:\n"
        "        return f'{self.prefix}:{path}'\n"
        "\n"
        "    async def post_sandbox_setup(self) -> str:\n"
        "        return 'ready'\n"
        "\n"
        "    def post_rollout(self, instruction: str, log_path: str | None = None) -> str:\n"
        "        return instruction\n",
        encoding="utf-8",
    )
    return "bootstrappkg.envs"


def _write_harness_hint_project(tmp_path: Path) -> str:
    """Write a tiny package whose tests/docs imply harness hooks."""
    sys.modules.pop("hintpkg.envs", None)
    sys.modules.pop("hintpkg", None)
    pkg = tmp_path / "hintpkg"
    tests_dir = tmp_path / "tests"
    docs_dir = tmp_path / "docs"
    pkg.mkdir()
    tests_dir.mkdir()
    docs_dir.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "envs.py").write_text(
        "class Env:\n"
        "    def __init__(self, prefix: str):\n"
        "        self.prefix = prefix\n"
        "        self.sandbox_client = None\n"
        "\n"
        "    def rollout(self, state: dict[str, str], prompt: str) -> str:\n"
        "        if self.sandbox_client is not None:\n"
        "            self.sandbox_client.execute_command(prompt)\n"
        "        return f'{self.prefix}:{state[\"seed\"]}:{prompt}'\n",
        encoding="utf-8",
    )
    (tests_dir / "support_factories.py").write_text(
        "from hintpkg.envs import Env\n"
        "\n"
        "class FakeSandbox:\n"
        "    def execute_command(self, prompt: str) -> None:\n"
        "        return None\n"
        "\n"
        "def make_env() -> Env:\n"
        "    env = Env('demo')\n"
        "    env.sandbox_client = FakeSandbox()\n"
        "    return env\n"
        "\n"
        "def make_env_state() -> dict[str, str]:\n"
        "    return {'seed': 'cached'}\n"
        "\n"
        "def teardown_env(env: Env) -> None:\n"
        "    env.prefix = 'closed'\n"
        "\n"
        "def sandbox_client():\n"
        "    return object()\n",
        encoding="utf-8",
    )
    (docs_dir / "lifecycle.md").write_text(
        "Cli lifecycle notes for Env.rollout.\n"
        "This target needs state setup and a sandbox client before teardown.\n",
        encoding="utf-8",
    )
    return "hintpkg.envs"


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
        assert payload["raw_details"]["config_suggestions"][0]["section"] == "[[scan]]"
        assert 'module = "pkg.mod"' in payload["raw_details"]["config_suggestions"][0]["snippet"]

    def test_scan_json_save_artifacts_includes_review_bundle_artifacts(
        self,
        monkeypatch,
        tmp_path,
        capsys,
    ):
        monkeypatch.chdir(tmp_path)
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.71,
            functions={"normalize": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 3},
            tree=SimpleNamespace(size=1),
            findings=["normalize: candidate issue on contract-valid inputs"],
            frontier={"normalize": ["candidate issue on contract-valid inputs"]},
            finding_details=[
                {
                    "kind": "crash",
                    "category": "semantic_contract",
                    "function": "normalize",
                    "summary": "command args drift under quoting edge case",
                    "error": "RuntimeError: command drift",
                    "failing_args": {"value": "demo path"},
                    "proof_bundle": {
                        "minimal_reproduction": {
                            "command": (
                                "uv run ordeal check pkg.mod.normalize "
                                "--contract command_arg_stability"
                            ),
                        },
                        "impact": {"summary": "shell command argument stability regression"},
                    },
                }
            ],
        )
        rows = [
            {
                "module": "pkg.mod",
                "source_module": "pkg.mod",
                "name": "Env.rollout",
                "target": "pkg.mod.Env.rollout",
                "kind": "instance",
                "async": "sync",
                "selected": True,
                "factory_required": True,
                "factory_configured": True,
                "factory_source": "mined",
                "setup_configured": True,
                "setup_source": "mined",
                "state_param": "state",
                "state_factory_configured": True,
                "state_factory_source": "mined",
                "teardown_configured": True,
                "teardown_source": "mined",
                "harness": "stateful",
                "harness_source": "mined",
                "scenario_count": 1,
                "scenario_source": "mined",
                "auto_harness": True,
                "contract_checks": ["command_arg_stability"],
                "lifecycle_phase": "rollout",
                "harness_hints": [
                    {
                        "kind": "factory",
                        "suggestion": "use make_env",
                        "evidence": "tests/support_factories.py:5:make_env",
                        "confidence": 0.94,
                        "config": {
                            "section": "[[objects]]",
                            "target": "pkg.mod:Env",
                            "key": "factory",
                            "value": "tests/support_factories.py:5:make_env",
                        },
                    },
                    {
                        "kind": "scenario_pack",
                        "suggestion": "use sandbox scenario library",
                        "evidence": "docs/lifecycle.md mentions sandbox client",
                        "confidence": 0.82,
                        "config": {
                            "section": "[[objects]]",
                            "target": "pkg.mod:Env",
                            "key": "scenarios",
                            "value": ["sandbox"],
                        },
                    },
                ],
                "runnable": True,
                "skip_reason": None,
            }
        ]

        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)
        monkeypatch.setattr(cli, "_callable_listing_rows", lambda *args, **kwargs: rows)

        rc = main(["scan", "pkg.mod", "--json", "--save-artifacts", "-n", "10"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        artifact_kinds = {artifact["kind"] for artifact in payload["artifacts"]}
        expected_kinds = {
            "report",
            "regression",
            "config-suggestion",
            "support-scaffold",
            "proof-bundle",
            "replay-notes",
            "scenario-library",
            "index",
        }
        assert payload["tool"] == "scan"
        assert expected_kinds <= artifact_kinds
        assert payload["raw_details"]["report"]["support_suggestions"][0]["filename"] == (
            "tests/ordeal_support.py"
        )
        assert (
            payload["raw_details"]["report"]["scenario_libraries"][0]["inferred"][0]["name"]
            == "sandbox"
        )

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

    def test_audit_agent_envelope_surfaces_mutation_views_and_config_suggestions(self):
        mod = _make_target_listing_module("tests._cli_audit_surface_targets")
        sys.modules[mod.__name__] = mod
        env_key = "tests._cli_audit_surface_targets:Env"
        ordeal_auto._REGISTERED_OBJECT_FACTORIES[env_key] = lambda: mod.Env()
        ordeal_auto._REGISTERED_OBJECT_SETUPS[env_key] = lambda instance: instance
        ordeal_auto._REGISTERED_OBJECT_SCENARIOS[env_key] = lambda instance: instance
        result = ModuleAudit(
            module="pkg.mod",
            mutation_score="3/4 (75%)",
            mutation_gaps=[
                {
                    "target": "pkg.mod.normalize",
                    "location": "L10",
                    "description": "x < 0 branch survived",
                }
            ],
            function_audits=[
                FunctionAudit(
                    name="normalize",
                    status="uncovered",
                    epistemic="none",
                )
            ],
        )
        result.mutation_targets = [
            {
                "target": "pkg.mod.normalize",
                "status": "exploratory_gaps",
                "score": "3/4 (75%)",
                "score_fraction": 0.75,
                "killed": 3,
                "total": 4,
                "survived": 1,
                "contract": None,
                "promoted_boundary_count": 0,
                "exploratory_survivors": 1,
                "promoted_boundaries": [],
                "weakest_killers": [],
            }
        ]
        result.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(90.0, 10, 1, frozenset({10}), "coverage.py API"),
        )
        result.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(92.0, 10, 0, frozenset(), "coverage.py API"),
        )
        try:
            surface_groups = [
                {
                    "module": mod.__name__,
                    "targets": cli._callable_listing_rows(mod.__name__),
                    "bootstrap_targets": [],
                }
            ]
            envelope = cli._build_audit_agent_envelope(
                [result],
                config_suggestions=[
                    {
                        "title": "Persist audit defaults",
                        "reason": "Keep audit settings versioned.",
                        "filename": "ordeal.toml",
                        "section": "[audit]",
                        "target": "pkg.mod",
                        "snippet": '[audit]\nmodules = ["pkg.mod"]\n',
                        "entries": [{"section": "[audit]", "modules": ["pkg.mod"]}],
                    }
                ],
                surface_groups=surface_groups,
            )
            payload = json.loads(envelope.to_json())

            assert payload["raw_details"]["mutation_views"][0]["module"] == "pkg.mod"
            assert "status" in payload["raw_details"]["mutation_views"][0]
            assert (
                payload["raw_details"]["report"]["config_suggestions"][0]["section"] == "[audit]"
            )
            assert any(
                section[0] == "Mutation Alignment"
                for section in payload["raw_details"]["report"]["extra_sections"]
            )
            assert payload["raw_details"]["surface_map"]["summary"]["entry_count"] >= 1
            entry = next(
                item
                for item in payload["raw_details"]["surface_map"]["entries"]
                if item["name"] == "Env.build_env_vars"
            )
            assert entry["provenance"]["summary"]["declared_count"] >= 1
        finally:
            ordeal_auto._REGISTERED_OBJECT_FACTORIES.pop(
                "tests._cli_audit_surface_targets:Env",
                None,
            )
            ordeal_auto._REGISTERED_OBJECT_SETUPS.pop(
                "tests._cli_audit_surface_targets:Env",
                None,
            )
            ordeal_auto._REGISTERED_OBJECT_SCENARIOS.pop(
                "tests._cli_audit_surface_targets:Env", None
            )
            sys.modules.pop(mod.__name__, None)

    def test_scan_json_replayable_speculative_crash_has_exploratory_summary(
        self, monkeypatch, capsys
    ):
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.31,
            functions={"decode": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 1},
            tree=SimpleNamespace(size=0),
            findings=[],
            frontier={"decode": ["crash replayed but not promoted"]},
            finding_details=[
                {
                    "kind": "crash",
                    "category": "speculative_crash",
                    "function": "decode",
                    "summary": "decode: replayable crash on semi-valid inputs, still exploratory",
                    "error": "boom",
                    "failing_args": {"x": 0},
                    "replayable": True,
                    "replay_attempts": 2,
                    "replay_matches": 2,
                }
            ],
        )
        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--json", "-n", "10"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "exploratory"
        assert (
            payload["findings"][0]["summary"]
            == "decode: replayable crash on semi-valid inputs, still exploratory"
        )

    def test_scan_json_includes_structured_proof_bundle_for_crash(self, monkeypatch, capsys):
        state = SimpleNamespace(
            module="pkg.mod",
            confidence=0.82,
            functions={"divide": object()},
            supervisor_info={"seed": 42, "trajectory_steps": 4},
            tree=SimpleNamespace(size=2),
            findings=["divide: strong candidate issue on contract-valid inputs"],
            frontier={"divide": ["strong candidate issue on contract-valid inputs"]},
            finding_details=[
                {
                    "kind": "crash",
                    "category": "likely_bug",
                    "evidence_class": "candidate_issue",
                    "function": "divide",
                    "summary": "divide: strong candidate issue on contract-valid inputs",
                    "error": "ZeroDivisionError: division by zero",
                    "failing_args": {"a": 1.0, "b": 0.0},
                    "replayable": True,
                    "replay_attempts": 2,
                    "replay_matches": 2,
                    "contract_fit": 0.92,
                    "reachability": 0.8,
                    "realism": 1.0,
                    "proof_bundle": {
                        "version": 2,
                        "witness": {"input": {"a": 1.0, "b": 0.0}, "source": "boundary"},
                        "contract_basis": {
                            "category": "likely_bug",
                            "evidence_class": "candidate_issue",
                            "fit": 0.92,
                            "reachability": 0.8,
                            "realism": 1.0,
                            "fixture_completeness": 1.0,
                            "basis": ["b: reaches boundary mined from code"],
                        },
                        "confidence_breakdown": {
                            "replayability": 1.0,
                            "contract_fit": 0.92,
                            "reachability": 0.8,
                            "realism": 1.0,
                            "fixture_completeness": 1.0,
                        },
                        "minimal_reproduction": {
                            "target": "pkg.mod:divide",
                            "command": (
                                "uv run ordeal scan pkg.mod --mode candidate "
                                "--targets pkg.mod:divide -n 1"
                            ),
                            "python_snippet": (
                                "from importlib import import_module\n"
                                "mod = import_module('pkg.mod')\n"
                                "args = {'a': 1.0, 'b': 0.0}\n"
                                "mod.divide(**args)"
                            ),
                            "direct_call_supported": True,
                            "note": None,
                        },
                        "failure_path": {
                            "target": "pkg.mod.divide",
                            "error_type": "ZeroDivisionError",
                            "error": "division by zero",
                            "traceback": ["_auto_target.py:12:divide"],
                        },
                        "impact": {
                            "summary": "valid inputs reach an unchecked divide-by-zero boundary",
                            "class": "candidate_issue",
                            "sink_categories": [],
                        },
                        "verdict": {
                            "category": "likely_bug",
                            "evidence_class": "candidate_issue",
                            "promoted": True,
                            "demotion_reason": None,
                        },
                    },
                }
            ],
        )
        monkeypatch.setattr(ordeal_state, "explore", lambda *args, **kwargs: state)

        rc = main(["scan", "pkg.mod", "--json", "-n", "10"])

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        proof = payload["findings"][0]["details"]["proof_bundle"]
        assert proof["version"] == 2
        assert proof["verdict"]["promoted"] is True
        assert proof["verdict"]["evidence_class"] == "candidate_issue"
        assert proof["confidence_breakdown"]["contract_fit"] == 0.92
        assert proof["minimal_reproduction"]["target"] == "pkg.mod:divide"

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
        assert payload["raw_details"]["config_suggestions"][0]["section"] == "[audit]"
        assert (
            'modules = ["ordeal.demo"]'
            in payload["raw_details"]["config_suggestions"][0]["snippet"]
        )
        assert {item["status"] for item in payload["raw_details"]["function_audits"]} == {
            "uncovered",
            "exploratory",
            "exercised",
        }
        assert (
            payload["raw_details"]["report"]["extra_sections"][0][0] == "Function-Level Evidence"
        )

    def test_scan_json_list_targets_outputs_callable_metadata(self, monkeypatch, tmp_path, capsys):
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

    def test_scan_json_list_targets_expands_package_root_lazy_exports(self, capsys):
        rc = main(["scan", "ordeal", "--json", "--list-targets"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        rows = payload["raw_details"]["targets"]
        names = {row["name"] for row in rows}
        assert "scan_module" in names
        assert "fuzz" in names
        assert "mutate" in names

    def test_scan_json_list_targets_marks_selected_rows_for_cli_target_selectors(self, capsys):
        rc = main(
            [
                "scan",
                "ordeal",
                "--json",
                "--list-targets",
                "--target",
                "mutate",
                "--target",
                "audit_*",
            ]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        rows = payload["raw_details"]["targets"]
        by_name = {row["name"]: row for row in rows}
        assert by_name["mutate"]["selected"] is True
        assert by_name["audit_report"]["selected"] is True
        assert by_name["scan_module"]["selected"] is False

    def test_scan_json_list_targets_includes_surface_map(
        self,
        monkeypatch,
        tmp_path,
        capsys,
    ):
        module_name = _write_harness_hint_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        rc = main(["scan", module_name, "--json", "--list-targets"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        surface_map = payload["raw_details"]["surface_map"]
        assert surface_map["summary"]["entry_count"] >= 1
        entry = next(item for item in surface_map["entries"] if item["name"] == "Env.rollout")
        assert entry["symbol"]["owner"] == "Env"
        assert entry["symbol"]["member"] == "rollout"
        assert entry["visibility"] == "public"
        assert entry["execution"]["can_execute_now"] is True
        assert entry["support"]["harness"]["auto"] is True
        assert "shell" in entry["contracts"]["sink_categories"]
        assert entry["contracts"]["lifecycle_phase"] == "rollout"
        assert entry["evidence"]["tests"]["supporting_hints"]
        assert entry["evidence"]["docs"]["files"]
        assert entry["provenance"]["primary_basis"] == "observed"
        assert entry["provenance"]["summary"]["observed_count"] >= 2
        assert entry["provenance"]["summary"]["inferred_count"] >= 1
        sections = {item["section"] for item in payload["raw_details"]["config_suggestions"]}
        assert {"[[scan]]", "[[objects]]", "[[audit.targets]]"} <= sections

    def test_scan_json_reports_surface_sampling_for_package_root(self, monkeypatch, capsys):
        rows = [
            {
                "module": "ordeal",
                "source_module": f"ordeal.source_{index % 4}",
                "name": f"target_{index}",
                "target": f"ordeal.target_{index}",
                "selected": True,
                "runnable": True,
            }
            for index in range(12)
        ]
        monkeypatch.setattr(cli, "_callable_listing_rows", lambda *args, **kwargs: rows)
        monkeypatch.setattr(
            cli,
            "_package_root_scan_sample",
            lambda *args, **kwargs: {
                "kind": "package_root_sample",
                "module": "ordeal",
                "limit": 10,
                "sampled": 2,
                "total_runnable": 12,
                "source_modules": 4,
                "targets": ["target_1", "target_9"],
            },
        )
        monkeypatch.setattr(
            ordeal_state,
            "explore",
            lambda *args, **kwargs: SimpleNamespace(
                module="ordeal",
                confidence=1.0,
                functions={"target_1": object(), "target_9": object()},
                supervisor_info={"seed": 42, "trajectory_steps": 2},
                tree=SimpleNamespace(size=1),
                findings=[],
                frontier={},
                skipped=[],
            ),
        )

        rc = main(["scan", "ordeal", "--json", "-n", "1"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["suggested_commands"][0] == "ordeal scan ordeal --list-targets"
        assert (
            payload["raw_details"]["report"]["summary"][1]
            == "Surface sampling: 2/12 runnable exports checked"
        )
        assert payload["raw_details"]["config_suggestions"][0]["section"] == "[[scan]]"
        assert (
            'targets = ["target_1", "target_9"]'
            in payload["raw_details"]["config_suggestions"][0]["snippet"]
        )
        assert payload["raw_details"]["state"]["supervisor_info"]["scan_sampling"] == {
            "kind": "package_root_sample",
            "module": "ordeal",
            "limit": 10,
            "sampled": 2,
            "total_runnable": 12,
            "source_modules": 4,
            "targets": ["target_1", "target_9"],
        }

    def test_check_json_explicit_contract_outputs_config_suggestions(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "pkg_mod", raising=False)
        (tmp_path / "pkg_mod.py").write_text(
            "def build_command(path: str = 'demo files/input.txt') -> list[str]:\n"
            "    return ['cp', path, '/tmp']\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            ordeal_auto,
            "_evaluate_contract_checks",
            lambda *args, **kwargs: ([], []),
        )

        rc = main(
            [
                "check",
                "pkg_mod.build_command",
                "--contract",
                "command_arg_stability",
                "--json",
            ]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "check"
        assert payload["status"] == "ok"
        assert payload["raw_details"]["config_suggestions"]
        snippets = [item["snippet"] for item in payload["raw_details"]["config_suggestions"]]
        assert any("[[contracts]]" in snippet for snippet in snippets)
        assert any("build_command" in snippet for snippet in snippets)
        assert any('checks = ["command_arg_stability"]' in snippet for snippet in snippets)

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
        sections = {item["section"] for item in payload["raw_details"]["config_suggestions"]}
        assert {"[audit]", "[[audit.targets]]"} <= sections
        assert any(
            row["name"] == "no_hints" and row["skip_reason"] == "missing inferable strategies"
            for row in rows
        )

    def test_audit_json_bootstrap_targets_include_review_scaffold_metadata(
        self,
        monkeypatch,
        tmp_path,
        capsys,
    ):
        module_name = _write_bootstrap_target_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(cli, "_callable_listing_rows", lambda *args, **kwargs: [])

        rc = main(["audit", module_name, "--json", "--list-targets"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["tool"] == "audit"
        assert payload["status"] == "exploratory"
        assert payload["raw_details"]["target_groups"][0]["module"] == module_name
        row = payload["raw_details"]["bootstrap_targets"][0]
        assert row["target"] == f"{module_name}:ComposableEnv"
        assert row["support_module"] == "tests.ordeal_support"
        assert row["support_path"] == "tests/ordeal_support.py"
        assert row["support_factory"] == "tests.ordeal_support:make_composable_env"
        assert row["support_setup"] == "tests.ordeal_support:prime_composable_env"
        assert row["support_teardown"] == "tests.ordeal_support:cleanup_composable_env"
        assert row["support_scenarios"] == [
            "tests.ordeal_support:scenario_empty_instruction",
            "tests.ordeal_support:scenario_missing_log_file",
            "tests.ordeal_support:scenario_no_system_prompt",
            "tests.ordeal_support:scenario_quote_paths",
            "tests.ordeal_support:scenario_space_paths",
        ]
        sections = {item["section"] for item in payload["raw_details"]["config_suggestions"]}
        assert "[[audit.targets]]" in sections
        assert payload["raw_details"]["bootstrap_suggestions"][0]["filename"] == (
            "tests/ordeal_support.py"
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

        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["tool"] == "mutate"
        assert payload["status"] == "findings"
        assert payload["suggested_test_file"] == "tests/test_ordeal_regressions.py"
        assert payload["findings"][0]["location"] == "L12:4"
        assert payload["raw_details"]["overall_score"] == 0.0
        assert payload["raw_details"]["surface_map"]["summary"]["entry_count"] >= 1
        entry = next(
            item
            for item in payload["raw_details"]["surface_map"]["entries"]
            if item["name"] == "normalize"
        )
        assert "inferred_count" in entry["provenance"]["summary"]

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
