"""Run the locked Evidence Closure bug/fixed release corpus."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(FIXTURE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIXTURE_ROOT))

from ordeal.auto import scan_module  # noqa: E402
from ordeal.cli import (  # noqa: E402
    _build_parser,
    _callable_listing_rows,
    _package_root_scan_sample,
)
from ordeal.reliability import (  # noqa: E402
    _build_reliability_map,
    _run_fault_probe,
    _runtime_fault_property,
)

PACKAGE = "evidence_closure_corpus_pkg"
BUG_TARGETS = tuple(
    f"case{index:02d}_{name}_bug"
    for index, name in enumerate(
        (
            "retry",
            "fallback",
            "recovery",
            "cache",
            "file",
            "http",
            "subprocess",
            "transaction",
            "model_loading",
            "shape",
            "dtype",
            "partial_batch",
        ),
        start=1,
    )
)
FIXED_TARGETS = tuple(target.removesuffix("_bug") + "_fixed" for target in BUG_TARGETS)
TOOL_LIMITATION_TARGET = "tool_side_strategy_blocked"


def _scan_target(target: str) -> dict[str, Any]:
    """Scan one locked target and return its release-relevant classification."""
    result = scan_module(
        PACKAGE,
        targets=[target],
        max_examples=15,
        mode="candidate",
        seed_from_tests=False,
        seed_from_fixtures=False,
        seed_from_call_sites=False,
    )
    if len(result.functions) != 1:
        raise AssertionError(f"{target}: expected one function result, got {result.summary()}")
    function = result.functions[0]
    supported = bool(
        function.promoted
        and function.replayable
        and function.replay_attempts > 0
        and function.replay_matches == function.replay_attempts
    )
    return {
        "target": target,
        "verdict": function.verdict,
        "supported": supported,
        "promoted": function.promoted,
        "replay_attempts": function.replay_attempts,
        "replay_matches": function.replay_matches,
        "error_type": function.error_type,
        "limitation_kind": function.limitation_kind,
        "blocking_reason": function.blocking_reason,
    }


def _metrics(
    results: list[dict[str, Any]],
    *,
    evaluated_targets: set[str],
) -> dict[str, Any]:
    """Return corpus-level supported-finding precision and recall."""
    positives = {target for target in BUG_TARGETS if target in evaluated_targets}
    controls = {target for target in FIXED_TARGETS if target in evaluated_targets}
    promoted = {str(item["target"]) for item in results if item["supported"]}
    true_positives = len(promoted & set(BUG_TARGETS))
    false_positives = len(promoted & set(FIXED_TARGETS))
    precision = true_positives / (true_positives + false_positives) if promoted else 1.0
    return {
        "evaluated": len(evaluated_targets),
        "evaluated_bugs": len(positives),
        "evaluated_fixed_controls": len(controls),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": len(BUG_TARGETS) - true_positives,
        "supported_finding_recall": true_positives / len(BUG_TARGETS),
        "supported_finding_precision": precision,
    }


def _validate_actions(reliability_map: dict[str, Any], row_names: set[str]) -> list[str]:
    """Return reasons for suggested commands that do not parse or resolve."""
    parser = _build_parser()
    invalid: list[str] = []
    for experiment in reliability_map["experiments"]:
        command = str(experiment.get("command") or "")
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            invalid.append(f"{command}: shell parse failed: {exc}")
            continue
        if not tokens or tokens[0] != "ordeal" or any(char in command for char in "<>"):
            invalid.append(f"{command}: not a concrete ordeal command")
            continue
        try:
            parser.parse_args(tokens[1:])
        except SystemExit as exc:
            invalid.append(f"{command}: CLI parse exited {exc.code}")
            continue
        if tokens[1:2] == ["scan"] and "--target" in tokens:
            selector = tokens[tokens.index("--target") + 1]
            if selector not in row_names:
                invalid.append(f"{command}: selector {selector!r} does not resolve")
    return invalid


def run_corpus(output: Path) -> dict[str, Any]:
    """Run the baseline and evidence-plan scans, assert gates, and write JSON."""
    if len(BUG_TARGETS) != 12 or len(FIXED_TARGETS) != 12:
        raise AssertionError("Evidence Closure corpus must contain 12 paired bug/fixed cases")
    rows = _callable_listing_rows(PACKAGE)
    sample = _package_root_scan_sample(PACKAGE, rows)
    if sample is None:
        raise AssertionError("held-out package no longer exercises bounded package sampling")
    baseline_targets = set(sample["targets"])
    state = SimpleNamespace(functions={}, supervisor_info={})
    reliability_map = _build_reliability_map(PACKAGE, state, rows)
    planned_targets = {
        str(operation["selector"])
        for operation in reliability_map["operations"]
        if str(operation["selector"]).startswith("case")
    }
    missing = (set(BUG_TARGETS) | set(FIXED_TARGETS)) - planned_targets
    if missing:
        raise AssertionError(f"reliability map omitted held-out targets: {sorted(missing)}")

    all_targets = set(BUG_TARGETS) | set(FIXED_TARGETS)
    scanned = {target: _scan_target(target) for target in sorted(all_targets)}
    baseline_results = [scanned[target] for target in sorted(baseline_targets & all_targets)]
    closure_results = [scanned[target] for target in sorted(planned_targets)]
    baseline_metrics = _metrics(baseline_results, evaluated_targets=baseline_targets & all_targets)
    closure_metrics = _metrics(closure_results, evaluated_targets=planned_targets)
    tool_limitation = _scan_target(TOOL_LIMITATION_TARGET)
    tool_misclassified = int(
        tool_limitation["verdict"] != "blocked"
        or tool_limitation["promoted"]
        or tool_limitation["supported"]
    )
    invalid_actions = _validate_actions(
        reliability_map,
        {str(row["name"]) for row in rows},
    )
    runtime_observation = _run_fault_probe(
        PACKAGE,
        "runtime_file_recovery",
        "disk_full",
        max_examples=2,
    )
    observed_map = _build_reliability_map(
        PACKAGE,
        SimpleNamespace(
            functions={},
            supervisor_info={"reliability_observations": [runtime_observation]},
        ),
        rows,
    )
    operation_targets = {
        operation["id"]: operation["target"] for operation in observed_map["operations"]
    }
    property_names = {item["id"]: item["name"] for item in observed_map["properties"]}
    closed_cells = [
        cell
        for cell in observed_map["cells"]
        if operation_targets[cell["operation_id"]].endswith("runtime_file_recovery")
        and cell["fault"] == "disk_full"
        and property_names[cell["property_id"]] == _runtime_fault_property("disk_full")
    ]

    if closure_metrics["supported_finding_recall"] <= baseline_metrics["supported_finding_recall"]:
        raise AssertionError("Evidence Closure did not improve supported-finding recall")
    if closure_metrics["supported_finding_recall"] < 0.9:
        raise AssertionError("Evidence Closure recall fell below the 90% release floor")
    if (
        closure_metrics["supported_finding_precision"]
        < baseline_metrics["supported_finding_precision"]
    ):
        raise AssertionError("Evidence Closure lost supported-finding precision")
    if closure_metrics["supported_finding_precision"] < 1.0:
        raise AssertionError("a fixed negative control was promoted")
    if tool_misclassified:
        raise AssertionError("a tool-side limitation was misclassified as target behavior")
    if invalid_actions:
        raise AssertionError(f"invalid suggested actions: {invalid_actions}")
    if runtime_observation["status"] != "PASS" or not closed_cells:
        raise AssertionError("fault-specific runtime observation did not close a map cell")
    if closed_cells[0]["status"] != "PASS":
        raise AssertionError("merged fault observation did not transition the cell to PASS")

    report = {
        "schema": "ordeal.evidence-closure-corpus/v1",
        "corpus": {
            "buggy_cases": len(BUG_TARGETS),
            "fixed_controls": len(FIXED_TARGETS),
            "tool_limitation_cases": 1,
        },
        "baseline": baseline_metrics,
        "evidence_closure": closure_metrics,
        "tool_failures_misclassified": tool_misclassified,
        "suggested_actions": {
            "count": len(reliability_map["experiments"]),
            "invalid": invalid_actions,
        },
        "runtime_cell_transition": {
            "before": "NOT EXERCISED",
            "after": closed_cells[0]["status"],
            "observation": runtime_observation,
        },
        "baseline_targets": sorted(baseline_targets),
        "planned_targets": sorted(planned_targets),
        "cases": closure_results,
        "tool_limitation": tool_limitation,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    """Run the release gate from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/evidence-closure-corpus.json"),
    )
    args = parser.parse_args()
    try:
        report = run_corpus(args.output)
    except AssertionError as exc:
        print(f"Evidence Closure corpus failed: {exc}", file=sys.stderr)
        return 1
    baseline = report["baseline"]
    closure = report["evidence_closure"]
    print(
        "Evidence Closure corpus passed: "
        f"recall {baseline['supported_finding_recall']:.0%} -> "
        f"{closure['supported_finding_recall']:.0%}, "
        f"precision {closure['supported_finding_precision']:.0%}, "
        "tool misclassifications 0."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
