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

        assert state.findings == ["normalize: crashes on random inputs"]
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

        explored = explore_scan(
            state,
            targets=["pkg.mod:Env.build_command"],
            object_factories={"pkg.mod:Env": factory},
            object_setups={"pkg.mod:Env": setup},
            contract_checks={},
        )

        assert explored is state
        assert calls["module"] == "pkg.mod"
        assert calls["targets"] == ["pkg.mod:Env.build_command"]
        assert calls["object_factories"] == {"pkg.mod:Env": factory}
        assert calls["object_setups"] == {"pkg.mod:Env": setup}
