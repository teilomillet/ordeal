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
