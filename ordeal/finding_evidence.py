"""Compact, claim-scoped evidence cards for user-facing findings."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
from collections.abc import Mapping, Sequence
from typing import Any

_FINDING_EVIDENCE_SCHEMA = "ordeal.finding-evidence/v1"
_DIVERGENCE_EVIDENCE_SCHEMA = "ordeal.divergence-evidence/v1"


def _json_ready(value: Any) -> Any:
    """Normalize arbitrary evidence values for stable JSON hashing."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="backslashreplace")
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_json_ready(item) for item in value), key=repr)
    if isinstance(value, Sequence):
        return [_json_ready(item) for item in value]
    return repr(value)


def _sha256_json(value: Any) -> str:
    """Hash one canonical JSON evidence value."""
    payload = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping view or an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def _integer(value: Any) -> int:
    """Return a non-negative integer evidence count."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _target(detail: Mapping[str, Any], module: str | None) -> str:
    """Return the most specific target name in a finding."""
    qualname = str(detail.get("qualname") or "").strip()
    if qualname:
        return qualname
    function = str(detail.get("function") or "?").strip()
    return f"{module}.{function}" if module else function


def _witness(detail: Mapping[str, Any], proof: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the exact input witness and its canonical digest."""
    proof_witness = _mapping(proof.get("witness") or proof.get("valid_input_witness"))
    counterexample = _mapping(detail.get("counterexample"))
    available = False
    value: Any = None
    if "input" in proof_witness:
        available = True
        value = proof_witness.get("input")
    elif detail.get("failing_args") is not None:
        available = True
        value = detail.get("failing_args")
    elif "input" in counterexample:
        available = True
        value = counterexample.get("input")
    elif counterexample:
        available = True
        value = counterexample
    source = proof_witness.get("source") or detail.get("input_source")
    return {
        "available": available,
        "input": _json_ready(value) if available else None,
        "sha256": _sha256_json(value) if available else None,
        "source": str(source) if source is not None else None,
    }


def _observation(detail: Mapping[str, Any], proof: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the exact observed failure or property violation."""
    kind = str(detail.get("kind") or "finding")
    failure = _mapping(proof.get("failure_path") or proof.get("failing_path"))
    error_type = str(detail.get("error_type") or failure.get("error_type") or "").strip()
    message = str(failure.get("error") or detail.get("error") or "").strip()
    if not error_type and message:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):\s*", message)
        if match is not None:
            error_type = match.group(1)
            message = message[match.end() :]
    if kind == "property":
        return {
            "kind": "property_violation",
            "property": str(detail.get("name") or "inferred property"),
            "message": str(detail.get("summary") or "property counterexample observed"),
        }
    if kind == "contract":
        return {
            "kind": "contract_violation",
            "contract": str(detail.get("name") or detail.get("category") or "contract"),
            "error_type": error_type or None,
            "message": message or str(detail.get("summary") or "contract violation observed"),
        }
    return {
        "kind": "exception" if error_type or message else kind,
        "error_type": error_type or None,
        "message": message or str(detail.get("summary") or "finding observed"),
    }


def _replay(detail: Mapping[str, Any], proof: Mapping[str, Any]) -> dict[str, Any]:
    """Return exact replay status without inferring unperformed checks."""
    reproduction = _mapping(proof.get("reproduction"))
    minimal = _mapping(proof.get("minimal_reproduction"))
    attempts = _integer(detail.get("replay_attempts") or reproduction.get("replay_attempts"))
    matches = _integer(detail.get("replay_matches") or reproduction.get("replay_matches"))
    replayable = bool(
        detail.get("replayable")
        if detail.get("replayable") is not None
        else reproduction.get("replayable")
    )
    if attempts > 0 and replayable and matches == attempts:
        status = "verified"
    elif attempts > 0:
        status = "failed"
    else:
        status = "not_run"
    match_basis = str(
        detail.get("replay_match_basis") or reproduction.get("match_basis") or ""
    ).strip()
    if not match_basis and status != "not_run":
        match_basis = "same exception type and message"
    return {
        "status": status,
        "attempts": attempts,
        "exact_matches": matches,
        "match_basis": match_basis or None,
        "command": minimal.get("command") or reproduction.get("command"),
    }


def _minimization(
    detail: Mapping[str, Any],
    replay: Mapping[str, Any],
) -> dict[str, Any]:
    """Report only minimization work that the discovery path actually performed."""
    recorded = _mapping(detail.get("minimization"))
    if recorded:
        return {
            "status": str(recorded.get("status") or "unknown"),
            "method": recorded.get("method"),
            "original_complexity": recorded.get("original_complexity"),
            "minimized_complexity": recorded.get("minimized_complexity"),
            "replay_attempts": _integer(recorded.get("replay_attempts")),
            "replay_matches": _integer(recorded.get("replay_matches")),
            "boundary": str(recorded.get("boundary") or ""),
        }
    return {
        "status": "not_run",
        "method": None,
        "original_complexity": None,
        "minimized_complexity": None,
        "replay_attempts": 0,
        "replay_matches": 0,
        "boundary": "No minimization claim is supported for this witness.",
    }


def _claim_status(
    detail: Mapping[str, Any],
    proof: Mapping[str, Any],
    replay: Mapping[str, Any],
) -> str:
    """Classify the finding's epistemic status."""
    category = str(detail.get("category") or "")
    if category == "expected_precondition_failure":
        return "expected"
    promoted = bool(_mapping(proof.get("verdict")).get("promoted"))
    if replay.get("status") == "verified" and (
        promoted or category in {"likely_bug", "lifecycle_contract", "semantic_contract"}
    ):
        return "supported"
    return "exploratory"


def _claim_statement(
    target: str,
    detail: Mapping[str, Any],
    observation: Mapping[str, Any],
    replay: Mapping[str, Any],
) -> str:
    """Write the smallest claim justified by the finding."""
    kind = str(observation.get("kind") or "finding")
    if detail.get("category") == "expected_precondition_failure":
        error_type = observation.get("error_type") or "the documented error"
        return f"The recorded input triggers the documented {error_type} precondition in {target}."
    if kind == "exception":
        error_type = observation.get("error_type") or "an exception"
        message = observation.get("message")
        suffix = f": {message}" if message else ""
        if replay.get("status") == "verified":
            return f"The recorded input reproducibly makes {target} raise {error_type}{suffix}."
        return f"The recorded input made {target} raise {error_type}{suffix} during this scan."
    if kind == "property_violation":
        verb = "reproducibly violates" if replay.get("status") == "verified" else "violates"
        return (
            f"The recorded counterexample {verb} the inferred "
            f"{observation.get('property')} property for {target}."
        )
    if kind == "contract_violation":
        contract = observation.get("contract")
        if replay.get("status") == "verified":
            return f"The recorded input reproducibly violates {contract} for {target}."
        return f"The recorded input violated {contract} for {target} during this scan."
    return str(detail.get("summary") or f"A finding was observed for {target}.")


def _post_fix_control(*, expected: bool, witness_available: bool) -> dict[str, Any]:
    """Describe the unperformed same-witness control without overstating it."""
    if expected:
        return {
            "status": "not_applicable",
            "method": None,
            "acceptance": "No defect claim was made for this documented precondition.",
        }
    if not witness_available:
        return {
            "status": "not_ready",
            "method": "capture_witness_then_retest",
            "acceptance": (
                "Capture an exact witness before claiming that a fix closes this finding."
            ),
        }
    return {
        "status": "pending",
        "method": "same_witness_after_fix",
        "acceptance": "After the fix, the generated regression must pass on the same witness.",
    }


def _boundaries(replay: Mapping[str, Any]) -> dict[str, Any]:
    """State what the observation establishes and explicitly leave broader claims open."""
    if replay.get("status") == "verified":
        attempts = replay.get("attempts", 0)
        matches = replay.get("exact_matches", 0)
        match_basis = replay.get("match_basis") or "the recorded failure"
        establishes = (
            f"The same input produced the {match_basis} in {matches}/{attempts} immediate replays."
        )
    else:
        establishes = "An observation or counterexample, without exact replay confirmation."
    return {
        "establishes": establishes,
        "does_not_establish": [
            "the root cause",
            "behavior for untested inputs or states",
            "that a future fix works",
        ],
    }


def _build_divergence_evidence(
    *,
    revisions: Mapping[str, Any],
    comparison: Mapping[str, Any],
    original_input: Mapping[str, Any],
    minimized_input: Mapping[str, Any],
    original_observations: Mapping[str, Any],
    observations: Mapping[str, Any],
    differences: Sequence[str],
    replay_attempts: int,
    replay_matches: int,
    expected_signature: str,
    observed_signatures: Sequence[str | None],
    witness_source: str = "hypothesis_shrunk_counterexample",
    minimization_method: str = "hypothesis shrinking",
    minimization_boundary: str = (
        "Shrinking searched only within the declared or inferred Hypothesis strategies."
    ),
) -> dict[str, Any]:
    """Build one source-bound, replay-scoped differential evidence artifact."""
    ready_revisions = _json_ready(revisions)
    ready_comparison = _json_ready(comparison)
    ready_original_input = _json_ready(original_input)
    ready_minimized_input = _json_ready(minimized_input)
    ready_original_observations = _json_ready(original_observations)
    ready_observations = _json_ready(observations)
    attempts = _integer(replay_attempts)
    matches = min(attempts, _integer(replay_matches))
    replay_status = "verified" if attempts > 0 and matches == attempts else "failed"

    missing_bindings: list[str] = []
    for revision in ("a", "b"):
        binding = _mapping(_mapping(ready_revisions).get(revision))
        if not binding.get("source_sha256"):
            missing_bindings.append(f"revision_{revision}")
    for role in ("comparator", "normalizer"):
        binding = _mapping(_mapping(ready_comparison).get(role))
        if not binding.get("source_sha256"):
            missing_bindings.append(role)
    binding_status = "complete" if not missing_bindings else "partial"
    supported = replay_status == "verified" and binding_status == "complete"

    witness = {
        "available": True,
        "original_input": ready_original_input,
        "original_sha256": _sha256_json(ready_original_input),
        "input": ready_minimized_input,
        "sha256": _sha256_json(ready_minimized_input),
        "source": witness_source,
    }
    changed = witness["original_sha256"] != witness["sha256"]
    minimization_status = (
        "not_run"
        if minimization_method == "not_run"
        else "verified"
        if replay_status == "verified"
        else "performed"
    )
    establishes = (
        "The recorded minimized input produced the same paired observations and "
        f"full-envelope divergence in {matches}/{attempts} exact immediate replays "
        "under the recorded comparator and normalizer."
        if replay_status == "verified"
        else (
            "A full-envelope divergence was observed for the recorded input, but only "
            f"{matches}/{attempts} immediate replays matched its paired observations."
        )
    )
    if missing_bindings:
        establishes += " Source binding is incomplete for: " + ", ".join(missing_bindings) + "."

    payload: dict[str, Any] = {
        "schema": _DIVERGENCE_EVIDENCE_SCHEMA,
        "status": "supported" if supported else "exploratory",
        "claim": (
            "For the recorded input, revision a and revision b produced different "
            "observable outcomes under the recorded comparison semantics."
        ),
        "revisions": ready_revisions,
        "source_binding": {
            "status": binding_status,
            "missing": missing_bindings,
        },
        "comparison": ready_comparison,
        "witness": witness,
        "observations": ready_observations,
        "replay": {
            "status": replay_status,
            "attempts": attempts,
            "exact_matches": matches,
            "match_basis": (
                "same minimized input, bound revisions, comparison semantics, "
                "paired observations, and divergence channels"
            ),
            "expected_signature": expected_signature,
            "observed_signatures": list(observed_signatures),
        },
        "minimization": {
            "status": minimization_status,
            "method": minimization_method,
            "changed_input": changed,
            "original_input": ready_original_input,
            "original_observations": ready_original_observations,
            "boundary": minimization_boundary,
        },
        "differences": list(differences),
        "boundaries": {
            "establishes": establishes,
            "does_not_establish": [
                "the root cause",
                "behavior for untested inputs, states, or unselected side effects",
                "general equivalence when no divergence is observed",
                "that either revision is correct",
            ],
        },
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
    }
    payload["artifact_id"] = f"div_{_sha256_json(payload)[:16]}"
    return payload


def _build_finding_evidence(
    detail: Mapping[str, Any],
    *,
    module: str | None = None,
) -> dict[str, Any]:
    """Build one compact, falsifiable evidence card for a scan finding.

    The card records exactly what was observed, whether exact replay passed,
    what post-fix control remains to run, and which broader claims are not
    supported. It never claims that a patched revision was tested when it was
    not.
    """
    proof = _mapping(detail.get("proof_bundle"))
    target = _target(detail, module)
    witness = _witness(detail, proof)
    observation = _observation(detail, proof)
    replay = _replay(detail, proof)
    minimization = _minimization(detail, replay)
    status = _claim_status(detail, proof, replay)
    expected = status == "expected"
    holds = _integer(detail.get("holds"))
    total = _integer(detail.get("total"))
    passing_examples = holds if total > 0 else None
    failing_examples = max(0, total - holds) if total > 0 else None
    source_sha256 = str(detail.get("source_sha256") or "").strip() or None
    post_fix_control = _post_fix_control(
        expected=expected,
        witness_available=bool(witness["available"]),
    )
    regression_status = (
        "not_applicable" if expected else "not_ready" if not witness["available"] else "not_saved"
    )
    ci_status = "not_applicable" if expected else "not_ready"
    return {
        "schema": _FINDING_EVIDENCE_SCHEMA,
        "status": status,
        "claim": _claim_statement(target, detail, observation, replay),
        "subject": {
            "target": target,
            "source_sha256": source_sha256,
        },
        "witness": witness,
        "observation": observation,
        "replay": replay,
        "minimization": minimization,
        "contrast": {
            "status": "observed" if total > 0 else "not_measured",
            "passing_examples": passing_examples,
            "failing_examples": failing_examples,
        },
        "regression": {
            "status": regression_status,
            "path": None,
            "test_name": None,
            "binding": None,
        },
        "post_fix_control": post_fix_control,
        "ci_guard": {
            "status": ci_status,
            "command": None,
            "acceptance": (
                "The bound regression must remain unchanged and pass in CI."
                if not expected
                else "No CI defect guard is needed for this documented precondition."
            ),
        },
        "workflow": {
            "discover": "observed",
            "reproduce": replay["status"],
            "minimize": minimization["status"],
            "save_regression": regression_status,
            "verify_fix": post_fix_control["status"],
            "guard_ci": ci_status,
        },
        "boundaries": _boundaries(replay),
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
    }
