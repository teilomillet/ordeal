"""Benchmark-manifest runner for public and private bug corpora.

This module benchmarks the user-facing ``ordeal scan --json`` workflow against
curated bug cases. It supports provenance-backed public reproductions,
original BugsInPy checkouts, and rolling private holdouts with one manifest
format so teams can report public results without optimizing exclusively for a
saturated set.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class BugBenchmarkSuite:
    """Results for a benchmark manifest run, including optional certification."""

    cases: tuple[BugBenchmarkCaseResult, ...]
    manifest_path: str
    selected_tier: str | None = None
    certification_policy: BugBenchmarkCertificationPolicy = field(
        default_factory=BugBenchmarkCertificationPolicy
    )
    manifest_sha256: str | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def case_count(self) -> int:
        """Return the number of executed cases."""
        return len(self.cases)

    @property
    def hit_count(self) -> int:
        """Return the number of true-positive bug cases."""
        return sum(1 for case in self.cases if case.classification == "true_positive")

    @property
    def correct_rejection_count(self) -> int:
        """Return the number of true-negative control cases."""
        return sum(1 for case in self.cases if case.classification == "true_negative")

    @property
    def false_positive_count(self) -> int:
        """Return the number of clean controls incorrectly flagged."""
        return sum(1 for case in self.cases if case.classification == "false_positive")

    @property
    def blocked_count(self) -> int:
        """Return the number of blocked cases."""
        return sum(1 for case in self.cases if case.status == "blocked")

    @property
    def error_count(self) -> int:
        """Return the number of failed-execution cases."""
        return sum(1 for case in self.cases if case.status == "error")

    @property
    def miss_count(self) -> int:
        """Return the number of false-negative bug cases."""
        return sum(1 for case in self.cases if case.classification == "false_negative")

    @property
    def positive_case_count(self) -> int:
        """Return the number of scoreable cases whose oracle says a bug is present."""
        return self.hit_count + self.miss_count

    @property
    def negative_case_count(self) -> int:
        """Return the number of scoreable clean-control cases."""
        return self.correct_rejection_count + self.false_positive_count

    @property
    def precision(self) -> float | None:
        """Return precision over scored positive predictions."""
        return _rate(self.hit_count, self.hit_count + self.false_positive_count)

    @property
    def recall(self) -> float | None:
        """Return recall over scored bug cases."""
        return _rate(self.hit_count, self.hit_count + self.miss_count)

    @property
    def specificity(self) -> float | None:
        """Return specificity over scored clean controls."""
        return _rate(
            self.correct_rejection_count,
            self.correct_rejection_count + self.false_positive_count,
        )

    @property
    def artifact_case_count(self) -> int:
        """Return the number of cases that produced artifacts."""
        return sum(1 for case in self.cases if case.artifacts)

    @property
    def public_case_count(self) -> int:
        """Return the number of public-saturation-risk cases."""
        return sum(1 for case in self.cases if case.spec.saturation_risk == "public")

    @property
    def private_case_count(self) -> int:
        """Return the number of private-saturation-risk cases."""
        return sum(1 for case in self.cases if case.spec.saturation_risk == "private")

    @property
    def optimization_case_count(self) -> int:
        """Return the number of cases explicitly allowed for tuning."""
        return sum(1 for case in self.cases if case.spec.allowed_for_optimization)

    @property
    def report_only_case_count(self) -> int:
        """Return the number of cases reserved for reporting only."""
        return sum(1 for case in self.cases if not case.spec.allowed_for_optimization)

    @property
    def median_seconds(self) -> float:
        """Return the median wall time for executed cases."""
        if not self.cases:
            return 0.0
        return statistics.median(case.seconds for case in self.cases)

    @property
    def hit_rate(self) -> float:
        """Return the legacy share of all cases that were bug hits."""
        if not self.cases:
            return 0.0
        return self.hit_count / len(self.cases)

    @property
    def passed(self) -> bool:
        """Return whether every completed case agreed with its oracle."""
        return bool(self.cases) and all(case.classification_correct for case in self.cases)

    def _provenance_failures(self) -> list[str]:
        """Return failures in the declared oracle and pairing evidence."""
        failures: list[str] = []
        for case in self.cases:
            spec = case.spec
            missing = [
                name
                for name, value in (
                    ("selection_reason", spec.selection_reason),
                    ("oracle_source", spec.oracle_source),
                    ("oracle_url", spec.oracle_url),
                    ("evidence_level", spec.evidence_level),
                    ("fix_commit", spec.fix_commit),
                    ("failure_command", spec.failure_command),
                    ("pair_id", spec.pair_id),
                )
                if not value
            ]
            if missing:
                failures.append(f"case {spec.name!r} lacks {', '.join(missing)}")
            if spec.fix_commit and not re.fullmatch(r"[0-9a-fA-F]{7,64}", spec.fix_commit):
                failures.append(f"case {spec.name!r} has a non-commit fix_commit")
            if spec.oracle_url:
                if not spec.oracle_url.startswith("https://"):
                    failures.append(f"case {spec.name!r} oracle_url is not HTTPS")
                if spec.fix_commit and spec.fix_commit.lower() not in spec.oracle_url.lower():
                    failures.append(f"case {spec.name!r} oracle_url does not bind fix_commit")

        if self.certification_policy.require_paired_controls:
            pairs: dict[str, list[BugBenchmarkSpec]] = {}
            for case in self.cases:
                if case.spec.pair_id:
                    pairs.setdefault(case.spec.pair_id, []).append(case.spec)
            for pair_id, specs in sorted(pairs.items()):
                outcomes = sorted(spec.expected_outcome for spec in specs)
                if outcomes != ["bug", "clean"]:
                    failures.append(
                        f"pair {pair_id!r} must contain exactly one bug and one clean control"
                    )
                    continue
                for field_name in ("project", "bug_id", "fix_commit", "oracle_url"):
                    values = {getattr(spec, field_name) for spec in specs}
                    if len(values) != 1:
                        failures.append(f"pair {pair_id!r} disagrees on {field_name}")
            paired_cases = sum(len(specs) for specs in pairs.values())
            if paired_cases != len(self.cases):
                failures.append("every certified case must belong to a declared pair")
        return failures

    def _evidence_failures(self) -> list[str]:
        """Return failures in executable evidence required for certification."""
        failures: list[str] = []
        for case in self.cases:
            spec = case.spec
            if not spec.evidence_path:
                failures.append(f"case {spec.name!r} has no linked evidence record")
                continue
            verification = case.evidence_verification
            if not isinstance(verification, Mapping):
                failures.append(f"case {spec.name!r} has no evidence verification result")
                continue
            if verification.get("local_verified") is not True:
                failures.append(f"case {spec.name!r} lacks verified local evidence")
            binding = verification.get("manifest_binding")
            if not isinstance(binding, Mapping) or binding.get("passed") is not True:
                failures.append(f"case {spec.name!r} lacks a passing manifest evidence binding")
            online_required = verification.get("online_sources_required")
            if not isinstance(online_required, bool):
                failures.append(
                    f"case {spec.name!r} evidence does not declare its online-source requirement"
                )
            if verification.get("verified") is not True:
                failures.append(f"case {spec.name!r} evidence is not fully verified")
            if online_required is True and verification.get("sources_verified") is not True:
                failures.append(
                    f"case {spec.name!r} requires authoritative online source verification"
                )
        return failures

    def certification_assessment(self) -> dict[str, Any]:
        """Evaluate the manifest's point, uncertainty, and provenance requirements."""
        policy = self.certification_policy
        bounds = {
            "recall": _wilson_lower_bound(
                self.hit_count,
                self.hit_count + self.miss_count,
                policy.confidence_level,
            ),
            "precision": _wilson_lower_bound(
                self.hit_count,
                self.hit_count + self.false_positive_count,
                policy.confidence_level,
            ),
            "specificity": _wilson_lower_bound(
                self.correct_rejection_count,
                self.correct_rejection_count + self.false_positive_count,
                policy.confidence_level,
            ),
        }
        metrics = {
            "true_positives": self.hit_count,
            "false_negatives": self.miss_count,
            "false_positives": self.false_positive_count,
            "true_negatives": self.correct_rejection_count,
            "positive_cases": self.positive_case_count,
            "negative_cases": self.negative_case_count,
            "blocked": self.blocked_count,
            "errors": self.error_count,
            "recall": self.recall,
            "precision": self.precision,
            "specificity": self.specificity,
        }
        failures: list[str] = []
        if policy.enabled:
            failures.extend(self._evidence_failures())
            invalid_classifications = sum(
                case.classification is None and case.status not in {"blocked", "error"}
                for case in self.cases
            )
            if invalid_classifications:
                failures.append("case statuses disagree with their declared outcomes")
            if self.positive_case_count < policy.min_positive_cases:
                failures.append(
                    f"positive cases {self.positive_case_count} < {policy.min_positive_cases}"
                )
            if self.negative_case_count < policy.min_negative_cases:
                failures.append(
                    f"negative cases {self.negative_case_count} < {policy.min_negative_cases}"
                )
            for name, threshold in (
                ("recall", policy.min_recall),
                ("precision", policy.min_precision),
                ("specificity", policy.min_specificity),
            ):
                observed = metrics[name]
                if observed is None or float(observed) + 1e-12 < threshold:
                    failures.append(f"{name} does not meet {threshold:.3f}")
                lower = bounds[name]
                if lower is None or lower + 1e-12 < policy.min_confidence_bound:
                    failures.append(
                        f"{name} Wilson lower bound does not meet "
                        f"{policy.min_confidence_bound:.3f}"
                    )
            if policy.require_complete and (self.blocked_count or self.error_count):
                failures.append("blocked or error cases make the evidence incomplete")
            if policy.require_provenance:
                failures.extend(self._provenance_failures())
            if not self.manifest_sha256:
                failures.append("manifest digest is missing")
        return {
            "enabled": policy.enabled,
            "certified": policy.enabled and not failures,
            "assurance": "self_attested_reproducible_evidence",
            "policy": policy.to_dict(),
            "metrics": metrics,
            "confidence": {
                "level": policy.confidence_level,
                "method": "wilson_score_two_sided",
                "lower_bounds": bounds,
            },
            "failure_reasons": failures,
        }

    @property
    def certified(self) -> bool:
        """Return whether the declared certification contract is satisfied."""
        return bool(self.certification_assessment()["certified"])

    @property
    def check_passed(self) -> bool:
        """Return the fail-closed CLI gate result for this manifest."""
        if self.certification_policy.enabled:
            return self.certified
        return self.passed

    def summary(self) -> str:
        """Return a compact text summary."""
        status = "PASS" if self.passed else "FAIL"
        certificate_status = ""
        if self.certification_policy.enabled:
            certificate_status = " [CERTIFIED]" if self.certified else " [UNCERTIFIED]"
        rates = (
            f"recall={self.recall:.0%}, precision={self.precision:.0%}, "
            f"specificity={self.specificity:.0%}"
            if self.recall is not None
            and self.precision is not None
            and self.specificity is not None
            else "precision/recall/specificity=undefined"
        )
        lines = [
            f"Bug Benchmark [{status}]{certificate_status}",
            f"  manifest={self.manifest_path}",
            (
                f"  cases={self.case_count}, true_positives={self.hit_count}, "
                f"false_negatives={self.miss_count}, false_positives={self.false_positive_count}, "
                f"true_negatives={self.correct_rejection_count}"
            ),
            f"  quality: {rates}",
            (
                f"  incomplete: blocked={self.blocked_count}, errors={self.error_count}; "
                f"artifacts={self.artifact_case_count}/{self.case_count}; "
                f"median_seconds={self.median_seconds:.3f}"
            ),
            (
                f"  epistemics: public={self.public_case_count}, "
                f"private={self.private_case_count}, "
                f"optimize={self.optimization_case_count}, "
                f"report_only={self.report_only_case_count}"
            ),
        ]
        if self.selected_tier is not None:
            lines.append(f"  tier={self.selected_tier}")
        if self.certification_policy.enabled and not self.certified:
            for failure in self.certification_assessment()["failure_reasons"]:
                lines.append(f"  certificate failure: {failure}")
        for case in self.cases:
            lines.append(
                f"  {case.status.upper()} {case.spec.name}: {case.summary} ({case.seconds:.3f}s)"
            )
        return "\n".join(lines)

    def _evidence_dict(self) -> dict[str, Any]:
        """Return the evidence payload covered by the certificate digest."""
        assessment = self.certification_assessment()
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "selected_tier": self.selected_tier,
            "case_count": self.case_count,
            "hit_count": self.hit_count,
            "hit_rate": self.hit_rate,
            "miss_count": self.miss_count,
            "false_positive_count": self.false_positive_count,
            "correct_rejection_count": self.correct_rejection_count,
            "blocked_count": self.blocked_count,
            "error_count": self.error_count,
            "artifact_case_count": self.artifact_case_count,
            "median_seconds": self.median_seconds,
            "precision": self.precision,
            "recall": self.recall,
            "specificity": self.specificity,
            "passed": self.passed,
            "certified": self.certified,
            "certification": assessment,
            "epistemics": {
                "public_case_count": self.public_case_count,
                "private_case_count": self.private_case_count,
                "optimization_case_count": self.optimization_case_count,
                "report_only_case_count": self.report_only_case_count,
            },
            "cases": [case.to_dict() for case in self.cases],
            "summary": self.summary(),
        }

    def _certificate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Build a tamper-evident, explicitly self-attested certificate."""
        try:
            ordeal_version = package_version("ordeal")
        except PackageNotFoundError:
            ordeal_version = "unknown"
        certificate = {
            "schema": "ordeal.bug-benchmark.evidence/v1",
            "assurance": "self_attested_reproducible_evidence",
            "issued_at": self.generated_at,
            "certified": self.certified,
            "subject": {
                "manifest_path": self.manifest_path,
                "manifest_sha256": self.manifest_sha256,
                "selected_tier": self.selected_tier,
                "case_count": self.case_count,
            },
            "claims": self.certification_assessment(),
            "runtime": {
                "ordeal_version": ordeal_version,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
            },
            "integrity": {
                "algorithm": "sha256",
                "evidence_sha256": _sha256_payload(evidence),
            },
            "limitations": [
                "Certifies only the declared cases, controls, thresholds, and captured run.",
                (
                    "Confidence bounds quantify sampling uncertainty but do not guarantee "
                    "unseen results."
                ),
                (
                    "SHA-256 detects evidence changes; it does not prove a third-party "
                    "signer identity."
                ),
            ],
        }
        certificate["integrity"]["certificate_sha256"] = _sha256_payload(certificate)
        return certificate

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly suite payload."""
        evidence = self._evidence_dict()
        if self.certification_policy.enabled:
            evidence["certificate"] = self._certificate(evidence)
        return evidence

    def to_json(self) -> str:
        """Return a stable JSON encoding."""
        return _json_dump(self.to_dict())


def parse_bug_benchmark_certification_policy(path: str) -> BugBenchmarkCertificationPolicy:
    """Parse and validate the optional ``[certification]`` table."""
    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)
    raw = data.get("certification")
    if raw is None:
        return BugBenchmarkCertificationPolicy()
    if not isinstance(raw, dict):
        raise ValueError("[certification] must be a table")

    policy = BugBenchmarkCertificationPolicy(
        enabled=bool(raw.get("enabled", True)),
        confidence_level=float(raw.get("confidence_level", 0.95)),
        min_positive_cases=int(raw.get("min_positive_cases", 1)),
        min_negative_cases=int(raw.get("min_negative_cases", 1)),
        min_recall=float(raw.get("min_recall", 1.0)),
        min_precision=float(raw.get("min_precision", 1.0)),
        min_specificity=float(raw.get("min_specificity", 1.0)),
        min_confidence_bound=float(raw.get("min_confidence_bound", 0.0)),
        require_complete=bool(raw.get("require_complete", True)),
        require_provenance=bool(raw.get("require_provenance", True)),
        require_paired_controls=bool(raw.get("require_paired_controls", True)),
    )
    if not 0.5 < policy.confidence_level < 1:
        raise ValueError("certification confidence_level must be between 0.5 and 1")
    if policy.min_positive_cases < 1 or policy.min_negative_cases < 1:
        raise ValueError("certification minimum case counts must be >= 1")
    for name in ("min_recall", "min_precision", "min_specificity", "min_confidence_bound"):
        value = float(getattr(policy, name))
        if not 0 <= value <= 1:
            raise ValueError(f"certification {name} must be between 0 and 1")
    return policy


def parse_bug_benchmark_manifest(path: str) -> tuple[BugBenchmarkSpec, ...]:
    """Parse a benchmark manifest for public/private bug and control cases."""
    manifest_path = Path(path)
    parse_bug_benchmark_certification_policy(path)
    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)

    defaults = data.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("[defaults] must be a table")

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Benchmark manifest must define at least one [[cases]] entry")

    cases: list[BugBenchmarkSpec] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("Each [[cases]] entry must be a table")
        name = _case_string(raw_case, defaults, "name", required=True)
        assert name is not None
        module = _case_string(raw_case, defaults, "module", required=True)
        assert module is not None
        protocol = _case_string(raw_case, defaults, "protocol") or "scan"
        if protocol != "scan":
            raise ValueError(f"Case {name!r} has unsupported protocol {protocol!r}")
        dataset = _case_string(raw_case, defaults, "dataset") or "custom"
        mode = _case_string(raw_case, defaults, "mode") or "candidate"
        if mode not in {"candidate", "real_bug", "evidence", "coverage_gap"}:
            raise ValueError(f"Case {name!r} has unsupported mode {mode!r}")
        tier = _case_string(raw_case, defaults, "tier") or "public"
        workspace = _case_string(raw_case, defaults, "workspace")
        project = _case_string(raw_case, defaults, "project")
        bug_id = _case_string(raw_case, defaults, "bug_id")
        expected_outcome = _case_string(raw_case, defaults, "expected_outcome") or "bug"
        if expected_outcome not in {"bug", "clean"}:
            raise ValueError(
                f"Case {name!r} has unsupported expected_outcome {expected_outcome!r}"
            )
        selection_reason = _case_string(raw_case, defaults, "selection_reason", required=True)
        oracle_source = _case_string(raw_case, defaults, "oracle_source", required=True)
        evidence_level = _case_string(raw_case, defaults, "evidence_level", required=True)
        saturation_risk = _case_string(raw_case, defaults, "saturation_risk") or "unknown"
        if saturation_risk not in {"public", "private", "unknown"}:
            raise ValueError(f"Case {name!r} has unsupported saturation_risk {saturation_risk!r}")
        allowed_for_optimization = _case_bool(
            raw_case,
            defaults,
            "allowed_for_optimization",
            default=False,
        )
        requires_python = _case_string(raw_case, defaults, "requires_python")
        python_version = _case_string(raw_case, defaults, "python_version")
        if requires_python and python_version:
            raise ValueError(
                f"Case {name!r} cannot define both requires_python and python_version"
            )
        if requires_python:
            try:
                SpecifierSet(requires_python)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"Case {name!r} has invalid requires_python {requires_python!r}"
                ) from exc
        expected_targets = _clean_str_list(
            raw_case.get("expected_targets", defaults.get("expected_targets"))
        )
        expected_files = _clean_str_list(
            raw_case.get("expected_files", defaults.get("expected_files"))
        )
        expected_error_type = _case_string(raw_case, defaults, "expected_error_type")
        expected_error_message = _case_string(raw_case, defaults, "expected_error_message")
        expected_witness_sha256 = _case_string(
            raw_case,
            defaults,
            "expected_witness_sha256",
        )
        if expected_witness_sha256 and not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected_witness_sha256
        ):
            raise ValueError(f"Case {name!r} has invalid expected_witness_sha256")
        if expected_outcome == "clean" and any(
            (expected_error_type, expected_error_message, expected_witness_sha256)
        ):
            raise ValueError(f"Clean case {name!r} cannot define expected bug evidence")
        if not expected_targets and not expected_files:
            raise ValueError(f"Case {name!r} must define expected_targets or expected_files")
        if not workspace and dataset != "bugsinpy":
            raise ValueError(f"Case {name!r} must define workspace unless dataset = 'bugsinpy'")
        if dataset == "bugsinpy" and not workspace and (not project or not bug_id):
            raise ValueError(
                f"Case {name!r} needs workspace or project/bug_id for BugsInPy checkout"
            )
        if dataset == "bugsinpy" and saturation_risk == "private":
            raise ValueError(f"Case {name!r} cannot mark BugsInPy data as private saturation risk")
        if tier == "public" and allowed_for_optimization:
            raise ValueError(
                f"Case {name!r} cannot be tier='public' and allowed_for_optimization=true"
            )
        if saturation_risk == "public" and allowed_for_optimization:
            raise ValueError(
                f"Case {name!r} cannot be public saturation risk and an optimization target"
            )

        reserved = {
            "name",
            "module",
            "dataset",
            "protocol",
            "tier",
            "workspace",
            "project",
            "bug_id",
            "expected_outcome",
            "pair_id",
            "evidence_path",
            "selection_reason",
            "oracle_source",
            "oracle_url",
            "evidence_level",
            "saturation_risk",
            "allowed_for_optimization",
            "harvested_at",
            "fix_commit",
            "failure_command",
            "oracle_python_version",
            "requires_python",
            "python_version",
            "pythonpath",
            "targets",
            "expected_targets",
            "expected_files",
            "expected_error_type",
            "expected_error_message",
            "expected_witness_sha256",
            "max_examples",
            "mode",
            "time_limit",
            "save_artifacts",
            "compile_checkout",
            "notes",
        }
        metadata = {str(key): value for key, value in raw_case.items() if key not in reserved}
        cases.append(
            BugBenchmarkSpec(
                name=name,
                module=module,
                dataset=dataset,
                protocol=protocol,
                tier=tier,
                workspace=workspace,
                project=project,
                bug_id=bug_id,
                expected_outcome=expected_outcome,
                pair_id=_case_string(raw_case, defaults, "pair_id"),
                evidence_path=_case_string(raw_case, defaults, "evidence_path"),
                selection_reason=selection_reason or "",
                oracle_source=oracle_source or "",
                oracle_url=_case_string(raw_case, defaults, "oracle_url"),
                evidence_level=evidence_level or "",
                saturation_risk=saturation_risk,
                allowed_for_optimization=allowed_for_optimization,
                harvested_at=_case_string(raw_case, defaults, "harvested_at"),
                fix_commit=_case_string(raw_case, defaults, "fix_commit"),
                failure_command=_case_string(raw_case, defaults, "failure_command"),
                oracle_python_version=_case_string(raw_case, defaults, "oracle_python_version"),
                requires_python=requires_python,
                python_version=python_version,
                pythonpath=_clean_str_list(raw_case.get("pythonpath", defaults.get("pythonpath"))),
                targets=_clean_str_list(raw_case.get("targets", defaults.get("targets"))),
                expected_targets=expected_targets,
                expected_files=expected_files,
                expected_error_type=expected_error_type,
                expected_error_message=expected_error_message,
                expected_witness_sha256=expected_witness_sha256,
                max_examples=_case_int(raw_case, defaults, "max_examples", default=20),
                mode=mode,
                time_limit=_case_float(raw_case, defaults, "time_limit"),
                save_artifacts=_case_bool(raw_case, defaults, "save_artifacts", default=False),
                compile_checkout=_case_bool(raw_case, defaults, "compile_checkout", default=True),
                notes=_case_string(raw_case, defaults, "notes"),
                metadata=metadata,
            )
        )
    return tuple(cases)


def _prepare_bugsinpy_workspace(
    spec: BugBenchmarkSpec,
    *,
    bugsinpy_root: str | None,
    checkout_root: str | None,
) -> Path:
    """Checkout one BugsInPy case into a local workspace directory."""
    checkout_root_path = Path(checkout_root or ".ordeal/bug-benchmark").resolve()
    checkout_root_path.mkdir(parents=True, exist_ok=True)
    case_name = spec.name.strip()
    case_path = Path(case_name)
    if (
        not case_name
        or case_path.is_absolute()
        or len(case_path.parts) != 1
        or case_name in {".", ".."}
        or "/" in case_name
        or "\\" in case_name
    ):
        raise ValueError(f"Unsafe BugsInPy case name: {spec.name!r}")
    workspace = (checkout_root_path / case_name).resolve()
    try:
        workspace.relative_to(checkout_root_path)
    except ValueError as exc:
        raise ValueError(f"BugsInPy workspace escapes checkout root: {spec.name!r}") from exc
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    checkout_exe = _find_bugsinpy_executable(
        "bugsinpy-checkout",
        bugsinpy_root=bugsinpy_root,
    )
    assert spec.project is not None
    assert spec.bug_id is not None
    subprocess.run(
        [
            checkout_exe,
            "-p",
            spec.project,
            "-v",
            "0",
            "-i",
            spec.bug_id,
            "-w",
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    project_root = _find_bugsinpy_checkout_root(workspace)
    if spec.compile_checkout:
        compile_exe = _find_bugsinpy_executable(
            "bugsinpy-compile",
            bugsinpy_root=bugsinpy_root,
        )
        compile_proc = subprocess.run(
            [compile_exe],
            cwd=str(project_root),
            text=True,
            capture_output=True,
            check=True,
        )
        compile_flag = project_root / "bugsinpy_compile_flag"
        if not compile_flag.exists():
            raise RuntimeError(
                "bugsinpy-compile exited without creating bugsinpy_compile_flag: "
                f"{compile_proc.stderr.strip() or compile_proc.stdout.strip()}"
            )
    return project_root


def _find_bugsinpy_checkout_root(workspace: Path) -> Path:
    """Return the real BugsInPy project root inside *workspace*."""
    manifest = workspace / "bugsinpy_bug.info"
    if manifest.exists():
        return workspace

    candidates = sorted(workspace.rglob("bugsinpy_bug.info"))
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one bugsinpy_bug.info under {workspace}, found {len(candidates)}"
        )
    return candidates[0].parent


def _parse_bugsinpy_info(path: Path) -> dict[str, str]:
    """Parse one BugsInPy `bug.info`-style file into a key/value map."""
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or "=" not in text:
            continue
        key, _, raw_value = text.partition("=")
        data[key.strip()] = raw_value.strip().strip('"')
    return data


def _resolve_pythonpath_entries(
    spec: BugBenchmarkSpec,
    *,
    workspace_path: Path,
) -> tuple[str, ...]:
    """Return absolute `PYTHONPATH` entries for one benchmark case."""
    entries = list(spec.pythonpath)
    bug_info_path = workspace_path / "bugsinpy_bug.info"
    if spec.dataset == "bugsinpy" and bug_info_path.exists():
        pythonpath_value = _parse_bugsinpy_info(bug_info_path).get("pythonpath", "")
        if pythonpath_value:
            entries.extend(item.strip() for item in pythonpath_value.split(";") if item.strip())

    resolved: list[str] = []
    for entry in entries:
        candidate = Path(entry)
        path = candidate if candidate.is_absolute() else (workspace_path / candidate)
        resolved.append(str(path.resolve()))
    return tuple(dict.fromkeys(resolved))


def _workspace_site_packages(workspace_path: Path) -> tuple[str, ...]:
    """Return site-packages paths from a checkout virtualenv when present."""
    candidates = sorted((workspace_path / "env" / "lib").glob("python*/site-packages"))
    windows_site_packages = workspace_path / "env" / "Lib" / "site-packages"
    if windows_site_packages.exists():
        candidates.append(windows_site_packages)
    return tuple(str(path.resolve()) for path in candidates if path.exists())


def _resolve_required_python_version(
    spec: BugBenchmarkSpec,
    *,
    workspace_path: Path,
) -> str | None:
    """Return the required Python version for one benchmark case when known."""
    if spec.python_version:
        return spec.python_version
    bug_info_path = workspace_path / "bugsinpy_bug.info"
    if spec.dataset == "bugsinpy" and bug_info_path.exists():
        return _parse_bugsinpy_info(bug_info_path).get("python_version") or None
    return None


def _major_minor(version: str) -> str:
    """Return the `major.minor` prefix from one version string."""
    pieces = [part for part in str(version).strip().split(".") if part]
    if len(pieces) < 2:
        return str(version).strip()
    return ".".join(pieces[:2])


def _interpreter_version(executable: str) -> str:
    """Return the semantic version reported by *executable*."""
    if Path(executable).resolve() == Path(sys.executable).resolve():
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    proc = subprocess.run(
        [
            executable,
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    version = proc.stdout.strip()
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(
            f"Interpreter {executable!r} reported invalid version {version!r}"
        ) from exc
    return version


def _python_requirement_error(spec: BugBenchmarkSpec, actual_python: str) -> str | None:
    """Return a runner-version mismatch message, if any."""
    if spec.requires_python:
        if Version(actual_python) not in SpecifierSet(spec.requires_python):
            return (
                f"requires Python {spec.requires_python}, "
                f"benchmark runner is using Python {actual_python}"
            )
        return None
    if spec.python_version and _major_minor(spec.python_version) != _major_minor(actual_python):
        return (
            f"requires Python {spec.python_version}, "
            f"benchmark runner is using Python {actual_python}"
        )
    return None


def _resolve_workspace(
    spec: BugBenchmarkSpec,
    *,
    manifest_path: str,
    bugsinpy_root: str | None,
    checkout_root: str | None,
) -> Path:
    """Resolve or materialize the workspace for one benchmark case."""
    if spec.workspace:
        raw = Path(spec.workspace)
        if raw.is_absolute():
            return raw.resolve()
        return (Path(manifest_path).resolve().parent / raw).resolve()
    if spec.dataset == "bugsinpy":
        return _prepare_bugsinpy_workspace(
            spec,
            bugsinpy_root=bugsinpy_root,
            checkout_root=checkout_root,
        )
    raise ValueError(f"Case {spec.name!r} has no workspace")


def _matching_targets(
    findings: list[dict[str, Any]],
    *,
    expected_targets: tuple[str, ...],
) -> tuple[str, ...]:
    """Return matched finding targets for the configured expectations."""
    if not expected_targets:
        return ()
    matches: list[str] = []
    for finding in findings:
        actual = str(finding.get("target") or "").strip()
        if not actual:
            details = finding.get("details") or {}
            module = str(details.get("module") or "").strip()
            function = str(details.get("function") or "").strip()
            if module and function:
                actual = f"{module}.{function}"
            elif module:
                actual = module
        if not actual:
            continue
        if any(_match_target(expected, actual) for expected in expected_targets):
            matches.append(actual)
    return tuple(dict.fromkeys(matches))


def _finding_matches_expected_evidence(
    finding: dict[str, Any],
    *,
    spec: BugBenchmarkSpec,
) -> bool:
    """Return whether a finding matches the case's optional exact oracle values."""
    details = finding.get("details")
    if not isinstance(details, dict):
        details = {}
    if spec.expected_error_message is not None:
        if str(details.get("error", "")) != spec.expected_error_message:
            return False
    if spec.expected_error_type is not None:
        proof_bundle = details.get("proof_bundle")
        if not isinstance(proof_bundle, dict):
            proof_bundle = {}
        failing_path = proof_bundle.get("failing_path")
        if not isinstance(failing_path, dict):
            failing_path = {}
        actual_error_type = str(failing_path.get("error_type") or details.get("error_type") or "")
        if actual_error_type != spec.expected_error_type:
            return False
    if spec.expected_witness_sha256 is not None:
        failing_args = details.get("failing_args")
        if not isinstance(failing_args, dict):
            return False
        if _sha256_payload(failing_args) != spec.expected_witness_sha256:
            return False
    return True


def _matching_files(
    raw_result: dict[str, Any],
    *,
    expected_files: tuple[str, ...],
) -> tuple[str, ...]:
    """Return matched file paths from the raw scan result."""
    if not expected_files:
        return ()
    report = raw_result.get("raw_details", {}).get("report", {})
    details = list(report.get("details", []))
    matches: list[str] = []
    for detail in details:
        source_path = str(detail.get("source_path") or "").strip()
        if not source_path:
            continue
        if any(source_path.endswith(expected.replace("\\", "/")) for expected in expected_files):
            matches.append(source_path)
    return tuple(dict.fromkeys(matches))


def run_bug_benchmark_case(
    spec: BugBenchmarkSpec,
    *,
    workspace: str,
    python_executable: str | None = None,
    ordeal_root: str | None = None,
) -> BugBenchmarkCaseResult:
    """Run one benchmark case against the real `ordeal scan --json` surface."""
    workspace_path = Path(workspace).resolve()
    executable = python_executable or sys.executable
    required_python = _resolve_required_python_version(spec, workspace_path=workspace_path)
    requirement_spec = spec
    if required_python and not spec.requires_python and not spec.python_version:
        requirement_spec = replace(spec, python_version=required_python)
    mismatch = None
    if requirement_spec.requires_python or requirement_spec.python_version:
        try:
            actual_python = _interpreter_version(executable)
            mismatch = _python_requirement_error(requirement_spec, actual_python)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
            mismatch = f"could not inspect benchmark interpreter {executable!r}: {exc}"
    if mismatch:
        return BugBenchmarkCaseResult(
            spec=spec,
            status="blocked",
            seconds=0.0,
            exit_code=1,
            summary="benchmark case requires a different Python version",
            workspace=str(workspace_path),
            command=(executable, "-m", "ordeal.cli", *_scan_command(spec)),
            findings=(),
            artifacts=(),
            raw_result={},
            error=mismatch,
        )
    command = [executable, "-m", "ordeal.cli", *_scan_command(spec)]
    env = dict(os.environ)
    pythonpath_entries: list[str] = []
    if ordeal_root:
        pythonpath_entries.append(str(Path(ordeal_root).resolve()))
    if spec.dataset == "bugsinpy":
        pythonpath_entries.extend(_workspace_site_packages(workspace_path))
    pythonpath_entries.extend(_resolve_pythonpath_entries(spec, workspace_path=workspace_path))
    if pythonpath_entries:
        current = env.get("PYTHONPATH", "")
        prefix = os.pathsep.join(pythonpath_entries)
        env["PYTHONPATH"] = prefix if not current else f"{prefix}{os.pathsep}{current}"

    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=str(workspace_path),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started

    raw_result: dict[str, Any] = {}
    if not proc.stdout.strip():
        return BugBenchmarkCaseResult(
            spec=spec,
            status="error",
            seconds=elapsed,
            exit_code=proc.returncode,
            summary="benchmark command did not emit JSON output",
            workspace=str(workspace_path),
            command=tuple(command),
            findings=(),
            artifacts=(),
            raw_result={},
            error=proc.stderr.strip() or None,
        )
    try:
        raw_result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return BugBenchmarkCaseResult(
            spec=spec,
            status="error",
            seconds=elapsed,
            exit_code=proc.returncode,
            summary="benchmark command did not emit valid JSON",
            workspace=str(workspace_path),
            command=tuple(command),
            findings=(),
            artifacts=(),
            raw_result={},
            error=f"{exc}: {proc.stdout[:400]}",
        )

    findings = list(raw_result.get("findings", []))
    artifacts = list(raw_result.get("artifacts", []))
    oracle_findings = [
        finding for finding in findings if _finding_matches_expected_evidence(finding, spec=spec)
    ]
    matched_targets = _matching_targets(
        oracle_findings,
        expected_targets=spec.expected_targets,
    )
    has_exact_oracle = any(
        (
            spec.expected_error_type,
            spec.expected_error_message,
            spec.expected_witness_sha256,
        )
    )
    matched_files = (
        _matching_files(raw_result, expected_files=spec.expected_files)
        if oracle_findings or not has_exact_oracle
        else ()
    )
    if raw_result.get("status") == "blocked":
        status = "blocked"
        summary = str(raw_result.get("summary", "")).strip() or "benchmark case was blocked"
    elif proc.returncode not in {0, 1}:
        status = "error"
        summary = "benchmark command failed before producing a scored result"
    elif matched_targets or matched_files:
        status = "hit" if spec.expected_outcome == "bug" else "false_positive"
        summary = (
            f"matched {len(matched_targets)} target(s) and {len(matched_files)} file(s) "
            f"across {len(findings)} finding(s)"
        )
    else:
        status = "miss" if spec.expected_outcome == "bug" else "correct_rejection"
        summary = f"no scoped targets matched across {len(findings)} finding(s)"

    error = None
    if status in {"blocked", "error"}:
        error = str(raw_result.get("blocking_reason") or proc.stderr or "").strip() or None

    return BugBenchmarkCaseResult(
        spec=spec,
        status=status,
        seconds=elapsed,
        exit_code=proc.returncode,
        summary=summary,
        workspace=str(workspace_path),
        command=tuple(command),
        matched_targets=matched_targets,
        matched_files=matched_files,
        findings=tuple(dict(item) for item in findings),
        artifacts=tuple(dict(item) for item in artifacts),
        raw_result=raw_result,
        error=error,
    )


def _evidence_binding_checks(
    spec: BugBenchmarkSpec,
    verification: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return exact manifest-to-evidence binding checks for one case."""
    backing = verification.get("backing_values", {})
    upstream = backing.get("upstream", {}) if isinstance(backing, dict) else {}
    oracle = backing.get("oracle", {}) if isinstance(backing, dict) else {}
    revision = "buggy" if spec.expected_outcome == "bug" else "fixed"
    revision_oracle = oracle.get(revision, {}) if isinstance(oracle, dict) else {}
    expected_observation = (
        revision_oracle.get("expected", {}) if isinstance(revision_oracle, dict) else {}
    )
    callable_name = (
        str(revision_oracle.get("callable", "")) if isinstance(revision_oracle, dict) else ""
    )
    expected_target = f"{spec.module}.{callable_name}" if callable_name else ""
    values: list[tuple[str, Any, Any]] = [
        ("evidence_id", spec.pair_id, verification.get("evidence_id")),
        ("project", spec.project, upstream.get("project")),
        ("bug_id", spec.bug_id, upstream.get("bug_id")),
        ("fixed_commit", spec.fix_commit, upstream.get("fixed_commit")),
        ("module", spec.module, revision_oracle.get("module")),
        (
            "expected_target",
            expected_target,
            expected_target if expected_target in spec.expected_targets else None,
        ),
    ]
    if spec.expected_outcome == "bug":
        values.extend(
            [
                (
                    "expected_error_type",
                    spec.expected_error_type,
                    expected_observation.get("exception_type"),
                ),
                (
                    "expected_error_message",
                    spec.expected_error_message,
                    expected_observation.get("exception_message"),
                ),
                (
                    "expected_witness_sha256",
                    spec.expected_witness_sha256,
                    oracle.get("kwargs_sha256"),
                ),
            ]
        )
    checks: list[dict[str, Any]] = []
    for name, expected, actual in values:
        checks.append(
            {
                "name": name,
                "passed": expected is not None and expected == actual,
                "expected": expected,
                "actual": actual,
                "category": "manifest_binding",
            }
        )
    return checks


def benchmark_bug_manifest(
    manifest_path: str,
    *,
    python_executable: str | None = None,
    ordeal_root: str | None = None,
    tier: str | None = None,
    bugsinpy_root: str | None = None,
    checkout_root: str | None = None,
    online_sources: bool = False,
) -> BugBenchmarkSuite:
    """Run one benchmark manifest and return the scored suite."""
    from ordeal.evidence import verify_bug_evidence

    specs = list(parse_bug_benchmark_manifest(manifest_path))
    certification_policy = parse_bug_benchmark_certification_policy(manifest_path)
    if tier is not None:
        specs = [spec for spec in specs if spec.tier == tier]

    cases: list[BugBenchmarkCaseResult] = []
    evidence_cache: dict[str, dict[str, Any]] = {}
    for spec in specs:
        evidence_payload: dict[str, Any] | None = None
        evidence_errors: list[str] = []
        if certification_policy.enabled and not spec.evidence_path:
            evidence_errors.append("certification requires a linked evidence record")
        if spec.evidence_path:
            evidence_path = str(
                (Path(manifest_path).resolve().parent / spec.evidence_path).resolve()
            )
            if evidence_path not in evidence_cache:
                try:
                    verification = verify_bug_evidence(
                        evidence_path,
                        online_sources=online_sources,
                        python_executable=python_executable,
                    )
                    evidence_cache[evidence_path] = verification.to_dict()
                except (OSError, ValueError) as exc:
                    evidence_cache[evidence_path] = {
                        "verified": False,
                        "local_verified": False,
                        "sources_verified": False,
                        "errors": [str(exc)],
                    }
            evidence_payload = dict(evidence_cache[evidence_path])
            evidence_payload["record_path"] = spec.evidence_path
            binding_checks = _evidence_binding_checks(spec, evidence_payload)
            evidence_payload["manifest_binding"] = {
                "passed": all(check["passed"] for check in binding_checks),
                "checks": binding_checks,
            }
            if not evidence_payload.get("local_verified"):
                evidence_errors.append("linked evidence did not pass local verification")
            if certification_policy.enabled and not evidence_payload.get("verified"):
                evidence_errors.append("certification requires fully verified linked evidence")
            if online_sources and not evidence_payload.get("verified"):
                evidence_errors.append("linked evidence did not pass online source verification")
            evidence_errors.extend(
                f"evidence binding failed: {check['name']}"
                for check in binding_checks
                if not check["passed"]
            )
        if evidence_errors:
            cases.append(
                BugBenchmarkCaseResult(
                    spec=spec,
                    status="blocked",
                    seconds=0.0,
                    exit_code=1,
                    summary="benchmark evidence could not be verified",
                    workspace=spec.workspace or "",
                    command=(),
                    evidence_verification=evidence_payload,
                    error="; ".join(evidence_errors),
                )
            )
            continue
        try:
            workspace = _resolve_workspace(
                spec,
                manifest_path=manifest_path,
                bugsinpy_root=bugsinpy_root,
                checkout_root=checkout_root,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
            cases.append(
                BugBenchmarkCaseResult(
                    spec=spec,
                    status="blocked",
                    seconds=0.0,
                    exit_code=1,
                    summary="benchmark workspace could not be prepared",
                    workspace=spec.workspace or "",
                    command=(),
                    findings=(),
                    artifacts=(),
                    raw_result={},
                    evidence_verification=evidence_payload,
                    error=str(exc),
                )
            )
            continue

        case = run_bug_benchmark_case(
            spec,
            workspace=str(workspace),
            python_executable=python_executable,
            ordeal_root=ordeal_root,
        )
        cases.append(replace(case, evidence_verification=evidence_payload))

    return BugBenchmarkSuite(
        cases=tuple(cases),
        manifest_path=manifest_path,
        selected_tier=tier,
        certification_policy=certification_policy,
        manifest_sha256=_sha256_file(manifest_path),
    )


@dataclass(frozen=True)
class BugBenchmarkCertificateVerification:
    """Independent verification result for one benchmark JSON artifact."""

    valid: bool
    certified: bool
    evidence_digest_valid: bool
    certificate_digest_valid: bool
    claims_consistent: bool
    manifest_digest_valid: bool | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Return whether the artifact is valid and carries a positive certificate."""
        return self.valid and self.certified

    def summary(self) -> str:
        """Return a compact human-readable verification report."""
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"Bug Benchmark Certificate Verification [{status}]",
            f"  valid={self.valid}, certified={self.certified}",
            (
                f"  evidence_digest={self.evidence_digest_valid}, "
                f"certificate_digest={self.certificate_digest_valid}, "
                f"claims_consistent={self.claims_consistent}"
            ),
            f"  manifest_digest={self.manifest_digest_valid}",
        ]
        lines.extend(f"  error: {error}" for error in self.errors)
        lines.extend(f"  warning: {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly verification payload."""
        return {
            "passed": self.passed,
            "valid": self.valid,
            "certified": self.certified,
            "evidence_digest_valid": self.evidence_digest_valid,
            "certificate_digest_valid": self.certificate_digest_valid,
            "claims_consistent": self.claims_consistent,
            "manifest_digest_valid": self.manifest_digest_valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "summary": self.summary(),
        }

    def to_json(self) -> str:
        """Return stable JSON for verification automation."""
        return _json_dump(self.to_dict())


def _artifact_classification_metrics(evidence: dict[str, Any]) -> dict[str, int | float | None]:
    """Recompute confusion-matrix metrics from serialized case evidence."""
    raw_cases = [case for case in evidence.get("cases", []) if isinstance(case, dict)]
    classified = [
        (
            str(case.get("spec", {}).get("expected_outcome", "bug")),
            str(case.get("status", "")),
        )
        for case in raw_cases
        if isinstance(case.get("spec"), dict)
    ]
    true_positives = classified.count(("bug", "hit"))
    false_negatives = classified.count(("bug", "miss"))
    false_positives = classified.count(("clean", "false_positive"))
    true_negatives = classified.count(("clean", "correct_rejection"))
    statuses = [status for _, status in classified]
    return {
        "hit_count": true_positives,
        "miss_count": false_negatives,
        "false_positive_count": false_positives,
        "correct_rejection_count": true_negatives,
        "blocked_count": statuses.count("blocked"),
        "error_count": statuses.count("error"),
        "precision": _rate(true_positives, true_positives + false_positives),
        "recall": _rate(true_positives, true_positives + false_negatives),
        "specificity": _rate(true_negatives, true_negatives + false_positives),
    }


def _artifact_case_oracles_are_consistent(evidence: dict[str, Any]) -> bool:
    """Return whether every completed status belongs to its declared oracle class."""
    allowed = {
        "bug": {"hit", "miss", "blocked", "error"},
        "clean": {"false_positive", "correct_rejection", "blocked", "error"},
    }
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return False
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("spec"), dict):
            return False
        outcome = str(case["spec"].get("expected_outcome", "bug"))
        if str(case.get("status", "")) not in allowed.get(outcome, set()):
            return False
    return True


def _artifact_certification_is_earned(evidence: dict[str, Any]) -> bool:
    """Re-evaluate certificate eligibility from serialized cases and policy."""
    assessment = evidence.get("certification")
    if not isinstance(assessment, dict):
        return False
    policy = assessment.get("policy")
    if not isinstance(policy, dict) or not bool(policy.get("enabled")):
        return False
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return False
    if not _artifact_case_oracles_are_consistent(evidence):
        return False
    specs = [case.get("spec", {}) for case in cases if isinstance(case, dict)]
    if len(specs) != len(cases) or any(not isinstance(spec, dict) for spec in specs):
        return False
    for case, spec in zip(cases, specs, strict=True):
        if not spec.get("evidence_path"):
            return False
        verification = case.get("evidence_verification")
        if not isinstance(verification, dict):
            return False
        binding = verification.get("manifest_binding")
        if (
            verification.get("local_verified") is not True
            or verification.get("verified") is not True
            or not isinstance(binding, dict)
            or binding.get("passed") is not True
        ):
            return False
        online_required = verification.get("online_sources_required")
        if not isinstance(online_required, bool):
            return False
        if online_required and verification.get("sources_verified") is not True:
            return False
    metrics = _artifact_classification_metrics(evidence)
    positives = int(metrics["hit_count"] or 0) + int(metrics["miss_count"] or 0)
    negatives = int(metrics["correct_rejection_count"] or 0) + int(
        metrics["false_positive_count"] or 0
    )
    if positives < int(policy.get("min_positive_cases", 1)):
        return False
    if negatives < int(policy.get("min_negative_cases", 1)):
        return False
    for name in ("recall", "precision", "specificity"):
        value = metrics[name]
        if value is None or float(value) + 1e-12 < float(policy.get(f"min_{name}", 1.0)):
            return False
    if bool(policy.get("require_complete", True)) and (
        int(metrics["blocked_count"] or 0) or int(metrics["error_count"] or 0)
    ):
        return False

    confidence_level = float(policy.get("confidence_level", 0.95))
    min_bound = float(policy.get("min_confidence_bound", 0.0))
    counts = {
        "recall": (
            int(metrics["hit_count"] or 0),
            int(metrics["hit_count"] or 0) + int(metrics["miss_count"] or 0),
        ),
        "precision": (
            int(metrics["hit_count"] or 0),
            int(metrics["hit_count"] or 0) + int(metrics["false_positive_count"] or 0),
        ),
        "specificity": (
            int(metrics["correct_rejection_count"] or 0),
            int(metrics["correct_rejection_count"] or 0)
            + int(metrics["false_positive_count"] or 0),
        ),
    }
    for successes, total in counts.values():
        lower = _wilson_lower_bound(successes, total, confidence_level)
        if lower is None or lower + 1e-12 < min_bound:
            return False

    if bool(policy.get("require_provenance", True)):
        required = (
            "selection_reason",
            "oracle_source",
            "oracle_url",
            "evidence_level",
            "fix_commit",
            "failure_command",
            "pair_id",
        )
        for spec in specs:
            if any(not spec.get(name) for name in required):
                return False
            fix_commit = str(spec["fix_commit"])
            oracle_url = str(spec["oracle_url"])
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", fix_commit):
                return False
            if (
                not oracle_url.startswith("https://")
                or fix_commit.lower() not in oracle_url.lower()
            ):
                return False
        if bool(policy.get("require_paired_controls", True)):
            pairs: dict[str, list[dict[str, Any]]] = {}
            for spec in specs:
                pairs.setdefault(str(spec["pair_id"]), []).append(spec)
            if sum(len(pair) for pair in pairs.values()) != len(specs):
                return False
            for pair in pairs.values():
                if sorted(str(spec.get("expected_outcome")) for spec in pair) != ["bug", "clean"]:
                    return False
                for field_name in ("project", "bug_id", "fix_commit", "oracle_url"):
                    if len({spec.get(field_name) for spec in pair}) != 1:
                        return False
    return bool(evidence.get("manifest_sha256"))


def verify_bug_benchmark_certificate(
    artifact_path: str,
    *,
    manifest_path: str | None = None,
) -> BugBenchmarkCertificateVerification:
    """Verify certificate digests, claims, metrics, and optional manifest bytes."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return BugBenchmarkCertificateVerification(
            valid=False,
            certified=False,
            evidence_digest_valid=False,
            certificate_digest_valid=False,
            claims_consistent=False,
            manifest_digest_valid=None,
            errors=(f"could not read certificate artifact: {exc}",),
        )
    if not isinstance(payload, dict):
        errors.append("artifact root must be a JSON object")
        payload = {}
    raw_certificate = payload.pop("certificate", None)
    if not isinstance(raw_certificate, dict):
        return BugBenchmarkCertificateVerification(
            valid=False,
            certified=False,
            evidence_digest_valid=False,
            certificate_digest_valid=False,
            claims_consistent=False,
            manifest_digest_valid=None,
            errors=("artifact does not contain a certificate object",),
        )

    certificate = dict(raw_certificate)
    integrity = certificate.get("integrity")
    if not isinstance(integrity, dict):
        integrity = {}
        errors.append("certificate integrity block is missing")
    expected_evidence_digest = str(integrity.get("evidence_sha256", ""))
    evidence_digest_valid = bool(expected_evidence_digest) and (
        _sha256_payload(payload) == expected_evidence_digest
    )
    if not evidence_digest_valid:
        errors.append("evidence SHA-256 digest does not match")

    certificate_for_digest = json.loads(json.dumps(certificate))
    digest_integrity = certificate_for_digest.get("integrity", {})
    expected_certificate_digest = str(digest_integrity.pop("certificate_sha256", ""))
    certificate_digest_valid = bool(expected_certificate_digest) and (
        _sha256_payload(certificate_for_digest) == expected_certificate_digest
    )
    if not certificate_digest_valid:
        errors.append("certificate SHA-256 digest does not match")

    claims_consistent = True
    if certificate.get("schema") != "ordeal.bug-benchmark.evidence/v1":
        claims_consistent = False
        errors.append("unsupported certificate schema")
    if certificate.get("assurance") != "self_attested_reproducible_evidence":
        claims_consistent = False
        errors.append("certificate assurance type is not recognized")
    if certificate.get("claims") != payload.get("certification"):
        claims_consistent = False
        errors.append("certificate claims differ from the evidence assessment")
    if bool(certificate.get("certified")) != bool(payload.get("certified")):
        claims_consistent = False
        errors.append("certificate status differs from the evidence status")

    subject = certificate.get("subject")
    if not isinstance(subject, dict):
        subject = {}
        claims_consistent = False
        errors.append("certificate subject is missing")
    for key in ("manifest_path", "manifest_sha256", "selected_tier", "case_count"):
        if subject.get(key) != payload.get(key):
            claims_consistent = False
            errors.append(f"certificate subject differs on {key}")

    computed_metrics = _artifact_classification_metrics(payload)
    if not _artifact_case_oracles_are_consistent(payload):
        claims_consistent = False
        errors.append("serialized case statuses disagree with their declared outcomes")
    for key, computed in computed_metrics.items():
        declared = payload.get(key)
        if isinstance(computed, float) and isinstance(declared, (float, int)):
            agrees = abs(float(declared) - computed) <= 1e-12
        else:
            agrees = declared == computed
        if not agrees:
            claims_consistent = False
            errors.append(f"serialized case evidence disagrees with {key}")
    earned_certification = _artifact_certification_is_earned(payload)
    if bool(payload.get("certified")) != earned_certification:
        claims_consistent = False
        errors.append("serialized evidence does not earn its declared certification status")

    manifest_candidate: Path | None = None
    if manifest_path:
        manifest_candidate = Path(manifest_path)
    else:
        declared_manifest_path = payload.get("manifest_path")
        if declared_manifest_path and Path(str(declared_manifest_path)).exists():
            manifest_candidate = Path(str(declared_manifest_path))
    manifest_digest_valid: bool | None = None
    if manifest_candidate is not None:
        try:
            manifest_digest_valid = _sha256_file(manifest_candidate) == payload.get(
                "manifest_sha256"
            )
        except OSError as exc:
            errors.append(f"could not read manifest for verification: {exc}")
            manifest_digest_valid = False
        if not manifest_digest_valid:
            errors.append("manifest SHA-256 digest does not match")
    else:
        errors.append("manifest bytes were unavailable; exact manifest verification is required")

    valid = (
        evidence_digest_valid
        and certificate_digest_valid
        and claims_consistent
        and manifest_digest_valid is True
        and not errors
    )
    return BugBenchmarkCertificateVerification(
        valid=valid,
        certified=bool(certificate.get("certified")),
        evidence_digest_valid=evidence_digest_valid,
        certificate_digest_valid=certificate_digest_valid,
        claims_consistent=claims_consistent,
        manifest_digest_valid=manifest_digest_valid,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
