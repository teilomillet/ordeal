"""Validate three materially different real Compose service systems."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _compose(fixture: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", *arguments],
        cwd=fixture,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )


def _run_clean_fixture(name: str) -> dict[str, Any]:
    fixture = FIXTURES / name
    shutil.rmtree(fixture / ".ordeal", ignore_errors=True)
    configured = _compose(fixture, "config", "--services")
    _require(configured.returncode == 0, f"{name} Compose config failed: {configured.stderr}")
    services = sorted(line for line in configured.stdout.splitlines() if line)
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "ordeal.cli",
            "explore",
            "--runner",
            "compose",
            "-c",
            "ordeal.toml",
            "--save-artifacts",
            "--json",
        ],
        cwd=fixture,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    if process.returncode != 0:
        raise AssertionError(
            f"{name} returned {process.returncode}\nstdout:\n{process.stdout}\n"
            f"stderr:\n{process.stderr}"
        )
    payload = json.loads(process.stdout)
    _require(payload.get("schema") == "ordeal.compose-run/v1", f"{name} schema mismatch")
    _require(payload.get("status") == "clean", f"{name} was not clean")
    summary = payload.get("reliability_coverage", {}).get("summary", {})
    _require(summary.get("fail") == 0, f"{name} has failed coverage cells")
    _require(summary.get("not_exercised") == 0, f"{name} has unexercised coverage cells")
    protection = payload.get("workload_protection", {})
    _require(
        protection.get("status") == "protective_within_measured_scope",
        f"{name} workload is not protective",
    )
    _require(protection.get("mutation_score") == "4/4 (100%)", f"{name} score changed")
    evidence_path = Path(str(payload["trace_file"])).with_suffix(".evidence.json")
    _require(evidence_path.is_file(), f"{name} complete run evidence was not persisted")
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    _require(persisted == payload, f"{name} stdout and persisted evidence differ")
    return {
        "services": services,
        "trace_sha256": payload["trace_sha256"],
        "evidence": evidence_path.relative_to(ROOT).as_posix(),
        "coverage": summary,
        "workload_protection": protection,
        "trace": payload["trace"],
    }


def run_matrix(recovery_report: Path, output: Path) -> dict[str, Any]:
    """Run and validate recovery, persistence, concurrency, and broader faults."""
    recovery = json.loads(recovery_report.read_text(encoding="utf-8"))
    _require(
        recovery.get("schema") == "ordeal.service-evidence-loop/v1",
        "recovery evidence report has the wrong schema",
    )
    _require(recovery.get("fixed", {}).get("verify_ci_exit") == 0, "recovery CI guard failed")
    persistence = _run_clean_fixture("compose_persistence")
    concurrency = _run_clean_fixture("compose_concurrency")
    try:
        _require(persistence["services"] == ["api", "store"], "persistence is not two-service")
        persistence_trace = persistence["trace"]
        _require(
            persistence_trace.get("final_state")
            == {"persisted_id": "item-1", "persisted_value": "committed"},
            "state did not survive API restart through the store service",
        )
        _require(
            "restart"
            in {
                action.get("name")
                for action in persistence_trace.get("actions", [])
                if action.get("kind") == "fault"
            },
            "persistence system did not execute restart",
        )
        _require(concurrency["services"] == ["api", "worker"], "fan-out is not two-service")
        concurrency_trace = concurrency["trace"]
        _require(
            concurrency_trace.get("final_state")
            == {"observed_unique": 8, "observed_max_concurrency": 8},
            "fan-out system did not prove eight simultaneous unique worker requests",
        )
        observed_faults = {
            action.get("name")
            for action in concurrency_trace.get("actions", [])
            if action.get("kind") == "fault"
        }
        _require(
            observed_faults == {"delay_response", "corrupt_response"},
            f"broader response faults were not both exercised: {observed_faults}",
        )
    finally:
        for name in ("compose_persistence", "compose_concurrency"):
            _compose(FIXTURES / name, "down", "--remove-orphans")

    report = {
        "schema": "ordeal.service-validation-matrix/v1",
        "systems": {
            "recovery": {
                "topology": "single API kill/restart defect and fixed control",
                "finding_id": recovery["finding_id"],
                "coverage": recovery["fixed"]["coverage"]["summary"],
                "workload_protection": recovery["fixed"]["workload_protection"],
            },
            "persistence": {key: value for key, value in persistence.items() if key != "trace"},
            "concurrency": {key: value for key, value in concurrency.items() if key != "trace"},
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the checked service matrix and persist its evidence index."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--recovery-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_matrix(args.recovery_report, args.output)
    except (AssertionError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Compose service matrix failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Verified Compose service matrix: "
        f"{len(report['systems'])} systems, multi-service persistence, "
        "8-way fan-out, delay and corruption faults."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
