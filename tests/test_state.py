"""Tests for ordeal.state serialization and agent-facing persistence."""

from __future__ import annotations

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
