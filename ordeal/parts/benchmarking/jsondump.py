from __future__ import annotations
# ruff: noqa
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
def _json_dump(payload: Any) -> str:
    """Return stable JSON for suite artifacts."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
def _canonical_json(payload: Any) -> bytes:
    """Return canonical UTF-8 JSON for integrity digests."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
def _sha256_payload(payload: Any) -> str:
    """Return the SHA-256 digest of a canonical JSON payload."""
    return hashlib.sha256(_canonical_json(payload)).hexdigest()
def _sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of one file's exact bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _clean_str_list(value: Any) -> tuple[str, ...]:
    """Normalize a TOML string/list field into a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, list):
        items: list[str] = []
        for raw in value:
            text = str(raw).strip()
            if text:
                items.append(text)
        return tuple(items)
    raise ValueError(f"Expected string or list[str], got {type(value).__name__}")
def _case_string(
    raw_case: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    *,
    required: bool = False,
) -> str | None:
    """Return one normalized string value from a case/default pair."""
    raw = raw_case.get(key, defaults.get(key))
    if raw is None:
        if required:
            raise ValueError(f"Case is missing required field {key!r}")
        return None
    text = str(raw).strip()
    if not text:
        if required:
            raise ValueError(f"Case field {key!r} cannot be empty")
        return None
    return text
def _case_int(
    raw_case: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    *,
    default: int,
) -> int:
    """Return one integer field from a case/default pair."""
    raw = raw_case.get(key, defaults.get(key, default))
    value = int(raw)
    if value <= 0:
        raise ValueError(f"Case field {key!r} must be >= 1")
    return value
def _case_bool(
    raw_case: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    """Return one boolean field from a case/default pair."""
    raw = raw_case.get(key, defaults.get(key, default))
    return bool(raw)
def _case_float(
    raw_case: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
) -> float | None:
    """Return one optional float field from a case/default pair."""
    raw = raw_case.get(key, defaults.get(key))
    if raw is None:
        return None
    value = float(raw)
    if value <= 0:
        raise ValueError(f"Case field {key!r} must be > 0")
    return value
def _match_target(expected: str, actual: str) -> bool:
    """Return whether *actual* matches the expected target selector."""
    exp = expected.strip()
    act = actual.strip()
    if not exp or not act:
        return False
    if exp == act:
        return True
    if act.endswith(f".{exp}"):
        return True
    if exp.endswith(f".{act}"):
        return True
    return False
def _find_bugsinpy_executable(
    name: str,
    *,
    bugsinpy_root: str | None,
) -> str:
    """Resolve one BugsInPy helper executable from PATH or *bugsinpy_root*."""
    if bugsinpy_root:
        candidate = Path(bugsinpy_root) / "framework" / "bin" / name
        if candidate.exists():
            return str(candidate)
    path = shutil.which(name)
    if path:
        return path
    raise FileNotFoundError(
        f"Could not locate {name!r}. Install BugsInPy or pass --bugsinpy-root."
    )
def _scan_command(spec: "BugBenchmarkSpec") -> list[str]:
    """Return the `ordeal scan` argument list for one benchmark case."""
    command = [
        "scan",
        spec.module,
        "--json",
        "--mode",
        spec.mode,
        "--max-examples",
        str(spec.max_examples),
    ]
    if spec.time_limit is not None:
        command.extend(["--time-limit", str(spec.time_limit)])
    for target in spec.targets:
        command.extend(["--target", target])
    if spec.save_artifacts:
        command.append("--save-artifacts")
    return command
@dataclass(frozen=True)
class BugBenchmarkCertificationPolicy:
    """Fail-closed requirements for a benchmark evidence certificate."""

    enabled: bool = False
    confidence_level: float = 0.95
    min_positive_cases: int = 1
    min_negative_cases: int = 1
    min_recall: float = 1.0
    min_precision: float = 1.0
    min_specificity: float = 1.0
    min_confidence_bound: float = 0.0
    require_complete: bool = True
    require_provenance: bool = True
    require_paired_controls: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly policy payload."""
        return {
            "enabled": self.enabled,
            "confidence_level": self.confidence_level,
            "min_positive_cases": self.min_positive_cases,
            "min_negative_cases": self.min_negative_cases,
            "min_recall": self.min_recall,
            "min_precision": self.min_precision,
            "min_specificity": self.min_specificity,
            "min_confidence_bound": self.min_confidence_bound,
            "require_complete": self.require_complete,
            "require_provenance": self.require_provenance,
            "require_paired_controls": self.require_paired_controls,
        }
@dataclass(frozen=True)
class BugBenchmarkSpec:
    """One benchmark case from a manifest."""

    name: str
    module: str
    dataset: str = "custom"
    protocol: str = "scan"
    tier: str = "public"
    workspace: str | None = None
    project: str | None = None
    bug_id: str | None = None
    expected_outcome: str = "bug"
    pair_id: str | None = None
    evidence_path: str | None = None
    selection_reason: str = ""
    oracle_source: str = ""
    oracle_url: str | None = None
    evidence_level: str = ""
    saturation_risk: str = "unknown"
    allowed_for_optimization: bool = False
    harvested_at: str | None = None
    fix_commit: str | None = None
    failure_command: str | None = None
    oracle_python_version: str | None = None
    requires_python: str | None = None
    python_version: str | None = None
    pythonpath: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    expected_targets: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    expected_error_type: str | None = None
    expected_error_message: str | None = None
    expected_witness_sha256: str | None = None
    max_examples: int = 20
    mode: str = "candidate"
    time_limit: float | None = None
    save_artifacts: bool = False
    compile_checkout: bool = True
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the case specification."""
        return {
            "name": self.name,
            "module": self.module,
            "dataset": self.dataset,
            "protocol": self.protocol,
            "tier": self.tier,
            "workspace": self.workspace,
            "project": self.project,
            "bug_id": self.bug_id,
            "expected_outcome": self.expected_outcome,
            "pair_id": self.pair_id,
            "evidence_path": self.evidence_path,
            "selection_reason": self.selection_reason,
            "oracle_source": self.oracle_source,
            "oracle_url": self.oracle_url,
            "evidence_level": self.evidence_level,
            "saturation_risk": self.saturation_risk,
            "allowed_for_optimization": self.allowed_for_optimization,
            "harvested_at": self.harvested_at,
            "fix_commit": self.fix_commit,
            "failure_command": self.failure_command,
            "oracle_python_version": self.oracle_python_version,
            "requires_python": self.requires_python,
            "python_version": self.python_version,
            "pythonpath": list(self.pythonpath),
            "targets": list(self.targets),
            "expected_targets": list(self.expected_targets),
            "expected_files": list(self.expected_files),
            "expected_error_type": self.expected_error_type,
            "expected_error_message": self.expected_error_message,
            "expected_witness_sha256": self.expected_witness_sha256,
            "max_examples": self.max_examples,
            "mode": self.mode,
            "time_limit": self.time_limit,
            "save_artifacts": self.save_artifacts,
            "compile_checkout": self.compile_checkout,
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }
@dataclass(frozen=True)
class BugBenchmarkCaseResult:
    """Observed outcome for one benchmark case."""

    spec: BugBenchmarkSpec
    status: str
    seconds: float
    exit_code: int
    summary: str
    workspace: str
    command: tuple[str, ...]
    matched_targets: tuple[str, ...] = ()
    matched_files: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    raw_result: dict[str, Any] = field(default_factory=dict)
    evidence_verification: dict[str, Any] | None = None
    error: str | None = None

    @property
    def hit(self) -> bool:
        """Whether the case produced a finding that matched the ground truth."""
        return self.status == "hit"

    @property
    def classification(self) -> str | None:
        """Return the confusion-matrix class for a completed case."""
        expected = {
            "bug": {"hit": "true_positive", "miss": "false_negative"},
            "clean": {
                "false_positive": "false_positive",
                "correct_rejection": "true_negative",
            },
        }
        return expected.get(self.spec.expected_outcome, {}).get(self.status)

    @property
    def classification_correct(self) -> bool:
        """Whether this case agrees with its positive or negative oracle."""
        return self.classification in {"true_positive", "true_negative"}

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of one benchmark result."""
        return {
            "name": self.spec.name,
            "dataset": self.spec.dataset,
            "tier": self.spec.tier,
            "module": self.spec.module,
            "status": self.status,
            "hit": self.hit,
            "classification": self.classification,
            "classification_correct": self.classification_correct,
            "seconds": self.seconds,
            "exit_code": self.exit_code,
            "workspace": self.workspace,
            "command": list(self.command),
            "matched_targets": list(self.matched_targets),
            "matched_files": list(self.matched_files),
            "finding_count": len(self.findings),
            "artifact_count": len(self.artifacts),
            "summary": self.summary,
            "error": self.error,
            "spec": self.spec.to_dict(),
            "findings": [dict(item) for item in self.findings],
            "artifacts": [dict(item) for item in self.artifacts],
            "raw_result": dict(self.raw_result),
            "evidence_verification": (
                dict(self.evidence_verification)
                if self.evidence_verification is not None
                else None
            ),
        }
def _rate(numerator: int, denominator: int) -> float | None:
    """Return a binomial rate, or ``None`` when it is undefined."""
    if denominator <= 0:
        return None
    return numerator / denominator
def _wilson_lower_bound(successes: int, total: int, confidence_level: float) -> float | None:
    """Return the two-sided Wilson interval's lower bound for a binomial rate."""
    if total <= 0:
        return None
    z = statistics.NormalDist().inv_cdf(1 - (1 - confidence_level) / 2)
    observed = successes / total
    denominator = 1 + z * z / total
    center = observed + z * z / (2 * total)
    spread = z * ((observed * (1 - observed) / total + z * z / (4 * total**2)) ** 0.5)
    return max(0.0, (center - spread) / denominator)
