"""Verify that the real Compose CI run exercised its exact kill/recovery path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXPECTED_ACTIONS = [
    ("lifecycle", "up"),
    ("lifecycle", "wait_ready"),
    ("request", "probe"),
    ("fault", "kill"),
    ("request", "probe"),
    ("lifecycle", "start_service"),
    ("lifecycle", "wait_ready"),
    ("request", "probe"),
    ("lifecycle", "down"),
]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_trace(trace_dir: Path) -> Path:
    """Validate one successful Docker-backed Compose trace and return its path."""
    traces = sorted(trace_dir.glob("compose-*.json"))
    _require(len(traces) == 1, f"expected one Compose trace, found {len(traces)}")
    trace_path = traces[0]
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    _require(payload.get("runner") == "compose", "trace does not use the Compose runner")
    _require(payload.get("failure") is None, "Compose exploration recorded a failure")

    actions = payload.get("actions")
    _require(isinstance(actions, list), "trace actions are missing")
    observed = [(action.get("kind"), action.get("name")) for action in actions]
    _require(observed == EXPECTED_ACTIONS, f"unexpected action sequence: {observed!r}")

    (
        up,
        first_ready,
        baseline,
        kill,
        fault_probe,
        start,
        second_ready,
        recovery_probe,
        down,
    ) = actions
    _require(up.get("result") == {"owned_cleanup": True}, "runner did not own the topology")
    _require(first_ready.get("result", {}).get("status") == 200, "initial readiness failed")
    _require(baseline.get("result", {}).get("status") == 200, "baseline request failed")
    _require(kill.get("result", {}).get("signal") == "SIGKILL", "SIGKILL was not recorded")
    fault_result = fault_probe.get("result", {})
    _require(fault_result.get("expected_fault_window") is True, "fault window was not recorded")
    _require("request_error" in fault_result, "request remained reachable after SIGKILL")
    _require(start.get("result", {}).get("started") is True, "service was not restarted")
    _require(second_ready.get("result", {}).get("status") == 200, "recovery readiness failed")
    recovery_result = recovery_probe.get("result", {})
    _require(recovery_result.get("status") == 200, "clean recovery request did not return 200")
    _require(recovery_result.get("expected_fault_window") is False, "recovery was not validated")
    _require(down.get("result") == {"stopped": True}, "runner did not clean up the topology")
    return trace_path


def main(argv: list[str] | None = None) -> int:
    """Validate the trace directory supplied on the command line."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: verify_compose_e2e_trace.py TRACE_DIR", file=sys.stderr)
        return 2
    try:
        path = verify_trace(Path(args[0]))
    except (AssertionError, json.JSONDecodeError, OSError) as exc:
        print(f"Compose E2E trace verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified real Compose kill/recovery trace: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
