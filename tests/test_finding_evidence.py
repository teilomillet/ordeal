"""Tests for bounded, user-facing scan evidence cards."""

from __future__ import annotations

import json

from ordeal.finding_evidence import _build_divergence_evidence, _build_finding_evidence


def _crash_detail(**overrides: object) -> dict[str, object]:
    """Return a replay-verified crash detail with optional overrides."""
    detail: dict[str, object] = {
        "kind": "crash",
        "category": "likely_bug",
        "function": "divide",
        "error": "ZeroDivisionError: division by zero",
        "failing_args": {"a": 1.0, "b": 0.0},
        "replayable": True,
        "replay_attempts": 2,
        "replay_matches": 2,
        "source_sha256": "a" * 64,
        "proof_bundle": {
            "witness": {"input": {"a": 1.0, "b": 0.0}, "source": "boundary"},
            "failure_path": {
                "error_type": "ZeroDivisionError",
                "error": "division by zero",
            },
            "verdict": {"promoted": True},
        },
    }
    detail.update(overrides)
    return detail


def test_replay_verified_crash_has_bounded_supported_claim() -> None:
    card = _build_finding_evidence(_crash_detail(), module="pkg.math")

    assert card["schema"] == "ordeal.finding-evidence/v1"
    assert card["status"] == "supported"
    assert card["subject"] == {
        "target": "pkg.math.divide",
        "source_sha256": "a" * 64,
    }
    assert card["witness"]["sha256"] == (
        "d4bbbac5a1a3152e40c88f1b22b2b216344d4d0a69388013046a8f2a4af50a60"
    )
    assert card["replay"] == {
        "status": "verified",
        "attempts": 2,
        "exact_matches": 2,
        "match_basis": "same exception type and message",
        "command": None,
    }
    assert card["post_fix_control"]["status"] == "pending"
    assert card["post_fix_control"]["method"] == "same_witness_after_fix"
    assert "2/2 immediate replays" in card["boundaries"]["establishes"]
    assert "the root cause" in card["boundaries"]["does_not_establish"]
    json.dumps(card)


def test_divergence_without_minimization_cannot_be_supported() -> None:
    artifact = _build_divergence_evidence(
        revisions={
            "a": {"source_sha256": "a" * 64},
            "b": {"source_sha256": "b" * 64},
        },
        comparison={
            "comparator": {"source_sha256": "c" * 64},
            "normalizer": {"source_sha256": "d" * 64},
        },
        original_input={"value": 1},
        minimized_input={"value": 1},
        original_observations={"a": 1, "b": 2},
        observations={"a": 1, "b": 2},
        differences=["return_value"],
        replay_attempts=2,
        replay_matches=2,
        expected_signature="same",
        observed_signatures=["same", "same"],
        minimization_method="not_run",
        minimization_boundary="No reducer was run.",
    )

    assert artifact["replay"]["status"] == "verified"
    assert artifact["minimization"]["status"] == "not_run"
    assert artifact["status"] == "exploratory"
    assert "minimization was not established" in artifact["boundaries"]["establishes"]


def test_replay_mismatch_remains_exploratory() -> None:
    card = _build_finding_evidence(
        _crash_detail(replayable=False, replay_matches=1),
        module="pkg.math",
    )

    assert card["status"] == "exploratory"
    assert card["replay"]["status"] == "failed"
    assert card["replay"]["exact_matches"] == 1
    assert "without exact replay confirmation" in card["boundaries"]["establishes"]


def test_replay_card_preserves_source_seam_match_basis() -> None:
    detail = _crash_detail()
    proof = detail["proof_bundle"]
    assert isinstance(proof, dict)
    proof["reproduction"] = {
        "match_basis": "same exception type, message, and terminal source location"
    }

    card = _build_finding_evidence(detail, module="pkg.math")

    assert card["replay"]["match_basis"] == (
        "same exception type, message, and terminal source location"
    )
    assert "terminal source location" in card["boundaries"]["establishes"]


def test_property_card_reports_observed_contrast_without_certifying_rule() -> None:
    card = _build_finding_evidence(
        {
            "kind": "property",
            "category": "speculative_property",
            "function": "normalize",
            "name": "idempotent",
            "summary": "idempotent (87%)",
            "holds": 26,
            "total": 30,
            "counterexample": {"input": {"xs": [9, 8, 7]}},
        },
        module="pkg.data",
    )

    assert card["status"] == "exploratory"
    assert card["contrast"] == {
        "status": "observed",
        "passing_examples": 26,
        "failing_examples": 4,
    }
    assert card["witness"]["sha256"] == (
        "80bec031bb73e92f34335a03abfa68b1d1955f9949bacf624b71606c6c095b2f"
    )
    assert card["replay"]["status"] == "not_run"
    assert card["post_fix_control"]["status"] == "pending"


def test_property_card_tracks_replay_minimization_and_durable_workflow() -> None:
    card = _build_finding_evidence(
        {
            "kind": "property",
            "category": "speculative_property",
            "function": "normalize",
            "name": "idempotent",
            "counterexample": {"input": {"xs": [0]}},
            "replayable": True,
            "replay_attempts": 2,
            "replay_matches": 2,
            "replay_match_basis": "same inferred property violated on the same input",
            "minimization": {
                "status": "verified",
                "method": "hypothesis.find",
                "original_complexity": 19,
                "minimized_complexity": 10,
                "replay_attempts": 2,
                "replay_matches": 2,
                "boundary": "Shrunk within the declared strategies.",
            },
        },
        module="pkg.data",
    )

    assert card["status"] == "exploratory"
    assert card["replay"]["status"] == "verified"
    assert card["replay"]["match_basis"] == ("same inferred property violated on the same input")
    assert card["minimization"]["status"] == "verified"
    assert card["regression"]["status"] == "not_saved"
    assert card["ci_guard"]["status"] == "not_ready"
    assert card["workflow"] == {
        "discover": "observed",
        "reproduce": "verified",
        "minimize": "verified",
        "save_regression": "not_saved",
        "verify_fix": "pending",
        "guard_ci": "not_ready",
    }


def test_expected_precondition_is_not_presented_as_a_defect() -> None:
    card = _build_finding_evidence(
        {
            "kind": "precondition",
            "category": "expected_precondition_failure",
            "function": "parse",
            "error_type": "ValueError",
            "error": "value must be positive",
            "failing_args": {"value": -1},
        },
        module="pkg.input",
    )

    assert card["status"] == "expected"
    assert "documented ValueError precondition" in card["claim"]
    assert card["post_fix_control"]["status"] == "not_applicable"


def test_empty_argument_witness_is_preserved_and_missing_witness_blocks_control() -> None:
    empty = _build_finding_evidence(
        _crash_detail(
            failing_args={},
            proof_bundle={
                "witness": {"input": {}, "source": "random_fuzz"},
                "verdict": {"promoted": True},
            },
        ),
        module="pkg.jobs",
    )
    missing = _build_finding_evidence(
        {"kind": "mutation", "function": "run", "summary": "one mutant survived"},
        module="pkg.jobs",
    )

    assert empty["witness"]["available"] is True
    assert empty["witness"]["input"] == {}
    assert empty["witness"]["sha256"] == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert missing["witness"]["available"] is False
    assert missing["post_fix_control"]["status"] == "not_ready"
    assert missing["post_fix_control"]["method"] == "capture_witness_then_retest"
