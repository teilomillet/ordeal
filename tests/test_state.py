"""Tests for ordeal.state serialization and agent-facing persistence."""

from __future__ import annotations

from ordeal.auto import FunctionResult, ScanResult
from ordeal.state import ExplorationState


class TestExplorationStateSerialization:
    def test_to_json_round_trip_preserves_supervisor_info(self):
        state = ExplorationState("pkg.mod")
        fn = state.function("normalize")
        fn.mined = True
        fn.properties = [{"name": "idempotent", "universal": False}]
        fn.scanned = True
        fn.crash_free = True
        state.supervisor_info = {
            "seed": 42,
            "trajectory_steps": 5,
            "unique_states": 3,
            "patch_io": True,
        }

        restored = ExplorationState.from_json(state.to_json())

        assert restored.supervisor_info["seed"] == 42
        assert restored.supervisor_info["trajectory_steps"] == 5
        assert restored.supervisor_info["patch_io"] is True

    def test_finding_details_separate_replayed_and_unreplayed_crashes(self):
        state = ExplorationState("pkg.mod")

        replayed = state.function("normalize")
        replayed.scanned = True
        replayed.crash_free = False
        replayed.scan_error = "boom"
        replayed.scan_replayable = True
        replayed.scan_crash_category = "likely_bug"

        unreplayed = state.function("flaky")
        unreplayed.scanned = True
        unreplayed.crash_free = False
        unreplayed.scan_error = "boom"
        unreplayed.scan_replayable = False
        unreplayed.scan_crash_category = "speculative_crash"

        assert state.findings == ["normalize: crashes on realistic inputs"]
        assert "flaky: unreplayed crash on random inputs" in state.exploratory_findings

        categories = {
            detail["function"]: detail["category"]
            for detail in state.finding_details
            if detail["kind"] == "crash"
        }
        assert categories == {
            "normalize": "likely_bug",
            "flaky": "speculative_crash",
        }

        restored = ExplorationState.from_json(state.to_json())
        assert restored.functions["normalize"].scan_crash_category == "likely_bug"
        assert restored.functions["flaky"].scan_crash_category == "speculative_crash"

    def test_replayable_speculative_crash_stays_exploratory_but_not_unreplayed(self):
        state = ExplorationState("pkg.mod")

        replayed = state.function("decode")
        replayed.scanned = True
        replayed.crash_free = False
        replayed.scan_error = "boom"
        replayed.scan_replayable = True
        replayed.scan_crash_category = "speculative_crash"

        assert (
            "decode: replayable crash on semi-valid inputs, still exploratory"
            in state.exploratory_findings
        )
        detail = next(item for item in state.finding_details if item["function"] == "decode")
        assert (
            detail["summary"]
            == "decode: replayable crash on semi-valid inputs, still exploratory"
        )

    def test_finding_details_preserve_scan_evidence_payloads(self):
        state = ExplorationState("pkg.mod")
        fs = state.function("render")
        fs.scanned = True
        fs.crash_free = False
        fs.scan_crash_category = "coverage_gap"
        fs.scan_contract_fit = 0.72
        fs.scan_reachability = 0.85
        fs.scan_realism = 1.0
        fs.scan_sink_signal = 1.0
        fs.scan_sink_categories = ["shell", "path"]
        fs.scan_input_sources = [{"source": "test", "evidence": "test_mod.py:10"}]
        fs.scan_input_source = "test"
        fs.scan_proof_bundle = {"likely_impact": "command construction may break execution"}

        detail = next(item for item in state.finding_details if item["function"] == "render")
        assert detail["sink_categories"] == ["shell", "path"]
        assert detail["input_sources"] == [{"source": "test", "evidence": "test_mod.py:10"}]
        assert (
            detail["proof_bundle"]["likely_impact"] == "command construction may break execution"
        )

    def test_lifecycle_contract_findings_promote_above_other_scan_output(self):
        state = ExplorationState("pkg.mod")

        lifecycle = state.function("cleanup")
        lifecycle.contract_violations = ["cleanup handlers were not all attempted"]
        lifecycle.contract_violation_details = [
            {
                "kind": "contract",
                "category": "lifecycle_contract",
                "summary": "cleanup handlers were not all attempted",
                "lifecycle_phase": "cleanup",
                "lifecycle_probe": {"phase": "cleanup", "attempts": ["cleanup_alpha"]},
            }
        ]

        crash = state.function("render")
        crash.scanned = True
        crash.crash_free = False
        crash.scan_crash_category = "likely_bug"
        crash.scan_error = "boom"
        crash.scan_contract_fit = 0.9
        crash.scan_reachability = 0.8

        assert state.findings[0] == "cleanup: violates an explicit lifecycle contract"

        details = state.finding_details
        assert details[0]["category"] == "lifecycle_contract"
        assert details[0]["function"] == "cleanup"
        assert details[0]["lifecycle_signal"] == 1.0

    def test_explore_scan_surfaces_contract_violations(self, monkeypatch):
        import ordeal.auto as auto_mod
        from ordeal.state import explore_scan

        scan_result = ScanResult(
            module="pkg.mod",
            functions=[
                FunctionResult(
                    name="build_command",
                    passed=True,
                    contract_violations=["shell-safe path quoting"],
                    contract_violation_details=[
                        {
                            "kind": "contract",
                            "category": "semantic_contract",
                            "name": "shell-safe path quoting",
                            "summary": "shell-safe path quoting",
                            "failing_args": {"path": "a b"},
                        }
                    ],
                )
            ],
        )
        monkeypatch.setattr(auto_mod, "scan_module", lambda *args, **kwargs: scan_result)

        state = ExplorationState("pkg.mod")
        explored = explore_scan(state, contract_checks={})

        fs = explored.functions["build_command"]
        assert fs.contract_violations == ["shell-safe path quoting"]
        assert any(
            "shell-safe path quoting" in finding for finding in explored.exploratory_findings
        )
        assert any(
            detail["category"] == "semantic_contract" for detail in explored.finding_details
        )

    def test_explore_scan_forwards_method_targets_and_object_factories(self, monkeypatch):
        import ordeal.auto as auto_mod
        from ordeal.state import explore_scan

        calls: dict[str, object] = {}

        def fake_scan_module(module, **kwargs):
            calls["module"] = module
            calls.update(kwargs)
            return ScanResult(module="pkg.mod")

        monkeypatch.setattr(auto_mod, "scan_module", fake_scan_module)

        state = ExplorationState("pkg.mod")

        def factory() -> object:
            return object()

        def setup(instance: object) -> None:
            return None

        def scenario(instance: object) -> None:
            return None

        explored = explore_scan(
            state,
            targets=["pkg.mod:Env.build_command"],
            object_factories={"pkg.mod:Env": factory},
            object_setups={"pkg.mod:Env": setup},
            object_scenarios={"pkg.mod:Env": scenario},
            contract_checks={},
        )

        assert explored is state
        assert calls["module"] == "pkg.mod"
        assert calls["targets"] == ["pkg.mod:Env.build_command"]
        assert calls["object_factories"] == {"pkg.mod:Env": factory}
        assert calls["object_setups"] == {"pkg.mod:Env": setup}
        assert calls["object_scenarios"] == {"pkg.mod:Env": scenario}

    def test_explore_forwards_scenarios_through_runtime_paths(self, monkeypatch):
        import ordeal.state as state_mod
        from ordeal.state import ExplorationState, explore

        calls: dict[str, dict[str, object]] = {}

        def fake_mine(state, **kwargs):
            calls["mine"] = kwargs
            return state

        def fake_scan(state, **kwargs):
            calls["scan"] = kwargs
            return state

        def fake_chaos(state, **kwargs):
            calls["chaos"] = kwargs
            return state

        monkeypatch.setattr(state_mod, "explore_mine", fake_mine)
        monkeypatch.setattr(state_mod, "explore_scan", fake_scan)
        monkeypatch.setattr(state_mod, "explore_chaos", fake_chaos)

        def factory() -> object:
            return object()

        def setup(instance: object) -> None:
            return None

        def scenario(instance: object) -> None:
            return None

        explored = explore(
            "pkg.mod",
            state=ExplorationState("pkg.mod"),
            scan_targets=["pkg.mod:Env.build_command"],
            scan_object_factories={"pkg.mod:Env": factory},
            scan_object_setups={"pkg.mod:Env": setup},
            scan_object_scenarios={"pkg.mod:Env": scenario},
        )

        assert explored.module == "pkg.mod"
        assert calls["mine"]["object_scenarios"] == {"pkg.mod:Env": scenario}
        assert calls["scan"]["object_scenarios"] == {"pkg.mod:Env": scenario}
        assert calls["chaos"]["object_scenarios"] == {"pkg.mod:Env": scenario}
