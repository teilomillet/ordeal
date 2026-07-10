"""Measure Evidence Closure on pinned public bug/fixed reproductions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "bug-benchmark.public.toml"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ordeal.benchmarking import benchmark_bug_manifest  # noqa: E402


def _deepening_result(case: Any, *, budget_seconds: float) -> dict[str, Any]:
    """Run the case through scan deepening and return measured map evidence."""
    command = [*case.command, "--deepen", "--time-limit", str(budget_seconds)]
    env = dict(os.environ)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) if not current else f"{ROOT}{os.pathsep}{current}"
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=case.workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=budget_seconds + 5.0,
    )
    elapsed = time.perf_counter() - started
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "seconds": elapsed,
            "error": f"scan did not emit JSON: {exc}",
            "stderr": completed.stderr[-500:],
        }
    reliability_map = payload.get("raw_details", {}).get("reliability_map")
    if not isinstance(reliability_map, dict):
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": "scan omitted the reliability map",
        }
    if reliability_map.get("schema") != "ordeal.reliability-map/v1":
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": f"unexpected reliability map schema: {reliability_map.get('schema')!r}",
        }
    if reliability_map.get("status") == "blocked":
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": str(reliability_map.get("blocking_reason") or "reliability map blocked"),
        }
    summary = reliability_map.get("summary")
    if not isinstance(summary, dict):
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": "reliability map omitted its summary",
        }
    count_fields = ("operations", "cells", "pass", "not_exercised", "fail", "blocked")
    if any(
        key not in summary
        or not isinstance(summary[key], int)
        or isinstance(summary[key], bool)
        or summary[key] < 0
        for key in count_fields
    ):
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": "reliability map summary has missing or invalid count fields",
        }
    cell_count = summary["cells"]
    closed_count = summary["pass"] + summary["fail"]
    if closed_count + summary["not_exercised"] != cell_count:
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": "reliability map status counts do not equal its cell count",
        }
    next_experiment = reliability_map.get("next_experiment")
    deepening = reliability_map.get("deepening")
    if not isinstance(deepening, dict):
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": "scan omitted the requested deepening result",
        }
    deepening_status = deepening.get("status")
    if closed_count and deepening_status == "completed":
        accounting = "measured_cells"
        reason = "runtime evidence closed at least one reliability cell"
    elif deepening_status == "review_required" and isinstance(next_experiment, dict):
        accounting = "actionable_gap"
        reason = str(next_experiment.get("reason") or "a concrete next experiment is available")
    elif deepening_status == "no_safe_experiment" and next_experiment is None:
        accounting = "explicit_out_of_scope"
        reason = (
            "the scoped upstream regression exposed no automatically closable reliability cell"
        )
    else:
        return {
            "status": "error",
            "seconds": round(elapsed, 6),
            "error": (
                "deepening evidence did not justify measured, actionable, or out-of-scope "
                f"accounting: status={deepening_status!r}, cells={cell_count}, "
                f"closed={closed_count}, next={next_experiment is not None}"
            ),
        }
    return {
        "status": "ok" if completed.returncode in {0, 1} else "error",
        "seconds": round(elapsed, 6),
        "exit_code": completed.returncode,
        "map_schema": reliability_map["schema"],
        "map_status": reliability_map.get("status", "ok"),
        "cell_count": cell_count,
        "closed_count": closed_count,
        "closure_rate": closed_count / cell_count if cell_count else None,
        "accounting": accounting,
        "reason": reason,
        "deepening": deepening,
        "next_experiment": next_experiment,
        "stderr": completed.stderr[-500:] if completed.returncode not in {0, 1} else "",
    }


def run_benchmark(
    output: Path,
    *,
    budget_seconds: float = 10.0,
    online_sources: bool = False,
) -> dict[str, Any]:
    """Run evidence-bound public pairs and write closure/timing measurements."""
    suite = benchmark_bug_manifest(
        str(MANIFEST),
        python_executable=sys.executable,
        ordeal_root=str(ROOT),
        online_sources=online_sources,
    )
    cases: list[dict[str, Any]] = []
    for case in suite.cases:
        deepening = (
            _deepening_result(case, budget_seconds=budget_seconds)
            if case.status not in {"blocked", "error"}
            else {"status": "not_run", "accounting": "blocked"}
        )
        cases.append(
            {
                "name": case.spec.name,
                "project": case.spec.project,
                "bug_id": case.spec.bug_id,
                "pair_id": case.spec.pair_id,
                "expected_outcome": case.spec.expected_outcome,
                "classification": case.classification,
                "scan_status": case.status,
                "time_to_witness_seconds": (
                    round(case.seconds, 6) if case.spec.expected_outcome == "bug" else None
                ),
                "fix_commit": case.spec.fix_commit,
                "oracle_url": case.spec.oracle_url,
                "evidence_level": case.spec.evidence_level,
                "limitation": case.spec.notes,
                "deepening": deepening,
            }
        )

    witness_times = [
        float(case["time_to_witness_seconds"])
        for case in cases
        if case["time_to_witness_seconds"] is not None
    ]
    total_cells = sum(int(case["deepening"].get("cell_count", 0)) for case in cases)
    closed_cells = sum(int(case["deepening"].get("closed_count", 0)) for case in cases)
    projects = {str(case.spec.project) for case in suite.cases if case.spec.project}
    pairs = {str(case.spec.pair_id) for case in suite.cases if case.spec.pair_id}
    errors = [
        case["name"]
        for case in cases
        if case["scan_status"] in {"blocked", "error"} or case["deepening"].get("status") != "ok"
    ]
    unaccounted = [
        case["name"]
        for case in cases
        if case["deepening"].get("accounting")
        not in {"measured_cells", "actionable_gap", "explicit_out_of_scope"}
    ]
    metrics = {
        "projects": len(projects),
        "pairs": len(pairs),
        "cases": len(cases),
        "true_positives": suite.hit_count,
        "true_negatives": suite.correct_rejection_count,
        "false_positives": suite.false_positive_count,
        "false_negatives": suite.miss_count,
        "recall": suite.recall,
        "precision": suite.precision,
        "specificity": suite.specificity,
        "median_time_to_witness_seconds": round(median(witness_times), 6),
        "max_time_to_witness_seconds": round(max(witness_times), 6),
        "reliability_cells": total_cells,
        "closed_reliability_cells": closed_cells,
        "observed_closure_rate": closed_cells / total_cells if total_cells else None,
        "accounted_cases": len(cases) - len(unaccounted),
    }
    failures: list[str] = []
    if len(projects) != 3 or len(pairs) != 3 or len(cases) != 6:
        failures.append("public benchmark must contain three paired projects and six cases")
    if suite.recall != 1.0 or suite.precision != 1.0 or suite.specificity != 1.0:
        failures.append("bug/fixed classification lost perfect scoped precision or recall")
    if errors:
        failures.append(f"blocked or errored cases: {errors}")
    if unaccounted:
        failures.append(
            f"cases lacked measured evidence or an explicit gap boundary: {unaccounted}"
        )
    if witness_times and max(witness_times) > budget_seconds:
        failures.append("a scoped bug witness exceeded the per-case evidence budget")
    if total_cells != 8 or closed_cells != 0:
        failures.append(
            "locked real-project closure baseline changed: expected 8/0 cells, "
            f"observed {total_cells}/{closed_cells}"
        )
    if any(
        case["deepening"].get("deepening", {}).get("status") != "no_safe_experiment"
        for case in cases
    ):
        failures.append("every locked real-project case must report no_safe_experiment")

    report = {
        "schema": "ordeal.evidence-closure-real-projects/v1",
        "manifest": MANIFEST.relative_to(ROOT).as_posix(),
        "benchmark_boundary": (
            "Three modern executable reproductions bound to pinned upstream bug/fixed "
            "revisions; this is not a broad accuracy estimate or the historical runtime."
        ),
        "budget_seconds_per_case": budget_seconds,
        "locked_closure_baseline": {
            "reliability_cells": 8,
            "closed_reliability_cells": 0,
            "deepening_status": "no_safe_experiment",
        },
        "passed": not failures,
        "metrics": metrics,
        "failures": failures,
        "cases": cases,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failures:
        raise AssertionError("; ".join(failures))
    return report


def main() -> int:
    """Run the benchmark from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=10.0)
    parser.add_argument("--online-sources", action="store_true")
    args = parser.parse_args()
    try:
        report = run_benchmark(
            args.output,
            budget_seconds=args.budget_seconds,
            online_sources=args.online_sources,
        )
    except AssertionError as exc:
        print(f"Real-project Evidence Closure benchmark failed: {exc}", file=sys.stderr)
        return 1
    metrics = report["metrics"]
    print(
        "Real-project Evidence Closure benchmark passed: "
        f"{metrics['projects']} projects, recall {metrics['recall']:.0%}, "
        f"precision {metrics['precision']:.0%}, max witness "
        f"{metrics['max_time_to_witness_seconds']:.3f}s."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
