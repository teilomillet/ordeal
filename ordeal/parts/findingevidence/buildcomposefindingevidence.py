from __future__ import annotations
# ruff: noqa
def _build_compose_finding_evidence(
    trace: Mapping[str, Any],
    *,
    replay: Mapping[str, Any] | None,
    coverage: Mapping[str, Any],
    protection: Mapping[str, Any],
    trace_path: str | None = None,
) -> dict[str, Any]:
    """Build the shared bounded evidence card for one Compose trace failure."""
    failure = _mapping(trace.get("failure"))
    failure_signature = str(trace.get("failure_signature") or "").strip() or None
    replay_data = _mapping(replay)
    attempts = _integer(replay_data.get("attempted"))
    reproduced = _integer(replay_data.get("reproduced"))
    replay_status = (
        "verified"
        if attempts > 0 and reproduced == attempts
        else "supported"
        if reproduced > 0
        else "failed"
        if attempts
        else "not_run"
    )
    compose = _mapping(trace.get("compose"))
    actions = trace.get("actions") if isinstance(trace.get("actions"), list) else []
    witness_input = {
        "seed": trace.get("seed"),
        "actions": actions,
        "trace_path": trace_path,
    }
    target = str(compose.get("project_name") or compose.get("file") or "compose service")
    if reproduced:
        claim = (
            f"The recorded Compose trace reproduced {failure.get('kind') or 'the failure'} "
            f"with the exact failure signature in {reproduced}/{attempts} attempts."
        )
        establishes = (
            f"The exact failure signature matched in {reproduced}/{attempts} replay attempts; "
            "the action/fault trace was exact but external timing was not deterministic."
        )
    else:
        claim = (
            f"The Compose exploration observed {failure.get('kind') or 'a failure'} in {target}; "
            "bounded replay support is not established."
        )
        establishes = "One recorded Compose observation without a matching replay attempt."
    summary = _mapping(coverage.get("summary"))
    return {
        "schema": _FINDING_EVIDENCE_SCHEMA,
        "status": "supported" if reproduced > 0 else "exploratory",
        "claim": claim,
        "subject": {
            "target": target,
            "runner": "compose",
            "source_sha256": None,
            "trace_sha256": _sha256_json(trace),
            "config_sha256": _sha256_json(compose),
        },
        "witness": {
            "available": bool(actions),
            "input": _json_ready(witness_input),
            "sha256": _sha256_json(witness_input),
            "source": "compose_trace",
        },
        "observation": {
            "kind": "compose_failure",
            "failure_kind": failure.get("kind"),
            "message": failure.get("message"),
            "action_index": failure.get("action_index"),
            "action_name": failure.get("action_name"),
            "failure_signature": failure_signature,
        },
        "replay": {
            "status": replay_status,
            "attempts": attempts,
            "exact_matches": reproduced,
            "match_basis": "same failure kind, message, action index, and action name",
            "command": f"ordeal replay {trace_path}" if trace_path else None,
            "boundary": replay_data.get("boundary"),
        },
        "minimization": {
            "status": "not_run",
            "method": None,
            "original_complexity": len(actions),
            "minimized_complexity": None,
            "replay_attempts": 0,
            "replay_matches": 0,
            "boundary": "Compose traces are exact but are not currently minimized.",
        },
        "contrast": {
            "status": "observed" if summary else "not_measured",
            "passing_examples": _integer(summary.get("pass")),
            "failing_examples": _integer(summary.get("fail")),
        },
        "reliability_coverage": _json_ready(coverage),
        "test_protection": _json_ready(protection),
        "regression": {
            "status": "not_saved" if reproduced else "not_ready",
            "path": None,
            "test_name": None,
            "binding": None,
        },
        "post_fix_control": {
            "status": "pending" if reproduced else "not_ready",
            "method": "same_trace_after_fix" if reproduced else "reproduce_then_retest",
            "acceptance": (
                "After the fix, the same trace must stop reproducing the exact failure signature."
                if reproduced
                else "Obtain at least one exact replay match before claiming a fixed regression."
            ),
        },
        "ci_guard": {
            "status": "not_ready",
            "command": None,
            "acceptance": "Commit a bound Compose trace before enabling the CI guard.",
        },
        "workflow": {
            "discover": "observed",
            "reproduce": replay_status,
            "minimize": "not_run",
            "save_regression": "not_saved" if reproduced else "not_ready",
            "verify_fix": "pending" if reproduced else "not_ready",
            "guard_ci": "not_ready",
        },
        "boundaries": {
            "establishes": establishes,
            "does_not_establish": [
                "deterministic replay",
                "the root cause",
                "behavior outside the recorded operation, fault, and property cells",
                "that a future fix works",
            ],
        },
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
    }
