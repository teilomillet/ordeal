"""Tests for ordeal.agent_schema."""

from __future__ import annotations

import json
from pathlib import Path

from ordeal.agent_schema import (
    AgentArtifact,
    AgentEnvelope,
    AgentFinding,
    SCHEMA_VERSION,
    build_agent_envelope,
)


class TestAgentSchema:
    def test_build_agent_envelope_produces_stable_required_keys(self):
        envelope = build_agent_envelope(
            tool="audit",
            target="pkg.mod",
            status="findings found",
            summary="1 gap remains",
            recommended_action="add a regression test for the uncovered branch",
            suggested_commands=("ordeal scan pkg.mod --save-artifacts", "ordeal mutate pkg.mod"),
            suggested_test_file="tests/test_pkg_mod.py",
            confidence=0.87,
            confidence_basis=("measured under 20 examples", "counterexample recorded"),
            blocking_reason="coverage gap remains",
            findings=[
                {
                    "kind": "property-gap",
                    "summary": "idempotent property survived",
                    "confidence": 0.93,
                    "target": "pkg.mod.normalize",
                    "location": "L42:8",
                    "details": {"evidence": Path("tests/test_pkg_mod.py")},
                }
            ],
            artifacts=[
                AgentArtifact(
                    kind="report",
                    uri=Path(".ordeal/findings/pkg/mod.md").as_posix(),
                    description="shareable bug report",
                    metadata={"tags": {"audit", "regression"}},
                )
            ],
            raw_details={
                "module_path": Path("pkg/mod.py"),
                "extra_paths": {Path("a.txt"), Path("b.txt")},
            },
        )

        payload = envelope.to_dict()

        assert envelope.schema_version == SCHEMA_VERSION
        assert list(payload.keys()) == [
            "schema_version",
            "tool",
            "target",
            "status",
            "summary",
            "recommended_action",
            "suggested_commands",
            "suggested_test_file",
            "confidence",
            "confidence_basis",
            "blocking_reason",
            "findings",
            "artifacts",
            "raw_details",
        ]
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["tool"] == "audit"
        assert payload["target"] == "pkg.mod"
        assert payload["suggested_commands"] == [
            "ordeal scan pkg.mod --save-artifacts",
            "ordeal mutate pkg.mod",
        ]
        assert payload["suggested_test_file"] == "tests/test_pkg_mod.py"
        assert payload["confidence"] == 0.87
        assert payload["confidence_basis"] == [
            "measured under 20 examples",
            "counterexample recorded",
        ]
        assert payload["blocking_reason"] == "coverage gap remains"
        assert payload["findings"][0]["details"]["evidence"] == "tests/test_pkg_mod.py"
        assert payload["artifacts"][0]["metadata"]["tags"] == ["audit", "regression"]
        assert payload["raw_details"]["module_path"] == "pkg/mod.py"
        assert payload["raw_details"]["extra_paths"] == ["a.txt", "b.txt"]

    def test_to_json_is_stable_and_json_serializable(self):
        envelope = AgentEnvelope(
            tool="scan",
            target="pkg.mod",
            status="no findings yet",
            summary="scan completed without gaps",
            recommended_action="continue with mutate",
            suggested_commands=("ordeal mutate pkg.mod",),
            suggested_test_file=None,
            confidence=None,
            confidence_basis=(),
            blocking_reason=None,
            findings=(
                AgentFinding(
                    kind="crash",
                    summary="function crashed on generated input",
                    confidence=0.91,
                    target="pkg.mod.div",
                    location="L12:4",
                    details={"failing_args": {"b": 0}},
                ),
            ),
            artifacts=(),
            raw_details={"trace": {"seed": 42}},
        )

        first = envelope.to_json()
        second = envelope.to_json()
        payload = json.loads(first)

        assert first == second
        assert payload == envelope.to_dict()
        assert list(payload.keys()) == sorted(payload.keys())
        assert payload["findings"][0]["details"]["failing_args"]["b"] == 0

    def test_helpers_accept_plain_mappings(self):
        envelope = build_agent_envelope(
            tool="replay",
            target=".ordeal/traces/run-1.json",
            status="failure reproduced",
            summary="trace replay succeeded",
            recommended_action="shrink and ablate",
            suggested_test_file=None,
            confidence_basis=(),
            findings=(),
            artifacts=(),
            raw_details={
                "trace_file": Path(".ordeal/traces/run-1.json"),
                "blocking": False,
            },
        )

        payload = json.loads(envelope.to_json())

        assert payload["tool"] == "replay"
        assert payload["raw_details"]["trace_file"] == ".ordeal/traces/run-1.json"
        assert payload["raw_details"]["blocking"] is False
