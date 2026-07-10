"""Release gate for evidence-bound real-project closure measurements."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.verify_evidence_closure_real_projects as real_projects
from scripts.verify_evidence_closure_real_projects import run_benchmark

pytestmark = pytest.mark.release_eval


def test_real_project_reproductions_are_measured_and_paired(tmp_path: Path) -> None:
    report = run_benchmark(tmp_path / "real-project-evidence-closure.json")
    metrics = report["metrics"]

    assert report["passed"] is True
    assert metrics["projects"] == 3
    assert metrics["pairs"] == 3
    assert metrics["cases"] == 6
    assert metrics["recall"] == 1.0
    assert metrics["precision"] == 1.0
    assert metrics["specificity"] == 1.0
    assert metrics["accounted_cases"] == 6
    assert metrics["max_time_to_witness_seconds"] <= 10.0
    assert metrics["reliability_cells"] == 8
    assert metrics["closed_reliability_cells"] == 0
    assert metrics["observed_closure_rate"] == 0.0
    assert all(
        case["deepening"]["map_schema"] == "ordeal.reliability-map/v1"
        and case["deepening"]["map_status"] == "ok"
        and case["deepening"]["accounting"] == "explicit_out_of_scope"
        and case["deepening"]["deepening"]["status"] == "no_safe_experiment"
        for case in report["cases"]
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"raw_details": {}}, "omitted the reliability map"),
        (
            {
                "raw_details": {
                    "reliability_map": {
                        "schema": "ordeal.reliability-map/v1",
                        "status": "blocked",
                        "blocking_reason": "map construction failed",
                    }
                }
            },
            "map construction failed",
        ),
        (
            {
                "raw_details": {
                    "reliability_map": {
                        "schema": "ordeal.reliability-map/v1",
                        "next_experiment": None,
                        "deepening": {"status": "no_safe_experiment"},
                    }
                }
            },
            "omitted its summary",
        ),
        (
            {
                "raw_details": {
                    "reliability_map": {
                        "schema": "ordeal.reliability-map/v1",
                        "summary": {
                            "operations": 1,
                            "cells": 1,
                            "pass": 0,
                            "not_exercised": 1,
                            "fail": 0,
                            "blocked": 0,
                        },
                        "next_experiment": None,
                        "deepening": {"status": "budget_exhausted"},
                    }
                }
            },
            "did not justify",
        ),
    ],
)
def test_real_project_gate_rejects_unmeasured_map_output(
    tmp_path: Path,
    monkeypatch,
    payload: dict,
    message: str,
) -> None:
    completed = subprocess.CompletedProcess(["scan"], 0, json.dumps(payload), "")
    monkeypatch.setattr(real_projects.subprocess, "run", lambda *args, **kwargs: completed)
    case = SimpleNamespace(command=("scan",), workspace=str(tmp_path))

    result = real_projects._deepening_result(case, budget_seconds=1.0)

    assert result["status"] == "error"
    assert message in result["error"]
