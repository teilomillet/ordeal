"""Evidence-closure map tests."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import ordeal.reliability as reliability
from ordeal.reliability import (
    RELIABILITY_MAP_SCHEMA,
    _build_reliability_map,
    _plan_diff,
    _write_reliability_map,
)


def _write_project(root: Path) -> None:
    sys.modules.pop("reliability_fixture", None)
    sys.modules.pop("reliability_fixture.workflows", None)
    package = root / "reliability_fixture"
    tests = root / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "workflows.py").write_text(
        "from typing import TypedDict\n"
        "\n"
        "class ChargeRequest(TypedDict):\n"
        "    order_id: str\n"
        "\n"
        "def charge(client: object, request: ChargeRequest) -> object:\n"
        '    """Retry a transient HTTP charge without duplicating an order."""\n'
        "    for attempt in range(3):\n"
        "        try:\n"
        "            return client.post('/charge', json=request)\n"
        "        except TimeoutError:\n"
        "            if attempt == 2:\n"
        "                raise\n"
        "\n"
        "def cached_lookup(cache: object, key: str) -> object:\n"
        "    value = cache.get(key)\n"
        "    return value if value is not None else cache.fallback(key)\n"
        "\n"
        "def persist(path: object, payload: str) -> None:\n"
        "    with open(path, 'w') as handle:\n"
        "        handle.write(payload)\n"
        "\n"
        "def commit(store: object) -> None:\n"
        "    try:\n"
        "        store.transaction().commit()\n"
        "    except Exception:\n"
        "        store.rollback()\n"
        "\n"
        "def run_worker(command: list[str]) -> bytes:\n"
        "    return subprocess.check_output(command)\n"
        "\n"
        "def load_model(torch: object, checkpoint: str) -> object:\n"
        "    return torch.load(checkpoint)\n"
        "\n"
        "def predict(batch: 'Tensor', feature_names: list[str]) -> 'Tensor':\n"
        "    assert batch.shape[0] > 0\n"
        "    dtype = batch.dtype\n"
        "    columns = feature_names\n"
        "    finite = batch.isfinite()\n"
        "    return batch.reshape(batch.shape[0], len(columns)).astype(dtype)\n"
        "\n"
        "def train_batch(dataloader: object, checkpoint: str) -> object:\n"
        "    batch = next(iter(dataloader))\n"
        "    return batch, checkpoint\n",
        encoding="utf-8",
    )
    (tests / "test_workflows.py").write_text(
        "def test_charge_contract():\n    assert charge is not None\n",
        encoding="utf-8",
    )


def _cell_operation(payload: dict[str, object], cell: dict[str, object]) -> str:
    operations = {
        item["id"]: item["target"]
        for item in payload["operations"]  # type: ignore[union-attr]
    }
    return str(operations[cell["operation_id"]])


def _cell_property(payload: dict[str, object], cell: dict[str, object]) -> str:
    properties = {
        item["id"]: item["name"]
        for item in payload["properties"]  # type: ignore[union-attr]
    }
    return str(properties[cell["property_id"]])


def test_reliability_map_detects_production_seams_and_ml_profiles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    state = SimpleNamespace(
        functions={},
        supervisor_info={"config_suggestions": []},
    )

    payload = _build_reliability_map("reliability_fixture", state, [])

    assert payload["schema"] == RELIABILITY_MAP_SCHEMA
    seams = {seam for operation in payload["operations"] for seam in operation["seams"]}
    assert {
        "retry",
        "fallback",
        "cache",
        "file",
        "http",
        "subprocess",
        "transaction",
        "recovery",
        "model_loading",
    } <= seams
    profiles = {
        profile for operation in payload["operations"] for profile in operation["ml_data_profiles"]
    }
    assert {
        "shape_drift",
        "dtype_drift",
        "non_finite_values",
        "partial_batches",
        "stale_artifacts",
        "feature_order",
    } <= profiles
    assert payload["summary"]["not_exercised"] == payload["summary"]["cells"]
    assert payload["summary"]["pass"] == 0
    assert payload["summary"]["fail"] == 0
    assert payload["next_experiment"]["auto_runnable"] is True
    assert "<" not in payload["next_experiment"]["command"]
    assert all(
        hypothesis["epistemic_status"] == "hypothesis" for hypothesis in payload["properties"]
    )
    charge = next(item for item in payload["operations"] if item["target"].endswith("charge"))
    assert {"source", "types", "schema", "documentation", "test"} <= set(charge["provenance"])
    assert charge["test_evidence"]
    predict = next(item for item in payload["operations"] if item["target"].endswith("predict"))
    assert "assertion" in predict["provenance"]

    sys.modules.pop("reliability_fixture", None)
    sys.modules.pop("reliability_fixture.workflows", None)


def test_reliability_map_keeps_tool_failures_blocked(tmp_path: Path, monkeypatch) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    state = SimpleNamespace(
        functions={
            "charge": SimpleNamespace(
                scan_limitation_kind="strategy_generation",
                scan_blocking_reason="typing.Any could not be generated",
                contract_violations=[],
                faults_tested=[],
                chaos_tested=False,
            )
        },
        supervisor_info={},
    )

    payload = _build_reliability_map("reliability_fixture", state, [])
    charge_cells = [
        cell for cell in payload["cells"] if _cell_operation(payload, cell).endswith("charge")
    ]

    assert charge_cells
    assert {cell["status"] for cell in charge_cells} == {"NOT EXERCISED"}
    assert all("typing.Any" in str(cell["blocking_reason"]) for cell in charge_cells)
    assert payload["summary"]["blocked"] >= len(charge_cells)


def test_plan_diff_tracks_new_removed_and_changed_cells() -> None:
    previous = {
        "cells": [
            {"id": "kept", "status": "NOT EXERCISED"},
            {"id": "removed", "status": "PASS"},
        ]
    }
    current = {
        "cells": [
            {"id": "kept", "status": "PASS"},
            {"id": "new", "status": "NOT EXERCISED"},
        ]
    }

    diff = _plan_diff(current, previous)

    assert diff == {
        "new_cell_count": 1,
        "new_cells": ["new"],
        "removed_cell_count": 1,
        "removed_cells": ["removed"],
        "status_change_count": 1,
        "status_changes": [{"id": "kept", "before": "NOT EXERCISED", "after": "PASS"}],
        "new_operation_count": 0,
        "new_operations": [],
        "removed_operation_count": 0,
        "removed_operations": [],
        "source_change_count": 0,
        "source_changes": [],
        "truncated": False,
        "retained_cells": 1,
    }


def test_plan_diff_reports_source_changes_between_saved_commits() -> None:
    previous = {
        "cells": [],
        "operations": [{"id": "operation", "target": "pkg.run", "source_sha256": "before"}],
    }
    current = {
        "cells": [],
        "operations": [{"id": "operation", "target": "pkg.run", "source_sha256": "after"}],
    }

    diff = _plan_diff(current, previous)

    assert diff["source_change_count"] == 1
    assert diff["source_changes"] == [
        {
            "id": "operation",
            "target": "pkg.run",
            "before_sha256": "before",
            "after_sha256": "after",
        }
    ]


def test_reliability_cells_only_pass_or_fail_from_runtime_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    state = SimpleNamespace(
        functions={
            "charge": SimpleNamespace(
                scan_limitation_kind=None,
                contract_violations=[],
                contract_violation_details=[],
                scan_crash_category=None,
                scan_replayable=None,
                scan_proof_bundle=None,
                scan_sink_categories=[],
                faults_tested=["timeout"],
                chaos_tested=True,
            ),
            "commit": SimpleNamespace(
                scan_limitation_kind=None,
                contract_violations=["atomicity violated"],
                contract_violation_details=[
                    {"category": "semantic_contract", "fault": "commit_failure"}
                ],
                faults_tested=[],
                chaos_tested=False,
            ),
        },
        supervisor_info={
            "reliability_observations": [
                {
                    "target": "reliability_fixture.workflows.persist",
                    "fault": "disk_full",
                    "property": reliability._runtime_fault_property("disk_full"),
                    "status": "PASS",
                    "blocking_reason": None,
                }
            ]
        },
    )

    payload = _build_reliability_map("reliability_fixture", state, [])

    persist_disk_full = next(
        cell
        for cell in payload["cells"]
        if _cell_operation(payload, cell).endswith("persist")
        and cell["fault"] == "disk_full"
        and _cell_property(payload, cell) == reliability._runtime_fault_property("disk_full")
    )
    commit_cells = {
        str(cell["fault"]): cell
        for cell in payload["cells"]
        if _cell_operation(payload, cell).endswith("commit")
    }
    assert persist_disk_full["status"] == "PASS"
    assert commit_cells["commit_failure"]["status"] == "FAIL"
    assert commit_cells["restart"]["status"] == "NOT EXERCISED"
    assert payload["summary"]["pass"] >= 1
    assert payload["summary"]["fail"] >= 1


def test_unrelated_function_failure_does_not_fail_an_unexercised_fault() -> None:
    function_state = SimpleNamespace(
        scan_limitation_kind=None,
        contract_violation_details=[{"category": "semantic_contract"}],
        faults_tested=[],
        chaos_tested=False,
    )

    assert reliability._cell_status(function_state, "timeout") == ("NOT EXERCISED", None)


def test_nested_surface_execution_blocker_prevents_automatic_probe() -> None:
    rows = [
        {
            "name": "Worker.run",
            "target": "pkg.Worker.run",
            "execution": {
                "can_execute_now": False,
                "blocking_reason": "missing object factory",
            },
        }
    ]

    assert reliability._surface_blocker(rows, "Worker.run", "run") == ("missing object factory")
    assert "no verified runnable harness" in str(
        reliability._surface_blocker([], "Worker.run", "run")
    )


def test_fault_probe_records_fault_specific_pass_and_fail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sys.modules.pop("fault_probe_fixture", None)
    (tmp_path / "fault_probe_fixture.py").write_text(
        "def persist(path: str) -> None:\n"
        "    try:\n"
        "        with open(path, 'w') as handle:\n"
        "            handle.write('ok')\n"
        "    except (OSError, ValueError):\n"
        "        return None\n"
        "\n"
        "def persist_bug(path: str) -> None:\n"
        "    with open(path, 'w') as handle:\n"
        "        handle.write('ok')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    passed = reliability._run_fault_probe(
        "fault_probe_fixture",
        "persist",
        "disk_full",
        max_examples=2,
    )
    failed = reliability._run_fault_probe(
        "fault_probe_fixture",
        "persist_bug",
        "disk_full",
        max_examples=2,
    )

    assert passed["status"] == "PASS"
    assert passed["injection"]["hits"] > 0
    assert failed["status"] == "FAIL"
    assert failed["injection"]["hits"] > 0


def test_expected_precondition_exception_does_not_become_fault_probe_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sys.modules.pop("fault_precondition_fixture", None)
    (tmp_path / "fault_precondition_fixture.py").write_text(
        "def persist(path: str) -> None:\n"
        '    """Raise OSError when the write precondition is unavailable."""\n'
        "    with open(path, 'w') as handle:\n"
        "        handle.write('ok')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    observation = reliability._run_fault_probe(
        "fault_precondition_fixture",
        "persist",
        "disk_full",
        max_examples=2,
        scan_kwargs={"expected_preconditions": {"persist": ["OSError"]}},
    )

    assert observation["injection"]["hits"] > 0
    assert observation["evidence"]["verdict"] == "expected_precondition_failure"
    assert observation["status"] == "NOT EXERCISED"
    assert "did not return cleanly" in observation["blocking_reason"]


def test_changed_files_include_worktree_and_untracked_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "config", "user.name", "Evidence Test"], check=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], check=True)
    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "untracked.py").write_text("VALUE = 3\n", encoding="utf-8")

    assert reliability._changed_files("HEAD") == {"tracked.py", "untracked.py"}


def test_persisted_map_diffs_runs_and_carries_hints_forward(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    state = SimpleNamespace(
        functions={},
        supervisor_info={"config_suggestions": [{"title": "keep me"}]},
    )
    path = tmp_path / ".ordeal" / "evidence-plans" / "fixture.json"
    first = _build_reliability_map("reliability_fixture", state, [])
    _write_reliability_map(path, first)

    second = _build_reliability_map(
        "reliability_fixture",
        SimpleNamespace(functions={}, supervisor_info={}),
        [],
        previous_path=path,
    )

    assert second["continuity"]["retained_cells"] == first["summary"]["cells"]
    assert second["continuity"]["new_cells"] == []
    assert second["continuity"]["removed_cells"] == []
    assert second["continuity"]["carried_forward_hints"]["config_suggestions"] == [
        {"title": "keep me"}
    ]
    assert second["productive_hints"]["config_suggestions"] == [{"title": "keep me"}]


def test_changed_files_raise_operation_priority(tmp_path: Path, monkeypatch) -> None:
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    monkeypatch.setattr(
        reliability, "_changed_files", lambda _base: {"reliability_fixture/workflows.py"}
    )
    state = SimpleNamespace(functions={}, supervisor_info={})

    payload = _build_reliability_map(
        "reliability_fixture",
        state,
        [],
        base_ref="origin/main",
    )

    assert payload["operations"]
    assert all(operation["changed_since_base"] for operation in payload["operations"])
    experiments = {item["id"]: item for item in payload["experiments"]}
    properties = {item["id"]: item["name"] for item in payload["properties"]}
    assert all(
        experiments[cell["next_experiment_id"]]["engine"] == "differential"
        for cell in payload["cells"]
        if not properties[cell["property_id"]].startswith(
            reliability.RUNTIME_FAULT_PROPERTY_PREFIX
        )
    )
    assert all(
        experiments[cell["next_experiment_id"]]["reason"] == "fault_specific_runtime_probe"
        for cell in payload["cells"]
        if properties[cell["property_id"]].startswith(reliability.RUNTIME_FAULT_PROPERTY_PREFIX)
    )


def test_compose_experiment_requires_explicit_opt_in_and_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    kwargs = {
        "module": "pkg.service",
        "target": "pkg.service.fetch",
        "selector": "fetch",
        "seam": "http",
        "status": "NOT EXERCISED",
        "blocker": None,
        "base_ref": None,
        "changed": False,
        "has_tests": False,
    }

    without_opt_in = reliability._next_experiment(**kwargs, allow_service_faults=False)
    (tmp_path / "ordeal.toml").write_text("[compose]\nproject_dir = '.'\n", encoding="utf-8")
    with_opt_in = reliability._next_experiment(**kwargs, allow_service_faults=True)

    assert without_opt_in["engine"] == "scan"
    assert without_opt_in["auto_runnable"] is False
    assert without_opt_in["safety"] == "review_required"
    assert with_opt_in["engine"] == "compose"
    assert with_opt_in["auto_runnable"] is True
    assert with_opt_in["safety"] == "service_faults_opted_in"


def test_stateful_experiment_does_not_reference_a_missing_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    experiment = reliability._next_experiment(
        module="pkg.service",
        target="pkg.service.fetch",
        selector="fetch",
        seam="http",
        status="NOT EXERCISED",
        blocker=None,
        base_ref=None,
        changed=False,
        has_tests=False,
        allow_service_faults=False,
    )

    assert experiment["command"] == "ordeal scan pkg.service --target fetch -n 100"
    assert experiment["safety"] == "review_required"
    assert experiment["auto_runnable"] is False


def test_planner_selects_mutation_or_configured_exploration_when_applicable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    common = {
        "module": "pkg.workflows",
        "target": "pkg.workflows.run",
        "selector": "run",
        "status": "NOT EXERCISED",
        "blocker": None,
        "base_ref": None,
        "changed": False,
        "allow_service_faults": False,
    }

    mutation = reliability._next_experiment(
        **common,
        seam="cache",
        has_tests=True,
    )
    (tmp_path / "ordeal.toml").write_text("[explore]\n", encoding="utf-8")
    exploration = reliability._next_experiment(
        **common,
        seam="subprocess",
        has_tests=False,
    )

    assert mutation["engine"] == "mutation"
    assert mutation["command"] == "ordeal mutate pkg.workflows.run"
    assert exploration["engine"] == "exploration"
    assert exploration["command"] == "ordeal explore -c ordeal.toml"
