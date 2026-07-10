"""Run the complete buggy-to-fixed Compose evidence loop used by CI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ordeal.compose import ComposeTrace

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "compose_e2e"
MANIFEST = FIXTURE / "tests" / "ordeal-regressions.json"
BUGGY_ACTIONS = [
    ("lifecycle", "up"),
    ("lifecycle", "wait_ready"),
    ("request", "probe"),
    ("fault", "kill"),
    ("request", "probe"),
    ("lifecycle", "start_service"),
    ("lifecycle", "wait_ready"),
    ("request", "probe"),
]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run_cli(
    arguments: list[str],
    *,
    variant: str,
    expected: int,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ORDEAL_SERVICE_VARIANT": variant}
    process = subprocess.run(
        [sys.executable, "-m", "ordeal.cli", *arguments],
        cwd=FIXTURE,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if process.returncode != expected:
        raise AssertionError(
            f"{variant} command returned {process.returncode}, expected {expected}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process


def _coverage_rows(record: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    evidence = record.get("evidence", {})
    coverage = evidence.get("reliability_coverage", {})
    rows = coverage.get("rows", [])
    return {(str(row["operation"]), str(row["fault"]), str(row["property"])): row for row in rows}


def run_loop(output: Path) -> dict[str, Any]:
    """Execute discovery, replay, regression, fix control, and workload control."""
    _require(
        shutil.which("docker") is not None,
        "docker is required for the Compose evidence loop",
    )
    compose_version = subprocess.run(
        ["docker", "compose", "version"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    _require(compose_version.returncode == 0, "the Docker Compose plugin is required")
    _require(MANIFEST.is_file(), "the portable service regression manifest is not checked in")
    committed = json.loads(MANIFEST.read_text(encoding="utf-8"))
    _require(committed.get("schema") == "ordeal.regression-manifest/v1", "bad manifest schema")
    committed_records = committed.get("regressions", [])
    _require(len(committed_records) == 1, "checked-in manifest must have one regression")
    committed_record = committed_records[0]
    committed_trace_file = str(committed_record.get("trace_file") or "")
    _require(bool(committed_trace_file), "checked-in regression has no trace path")
    committed_trace = ComposeTrace.load(FIXTURE / committed_trace_file)

    # Exercise the checked-in binding before discovery can regenerate the same paths.
    buggy_guard = _run_cli(
        ["verify", "--ci", "--manifest", "tests/ordeal-regressions.json"],
        variant="buggy",
        expected=1,
    )
    _require("clean replays 0/3" in buggy_guard.stderr, "buggy CI guard did not fail 0/3")
    shutil.rmtree(FIXTURE / ".ordeal", ignore_errors=True)
    discovery = _run_cli(
        ["explore", "--runner", "compose", "-c", "ordeal.toml", "--save-artifacts"],
        variant="buggy",
        expected=1,
    )

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    records = manifest.get("regressions", [])
    _require(len(records) == 1, f"expected one service regression, found {len(records)}")
    record = records[0]
    _require(record.get("runner") == "compose", "manifest record is not a Compose regression")
    _require(
        record.get("finding_id") == committed_record.get("finding_id"),
        "regenerated finding id differs from the checked-in regression",
    )
    trace_file = str(record.get("trace_file") or "")
    _require(trace_file and not Path(trace_file).is_absolute(), "trace path is not portable")
    trace = ComposeTrace.load(FIXTURE / trace_file)
    stable_binding_fields = ("schema", "failure_signature", "action_count")
    regenerated_binding = record.get("binding", {})
    committed_binding = committed_record.get("binding", {})
    _require(
        all(
            regenerated_binding.get(field) == committed_binding.get(field)
            for field in stable_binding_fields
        ),
        "regenerated stable binding fields differ from the checked-in regression",
    )
    _require(
        trace.failure_signature == committed_trace.failure_signature,
        "regenerated failure signature differs from the checked-in regression",
    )
    action_identity = [(action.kind, action.name, action.params) for action in trace.actions]
    committed_action_identity = [
        (action.kind, action.name, action.params) for action in committed_trace.actions
    ]
    _require(
        action_identity == committed_action_identity,
        "regenerated action witness differs from the checked-in regression",
    )
    actions = [(action.kind, action.name) for action in trace.actions]
    _require(actions == BUGGY_ACTIONS, f"unexpected buggy action sequence: {actions!r}")
    _require(trace.failure is not None, "buggy service did not produce a bounded finding")
    _require(trace.failure.kind == "unexpected_json", "buggy service failed for the wrong reason")
    _require(trace.failure.action_index == 7, "failure did not occur on the recovery request")
    _require("json.status" in trace.failure.message, "recovery status property did not fail")
    _require(trace.replay is not None, "buggy trace has no immediate replay report")
    _require(trace.replay.attempted == 3, "buggy trace did not run three exact replays")
    _require(trace.replay.reproduced == 3, "buggy trace did not reproduce exactly 3/3")
    _require(
        trace.replay.observed_signatures == [trace.failure_signature] * 3,
        "buggy replay signatures were not exact",
    )
    _require(trace.compose.get("file") == "compose.yaml", "saved Compose path is not portable")

    rows = _coverage_rows(record)
    _require(rows[("probe", "none", "json:json.status")]["status"] == "PASS", "baseline missed")
    _require(rows[("probe", "kill", "status:200")]["status"] == "PASS", "recovery HTTP failed")
    _require(
        rows[("probe", "kill", "json:json.status")]["status"] == "FAIL",
        "recovery defect is absent from the property report",
    )

    fixed_control = _run_cli(
        [
            "verify",
            str(record["finding_id"]),
            "--manifest",
            "tests/ordeal-regressions.json",
            "--allow-unsafe-artifacts",
        ],
        variant="fixed",
        expected=0,
    )
    _require(
        "fixed-state evidence: complete" in fixed_control.stdout,
        "normal verify did not persist complete fixed-state evidence",
    )
    persisted = json.loads(MANIFEST.read_text(encoding="utf-8"))
    persisted_record = persisted["regressions"][0]
    control = persisted_record["evidence"]["post_fix_control"]
    _require(control.get("status") == "passed", "post-fix control was not persisted as passed")
    fixed_state = control.get("fixed_state", {})
    _require(fixed_state.get("failure") is None, "fixed control still fails the recovery trace")
    _require(
        fixed_state.get("reliability_coverage", {}).get("summary")
        == {"pass": 9, "not_exercised": 0, "fail": 0, "total": 9},
        "fixed control did not cover every configured operation/fault/property cell",
    )
    protection = fixed_state.get("workload_protection", {})
    _require(
        protection.get("status") == "protective_within_measured_scope",
        "fixed workload did not kill every measured response-oracle mutation",
    )
    _require(protection.get("mutation_score") == "4/4 (100%)", "unexpected mutation score")
    _require(
        {row["fault"] for row in protection.get("mutations", [])} == {"none", "kill"},
        "workload mutations did not control both baseline and kill/restart paths",
    )
    fixed_guard = _run_cli(
        ["verify", "--ci", "--manifest", "tests/ordeal-regressions.json"],
        variant="fixed",
        expected=0,
    )
    _require("Compose clean replays 3/3" in fixed_guard.stdout, "fixed CI guard did not pass 3/3")

    report = {
        "schema": "ordeal.service-evidence-loop/v1",
        "finding_id": record["finding_id"],
        "buggy": {
            "explore_exit": discovery.returncode,
            "verify_ci_exit": buggy_guard.returncode,
            "exact_replay": trace.replay.to_dict(),
            "failure": trace.to_dict()["failure"],
        },
        "fixed": {
            "verify_ci_exit": fixed_guard.returncode,
            "clean_replays": "3/3",
            "coverage": fixed_state["reliability_coverage"],
            "workload_protection": protection,
        },
        "portable_regression": {
            "manifest": MANIFEST.relative_to(ROOT).as_posix(),
            "trace": (FIXTURE / trace_file).relative_to(ROOT).as_posix(),
            "binding": record["binding"],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the evidence loop and write its machine-readable acceptance report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_loop(args.output)
    except (AssertionError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Compose evidence loop failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Verified Compose evidence loop: buggy verify --ci failed, fixed passed, "
        f"exact replay {report['buggy']['exact_replay']['reproduced']}/"
        f"{report['buggy']['exact_replay']['attempted']}, workload mutations 4/4."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
