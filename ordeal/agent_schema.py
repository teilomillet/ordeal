"""Typed agent-facing result envelopes.

This module defines a stable JSON shape for machine consumers of ordeal.
It is intentionally small and dependency-free so command handlers can
wrap their existing results without introducing new coupling.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"


def _jsonable(value: Any) -> Any:
    """Convert *value* into something JSON serializable."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="backslashreplace")
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_jsonable(item) for item in value]
        return sorted(items, key=lambda item: repr(item))
    if isinstance(value, Sequence):
        return [_jsonable(item) for item in value]
    if isinstance(value, Exception):
        return f"{type(value).__name__}: {value}"
    return str(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentFinding:
    """One structured finding for an AI agent."""

    kind: str
    summary: str
    confidence: float | None = None
    target: str | None = None
    location: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "kind": self.kind,
            "summary": self.summary,
            "confidence": self.confidence,
            "target": self.target,
            "location": self.location,
            "details": _jsonable(self.details),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentArtifact:
    """A generated artifact produced by ordeal."""

    kind: str
    uri: str
    description: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "kind": self.kind,
            "uri": self.uri,
            "description": self.description,
            "metadata": _jsonable(self.metadata),
        }


def _coerce_finding(value: AgentFinding | Mapping[str, Any]) -> AgentFinding:
    """Normalize a finding input into :class:`AgentFinding`."""
    if isinstance(value, AgentFinding):
        return value
    return AgentFinding(
        kind=str(value["kind"]),
        summary=str(value["summary"]),
        confidence=(float(value["confidence"]) if value.get("confidence") is not None else None),
        target=str(value["target"]) if value.get("target") is not None else None,
        location=str(value["location"]) if value.get("location") is not None else None,
        details=dict(value.get("details", {})),
    )


def _coerce_artifact(value: AgentArtifact | Mapping[str, Any]) -> AgentArtifact:
    """Normalize an artifact input into :class:`AgentArtifact`."""
    if isinstance(value, AgentArtifact):
        return value
    return AgentArtifact(
        kind=str(value["kind"]),
        uri=str(value["uri"]),
        description=str(value["description"]) if value.get("description") is not None else None,
        metadata=dict(value.get("metadata", {})),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentEnvelope:
    """Stable JSON envelope for command output."""

    tool: str
    target: str
    status: str
    summary: str
    recommended_action: str
    suggested_commands: tuple[str, ...] = ()
    suggested_test_file: str | None = None
    confidence: float | None = None
    confidence_basis: tuple[str, ...] = ()
    blocking_reason: str | None = None
    findings: tuple[AgentFinding, ...] = ()
    artifacts: tuple[AgentArtifact, ...] = ()
    raw_details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the stable machine-readable envelope."""
        return {
            "schema_version": self.schema_version,
            "tool": self.tool,
            "target": self.target,
            "status": self.status,
            "summary": self.summary,
            "recommended_action": self.recommended_action,
            "suggested_commands": list(self.suggested_commands),
            "suggested_test_file": self.suggested_test_file,
            "confidence": self.confidence,
            "confidence_basis": list(self.confidence_basis),
            "blocking_reason": self.blocking_reason,
            "findings": [finding.to_dict() for finding in self.findings],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "raw_details": _jsonable(self.raw_details),
        }

    def to_json(self) -> str:
        """Serialize the envelope with a stable key order."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False)


def build_agent_envelope(
    *,
    tool: str,
    target: str,
    status: str,
    summary: str,
    recommended_action: str = "",
    suggested_commands: Sequence[str] = (),
    suggested_test_file: str | None = None,
    confidence: float | None = None,
    confidence_basis: Sequence[str] = (),
    blocking_reason: str | None = None,
    findings: Sequence[AgentFinding | Mapping[str, Any]] = (),
    artifacts: Sequence[AgentArtifact | Mapping[str, Any]] = (),
    raw_details: Mapping[str, Any] | None = None,
    schema_version: str = SCHEMA_VERSION,
) -> AgentEnvelope:
    """Build an :class:`AgentEnvelope` from mixed structured inputs."""
    return AgentEnvelope(
        tool=tool,
        target=target,
        status=status,
        summary=summary,
        recommended_action=recommended_action,
        suggested_commands=tuple(str(command) for command in suggested_commands),
        suggested_test_file=str(suggested_test_file) if suggested_test_file is not None else None,
        confidence=confidence,
        confidence_basis=tuple(str(item) for item in confidence_basis),
        blocking_reason=blocking_reason,
        findings=tuple(_coerce_finding(item) for item in findings),
        artifacts=tuple(_coerce_artifact(item) for item in artifacts),
        raw_details=dict(raw_details or {}),
        schema_version=schema_version,
    )
