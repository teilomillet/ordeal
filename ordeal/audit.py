"""Audit a module's test coverage — current vs ordeal migration.

One command to justify adoption::

    ordeal audit myapp.scoring --test-dir tests/

Output::

    myapp.scoring
      current:  33 tests | 343 lines | 98% coverage [verified]
      migrated: 12 tests | 130 lines | 96% coverage [verified]
      saving:   64% fewer tests | 62% less code | same coverage
      suggest:  L117: test with structured input

**Epistemic guarantees:**

- Every number is either ``[verified]`` or ``FAILED: reason``.
  The audit never silently returns 0%.
- Coverage is measured via coverage.py JSON reports (stable schema),
  not by parsing terminal output (fragile).
- Mined properties state confidence intervals, not "always" claims.
- Every failure mode produces a visible ``warnings`` entry.

**How coverage is measured:**

When ``coverage.py`` is available, the audit runs pytest under its tracer
and parses a structured JSON report. When it is not available, ordeal
falls back to an internal tracer and computes executed/missing lines
directly. Both paths are cross-checked for internal consistency.

**How the migrated test is generated:**

For each public function with type hints, ordeal generates a ``fuzz()``
call (crash-safety test) plus comments describing mined properties.
The generated file is written to ``.ordeal/test_<mod>_migrated.py``
so the developer can inspect, run, and debug it.

**Limitations (stated, not hidden):**

- ``fuzz()`` only checks crash safety, not behavioral correctness.
  The coverage number reflects "lines executed during fuzzing",
  not "lines tested for correct behavior".
- Mined properties are probabilistic (N samples), not proofs.
  The Wilson score interval gives the lower confidence bound.
- Test suggestions are heuristic (source pattern matching).
  They may be wrong if the source changed after coverage was measured.
"""

from __future__ import annotations

import ast
import asyncio
import enum
import functools
import hashlib
import importlib.util
import inspect
import json
import math
import re
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Literal, Mapping, Sequence, Union, get_args, get_origin

from ordeal.auto import (
    _evaluate_contract_checks,
    _get_public_functions,
    _infer_strategies,
    _prepare_bound_method_call,
    _resolve_module,
    _snapshot_instance_state,
    _state_param_name_for_callable,
)
from ordeal.introspection import safe_get_annotations
from ordeal.mine import MineResult, mine

# ============================================================================
# Constants — every number has a documented rationale
# ============================================================================

SUBPROCESS_TIMEOUT_SECONDS: int = 120
"""Timeout for the coverage/pytest subprocess, in seconds.

**Why 120s:** Large projects with 500+ tests may take 60-90s under
coverage instrumentation.  120s provides 2x margin without hanging
indefinitely.  Measured: ordeal's own 276 tests complete in ~9s;
vauban's 3000 tests complete in ~14s.
"""


def _format_relative_change(
    current: int,
    migrated: int,
    *,
    lower_word: str,
    higher_word: str,
    same_word: str,
) -> str:
    """Describe a relative change without producing nonsensical negatives."""
    if current <= 0:
        return same_word
    if migrated == current:
        return same_word
    pct = abs(1.0 - migrated / current) * 100
    if migrated < current:
        return f"{pct:.0f}% {lower_word}"
    return f"{pct:.0f}% {higher_word}"


def _format_change_summary(
    current_tests: int,
    migrated_tests: int,
    current_lines: int,
    migrated_lines: int,
    *,
    coverage_delta: float | None = None,
) -> tuple[str, str]:
    """Return a label/value pair for audit count deltas."""
    tests_change = _format_relative_change(
        current_tests,
        migrated_tests,
        lower_word="fewer tests",
        higher_word="more tests",
        same_word="same number of tests",
    )
    lines_change = _format_relative_change(
        current_lines,
        migrated_lines,
        lower_word="less code",
        higher_word="more code",
        same_word="same amount of code",
    )

    parts = [tests_change, lines_change]
    if coverage_delta is not None:
        parts.append(
            "same coverage"
            if abs(coverage_delta) < COVERAGE_TOLERANCE_PCT
            else f"coverage {coverage_delta:+.0f}%"
        )

    label = (
        "saving"
        if migrated_tests <= current_tests
        and migrated_lines <= current_lines
        and (migrated_tests < current_tests or migrated_lines < current_lines)
        else "change"
    )
    return label, " | ".join(parts)


COVERAGE_TOLERANCE_PCT: float = 2.0
"""Tolerance for the ``coverage_preserved`` comparison, in percentage points.

**Why 2%:** Coverage instrumentation has measurement noise from dynamic
imports, conditional platform branches, and test ordering.  Empirically,
run-to-run coverage varies by 0.3-0.8% on vauban's suite.  2% absorbs
this noise without masking real regressions.
"""

MAX_SUGGESTIONS: int = 8
"""Maximum number of test suggestions per module.

**Why 8:** More than 8 suggestions overwhelm the developer.  The first
8 uncovered blocks capture the highest-value gaps (blocks are sorted
by line number, which correlates with control flow order).
"""

MINE_EXAMPLES_FOR_GENERATED_TEST: int = 50
"""Max Hypothesis examples when mining properties for the generated test.

**Why 50:** Balances mining quality against audit runtime.  At 50
examples, a Wilson CI width at 95% confidence is ~0.06.  Increasing
to 500 would narrow to ~0.02 but add ~10x runtime per function.
"""

MIN_SAMPLES_FOR_PROPERTY: int = 10
"""Minimum samples to include a mined property in the generated test.

**Why 10:** Below 10 samples, even a 100% hit rate gives a Wilson CI
lower bound of only 0.72 (95% confidence).  Not enough evidence to
justify generating a test assertion.
"""

LINE_BLOCK_GAP: int = 2
"""Gap between line numbers to start a new suggestion block.

**Why 2:** Lines within 2 of each other are typically part of the same
branch (e.g., an if-body spanning 3 lines).  Grouping them produces
one suggestion per branch, not one per line.
"""

DISPLAY_CAP: int = 5
"""Maximum items to show in summary one-liners (mined props, gaps)."""

SOURCE_TRUNCATION: int = 60
"""Maximum characters of source code to show in suggestions."""

AuditValidationMode = Literal["fast", "deep"]
"""How audit validates mined properties against mutants."""

FunctionAuditStatus = Literal["exercised", "exploratory", "uncovered"]
"""Epistemic function-level audit status."""

EvidenceLabel = Literal["verified", "inferred", "none"]
"""Epistemic label attached to one audit evidence item."""


@dataclass(slots=True)
class FunctionAudit:
    """Function-level audit evidence and status."""

    name: str
    status: FunctionAuditStatus
    epistemic: EvidenceLabel
    covered_body_lines: int = 0
    total_body_lines: int = 0
    evidence: list[dict[str, str]] = field(default_factory=list)

    def summary_label(self) -> str:
        """Return a compact label suitable for audit summaries."""
        return f"{self.status} [{self.epistemic}]"


@dataclass(slots=True)
class TestFileEvidence:
    """Evidence that a test file belongs to the target module."""

    path: str
    basis: Literal["filename", "import", "pytest_collection"]
    epistemic: EvidenceLabel
    nodeids: list[str] = field(default_factory=list)


def _normalize_validation_mode(validation_mode: str) -> AuditValidationMode:
    """Validate the requested audit mutation-validation mode."""
    match validation_mode:
        case "fast" | "deep":
            return validation_mode
        case _:
            raise ValueError(
                f"validation_mode must be 'fast' or 'deep', got {validation_mode!r}",
            )


def _normalize_function_audit_status(status: str) -> FunctionAuditStatus:
    """Validate a cached function audit status."""
    match status:
        case "exercised" | "exploratory" | "uncovered":
            return status
        case _:
            raise ValueError(f"unknown function audit status: {status!r}")


def _normalize_evidence_label(label: str) -> EvidenceLabel:
    """Validate a cached epistemic label."""
    match label:
        case "verified" | "inferred" | "none":
            return label
        case _:
            raise ValueError(f"unknown evidence label: {label!r}")


_MUTATION_SCORE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*\((\d+)%\)\s*$")


def _parse_mutation_score(score: str) -> tuple[int, int] | None:
    """Parse ``\"killed/total (pct%)\"`` into exact counts."""
    match = _MUTATION_SCORE_RE.fullmatch(score)
    if match is None:
        return None
    killed = int(match.group(1))
    total = int(match.group(2))
    return killed, total


def _function_body_line_numbers(func: object) -> frozenset[int] | None:
    """Return executable line numbers for *func*'s body when available."""
    try:
        source_lines, start_line = inspect.getsourcelines(func)
    except (OSError, TypeError):
        return None

    try:
        tree = ast.parse(textwrap.dedent("".join(source_lines)))
    except SyntaxError:
        return None

    func_node = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_node = node
            break
    if func_node is None:
        return None

    body_lines: set[int] = set()
    for stmt in func_node.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        end_line = getattr(stmt, "end_lineno", stmt.lineno)
        for lineno in range(stmt.lineno, end_line + 1):
            body_lines.add(start_line + lineno - 1)
    return frozenset(body_lines)


# ============================================================================
# Epistemic types — every measurement carries its status
# ============================================================================


class Status(enum.Enum):
    """Epistemic status of a measurement.

    Every number the audit produces MUST be one of these.  The consumer
    MUST check status before using the value.
    """

    VERIFIED = "verified"
    """Measured by a reliable method AND cross-checked for consistency."""

    FAILED = "failed"
    """Could not measure.  The ``error`` field explains why."""


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Structured coverage data from a reliable source.

    Fields match the coverage.py JSON schema (v7+).
    The ``source`` field documents HOW this was measured.
    """

    percent: float
    total_statements: int
    missing_count: int
    missing_lines: frozenset[int]
    source: str  # e.g., "coverage.py JSON"


@dataclass(frozen=True, slots=True)
class CoverageMeasurement:
    """A coverage measurement with epistemic status.

    Check ``.status`` before using ``.result``.  If status is FAILED,
    ``.result`` is None and ``.error`` explains why.
    """

    status: Status
    result: CoverageResult | None = None
    error: str | None = None

    @property
    def percent(self) -> float:
        """Coverage percentage, or 0.0 if measurement failed."""
        return self.result.percent if self.result else 0.0

    @property
    def missing_lines(self) -> frozenset[int]:
        """Set of uncovered line numbers, or empty if measurement failed."""
        return self.result.missing_lines if self.result else frozenset()


# ============================================================================
# Wilson score interval — for honest probabilistic claims
# ============================================================================


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score confidence interval.

    For a property that held in ``successes`` out of ``total`` samples,
    this returns the lower bound at the confidence level determined by
    ``z`` (default 1.96 = 95% confidence).

    Example: 500/500 at 95% CI → lower bound ≈ 0.994.
    This means "holds with ≥99.4% probability", not "always holds".

    **Why Wilson:** The normal approximation breaks near 0% and 100%.
    Wilson is accurate for all sample sizes and hit rates.

    Reference: Wilson, E.B. (1927). "Probable inference, the law of
    succession, and statistical inference". JASA 22(158):209-212.
    """
    if total == 0:
        return 0.0
    p = successes / total
    denominator = 1 + z**2 / total
    center = p + z**2 / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
    return (center - spread) / denominator


# ============================================================================
# Data types
# ============================================================================


_PROPERTY_TO_RELATION: dict[str, dict[str, str]] = {
    "commutative": {
        "name": "commutative",
        "code": 'Relation("commutative", '
        "transform=lambda args: (args[1], args[0]), "
        "check=lambda a, b: a == b)",
    },
    "idempotent": {
        "name": "idempotent",
        "code": 'Relation("idempotent", '
        "transform=lambda args: (args[0],), "  # apply result as input
        "check=lambda a, b: a == b)",
    },
    "involution": {
        "name": "involution",
        "code": 'Relation("involution", '
        "transform=lambda args: (args[0],), "  # apply f twice
        "check=lambda a, b: b == args[0])",  # f(f(x)) == x
    },
    "deterministic": {
        "name": "deterministic",
        "code": 'Relation("deterministic", '
        "transform=lambda args: args, "
        "check=lambda a, b: a == b)",
    },
}


def _suggest_relations(mined_properties: list[str]) -> list[dict[str, str]]:
    """Convert mined properties into suggested metamorphic Relation objects.

    Each mined property string is ``"func: property (stats)"``.
    Returns a list of dicts with ``function``, ``property``, ``code``.
    """
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in mined_properties:
        # Parse "func: property (stats)"
        if ": " not in entry:
            continue
        func, rest = entry.split(": ", 1)
        prop_name = rest.split(" (")[0].strip()
        # Check for length relationships
        if prop_name.startswith("len(output)"):
            results.append(
                {
                    "function": func,
                    "property": prop_name,
                    "code": 'Relation("length_preserving", '
                    "transform=lambda args: args, "
                    "check=lambda a, b: len(a) == len(b))",
                }
            )
            continue
        if prop_name in _PROPERTY_TO_RELATION:
            key = (func, prop_name)
            if key not in seen:
                seen.add(key)
                rel = dict(_PROPERTY_TO_RELATION[prop_name])
                rel["function"] = func
                rel["property"] = prop_name
                results.append(rel)
    return results


def _group_mined_properties(raw: list[str]) -> str:
    """Group ``"func: prop (stats)"`` entries by property kind.

    Input:  ``["add: commutative (...)", "add: deterministic (...)",
              "mul: commutative (...)", "mul: associative (...)"]``

    Output: ``"commutative(add, mul), deterministic(add), associative(mul)"``

    This is more scannable than the raw list when many functions share
    the same property.
    """
    from collections import defaultdict

    by_prop: dict[str, list[str]] = defaultdict(list)
    for entry in raw:
        # Format: "func_name: prop_name (holds/total, >=CI)"
        colon = entry.find(": ")
        if colon == -1:
            continue
        func = entry[:colon]
        rest = entry[colon + 2 :]
        paren = rest.find(" (")
        prop = rest[:paren] if paren != -1 else rest
        by_prop[prop].append(func)

    parts = []
    for prop, funcs in by_prop.items():
        parts.append(f"{prop}({', '.join(funcs)})")
    return ", ".join(parts[:DISPLAY_CAP]) if parts else ""


@dataclass
class ModuleAudit:
    """Audit result for one module.

    Every numeric field is accompanied by a status indicator.
    If a measurement failed, the corresponding field is 0 and the
    ``warnings`` list explains why.
    """

    module: str

    # Current state (from existing tests)
    current_test_count: int = 0
    current_test_lines: int = 0
    current_coverage: CoverageMeasurement = field(
        default_factory=lambda: CoverageMeasurement(Status.FAILED, error="not measured yet"),
    )

    # Migrated state (ordeal auto + mined properties)
    migrated_test_count: int = 0
    migrated_lines: int = 0
    migrated_coverage: CoverageMeasurement = field(
        default_factory=lambda: CoverageMeasurement(Status.FAILED, error="not measured yet"),
    )

    # What ordeal discovered (with confidence bounds)
    mined_properties: list[str] = field(default_factory=list)
    mutation_score: str = ""  # e.g. "8/10 (80%)" or "" if not run
    validation_mode: AuditValidationMode = "fast"
    gap_functions: list[str] = field(default_factory=list)
    total_functions: int = 0
    function_audits: list[FunctionAudit] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    suggested_relations: list[dict[str, str]] = field(default_factory=list)
    mutation_gaps: list[dict[str, str]] = field(default_factory=list)
    weakest_tests: list[dict[str, int | str]] = field(default_factory=list)
    mutation_gap_stubs: list[dict[str, str]] = field(default_factory=list)
    contract_findings: list[dict[str, Any]] = field(default_factory=list)
    blocking_reason: str | None = None

    # Known unknowns — what ordeal structurally cannot verify
    not_checked: list[str] = field(default_factory=list)

    # Audit health — every problem is visible here
    warnings: list[str] = field(default_factory=list)

    # Generated test file content (for inspection/debugging)
    generated_test: str = ""

    @property
    def coverage_preserved(self) -> bool:
        """True if migrated coverage >= current coverage - tolerance.

        Returns False if either measurement failed.
        """
        if self.current_coverage.status == Status.FAILED:
            return False
        if self.migrated_coverage.status == Status.FAILED:
            return False
        return (
            self.migrated_coverage.percent
            >= self.current_coverage.percent - COVERAGE_TOLERANCE_PCT
        )

    def summary(self) -> str:
        """Human-readable one-module report with epistemic labels."""
        lines = [f"\n  {self.module}"]

        # Current
        cur_cov = self._format_coverage(self.current_coverage)
        lines.append(
            f"    current:  {self.current_test_count:>4} tests "
            f"| {self.current_test_lines:>5} lines "
            f"| {cur_cov}"
        )

        # Migrated
        mig_cov = self._format_coverage(self.migrated_coverage)
        lines.append(
            f"    migrated: {self.migrated_test_count:>4} tests "
            f"| {self.migrated_lines:>5} lines "
            f"| {mig_cov}"
        )

        # Savings (only if both measurements succeeded)
        cur = self.current_coverage
        mig = self.migrated_coverage
        if (
            cur.status == Status.VERIFIED
            and mig.status == Status.VERIFIED
            and self.current_test_count > 0
            and self.current_test_lines > 0
        ):
            delta = mig.percent - cur.percent
            label, summary = _format_change_summary(
                self.current_test_count,
                self.migrated_test_count,
                self.current_test_lines,
                self.migrated_lines,
                coverage_delta=delta,
            )
            lines.append(f"    {label}:   {summary}")

        if self.mined_properties:
            grouped = _group_mined_properties(self.mined_properties)
            lines.append(f"    mined:    {grouped}")

        if self.mutation_score:
            lines.append(f"    mutation: {self.mutation_score}")
        if self.mutation_score or self.validation_mode == "deep":
            lines.append(f"    validation: {self._format_validation_mode()}")
        if self.mutation_gaps:
            lines.append("    surviving mutants:")
            for gap in self.mutation_gaps[:DISPLAY_CAP]:
                lines.append(f"      - {gap['target']}: {gap['location']} {gap['description']}")
        if self.weakest_tests:
            lines.append("    weakest killers:")
            for item in self.weakest_tests[:DISPLAY_CAP]:
                lines.append(f"      - {item['test']}: {item['kills']} kill(s)")
        if self.mutation_gap_stubs:
            lines.append(f"    stubs:    {len(self.mutation_gap_stubs)} draft review stub file(s)")
        if self.contract_findings:
            lines.append(
                f"    contracts: {len(self.contract_findings)} explicit contract finding(s)"
            )

        if self.function_audits:
            counts = self.function_audit_counts
            lines.append(
                "    functions:"
                f" {counts['exercised']} exercised [verified],"
                f" {counts['exploratory']} exploratory [inferred],"
                f" {counts['uncovered']} no effective tests [none]"
            )
            for status in ("exercised", "exploratory", "uncovered"):
                entries = [item for item in self.function_audits if item.status == status]
                if not entries:
                    continue
                names = ", ".join(item.name for item in entries[:DISPLAY_CAP])
                lines.append(f"      - {entries[0].summary_label()}: {names}")
                if entries[0].evidence:
                    first = entries[0].evidence[0]
                    lines.append(f"        evidence: {first['kind']} — {first['detail']}")

        if self.gap_functions:
            lines.append(
                f"    gaps:     {len(self.gap_functions)} functions need fixtures: "
                f"{', '.join(self.gap_functions[:DISPLAY_CAP])}"
            )
        if self.blocking_reason:
            lines.append(f"    blocked:  {self.blocking_reason}")

        if self.suggestions:
            lines.append("    suggest:")
            for s in self.suggestions:
                lines.append(f"      - {s}")

        if self.suggested_relations:
            lines.append("    metamorphic relations (auto-discovered):")
            for rel in self.suggested_relations:
                lines.append(f"      - {rel['function']}: {rel['code']}")

        if self.not_checked:
            lines.append("    NOT verified (requires manual tests):")
            for item in self.not_checked:
                lines.append(f"      - {item}")

        if self.warnings:
            lines.append(f"    warnings: {len(self.warnings)}")

        return "\n".join(lines)

    @staticmethod
    def _format_coverage(m: CoverageMeasurement) -> str:
        """Format a coverage measurement with its epistemic label."""
        if m.status == Status.FAILED:
            return f"FAILED: {m.error}"
        return f"{m.percent:.0f}% coverage [{m.status.value}]"

    def _format_validation_mode(self) -> str:
        """Describe how mutation validation was performed."""
        if self.validation_mode == "deep":
            return "deep replay + re-mine"
        return "fast replay"

    @property
    def mutation_score_counts(self) -> tuple[int, int] | None:
        """Exact ``(killed, total)`` counts parsed from ``mutation_score``."""
        return _parse_mutation_score(self.mutation_score)

    @property
    def mutation_score_fraction(self) -> float | None:
        """Exact mutation score as a fraction, or ``None`` when unavailable."""
        counts = self.mutation_score_counts
        if counts is None:
            return None
        killed, total = counts
        if total <= 0:
            return None
        return killed / total

    @property
    def function_audit_counts(self) -> dict[str, int]:
        """Count function audits by epistemic status."""
        counts = {"exercised": 0, "exploratory": 0, "uncovered": 0}
        for item in self.function_audits:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    @property
    def direct_test_gap_counts(self) -> dict[str, int]:
        """Count function audits that do not satisfy the direct-test gate."""
        counts = {"exploratory": 0, "uncovered": 0}
        for item in self.function_audits:
            if item.status in counts:
                counts[item.status] += 1
        return counts

    @property
    def direct_test_gaps(self) -> list[FunctionAudit]:
        """Return functions that still lack direct verified tests."""
        return [item for item in self.function_audits if item.status != "exercised"]

    @property
    def has_direct_test_gaps(self) -> bool:
        """True when any function is only exploratory or fully uncovered."""
        return bool(self.direct_test_gaps)

    @property
    def fixture_completeness(self) -> float:
        """Fraction of discovered functions that ordeal could execute directly."""
        if self.total_functions <= 0:
            return 0.0
        return max(self.total_functions - len(self.gap_functions), 0) / self.total_functions


def _coverage_result_to_dict(result: CoverageResult | None) -> dict[str, object] | None:
    """Serialize a coverage result for the audit cache."""
    if result is None:
        return None
    return {
        "percent": result.percent,
        "total_statements": result.total_statements,
        "missing_count": result.missing_count,
        "missing_lines": sorted(result.missing_lines),
        "source": result.source,
    }


def _coverage_result_from_dict(data: dict[str, object] | None) -> CoverageResult | None:
    """Deserialize a cached coverage result."""
    if data is None:
        return None
    return CoverageResult(
        percent=float(data["percent"]),
        total_statements=int(data["total_statements"]),
        missing_count=int(data["missing_count"]),
        missing_lines=frozenset(int(line) for line in data["missing_lines"]),
        source=str(data["source"]),
    )


def _coverage_measurement_to_dict(measurement: CoverageMeasurement) -> dict[str, object]:
    """Serialize a coverage measurement for the audit cache."""
    return {
        "status": measurement.status.value,
        "result": _coverage_result_to_dict(measurement.result),
        "error": measurement.error,
    }


def _coverage_measurement_from_dict(data: dict[str, object]) -> CoverageMeasurement:
    """Deserialize a cached coverage measurement."""
    return CoverageMeasurement(
        status=Status(str(data["status"])),
        result=_coverage_result_from_dict(data.get("result")),
        error=data.get("error"),
    )


def _function_audit_to_dict(result: FunctionAudit) -> dict[str, object]:
    """Serialize one function audit result for the on-disk cache."""
    return {
        "name": result.name,
        "status": result.status,
        "epistemic": result.epistemic,
        "covered_body_lines": result.covered_body_lines,
        "total_body_lines": result.total_body_lines,
        "evidence": result.evidence,
    }


def _function_audit_from_dict(data: dict[str, object]) -> FunctionAudit:
    """Deserialize one cached function audit result."""
    return FunctionAudit(
        name=str(data["name"]),
        status=_normalize_function_audit_status(str(data["status"])),
        epistemic=_normalize_evidence_label(str(data["epistemic"])),
        covered_body_lines=int(data.get("covered_body_lines", 0)),
        total_body_lines=int(data.get("total_body_lines", 0)),
        evidence=[dict(item) for item in data.get("evidence", [])],
    )


def _module_audit_to_dict(result: ModuleAudit) -> dict[str, object]:
    """Serialize a module audit result for the on-disk cache."""
    return {
        "module": result.module,
        "current_test_count": result.current_test_count,
        "current_test_lines": result.current_test_lines,
        "current_coverage": _coverage_measurement_to_dict(result.current_coverage),
        "migrated_test_count": result.migrated_test_count,
        "migrated_lines": result.migrated_lines,
        "migrated_coverage": _coverage_measurement_to_dict(result.migrated_coverage),
        "mined_properties": result.mined_properties,
        "mutation_score": result.mutation_score,
        "validation_mode": result.validation_mode,
        "gap_functions": result.gap_functions,
        "total_functions": result.total_functions,
        "function_audits": [_function_audit_to_dict(item) for item in result.function_audits],
        "suggestions": result.suggestions,
        "suggested_relations": result.suggested_relations,
        "mutation_gaps": result.mutation_gaps,
        "weakest_tests": result.weakest_tests,
        "mutation_gap_stubs": result.mutation_gap_stubs,
        "contract_findings": result.contract_findings,
        "blocking_reason": result.blocking_reason,
        "not_checked": result.not_checked,
        "warnings": result.warnings,
        "generated_test": result.generated_test,
    }


def _module_audit_from_dict(data: dict[str, object]) -> ModuleAudit:
    """Deserialize a cached module audit result."""
    return ModuleAudit(
        module=str(data["module"]),
        current_test_count=int(data["current_test_count"]),
        current_test_lines=int(data["current_test_lines"]),
        current_coverage=_coverage_measurement_from_dict(data["current_coverage"]),
        migrated_test_count=int(data["migrated_test_count"]),
        migrated_lines=int(data["migrated_lines"]),
        migrated_coverage=_coverage_measurement_from_dict(data["migrated_coverage"]),
        mined_properties=list(data.get("mined_properties", [])),
        mutation_score=str(data.get("mutation_score", "")),
        validation_mode=_normalize_validation_mode(str(data.get("validation_mode", "fast"))),
        gap_functions=list(data.get("gap_functions", [])),
        total_functions=int(data.get("total_functions", 0)),
        function_audits=[
            _function_audit_from_dict(item) for item in data.get("function_audits", [])
        ],
        suggestions=list(data.get("suggestions", [])),
        suggested_relations=list(data.get("suggested_relations", [])),
        mutation_gaps=list(data.get("mutation_gaps", [])),
        weakest_tests=list(data.get("weakest_tests", [])),
        mutation_gap_stubs=list(data.get("mutation_gap_stubs", [])),
        contract_findings=list(data.get("contract_findings", [])),
        blocking_reason=(
            str(data["blocking_reason"])
            if data.get("blocking_reason") is not None
            else None
        ),
        not_checked=list(data.get("not_checked", [])),
        warnings=list(data.get("warnings", [])),
        generated_test=str(data.get("generated_test", "")),
    )


def _render_audit_results(results: Sequence[ModuleAudit]) -> str:
    """Render a human-readable audit report from precomputed results."""
    lines = ["ordeal audit"]
    total_cur_tests = 0
    total_cur_lines = 0
    total_mig_tests = 0
    total_mig_lines = 0
    total_warnings = 0

    for result in results:
        lines.append(result.summary())
        total_cur_tests += result.current_test_count
        total_cur_lines += result.current_test_lines
        total_mig_tests += result.migrated_test_count
        total_mig_lines += result.migrated_lines
        total_warnings += len(result.warnings)

    if len(results) > 1:
        lines.append("\n  total:")
        lines.append(f"    current:  {total_cur_tests} tests | {total_cur_lines} lines")
        lines.append(f"    migrated: {total_mig_tests} tests | {total_mig_lines} lines")
        if total_cur_tests > 0:
            label, summary = _format_change_summary(
                total_cur_tests,
                total_mig_tests,
                total_cur_lines,
                total_mig_lines,
            )
            lines.append(f"    {label}:   {summary}")
        if total_warnings > 0:
            lines.append(f"    warnings: {total_warnings} (run with --verbose)")

    return "\n".join(lines)


# ============================================================================
# File counting — with explicit failure handling
# ============================================================================


def _count_tests_in_file(path: Path) -> tuple[int, str | None]:
    """Count ``def test_`` occurrences in a file.

    Returns ``(count, error)``.  If the file can't be read, returns
    ``(0, "reason")`` — never silently returns 0.

    **Limitation:** Counts by string match, not AST parsing.
    May overcount ``def test_`` in docstrings/comments.
    May undercount parameterized test generators.
    """
    try:
        text = path.read_text(encoding="utf-8")
        return text.count("def test_"), None
    except OSError as exc:
        return 0, f"cannot read {path.name}: {exc}"


def _count_lines_in_file(path: Path) -> tuple[int, str | None]:
    """Count non-empty lines in a file.

    Returns ``(count, error)``.  Non-empty = at least one non-whitespace char.

    **Why non-empty:** Empty lines and comment-only lines inflate the
    count.  Non-empty lines better represent code volume.
    """
    try:
        text = path.read_text(encoding="utf-8")
        return sum(1 for line in text.splitlines() if line.strip()), None
    except OSError as exc:
        return 0, f"cannot read {path.name}: {exc}"


# ============================================================================
# Test file discovery
# ============================================================================


def _pytest_collected_test_files(test_dir: Path) -> list[Path]:
    """Ask pytest which files it would collect beneath *test_dir*."""
    if not test_dir.is_dir():
        return []

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_dir)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if completed.returncode not in (0, 1, 5):
        return []

    seen: set[Path] = set()
    results: list[Path] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("=") or "collected " in line:
            continue
        path_text = line.split("::", 1)[0]
        if not path_text.endswith(".py"):
            continue
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if candidate.exists() and candidate not in seen:
            seen.add(candidate)
            results.append(candidate)
    return results


def _looks_like_test_file(path: Path) -> bool:
    """Return whether *path* appears to define pytest-style tests."""
    if path.name in {"conftest.py", "__init__.py"}:
        return False

    stem = path.stem
    if stem.startswith("test_") or stem.endswith("_test"):
        return True

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    return "def test_" in text or "class Test" in text


def _find_test_files(module_name: str, test_dir: Path) -> list[Path]:
    """Find test files that primarily test the given module.

    First uses cheap filesystem/AST checks to find likely test modules.
    Filename conventions (``test_<short>.py``, ``test_<short>_*.py``,
    and ``<short>_test.py``) still win when present. When naming does
    not help, import matching over likely test files handles non-standard
    names without paying a subprocess cost. Pytest collection remains the
    last fallback for custom collection setups.
    """
    import ast

    results: list[Path] = []
    mod_short = module_name.rsplit(".", 1)[-1]

    if not test_dir.is_dir():
        return results

    candidates = sorted(
        path.resolve()
        for path in test_dir.rglob("*.py")
        if path.is_file() and _looks_like_test_file(path)
    )

    for test_file in candidates:
        stem = test_file.stem
        if (
            stem == f"test_{mod_short}"
            or stem.startswith(f"test_{mod_short}_")
            or stem == f"{mod_short}_test"
        ):
            results.append(test_file)

    if results:
        return results

    def _imports_target(path: Path) -> bool:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name:
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == module_name:
                    return True
                if node.module:
                    for alias in node.names:
                        if f"{node.module}.{alias.name}" == module_name:
                            return True
        return False

    results = [path for path in candidates if _imports_target(path)]
    if results:
        return results

    collected = _pytest_collected_test_files(test_dir)
    if not collected:
        return []

    for test_file in collected:
        stem = test_file.stem
        if (
            stem == f"test_{mod_short}"
            or stem.startswith(f"test_{mod_short}_")
            or stem == f"{mod_short}_test"
        ):
            results.append(test_file)

    if results:
        return results

    return [path for path in collected if _imports_target(path)]


def _find_test_file_evidence(module_name: str, test_dir: Path) -> list[TestFileEvidence]:
    """Return test-file evidence with an explicit epistemic basis."""
    import ast

    mod_short = module_name.rsplit(".", 1)[-1]

    if not test_dir.is_dir():
        return []

    candidates = sorted(
        path.resolve()
        for path in test_dir.rglob("*.py")
        if path.is_file() and _looks_like_test_file(path)
    )

    def _imports_target(path: Path) -> bool:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name:
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == module_name:
                    return True
                if node.module:
                    for alias in node.names:
                        if f"{node.module}.{alias.name}" == module_name:
                            return True
        return False

    def _wrap(
        paths: list[Path],
        basis: Literal["filename", "import", "pytest_collection"],
    ) -> list[TestFileEvidence]:
        return [
            TestFileEvidence(
                path=str(path),
                basis=basis,
                epistemic="verified" if basis == "pytest_collection" else "inferred",
            )
            for path in paths
        ]

    filename_matches = [
        path
        for path in candidates
        if (
            path.stem == f"test_{mod_short}"
            or path.stem.startswith(f"test_{mod_short}_")
            or path.stem == f"{mod_short}_test"
        )
    ]
    if filename_matches:
        return _wrap(filename_matches, "filename")

    import_matches = [path for path in candidates if _imports_target(path)]
    if import_matches:
        return _wrap(import_matches, "import")

    collected = _pytest_collected_test_files(test_dir)
    if not collected:
        return []

    filename_matches = [
        path
        for path in collected
        if (
            path.stem == f"test_{mod_short}"
            or path.stem.startswith(f"test_{mod_short}_")
            or path.stem == f"{mod_short}_test"
        )
    ]
    if filename_matches:
        return _wrap(filename_matches, "pytest_collection")

    import_matches = [path for path in collected if _imports_target(path)]
    if import_matches:
        return _wrap(import_matches, "pytest_collection")

    return []


def _collect_pytest_nodeids(test_files: list[Path]) -> dict[Path, list[str]]:
    """Collect node IDs for pytest test files when available."""
    if not test_files:
        return {}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                *[str(f) for f in test_files],
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if completed.returncode not in (0, 1, 5):
        return {}

    results: dict[Path, list[str]] = {}
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("=") or "collected " in line:
            continue
        path_text = line.split("::", 1)[0]
        if not path_text.endswith(".py"):
            continue
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        results.setdefault(candidate, []).append(line)
    return results


def _generated_test_path(module: str) -> Path:
    """Return the generated migrated-test path for *module*."""
    mod_short = module.rsplit(".", 1)[-1]
    return Path(".ordeal") / f"test_{mod_short}_migrated.py"


def _audit_target_cache_key(module: str, target_specs: Sequence[Any] | None = None) -> str:
    """Return a stable cache key for a module plus optional object targets."""
    if not target_specs:
        return module

    serial: list[dict[str, Any]] = []
    for spec in target_specs:
        if isinstance(spec, str):
            serial.append({"target": spec})
            continue
        serial.append(
            {
                "target": str(getattr(spec, "target", "")),
                "factory": getattr(spec, "factory", None),
                "setup": getattr(spec, "setup", None),
                "methods": list(getattr(spec, "methods", [])),
                "include_private": bool(getattr(spec, "include_private", False)),
            }
        )
    digest = hashlib.sha256(json.dumps(serial, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{module}::{digest}"


def _audit_cache_path(module: str) -> Path:
    """Return the on-disk cache path for *module*."""
    safe = module.replace(".", "_").replace(":", "_")
    return Path(".ordeal") / "audit" / f"{safe}.json"


def _hash_file_if_exists(hasher: "hashlib._Hash", path: Path) -> None:
    """Add a file's path and contents to *hasher* when it exists."""
    if not path.exists() or not path.is_file():
        return
    hasher.update(str(path.resolve()).encode("utf-8"))
    hasher.update(path.read_bytes())


def _audit_state_hash(
    module: str,
    *,
    test_dir: str,
    max_examples: int,
    validation_mode: AuditValidationMode,
    target_specs: Sequence[Any] | None = None,
) -> str:
    """Hash the inputs that determine an audit result.

    The cache key includes the target module, relevant current tests,
    active coverage backend availability, the dependency lockfile, and
    the ordeal source files that affect generated tests or validation.
    """
    h = hashlib.sha256()
    h.update(module.encode("utf-8"))
    h.update(_audit_target_cache_key(module, target_specs).encode("utf-8"))
    h.update(str(Path(test_dir).resolve()).encode("utf-8"))
    h.update(str(max_examples).encode("utf-8"))
    h.update(validation_mode.encode("utf-8"))
    h.update(str(importlib.util.find_spec("coverage") is not None).encode("utf-8"))
    h.update(str(importlib.util.find_spec("pytest_cov") is not None).encode("utf-8"))

    mod = _resolve_module(module)
    source_file = getattr(mod, "__file__", None)
    if source_file is None:
        raise ValueError(f"Cannot locate source for {module!r}")
    _hash_file_if_exists(h, Path(source_file))

    test_path = Path(test_dir)
    test_files = _find_test_files(module, test_path)
    for test_file in test_files:
        _hash_file_if_exists(h, test_file)

    conftests: set[Path] = set()
    root = Path.cwd().resolve()
    if (root / "conftest.py").exists():
        conftests.add(root / "conftest.py")
    if (test_path / "conftest.py").exists():
        conftests.add((test_path / "conftest.py").resolve())
    for test_file in test_files:
        resolved = test_file.resolve()
        for parent in [resolved.parent, *resolved.parents]:
            candidate = parent / "conftest.py"
            if candidate.exists():
                conftests.add(candidate.resolve())
            if parent == root:
                break
    for conftest in sorted(conftests):
        _hash_file_if_exists(h, conftest)

    for lockfile in ("uv.lock", "poetry.lock", "requirements.txt"):
        candidate = Path(lockfile)
        if candidate.exists():
            _hash_file_if_exists(h, candidate)
            break

    for spec_name in ("ordeal.audit", "ordeal.auto", "ordeal.mine", "ordeal.mutations"):
        spec = importlib.util.find_spec(spec_name)
        if spec and spec.origin:
            _hash_file_if_exists(h, Path(spec.origin))

    return h.hexdigest()[:16]


def _load_audit_cache(cache_key: str, state_hash: str) -> ModuleAudit | None:
    """Load a cached audit result when the state hash still matches."""
    cache_path = _audit_cache_path(cache_key)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("state_hash") != state_hash:
        return None
    data = payload.get("result")
    if not isinstance(data, dict):
        return None
    try:
        return _module_audit_from_dict(data)
    except Exception:
        return None


def _save_audit_cache(cache_key: str, state_hash: str, result: ModuleAudit) -> None:
    """Persist an audit result to the local `.ordeal/audit` cache."""
    cache_path = _audit_cache_path(cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state_hash": state_hash,
        "result": _module_audit_to_dict(result),
    }
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.rename(cache_path)


# ============================================================================
# Audit planning helpers
# ============================================================================


def _split_audit_target_spec(target: str) -> tuple[str, str | None, str | None]:
    """Split ``module[:Owner[.method]]`` into module, owner, and method parts."""
    if ":" not in target:
        return target, None, None
    module_path, remainder = target.split(":", 1)
    if "." not in remainder:
        return module_path, remainder, None
    owner_path, method_name = remainder.rsplit(".", 1)
    return module_path, owner_path, method_name


def _resolve_audit_symbol(path: str) -> Any:
    """Resolve a dotted import path, including lazy-exported package members."""
    if ":" in path:
        module_path, attr_path = path.split(":", 1)
        obj: Any = importlib.import_module(module_path)
        for part in attr_path.split("."):
            obj = getattr(obj, part)
        return obj

    try:
        return importlib.import_module(path)
    except ImportError:
        pass

    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj: Any = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        try:
            for part in parts[i:]:
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    raise ImportError(f"Cannot resolve target: {path!r}")


def _call_with_async_support(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call *func* and run any awaitable result to completion."""
    result = func(*args, **kwargs)
    if not inspect.isawaitable(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(result)
    finally:
        loop.close()


def _resolve_audit_hook(hook: str | Any | None) -> Any | None:
    """Resolve a hook path or passthrough a callable/object."""
    if hook is None or not isinstance(hook, str):
        return hook
    return _resolve_audit_symbol(hook)


def _instantiate_audit_owner(
    cls: type[Any],
    *,
    factory: str | Any | None = None,
    setup: str | Any | None = None,
    scenarios: Sequence[str | Any] | None = None,
) -> tuple[Any | None, str | None]:
    """Create an object for auditing class methods."""
    try:
        if factory:
            factory_obj = _resolve_audit_hook(factory)
            if not callable(factory_obj):
                return None, f"factory {factory} is not callable"
            instance = _call_with_async_support(factory_obj)
        else:
            instance = cls()
    except Exception as exc:
        return None, f"cannot instantiate {cls.__name__}: {type(exc).__name__}: {exc}"

    if setup:
        try:
            setup_obj = _resolve_audit_hook(setup)
            if not callable(setup_obj):
                return None, f"setup hook {setup} is not callable"
            setup_result = _call_with_async_support(setup_obj, instance)
            if setup_result is not None:
                instance = setup_result
        except Exception as exc:
            return None, f"setup failed for {cls.__name__}: {type(exc).__name__}: {exc}"

    for scenario in scenarios or ():
        try:
            scenario_obj = _resolve_audit_hook(scenario)
            if not callable(scenario_obj):
                return None, f"scenario hook {scenario} is not callable"
            scenario_result = _call_with_async_support(scenario_obj, instance)
            if scenario_result is not None:
                instance = scenario_result
        except Exception as exc:
            return None, f"scenario failed for {cls.__name__}: {type(exc).__name__}: {exc}"

    return instance, None


def _wrap_audit_callable(
    reference: Callable[..., Any],
    invoke: Callable[..., Any],
    *,
    module_name: str,
    qualname: str,
    owner_name: str | None = None,
    method_name: str | None = None,
    factory: str | None = None,
    setup: str | None = None,
    scenarios: Sequence[str | Any] | None = None,
    state_factory: str | Any | None = None,
    teardown: str | Any | None = None,
    harness: str = "fresh",
    kind: str = "function",
) -> Callable[..., Any]:
    """Wrap a callable while preserving its signature and metadata."""

    @functools.wraps(reference)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = _call_with_async_support(invoke, *args, **kwargs)
        wrapped.__ordeal_last_call_context__ = getattr(  # type: ignore[attr-defined]
            invoke,
            "__ordeal_last_call_context__",
            None,
        )
        return result

    wrapped.__ordeal_module__ = module_name  # type: ignore[attr-defined]
    wrapped.__ordeal_qualname__ = qualname  # type: ignore[attr-defined]
    wrapped.__ordeal_owner__ = owner_name  # type: ignore[attr-defined]
    wrapped.__ordeal_method__ = method_name  # type: ignore[attr-defined]
    wrapped.__ordeal_method_name__ = method_name  # type: ignore[attr-defined]
    wrapped.__ordeal_factory__ = factory  # type: ignore[attr-defined]
    wrapped.__ordeal_setup__ = setup  # type: ignore[attr-defined]
    wrapped.__ordeal_scenarios__ = list(scenarios or ())  # type: ignore[attr-defined]
    wrapped.__ordeal_state_factory__ = state_factory  # type: ignore[attr-defined]
    wrapped.__ordeal_teardown__ = teardown  # type: ignore[attr-defined]
    wrapped.__ordeal_harness__ = harness  # type: ignore[attr-defined]
    wrapped.__ordeal_lifecycle_phase__ = getattr(reference, "__ordeal_lifecycle_phase__", None)  # type: ignore[attr-defined]
    wrapped.__ordeal_kind__ = kind  # type: ignore[attr-defined]
    return wrapped


def _class_target_callables(
    module_name: str,
    cls_name: str,
    cls: type[Any],
    *,
    factory: str | Any | None = None,
    setup: str | Any | None = None,
    scenarios: Sequence[str | Any] | None = None,
    state_factory: str | Any | None = None,
    teardown: str | Any | None = None,
    harness: str = "fresh",
    method_names: Sequence[str] | None = None,
    include_private: bool = False,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Collect callable methods from a class target."""
    discovered: list[tuple[str, object]] = []
    skipped: list[str] = []
    candidate_names = list(method_names or [])
    if not candidate_names:
        candidate_names = [
            name
            for name in sorted(dir(cls))
            if not name.startswith("__") and (include_private or not name.startswith("_"))
        ]

    for method_name in candidate_names:
        if not include_private and method_name.startswith("_"):
            continue
        try:
            descriptor = inspect.getattr_static(cls, method_name)
        except AttributeError:
            skipped.append(f"{cls_name}.{method_name} (missing attribute)")
            continue

        qualname = f"{cls_name}.{method_name}"

        if isinstance(descriptor, staticmethod):
            reference = getattr(cls, method_name)
            discovered.append(
                (
                    qualname,
                    _wrap_audit_callable(
                        reference,
                        reference,
                        module_name=module_name,
                        qualname=qualname,
                        owner_name=cls_name,
                        method_name=method_name,
                        factory=factory,
                        setup=setup,
                        scenarios=scenarios,
                        state_factory=state_factory,
                        teardown=teardown,
                        harness=harness,
                        kind="static",
                    ),
                )
            )
            continue
        if isinstance(descriptor, classmethod):
            reference = getattr(cls, method_name)
            discovered.append(
                (
                    qualname,
                    _wrap_audit_callable(
                        reference,
                        reference,
                        module_name=module_name,
                        qualname=qualname,
                        owner_name=cls_name,
                        method_name=method_name,
                        factory=factory,
                        setup=setup,
                        scenarios=scenarios,
                        state_factory=state_factory,
                        teardown=teardown,
                        harness=harness,
                        kind="class",
                    ),
                )
            )
            continue
        if callable(descriptor):
            prototype, reason = _instantiate_audit_owner(
                cls,
                factory=factory,
                setup=setup,
                scenarios=scenarios,
            )
            if prototype is None:
                skipped.append(f"{qualname} ({reason})")
                continue

            reference = getattr(prototype, method_name)
            state_param = _state_param_name_for_callable(reference)

            def _invoke(
                *args: Any,
                __cls: type[Any] = cls,
                __method_name: str = method_name,
                __factory: str | Any | None = factory,
                __setup: str | Any | None = setup,
                __scenarios: tuple[str | Any, ...] = tuple(scenarios or ()),
                __state_factory: str | Any | None = state_factory,
                __state_param: str | None = state_param,
                __teardown: str | Any | None = teardown,
                __reference: Any = reference,
                **kwargs: Any,
            ) -> Any:
                instance, inst_reason = _instantiate_audit_owner(
                    __cls,
                    factory=__factory,
                    setup=__setup,
                    scenarios=__scenarios,
                )
                if instance is None:
                    raise RuntimeError(inst_reason or f"cannot instantiate {__cls.__name__}")
                bound = getattr(instance, __method_name)
                call_args, call_kwargs = _prepare_bound_method_call(
                    __reference,
                    args,
                    kwargs,
                    instance=instance,
                    state_factory=_resolve_audit_hook(__state_factory),
                    state_param=__state_param,
                )
                before_state = _snapshot_instance_state(instance)
                try:
                    result = _call_with_async_support(bound, *call_args, **call_kwargs)
                    _invoke.__ordeal_last_call_context__ = {
                        "instance": instance,
                        "before_state": before_state,
                        "after_state": _snapshot_instance_state(instance),
                        "kwargs": dict(call_kwargs),
                        "args": tuple(call_args),
                        "method_name": __method_name,
                        "owner": __cls,
                        "harness": harness,
                        "result": result,
                    }
                    return result
                finally:
                    teardown_obj = _resolve_audit_hook(__teardown)
                    if teardown_obj is not None:
                        _call_with_async_support(teardown_obj, instance)

            discovered.append(
                (
                    qualname,
                    _wrap_audit_callable(
                        reference,
                        _invoke,
                        module_name=module_name,
                        qualname=qualname,
                        owner_name=cls_name,
                        method_name=method_name,
                        factory=factory,
                        setup=setup,
                        scenarios=scenarios,
                        state_factory=state_factory,
                        teardown=teardown,
                        harness=harness,
                        kind="instance",
                    ),
                )
            )
            continue

        skipped.append(f"{qualname} (not callable)")

    return discovered, skipped


def _module_target_callables(
    mod: ModuleType,
    *,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Collect public module callables plus class methods when available."""
    discovered = _get_public_functions(
        mod,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )
    return discovered, []


def _collect_target_callables(
    target: str,
    *,
    factory: str | Any | None = None,
    setup: str | Any | None = None,
    scenarios: Sequence[str | Any] | None = None,
    state_factory: str | Any | None = None,
    teardown: str | Any | None = None,
    harness: str = "fresh",
    methods: Sequence[str] | None = None,
    include_private: bool = False,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Collect callables for a configured audit target."""
    module_path, owner_path, method_name = _split_audit_target_spec(target)
    mod = _resolve_audit_symbol(module_path)
    if not isinstance(mod, ModuleType):
        raise TypeError(f"{module_path!r} is not a module")

    if owner_path is None:
        return _module_target_callables(mod, include_private=include_private)

    owner: Any = mod
    for part in owner_path.split("."):
        owner = getattr(owner, part)

    if not inspect.isclass(owner):
        raise TypeError(f"{target!r} does not resolve to a class target")

    if method_name is not None:
        return _class_target_callables(
            mod.__name__,
            owner_path,
            owner,
            factory=factory,
            setup=setup,
            scenarios=scenarios,
            state_factory=state_factory,
            teardown=teardown,
            harness=harness,
            method_names=[method_name],
            include_private=include_private,
        )

    return _class_target_callables(
        mod.__name__,
        owner_path,
        owner,
        factory=factory,
        setup=setup,
        scenarios=scenarios,
        state_factory=state_factory,
        teardown=teardown,
        harness=harness,
        method_names=methods,
        include_private=include_private,
    )


def _audit_object_hook_maps(
    target_specs: Sequence[Any] | None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, str],
]:
    """Resolve configured object hooks for class targets."""
    factories: dict[str, Any] = {}
    setups: dict[str, Any] = {}
    scenarios: dict[str, Any] = {}
    state_factories: dict[str, Any] = {}
    teardowns: dict[str, Any] = {}
    harnesses: dict[str, str] = {}
    for spec in target_specs or []:
        if isinstance(spec, str):
            target = spec
            factory = None
            setup = None
            state_factory = None
            teardown = None
            harness = "fresh"
            scenario_paths: list[str] = []
        else:
            target = str(getattr(spec, "target"))
            factory = getattr(spec, "factory", None)
            setup = getattr(spec, "setup", None)
            state_factory = getattr(spec, "state_factory", None)
            teardown = getattr(spec, "teardown", None)
            harness = str(getattr(spec, "harness", "fresh") or "fresh").strip().lower()
            scenario_paths = list(getattr(spec, "scenarios", []) or [])
        module_path, owner_path, _method_name = _split_audit_target_spec(target)
        if owner_path is None:
            continue
        owner_keys = {
            owner_path,
            f"{module_path}:{owner_path}",
            f"{module_path}.{owner_path}",
            f"{module_path}:{owner_path.split('.')[-1]}",
            f"{module_path}.{owner_path.split('.')[-1]}",
        }
        if factory:
            factory_obj = _resolve_audit_symbol(factory)
            for key in owner_keys:
                factories[key] = factory_obj
        if setup:
            setup_obj = _resolve_audit_symbol(setup)
            for key in owner_keys:
                setups[key] = setup_obj
        if state_factory:
            state_factory_obj = _resolve_audit_symbol(state_factory)
            for key in owner_keys:
                state_factories[key] = state_factory_obj
        if teardown:
            teardown_obj = _resolve_audit_symbol(teardown)
            for key in owner_keys:
                teardowns[key] = teardown_obj
        for key in owner_keys:
            harnesses[key] = harness if harness in {"fresh", "stateful"} else "fresh"
        if scenario_paths:
            resolved_hooks = [_resolve_audit_symbol(path) for path in scenario_paths]

            def _scenario_hook(
                instance: Any,
                *,
                __hooks: Sequence[Any] = tuple(resolved_hooks),
            ) -> Any:
                current = instance
                for hook in __hooks:
                    result = _call_with_async_support(hook, current)
                    if result is not None:
                        current = result
                return current

            setattr(_scenario_hook, "__ordeal_scenario_count__", len(resolved_hooks))
            for key in owner_keys:
                scenarios[key] = _scenario_hook
    return factories, setups, scenarios, state_factories, teardowns, harnesses


def _collect_audit_functions(
    module: str | ModuleType,
    *,
    target_specs: Sequence[Any] | None = None,
) -> tuple[list[tuple[str, object]], list[str], list[tuple[str, object]]]:
    """Split public module and object-target callables into scannable and skipped groups.

    ``scannable`` means ordeal can infer Hypothesis strategies for the
    function, so audit can fuzz it, mine properties, and generate tests.
    ``skipped`` functions still appear in the summary as fixture gaps.
    """
    mod = _resolve_module(module)
    (
        object_factories,
        object_setups,
        object_scenarios,
        object_state_factories,
        object_teardowns,
        object_harnesses,
    ) = _audit_object_hook_maps(target_specs)
    scannable: dict[str, object] = {}
    discovered_callables: dict[str, object] = {}
    skipped: dict[str, None] = {}

    def _add_scannable(name: str, func: object) -> None:
        scannable[name] = func
        discovered_callables[name] = func
        skipped.pop(name, None)

    def _add_skipped(name: str, func: object | None = None) -> None:
        if func is not None:
            discovered_callables[name] = func
        skipped.setdefault(name, None)

    discovered, module_skipped = _module_target_callables(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )
    for name, func in discovered:
        _add_scannable(name, func)
    for name in module_skipped:
        _add_skipped(name)

    for spec in target_specs or []:
        if isinstance(spec, str):
            target = spec
            factory = None
            setup = None
            scenarios = None
            methods = None
            include_private = False
        else:
            target = str(getattr(spec, "target"))
            factory = getattr(spec, "factory", None)
            setup = getattr(spec, "setup", None)
            scenarios = list(getattr(spec, "scenarios", []) or [])
            state_factory = getattr(spec, "state_factory", None)
            teardown = getattr(spec, "teardown", None)
            harness = str(getattr(spec, "harness", "fresh") or "fresh")
            methods = list(getattr(spec, "methods", []))
            include_private = bool(getattr(spec, "include_private", False))
        discovered, target_skipped = _collect_target_callables(
            target,
            factory=factory,
            setup=setup,
            scenarios=scenarios,
            state_factory=state_factory,
            teardown=teardown,
            harness=harness,
            methods=methods,
            include_private=include_private,
        )
        for name, func in discovered:
            _add_scannable(name, func)
        for name in target_skipped:
            _add_skipped(name)

    for name, func in list(scannable.items()):
        if _infer_strategies(func) is None:
            scannable.pop(name, None)
            _add_skipped(name, func)

    return list(scannable.items()), list(skipped.keys()), list(discovered_callables.items())


def _normalize_audit_function_collection(
    collected: tuple[Any, ...],
) -> tuple[list[tuple[str, object]], list[str], list[tuple[str, object]]]:
    """Normalize collector output across tuple-shape variations.

    The current implementation returns ``(scannable, skipped, discovered)``,
    but older call sites and cached environments may still provide the older
    ``(scannable, skipped)`` shape. Preserve epistemic reporting by filling in
    placeholder discovered entries when necessary.
    """
    if len(collected) == 3:
        scannable, skipped, discovered = collected
        return list(scannable), list(skipped), list(discovered)
    if len(collected) == 2:
        scannable, skipped = collected
        discovered = list(scannable) + [(name, object()) for name in skipped]
        return list(scannable), list(skipped), discovered
    raise ValueError(f"unexpected audit collection shape: {len(collected)}")


def _audit_contract_findings(
    functions: Sequence[tuple[str, object]],
    *,
    contract_checks: Mapping[str, Sequence[Any]] | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Run explicit contract probes against discovered audit callables."""
    findings: list[dict[str, Any]] = []
    if not contract_checks:
        return findings
    for name, func in functions:
        checks = list(contract_checks.get(name, []))
        if not checks:
            continue
        try:
            _violations, details = _evaluate_contract_checks(func, checks)
        except Exception as exc:
            warnings.append(f"contract checks failed for {name}: {type(exc).__name__}: {exc}")
            continue
        for detail in details:
            enriched = dict(detail)
            enriched.setdefault("function", name)
            findings.append(enriched)
    return findings


def _audit_blocking_reason(
    *,
    total_functions: int,
    gap_functions: Sequence[str],
    discovered_functions: Sequence[tuple[str, object]],
    min_fixture_completeness: float,
) -> str | None:
    """Return an early blocking reason when audit lacks enough runnable leverage."""
    if total_functions <= 0:
        return "no callable targets were discovered"
    completeness = max(total_functions - len(gap_functions), 0) / max(total_functions, 1)
    if completeness <= 0.0:
        if any("." in name for name, _func in discovered_functions):
            return "need instance/state harness or object/state factory for discovered methods"
        return "no discovered targets had inferable fixtures or strategies"
    if min_fixture_completeness > 0.0 and completeness < min_fixture_completeness:
        return (
            "fixture completeness is too low for meaningful audit "
            f"({completeness:.0%} < {min_fixture_completeness:.0%})"
        )
    return None


def _function_name_in_nodeid(function_name: str, nodeid: str) -> bool:
    """Return True when *nodeid* directly mentions *function_name*."""
    tail = nodeid.rsplit("::", 1)[-1]
    return function_name in tail or function_name in nodeid


def _should_collect_pytest_nodeids(
    functions: list[tuple[str, object]],
    *,
    current_coverage: CoverageMeasurement,
    test_file_evidence: list[TestFileEvidence],
) -> bool:
    """Return True when pytest node IDs add signal beyond verified coverage."""
    if not functions or not test_file_evidence:
        return False
    if current_coverage.status != Status.VERIFIED:
        return True
    for _name, func in functions:
        body_lines = _function_body_line_numbers(func)
        if not body_lines:
            return True
        if body_lines.issubset(current_coverage.missing_lines):
            return True
    return False


def _build_function_audits(
    functions: list[tuple[str, object]],
    *,
    current_coverage: CoverageMeasurement,
    test_file_evidence: list[TestFileEvidence],
    collected_nodeids: dict[Path, list[str]],
) -> list[FunctionAudit]:
    """Build an epistemic function-level coverage map."""
    audits: list[FunctionAudit] = []

    for name, func in functions:
        body_lines = _function_body_line_numbers(func)
        total_body_lines = len(body_lines) if body_lines is not None else 0
        covered_body_lines = 0
        evidence: list[dict[str, str]] = []

        if body_lines and current_coverage.status == Status.VERIFIED:
            covered = sorted(body_lines - current_coverage.missing_lines)
            covered_body_lines = len(covered)
            if covered_body_lines > 0:
                evidence.append(
                    {
                        "kind": "coverage_lines",
                        "epistemic": "verified",
                        "detail": (
                            f"coverage hits {covered_body_lines}/{total_body_lines} body line(s)"
                        ),
                    }
                )

        direct_nodeids = [
            nodeid
            for nodeids in collected_nodeids.values()
            for nodeid in nodeids
            if _function_name_in_nodeid(name, nodeid)
        ]
        if direct_nodeids:
            evidence.append(
                {
                    "kind": "pytest_nodeid",
                    "epistemic": "inferred",
                    "detail": ", ".join(direct_nodeids[:DISPLAY_CAP]),
                }
            )

        if covered_body_lines > 0:
            status: FunctionAuditStatus = "exercised"
            epistemic: EvidenceLabel = "verified"
        elif test_file_evidence:
            status = "exploratory"
            epistemic = "inferred"
            if not evidence:
                evidence.extend(
                    {
                        "kind": item.basis,
                        "epistemic": item.epistemic,
                        "detail": item.path,
                    }
                    for item in test_file_evidence[:DISPLAY_CAP]
                )
        else:
            status = "uncovered"
            epistemic = "none"
            evidence.append(
                {
                    "kind": "no_tests",
                    "epistemic": "none",
                    "detail": "no matching pytest files or collected nodeids",
                }
            )

        audits.append(
            FunctionAudit(
                name=name,
                status=status,
                epistemic=epistemic,
                covered_body_lines=covered_body_lines,
                total_body_lines=total_body_lines,
                evidence=evidence,
            )
        )

    return audits


def _mine_audit_functions(
    functions: list[tuple[str, object]],
    *,
    max_examples: int,
    warnings: list[str],
) -> dict[str, MineResult]:
    """Mine each scannable function once for reuse across the audit.

    The resulting mine outputs drive both generated property tests and the
    human-readable summary, avoiding redundant calls to ``mine()``.
    """
    results: dict[str, MineResult] = {}
    for name, func in functions:
        try:
            results[name] = mine(func, max_examples=max_examples)
        except Exception as exc:
            warnings.append(f"mining failed for {name}: {type(exc).__name__}: {exc}")
    return results


def _is_generated_test_file(path: Path) -> bool:
    """Return True when *path* lives under the generated ``.ordeal`` tree."""
    return ".ordeal" in path.parts


# ============================================================================
# Coverage measurement — via JSON, not stdout parsing
# ============================================================================


def _coverage_runtime_context(
    test_files: list[Path],
) -> tuple[str, dict[str, str], list[str], bool]:
    """Build subprocess coverage context for a set of test files."""
    cwd = str(Path.cwd())
    generated_only = all(_is_generated_test_file(f) for f in test_files)
    in_project = any(str(f).startswith(cwd) and "/.ordeal/" not in str(f) for f in test_files)
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = cwd
    pytest_args = [
        *[str(f) for f in test_files],
        "-q",
        "--tb=no",
        "--no-header",
        "-o",
        "addopts=",
        "-p",
        "no:ordeal",
    ]

    if not in_project:
        pytest_args.extend(["--override-ini", f"confcutdir={test_files[0].parent}"])

    return cwd, env, pytest_args, generated_only


def _measure_coverage(
    test_files: list[Path],
    module_name: str,
) -> CoverageMeasurement:
    """Run tests and measure coverage via coverage.py or an internal tracer.

    **Preferred path:** When ``coverage.py`` is available, ordeal runs
    pytest under its tracer and reads a structured JSON report. The JSON
    schema is stable and easy to cross-check.

    **Fallback path:** When ``coverage.py`` is not installed, ordeal traces
    the target module directly in a subprocess and computes executed/missing
    lines itself. This keeps ``ordeal audit`` usable in a fresh environment.
    """
    if not test_files:
        return CoverageMeasurement(Status.FAILED, error="no test files provided")

    cwd, env, pytest_args, generated_only = _coverage_runtime_context(test_files)

    if generated_only:
        if importlib.util.find_spec("coverage") is not None:
            return _measure_generated_coverage_with_coverage_py(module_name, test_files, cwd, env)
        return _measure_generated_coverage_with_trace(module_name, test_files, cwd, env)

    if importlib.util.find_spec("coverage") is not None:
        return _measure_coverage_with_coverage_py(module_name, pytest_args, cwd, env)

    if importlib.util.find_spec("pytest_cov") is not None:
        return _measure_coverage_with_pytest_cov(module_name, pytest_args, cwd, env)

    return _measure_coverage_with_trace(module_name, pytest_args, cwd, env)


def _measure_audit_coverages_with_coverage_py(
    module_name: str,
    current_test_files: list[Path],
    generated_test_files: list[Path],
) -> tuple[CoverageMeasurement, CoverageMeasurement]:
    """Measure current and migrated coverage in one coverage.py subprocess."""
    cwd, env, current_pytest_args, _generated_only = _coverage_runtime_context(current_test_files)

    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_dual_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import importlib.util
import contextlib
import inspect
import json
import sys
import tempfile
from types import SimpleNamespace
from typing import Literal, Union, get_args, get_origin
from pathlib import Path

import coverage
import pytest

module_name = sys.argv[1]
json_path = sys.argv[2]
current_count = int(sys.argv[3])
current_args = sys.argv[4 : 4 + current_count]
generated_files = sys.argv[4 + current_count :]

def run_pytest_suite(pytest_args):
    payload = {"return_code": 0, "coverage": None, "error": None}
    cov_json = Path(tempfile.mkstemp(prefix="ordeal_cov_raw_", suffix=".json")[1])
    cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)
    try:
        cov.start()
        payload["return_code"] = int(pytest.main(pytest_args))
    except Exception as exc:
        payload["return_code"] = 2
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cov.stop()
        except Exception:
            pass

    try:
        cov.json_report(outfile=str(cov_json))
        payload["coverage"] = json.loads(cov_json.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["error"] = payload["error"] or f"coverage JSON failed: {type(exc).__name__}: {exc}"
    finally:
        cov_json.unlink(missing_ok=True)
    return payload

def run_generated_suite(test_files):
    payload = {"return_code": 0, "coverage": None, "error": None}
    cov_json = Path(tempfile.mkstemp(prefix="ordeal_cov_raw_", suffix=".json")[1])
    cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)
    original_fuzz = None
    original_auto_fuzz = None
    original_quickcheck = None
    ordeal = None
    ordeal_auto = None
    ordeal_quickcheck = None
    try:
        import ordeal
        import ordeal.auto as ordeal_auto
        import ordeal.quickcheck as ordeal_quickcheck

        original_fuzz = ordeal.fuzz
        original_auto_fuzz = ordeal_auto.fuzz
        original_quickcheck = ordeal_quickcheck.quickcheck

        def example_value(annotation):
            if annotation in {inspect._empty, None, object}:
                return None
            origin = get_origin(annotation)
            if origin is Literal:
                values = get_args(annotation)
                return values[0] if values else None
            if origin is Union:
                for option in get_args(annotation):
                    if option is type(None):
                        continue
                    return example_value(option)
                return None
            if origin is list:
                return []
            if origin is tuple:
                return ()
            if origin is dict:
                return {}
            if origin is set:
                return set()
            if annotation is int:
                return 1
            if annotation is float:
                return 1.0
            if annotation is str:
                return "x"
            if annotation is bool:
                return True
            if annotation is bytes:
                return b"x"
            return None

        def call_kwargs(fn):
            kwargs = {}
            for name, param in inspect.signature(fn).parameters.items():
                if param.default is not inspect._empty:
                    kwargs[name] = param.default
                else:
                    kwargs[name] = example_value(param.annotation)
            return kwargs

        def fuzz_smoke(target, *args, **kwargs):
            try:
                target(**call_kwargs(target))
            except Exception as exc:
                return SimpleNamespace(
                    passed=False,
                    summary=lambda: f"{type(exc).__name__}: {exc}",
                )
            return SimpleNamespace(passed=True, summary=lambda: "smoke")

        def quickcheck_smoke(*decorator_args, **decorator_kwargs):
            def decorator(fn):
                def wrapped(*args, **kwargs):
                    return fn(**call_kwargs(fn))

                return wrapped

            if decorator_args and callable(decorator_args[0]) and not decorator_kwargs:
                return decorator(decorator_args[0])
            return decorator

        ordeal.fuzz = fuzz_smoke
        ordeal_auto.fuzz = fuzz_smoke
        ordeal_quickcheck.quickcheck = quickcheck_smoke

        cov.start()
        for index, test_path in enumerate(test_files):
            spec = importlib.util.spec_from_file_location(f"_ordeal_generated_{index}", test_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot import generated test file: {test_path}")
            test_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(test_module)
            for name in sorted(dir(test_module)):
                if not name.startswith("test_"):
                    continue
                test_fn = getattr(test_module, name)
                if callable(test_fn):
                    test_fn()
    except Exception as exc:
        payload["return_code"] = 1
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cov.stop()
        except Exception:
            pass
        with contextlib.suppress(Exception):
            ordeal.fuzz = original_fuzz
        with contextlib.suppress(Exception):
            ordeal_auto.fuzz = original_auto_fuzz
        with contextlib.suppress(Exception):
            ordeal_quickcheck.quickcheck = original_quickcheck

    try:
        cov.json_report(outfile=str(cov_json))
        payload["coverage"] = json.loads(cov_json.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["error"] = payload["error"] or f"coverage JSON failed: {type(exc).__name__}: {exc}"
    finally:
        cov_json.unlink(missing_ok=True)
    return payload

payload = {
    "current": run_pytest_suite(current_args),
    "generated": run_generated_suite(generated_files),
}
Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_dual_cov_",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)

    cmd = [
        sys.executable,
        str(script_path),
        module_name,
        str(json_path),
        str(len(current_pytest_args)),
        *current_pytest_args,
        *[str(f) for f in generated_test_files],
    ]

    def _failed(error: str) -> tuple[CoverageMeasurement, CoverageMeasurement]:
        failed = CoverageMeasurement(Status.FAILED, error=error)
        return failed, failed

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return _failed(f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return _failed("python not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return _failed(f"coverage report not generated. stderr: {stderr_hint}")
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return _failed(
            f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else "")
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    def _measurement_from_payload(
        payload: object,
        *,
        source: str,
        error_prefix: str,
    ) -> CoverageMeasurement:
        if not isinstance(payload, dict):
            return CoverageMeasurement(Status.FAILED, error=f"{error_prefix}: missing payload")
        if payload.get("error"):
            return CoverageMeasurement(Status.FAILED, error=str(payload["error"]))
        return_code = int(payload.get("return_code", 0))
        if return_code not in (0, 1):
            return CoverageMeasurement(
                Status.FAILED,
                error=f"{error_prefix}: runner exited with code {return_code}",
            )
        coverage_json = payload.get("coverage")
        if not isinstance(coverage_json, dict):
            return CoverageMeasurement(Status.FAILED, error=f"{error_prefix}: coverage missing")
        return _coverage_measurement_from_json(coverage_json, module_name, source=source)

    current = _measurement_from_payload(
        raw.get("current"),
        source="coverage.py API",
        error_prefix="current coverage",
    )
    migrated = _measurement_from_payload(
        raw.get("generated"),
        source="coverage.py API direct",
        error_prefix="generated coverage",
    )
    return current, migrated


def _measure_audit_coverages(
    current_test_files: list[Path],
    generated_test_files: list[Path],
    module_name: str,
) -> tuple[CoverageMeasurement, CoverageMeasurement]:
    """Measure current and migrated audit coverage with shared fast paths."""
    if (
        current_test_files
        and generated_test_files
        and importlib.util.find_spec("coverage") is not None
        and all(_is_generated_test_file(f) for f in generated_test_files)
    ):
        return _measure_audit_coverages_with_coverage_py(
            module_name,
            current_test_files,
            generated_test_files,
        )

    current = (
        _measure_coverage(current_test_files, module_name)
        if current_test_files
        else CoverageMeasurement(Status.FAILED, error="no test files found")
    )
    migrated = _measure_coverage(generated_test_files, module_name)
    return current, migrated


def _measure_coverage_with_coverage_py(
    module_name: str,
    pytest_args: list[str],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage via the ``coverage.py`` API when available.

    This avoids the pytest-cov plugin dependency while still using
    coverage.py's much faster tracer instead of Python-level ``sys.settrace``.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import json
import sys
import tempfile
from pathlib import Path

import coverage
import pytest

module_name = sys.argv[1]
json_path = sys.argv[2]
pytest_args = sys.argv[3:]

payload = {"return_code": 0, "coverage": None, "error": None}
cov_json = Path(tempfile.mkstemp(prefix="ordeal_cov_raw_", suffix=".json")[1])
cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)

try:
    cov.start()
    payload["return_code"] = int(pytest.main(pytest_args))
except Exception as exc:
    payload["return_code"] = 2
    payload["error"] = f"{type(exc).__name__}: {exc}"
finally:
    try:
        cov.stop()
    except Exception:
        pass

try:
    cov.json_report(outfile=str(cov_json))
    payload["coverage"] = json.loads(cov_json.read_text(encoding="utf-8"))
except Exception as exc:
    payload["error"] = payload["error"] or f"coverage JSON failed: {type(exc).__name__}: {exc}"
finally:
    cov_json.unlink(missing_ok=True)

Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_cov_runner_",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)

    cmd = [
        sys.executable,
        str(script_path),
        module_name,
        str(json_path),
        *pytest_args,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="python not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    if raw.get("error"):
        return CoverageMeasurement(Status.FAILED, error=str(raw["error"]))
    return_code = int(raw.get("return_code", 0))
    if return_code not in (0, 1):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"pytest exited with code {return_code} during coverage measurement",
        )
    coverage_json = raw.get("coverage")
    if not isinstance(coverage_json, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing payload")

    return _coverage_measurement_from_json(
        coverage_json,
        module_name,
        source="coverage.py API",
    )


def _measure_coverage_with_pytest_cov(
    module_name: str,
    pytest_args: list[str],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage via pytest-cov when the plugin is available."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *pytest_args,
        f"--cov={module_name}",
        f"--cov-report=json:{json_path}",
        "--cov-report=",  # suppress terminal output
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="pytest not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200] if result else ""
        json_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)

    return _coverage_measurement_from_json(raw, module_name, source="coverage.py JSON")


def _measure_coverage_with_trace(
    module_name: str,
    pytest_args: list[str],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage without external plugins by tracing a pytest subprocess."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_trace_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import ast
import importlib
import inspect
import json
import os
import sys
import threading
from pathlib import Path

import pytest

module_name = sys.argv[1]
json_path = sys.argv[2]
pytest_args = sys.argv[3:]

module = importlib.import_module(module_name)
module_file = getattr(module, "__file__", None)
source_file = inspect.getsourcefile(module) or module_file
if source_file is None:
    err = {"error": f"{module_name} has no source file"}
    Path(json_path).write_text(json.dumps(err), encoding="utf-8")
    raise SystemExit(0)

target_file = os.path.realpath(source_file)
hits = set()

def tracer(frame, event, arg):
    if event == "line" and os.path.realpath(frame.f_code.co_filename) == target_file:
        hits.add(frame.f_lineno)
    return tracer

return_code = 0

class _CoveragePlugin:
    def pytest_runtest_setup(self, item):
        sys.settrace(tracer)
        threading.settrace(tracer)

    def pytest_runtest_teardown(self, item, nextitem):
        sys.settrace(None)
        threading.settrace(None)

return_code = pytest.main(pytest_args, plugins=[_CoveragePlugin()])

source_text = Path(target_file).read_text(encoding="utf-8")
tree = ast.parse(source_text, filename=target_file)
stmt_lines = sorted(
    {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and hasattr(node, "lineno")
    }
)
measured_hits = sorted(set(stmt_lines) & hits)
missing = sorted(set(stmt_lines) - set(measured_hits))
total = len(stmt_lines)
percent = ((total - len(missing)) / total * 100.0) if total else 100.0

payload = {
    "return_code": int(return_code),
    "module_file": target_file,
    "num_statements": total,
    "executed_lines": measured_hits,
    "missing_lines": missing,
    "percent_covered": percent,
}
Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_trace_cov_",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)

    cmd = [
        sys.executable,
        str(script_path),
        module_name,
        str(json_path),
        *pytest_args,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="pytest not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    if "error" in raw:
        return CoverageMeasurement(Status.FAILED, error=str(raw["error"]))
    return_code = int(raw.get("return_code", 0))
    if return_code not in (0, 1):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"pytest exited with code {return_code} during trace coverage",
        )

    return _coverage_measurement_from_trace_payload(raw)


def _measure_generated_coverage_with_trace(
    module_name: str,
    test_files: list[Path],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage for generated ordeal tests without invoking pytest."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_direct_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import ast
import importlib
import importlib.util
import inspect
import json
import os
import sys
import threading
from pathlib import Path

module_name = sys.argv[1]
json_path = sys.argv[2]
test_files = sys.argv[3:]

module = importlib.import_module(module_name)
module_file = getattr(module, "__file__", None)
source_file = inspect.getsourcefile(module) or module_file
if source_file is None:
    err = {"error": f"{module_name} has no source file"}
    Path(json_path).write_text(json.dumps(err), encoding="utf-8")
    raise SystemExit(0)

target_file = os.path.realpath(source_file)
hits = set()

def tracer(frame, event, arg):
    if event == "line" and os.path.realpath(frame.f_code.co_filename) == target_file:
        hits.add(frame.f_lineno)
    return tracer

return_code = 0
error = None
try:
    for index, test_path in enumerate(test_files):
        spec = importlib.util.spec_from_file_location(f"_ordeal_generated_{index}", test_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import generated test file: {test_path}")
        test_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test_module)
        for name in sorted(dir(test_module)):
            if not name.startswith("test_"):
                continue
            test_fn = getattr(test_module, name)
            if not callable(test_fn):
                continue
            sys.settrace(tracer)
            threading.settrace(tracer)
            try:
                test_fn()
            finally:
                sys.settrace(None)
                threading.settrace(None)
except Exception as exc:
    return_code = 1
    error = f"{type(exc).__name__}: {exc}"

source_text = Path(target_file).read_text(encoding="utf-8")
tree = ast.parse(source_text, filename=target_file)
stmt_lines = sorted(
    {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and hasattr(node, "lineno")
    }
)
measured_hits = sorted(set(stmt_lines) & hits)
missing = sorted(set(stmt_lines) - set(measured_hits))
total = len(stmt_lines)
percent = ((total - len(missing)) / total * 100.0) if total else 100.0

payload = {
    "return_code": int(return_code),
    "error": error,
    "module_file": target_file,
    "num_statements": total,
    "executed_lines": measured_hits,
    "missing_lines": missing,
    "percent_covered": percent,
}
Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_direct_cov_",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)

    cmd = [
        sys.executable,
        str(script_path),
        module_name,
        str(json_path),
        *[str(f) for f in test_files],
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="python not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    if raw.get("error"):
        return CoverageMeasurement(Status.FAILED, error=str(raw["error"]))
    return_code = int(raw.get("return_code", 0))
    if return_code not in (0, 1):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"generated test runner exited with code {return_code}",
        )

    return _coverage_measurement_from_trace_payload(raw)


def _measure_generated_coverage_with_coverage_py(
    module_name: str,
    test_files: list[Path],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure generated ordeal tests directly under the ``coverage.py`` API."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_direct_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import importlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import coverage

module_name = sys.argv[1]
json_path = sys.argv[2]
test_files = sys.argv[3:]

payload = {"return_code": 0, "coverage": None, "error": None}
cov_json = Path(tempfile.mkstemp(prefix="ordeal_cov_raw_", suffix=".json")[1])
cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)

try:
    cov.start()
    for index, test_path in enumerate(test_files):
        spec = importlib.util.spec_from_file_location(f"_ordeal_generated_{index}", test_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import generated test file: {test_path}")
        test_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test_module)
        for name in sorted(dir(test_module)):
            if not name.startswith("test_"):
                continue
            test_fn = getattr(test_module, name)
            if callable(test_fn):
                test_fn()
except Exception as exc:
    payload["return_code"] = 1
    payload["error"] = f"{type(exc).__name__}: {exc}"
finally:
    try:
        cov.stop()
    except Exception:
        pass

try:
    cov.json_report(outfile=str(cov_json))
    payload["coverage"] = json.loads(cov_json.read_text(encoding="utf-8"))
except Exception as exc:
    payload["error"] = payload["error"] or f"coverage JSON failed: {type(exc).__name__}: {exc}"
finally:
    cov_json.unlink(missing_ok=True)

Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_direct_cov_",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)

    cmd = [
        sys.executable,
        str(script_path),
        module_name,
        str(json_path),
        *[str(f) for f in test_files],
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="python not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    if raw.get("error"):
        return CoverageMeasurement(Status.FAILED, error=str(raw["error"]))
    return_code = int(raw.get("return_code", 0))
    if return_code not in (0, 1):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"generated coverage runner exited with code {return_code}",
        )
    coverage_json = raw.get("coverage")
    if not isinstance(coverage_json, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing payload")

    return _coverage_measurement_from_json(
        coverage_json,
        module_name,
        source="coverage.py API direct",
    )


def _coverage_measurement_from_json(
    raw: dict[str, object],
    module_name: str,
    *,
    source: str,
) -> CoverageMeasurement:
    """Build a CoverageMeasurement from coverage.py JSON output."""
    files_data = raw.get("files", {})
    if not isinstance(files_data, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing files section")

    mod_path = module_name.replace(".", "/")

    file_data = None
    for file_key, data in files_data.items():
        if mod_path in str(file_key):
            file_data = data
            break

    if not isinstance(file_data, dict):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"module {module_name} not found in coverage report",
        )

    summary = file_data.get("summary", {})
    if not isinstance(summary, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing summary")

    percent = float(summary.get("percent_covered", 0.0))
    total_stmts = int(summary.get("num_statements", 0))
    missing_raw = file_data.get("missing_lines", [])
    missing_lines = (
        frozenset(int(x) for x in missing_raw) if isinstance(missing_raw, list) else frozenset()
    )
    missing_count = summary.get("missing_lines", 0)

    return _build_verified_coverage(
        percent=percent,
        total_stmts=total_stmts,
        missing_lines=missing_lines,
        missing_count=int(missing_count) if isinstance(missing_count, int) else len(missing_lines),
        source=source,
    )


def _coverage_measurement_from_trace_payload(raw: dict[str, object]) -> CoverageMeasurement:
    """Build a CoverageMeasurement from the internal trace fallback payload."""
    percent = float(raw.get("percent_covered", 0.0))
    total_stmts = int(raw.get("num_statements", 0))
    missing_raw = raw.get("missing_lines", [])
    missing_lines = (
        frozenset(int(x) for x in missing_raw) if isinstance(missing_raw, list) else frozenset()
    )

    return _build_verified_coverage(
        percent=percent,
        total_stmts=total_stmts,
        missing_lines=missing_lines,
        missing_count=len(missing_lines),
        source="stdlib trace fallback",
    )


def _build_verified_coverage(
    *,
    percent: float,
    total_stmts: int,
    missing_lines: frozenset[int],
    missing_count: int,
    source: str,
) -> CoverageMeasurement:
    """Cross-check a coverage payload and wrap it as a verified result."""
    if total_stmts > 0:
        computed = (total_stmts - len(missing_lines)) / total_stmts * 100
        if abs(computed - percent) > 1.0:
            return CoverageMeasurement(
                Status.FAILED,
                error=(
                    f"coverage data inconsistent: reported {percent:.1f}% "
                    f"but computed {computed:.1f}% from "
                    f"{total_stmts} stmts - {len(missing_lines)} missing"
                ),
            )

    return CoverageMeasurement(
        Status.VERIFIED,
        result=CoverageResult(
            percent=percent,
            total_statements=total_stmts,
            missing_count=missing_count,
            missing_lines=missing_lines,
            source=source,
        ),
    )


# ============================================================================
# Test suggestions — from coverage gap analysis
# ============================================================================


def _suggest_tests(
    module_name: str,
    current_missing: frozenset[int],
    migrated_missing: frozenset[int],
) -> list[str]:
    """Generate test suggestions for lines covered by current but not migrated.

    Reads the source at each gap line and describes what to test.

    **How it works:**

    1. Compute ``gap = migrated_missing - current_missing``
       (lines that current tests cover but migrated tests don't)
    2. Group consecutive gap lines into blocks (within LINE_BLOCK_GAP)
    3. For each block, read the source and identify the construct
       (if/elif, return, raise, for, assignment)
    4. Find the enclosing function name by scanning backwards

    **Limitations:**

    - Uses string matching on source, not AST. Can match keywords
      inside strings or comments.
    - Assumes line numbers are still valid (source hasn't changed
      since coverage was measured).
    - Capped at MAX_SUGGESTIONS to avoid overwhelming the user.
    """
    gap_lines = migrated_missing - current_missing
    if not gap_lines:
        return []

    try:
        mod = _resolve_module(module_name)
        source_file = getattr(mod, "__file__", None)
        if source_file is None:
            return [f"cannot suggest: {module_name} has no __file__"]
        source = Path(source_file).read_text(encoding="utf-8").splitlines()
    except (ImportError, OSError) as exc:
        return [f"cannot suggest: {exc}"]

    suggestions: list[str] = []
    sorted_lines = sorted(gap_lines)

    # Group consecutive lines into blocks
    blocks: list[list[int]] = []
    current_block: list[int] = []
    for ln in sorted_lines:
        if current_block and ln > current_block[-1] + LINE_BLOCK_GAP:
            blocks.append(current_block)
            current_block = [ln]
        else:
            current_block.append(ln)
    if current_block:
        blocks.append(current_block)

    for block in blocks[:MAX_SUGGESTIONS]:
        first = block[0]
        if first - 1 >= len(source):
            suggestions.append(f"L{first}: line number out of range (source may have changed)")
            continue

        line_text = source[first - 1].strip()

        # Find enclosing function by scanning backwards for "def "
        func_name = "<module>"
        for i in range(first - 1, -1, -1):
            stripped = source[i].strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                func_name = stripped.split("(")[0].replace("def ", "").replace("async ", "")
                break

        # Describe the construct
        if "if " in line_text or "elif " in line_text:
            condition = line_text.split("if ", 1)[-1].rstrip(":")
            suggestions.append(f"L{first} in {func_name}(): test when {condition}")
        elif "return " in line_text:
            suggestions.append(f"L{first} in {func_name}(): test input that triggers this return")
        elif "raise " in line_text:
            exc_type = line_text.split("raise ", 1)[-1].split("(")[0]
            suggestions.append(f"L{first} in {func_name}(): test that {exc_type} is raised")
        elif "for " in line_text:
            suggestions.append(f"L{first} in {func_name}(): test with non-empty input for loop")
        else:
            suggestions.append(
                f"L{first} in {func_name}(): cover '{line_text[:SOURCE_TRUNCATION]}'"
            )

    return suggestions


# ============================================================================
# Migrated test generation
# ============================================================================


def _type_expr(
    tp: object,
    *,
    current_module: str,
    imports: set[str],
) -> str | None:
    """Render a type annotation and collect any imports it needs."""
    import types as pytypes

    if isinstance(tp, str):
        return tp
    if tp is type(None):
        return "None"
    if tp is Any:
        imports.add("from typing import Any")
        return "Any"

    origin = get_origin(tp)
    if origin is Literal:
        imports.add("from typing import Literal")
        return f"Literal[{', '.join(repr(arg) for arg in get_args(tp))}]"

    if origin is Union or (hasattr(pytypes, "UnionType") and origin is pytypes.UnionType):
        parts = []
        for arg in get_args(tp):
            part = _type_expr(arg, current_module=current_module, imports=imports)
            if part is None:
                return None
            parts.append(part)
        return " | ".join(parts)

    if origin in {list, set, frozenset}:
        args = get_args(tp)
        if len(args) != 1:
            return origin.__name__
        inner = _type_expr(args[0], current_module=current_module, imports=imports)
        if inner is None:
            return None
        return f"{origin.__name__}[{inner}]"

    if origin is dict:
        args = get_args(tp)
        if len(args) != 2:
            return "dict"
        key = _type_expr(args[0], current_module=current_module, imports=imports)
        value = _type_expr(args[1], current_module=current_module, imports=imports)
        if key is None or value is None:
            return None
        return f"dict[{key}, {value}]"

    if origin is tuple:
        rendered: list[str] = []
        for arg in get_args(tp):
            if arg is Ellipsis:
                rendered.append("...")
                continue
            part = _type_expr(arg, current_module=current_module, imports=imports)
            if part is None:
                return None
            rendered.append(part)
        return f"tuple[{', '.join(rendered)}]"

    if origin is not None:
        origin_expr = _type_expr(origin, current_module=current_module, imports=imports)
        if origin_expr is None:
            return None
        rendered_args: list[str] = []
        for arg in get_args(tp):
            part = _type_expr(arg, current_module=current_module, imports=imports)
            if part is None:
                return None
            rendered_args.append(part)
        if not rendered_args:
            return origin_expr
        return f"{origin_expr}[{', '.join(rendered_args)}]"

    module = getattr(tp, "__module__", None)
    qualname = getattr(tp, "__qualname__", None) or getattr(tp, "__name__", None)
    if qualname is None:
        return None
    if module in {None, "builtins"}:
        return qualname
    imports.add(f"import {module}")
    return f"{module}.{qualname}"


def _func_sig_for_codegen(
    func: object,
) -> tuple[list[str], list[str], str, list[str]] | None:
    """Extract param info for generated ``@quickcheck`` test, or *None*.

    Returns ``(param_names, param_decls_with_types, call_args_str, imports)``
    only when every required parameter has a renderable type hint.
    """
    try:
        hints = safe_get_annotations(func)
    except Exception:
        return None

    sig = inspect.signature(func)
    names: list[str] = []
    decls: list[str] = []
    imports: set[str] = set()
    current_module = getattr(func, "__module__", "")
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        if pname not in hints:
            return None
        type_str = _type_expr(
            hints[pname],
            current_module=current_module,
            imports=imports,
        )
        if type_str is None:
            return None
        names.append(pname)
        decls.append(f"{pname}: {type_str}")

    return (names, decls, ", ".join(names), sorted(imports)) if names else None


def _generated_callable_helper(
    module: str,
    func: object,
    name: str,
    param_names: list[str],
    param_decls: list[str],
    call_args: str,
) -> tuple[list[str], set[str], str]:
    """Generate a helper wrapper for a callable used in migrated tests."""
    safe_name = re.sub(r"[^0-9a-zA-Z_]", "_", name.replace(".", "_"))
    helper_name = f"_ordeal_target_{safe_name}"
    imports: set[str] = {
        "from ordeal.audit import _call_with_async_support",
        "from ordeal.auto import _prepare_bound_method_call",
    }
    lines = [f"def {helper_name}({', '.join(param_decls)}):"]

    kind = str(getattr(func, "__ordeal_kind__", "function"))
    owner_value = getattr(func, "__ordeal_owner__", None)
    owner = (
        getattr(owner_value, "__qualname__", getattr(owner_value, "__name__", None))
        if owner_value is not None and not isinstance(owner_value, str)
        else owner_value
    )
    method = getattr(func, "__ordeal_method__", None) or getattr(
        func,
        "__ordeal_method_name__",
        None,
    )
    factory = getattr(func, "__ordeal_factory__", None)
    setup = getattr(func, "__ordeal_setup__", None)
    scenarios = list(getattr(func, "__ordeal_scenarios__", []) or [])
    state_factory = getattr(func, "__ordeal_state_factory__", None)
    state_param = getattr(func, "__ordeal_state_param__", None)
    teardown = getattr(func, "__ordeal_teardown__", None)
    arg_suffix = f", {call_args}" if call_args else ""
    call_kwargs_expr = "{" + ", ".join(f"{item!r}: {item}" for item in param_names) + "}"

    if not owner or not method:
        target_expr = f"{module}.{name}"
        lines.append(f"    return _call_with_async_support({target_expr}{arg_suffix})")
        return lines, imports, helper_name

    if kind in {"static", "class"}:
        target_expr = f"{module}.{owner}.{method}"
        lines.append(f"    return _call_with_async_support({target_expr}{arg_suffix})")
        return lines, imports, helper_name

    factory_expr = None
    if factory:
        hook_import = _generated_hook_import(factory, f"_ordeal_factory_mod_{safe_name}")
        if hook_import is not None:
            import_line, factory_expr = hook_import
            imports.add(import_line)

    if factory_expr is None:
        lines.append(f"    instance = {module}.{owner}()")
    else:
        lines.append(f"    instance = _call_with_async_support({factory_expr})")
    if setup:
        hook_import = _generated_hook_import(setup, f"_ordeal_setup_mod_{safe_name}")
        if hook_import is not None:
            import_line, setup_expr = hook_import
            imports.add(import_line)
            lines.append(f"    setup_result = _call_with_async_support({setup_expr}, instance)")
            lines.append("    if setup_result is not None:")
            lines.append("        instance = setup_result")
    for idx, scenario in enumerate(scenarios, 1):
        hook_import = _generated_hook_import(scenario, f"_ordeal_scenario_mod_{safe_name}_{idx}")
        if hook_import is None:
            continue
        import_line, scenario_expr = hook_import
        imports.add(import_line)
        lines.append(f"    scenario_result = _call_with_async_support({scenario_expr}, instance)")
        lines.append("    if scenario_result is not None:")
        lines.append("        instance = scenario_result")
    lines.append("    try:")
    if state_factory and state_param:
        hook_import = _generated_hook_import(state_factory, f"_ordeal_state_mod_{safe_name}")
        if hook_import is not None:
            import_line, state_factory_expr = hook_import
            imports.add(import_line)
            lines.append(f"        _ordeal_bound = instance.{method}")
            lines.append(
                "        _ordeal_call_args, _ordeal_call_kwargs = "
                "_prepare_bound_method_call("
            )
            lines.append("            _ordeal_bound,")
            lines.append("            (),")
            lines.append(f"            {call_kwargs_expr},")
            lines.append("            instance=instance,")
            lines.append(f"            state_factory={state_factory_expr},")
            lines.append(f"            state_param={state_param!r},")
            lines.append("        )")
            lines.append(
                "        return _call_with_async_support("
                "_ordeal_bound, *_ordeal_call_args, **_ordeal_call_kwargs)"
            )
        else:
            lines.append(f"        return _call_with_async_support(instance.{method}{arg_suffix})")
    else:
        lines.append(f"        return _call_with_async_support(instance.{method}{arg_suffix})")
    lines.append("    finally:")
    if teardown:
        hook_import = _generated_hook_import(teardown, f"_ordeal_teardown_mod_{safe_name}")
        if hook_import is not None:
            import_line, teardown_expr = hook_import
            imports.add(import_line)
            lines.append(f"        _call_with_async_support({teardown_expr}, instance)")
        else:
            lines.append("        pass")
    else:
        lines.append("        pass")
    return lines, imports, helper_name


def _property_to_assertion(
    prop_name: str,
    call_expr: str,
    param_names: list[str],
) -> str | None:
    """Map a mined property name to an assertion line, or *None*."""
    call = call_expr

    if prop_name == "never None":
        return "assert result is not None"
    if prop_name.startswith("output type is "):
        tp = prop_name.removeprefix("output type is ")
        return f"assert type(result).__name__ == {tp!r}"
    if prop_name == "no NaN":
        return "assert not (isinstance(result, float) and math.isnan(result))"
    if prop_name == "output >= 0":
        return "assert result >= 0"
    if prop_name == "output in [0, 1]":
        return "assert 0 <= result <= 1"
    if prop_name == "never empty":
        return "assert len(result) > 0"
    if prop_name == "deterministic":
        return f"assert {call}({', '.join(param_names)}) == result"
    if prop_name == "idempotent":
        if len(param_names) == 1:
            return f"assert {call}(result) == result"
        if len(param_names) >= 2:
            rest = ", ".join(param_names[1:])
            return f"assert {call}(result, {rest}) == result"
    if prop_name == "involution":
        if len(param_names) == 1:
            return f"assert {call}(result) == {param_names[0]}"
        if len(param_names) >= 2:
            rest = ", ".join(param_names[1:])
            return f"assert {call}(result, {rest}) == {param_names[0]}"
    if prop_name == "commutative" and len(param_names) == 2:
        return f"assert {call}({param_names[1]}, {param_names[0]}) == result"
    for op in ("==", "<=", ">="):
        prefix = f"len(output) {op} len("
        if prop_name.startswith(prefix):
            param = prop_name.removeprefix(prefix).rstrip(")")
            if param in param_names:
                return f"assert len(result) {op} len({param})"

    return None


def _generated_hook_import(
    hook: str | Any | None,
    alias: str,
) -> tuple[str, str] | None:
    """Return ``(import_line, expr)`` for a generated helper hook."""
    if hook is None:
        return None
    if isinstance(hook, str):
        module_name, sep, attr_path = hook.partition(":")
        if not sep:
            module_name, _, attr_path = hook.rpartition(".")
    else:
        module_name = getattr(hook, "__module__", "")
        attr_path = getattr(hook, "__qualname__", getattr(hook, "__name__", ""))
    if not module_name or not attr_path or "<locals>" in attr_path:
        return None
    return (f"import {module_name} as {alias}", f"{alias}.{attr_path}")


def _generate_migrated_test(
    module: str,
    max_examples: int,
    warnings: list[str],
    *,
    scannable_functions: list[tuple[str, object]] | None = None,
    skipped_functions: list[str] | None = None,
    mine_results: dict[str, MineResult] | None = None,
) -> tuple[str, int, list[str]]:
    """Generate a consolidated test file: ordeal fuzz + mined property assertions.

    Returns ``(source_code, test_count, skipped_functions)``.

    The generated file has two layers per function:

    - ``fuzz()`` test — crash safety (does NOT verify correctness).
    - ``@quickcheck`` test — asserts mined properties with random inputs.
      Falls back to informational comments when type hints are missing.

    Args:
        module: Dotted module path.
        max_examples: Hypothesis examples for fuzz and mine.
        warnings: Mutable list — mining failures are appended here.
        scannable_functions: Optional pre-filtered ``(name, func)`` pairs.
        skipped_functions: Optional names missing inferred strategies.
        mine_results: Optional precomputed mine outputs keyed by function name.
    """
    mod = _resolve_module(module)

    base_imports = [
        "from ordeal.auto import fuzz",
        f"import {module}",
    ]
    extra_imports: set[str] = set()

    header = [
        f'"""Auto-generated ordeal test for {module}.',
        "",
        "fuzz() tests crash safety only — it does NOT verify correctness.",
        "Property tests assert mined invariants (confirmed by sampling).",
        '"""',
    ]
    body: list[str] = []

    if scannable_functions is None or skipped_functions is None:
        scannable, skipped, _discovered = _normalize_audit_function_collection(
            _collect_audit_functions(mod)
        )
    else:
        scannable = list(scannable_functions)
        skipped = list(skipped_functions)

    test_count = 0

    # Mine properties and generate assertion tests
    for name, func in scannable:
        safe_name = re.sub(r"[^0-9a-zA-Z_]", "_", name.replace(".", "_"))
        mine_result = None if mine_results is None else mine_results.get(name)
        if mine_result is None:
            try:
                cap = min(max_examples, MINE_EXAMPLES_FOR_GENERATED_TEST)
                mine_result = mine(func, max_examples=cap)
            except Exception as exc:
                warnings.append(f"mining failed for {name}: {type(exc).__name__}: {exc}")
                continue

        strong = [
            p
            for p in mine_result.properties
            if p.universal and p.total >= MIN_SAMPLES_FOR_PROPERTY
        ]

        sig_info = _func_sig_for_codegen(func)
        if strong and sig_info:
            param_names, param_decls, call_args, sig_imports = sig_info
            extra_imports.update(sig_imports)
            helper_lines, helper_imports, helper_name = _generated_callable_helper(
                module,
                func,
                name,
                param_names,
                param_decls,
                call_args,
            )
            extra_imports.update(helper_imports)
            extra_imports.add("from ordeal.quickcheck import quickcheck")
            body.extend(helper_lines)
            body.append("")
            assertions = [
                (p, _property_to_assertion(p.name, helper_name, param_names)) for p in strong
            ]
            has_any = any(a for _, a in assertions)
        else:
            helper_name = f"{module}.{name}"
            has_any = False

        test_count += 1
        body.append(f"def test_{safe_name}_no_crash():")
        body.append(f'    """Crash safety: {module}.{name} does not raise."""')
        body.append(f"    result = fuzz({helper_name}, max_examples={max_examples})")
        body.append("    assert result.passed, result.summary()")
        body.append("")

        if not strong:
            continue

        test_count += 1

        if has_any:
            if any(a and "math." in a for _, a in assertions):
                extra_imports.add("import math")

            body.append(f"@quickcheck(max_examples={max_examples})")
            body.append(f"def test_{safe_name}_properties({', '.join(param_decls)}):")
            body.append(f'    """Mined properties for {module}.{name}."""')
            body.append(f"    result = {helper_name}({call_args})")

            for prop, assertion in assertions:
                lower = wilson_lower(prop.holds, prop.total)
                ci = f">={lower:.1%} CI"
                if assertion:
                    body.append(f"    {assertion}  # {ci}")
                else:
                    body.append(f"    # {prop.name}: {prop.holds}/{prop.total} ({ci})")
            body.append("")
        else:
            # Fallback: comment-only (no type hints or no expressible assertions)
            body.append(f"def test_{safe_name}_properties():")
            body.append(f'    """Mined properties for {module}.{name}."""')
            for prop in strong:
                lower = wilson_lower(prop.holds, prop.total)
                body.append(
                    f"    # {prop.name}: {prop.holds}/{prop.total} (>={lower:.1%} at 95% CI)"
                )
            body.append(f"    result = fuzz({helper_name}, max_examples={max_examples})")
            body.append("    assert result.passed")
            body.append("")

    all_imports = base_imports + sorted(extra_imports)
    full = header + [""] + all_imports + ["", ""] + body
    return "\n".join(full), test_count, skipped


# ============================================================================
# Self-verification
# ============================================================================


def _verify_consistency(
    current: CoverageMeasurement,
    migrated: CoverageMeasurement,
    generated_test: str,
    migrated_test_count: int,
    warnings: list[str],
) -> None:
    """Cross-check audit outputs for internal consistency.

    Appends warnings for any inconsistency found.  Does NOT change
    measurement status — just flags concerns.

    **Checks performed:**

    1. If both measurements succeeded, ``total_statements`` should match
       (same module, same source file).
    2. ``migrated_test_count`` should match ``def test_`` count in the
       generated file.
    """
    if current.status == Status.VERIFIED and migrated.status == Status.VERIFIED:
        cur_stmts = current.result.total_statements  # type: ignore[union-attr]
        mig_stmts = migrated.result.total_statements  # type: ignore[union-attr]
        if cur_stmts != mig_stmts:
            warnings.append(
                f"statement count mismatch: current={cur_stmts}, "
                f"migrated={mig_stmts} (expected same source)"
            )

    actual_count = generated_test.count("def test_")
    if actual_count != migrated_test_count:
        warnings.append(
            f"test count mismatch: reported={migrated_test_count}, "
            f"actual in generated file={actual_count}"
        )


def _should_validate_mined_properties(mine_result: MineResult) -> bool:
    """Return whether mutation validation is likely to be useful.

    Validation is expensive because it runs mutation tests. We only run it
    when mining found at least one high-confidence universal property that
    can become a concrete assertion.
    """
    return any(p.universal and p.total >= 5 for p in mine_result.properties)


def _record_validation_result(
    result: ModuleAudit,
    target_path: str,
    mutation_result: Any,
    *,
    kill_counts: dict[str, int],
) -> None:
    """Aggregate one mutation-validation result into the module audit."""
    for mutant in mutation_result.survived:
        result.mutation_gaps.append(
            {
                "target": target_path,
                "location": mutant.location,
                "description": mutant.description,
                "source_line": mutant.source_line or "",
                "remediation": mutant.remediation,
            }
        )

    for test_name, mutants in mutation_result.kill_attribution().items():
        kill_counts[test_name] = kill_counts.get(test_name, 0) + len(mutants)

    stub = mutation_result.generate_test_stubs()
    if stub:
        result.mutation_gap_stubs.append({"target": target_path, "content": stub})


# ============================================================================
# Main audit function
# ============================================================================


def audit(
    module: str,
    *,
    targets: Sequence[Any] | None = None,
    test_dir: str = "tests",
    max_examples: int = 20,
    workers: int = 1,
    validation_mode: AuditValidationMode = "fast",
    contract_checks: Mapping[str, Sequence[Any]] | None = None,
    min_fixture_completeness: float = 0.0,
) -> ModuleAudit:
    """Audit a module: measure current tests vs ordeal-migrated tests.

    Runs BOTH test suites and MEASURES coverage.  Every number in the
    result is either ``[verified]`` or ``FAILED: reason``.

    The "migrated" test combines ordeal ``fuzz()`` (crash safety) with
    mined property descriptions.  The generated file is written to
    ``.ordeal/test_<mod>_migrated.py`` for inspection and debugging.

    Args:
        module: Dotted module path (e.g., ``"myapp.scoring"``).
        test_dir: Directory containing existing tests.
        max_examples: Hypothesis examples per function.
        workers: Parallel workers for mutation validation.
            ``1`` keeps the current sequential behavior.
        validation_mode: ``"fast"`` replays mined inputs against mutants.
            ``"deep"`` replays mined inputs, then re-mines each mutant.
        contract_checks: Explicit semantic contract probes keyed by callable name.
        min_fixture_completeness: Minimum runnable-target fraction required before
            audit spends time on migrated-test generation and mutation checks.

    Returns:
        A ``ModuleAudit`` with verified or explicitly-failed measurements.
    """
    validation_mode = _normalize_validation_mode(validation_mode)
    raw_target = module
    base_module, owner_path, _method_name = _split_audit_target_spec(module)
    result = ModuleAudit(module=base_module, validation_mode=validation_mode)
    test_path = Path(test_dir)
    state_hash: str | None = None
    target_specs = list(targets or [])
    if owner_path is not None:
        target_specs = [raw_target, *target_specs]
    cache_key = _audit_target_cache_key(base_module, target_specs)

    try:
        state_hash = _audit_state_hash(
            base_module,
            test_dir=test_dir,
            max_examples=max_examples,
            validation_mode=validation_mode,
            target_specs=target_specs,
        )
        cached = _load_audit_cache(cache_key, state_hash)
        if cached is not None:
            out_path = _generated_test_path(base_module)
            out_path.parent.mkdir(exist_ok=True)
            if cached.generated_test:
                out_path.write_text(cached.generated_test, encoding="utf-8")
            return cached
    except Exception:
        state_hash = None

    # -- 1. Find and measure existing tests --
    test_files = _find_test_files(base_module, test_path)
    test_file_evidence = _find_test_file_evidence(base_module, test_path)
    for tf in test_files:
        count, err = _count_tests_in_file(tf)
        result.current_test_count += count
        if err:
            result.warnings.append(err)

        lines, err = _count_lines_in_file(tf)
        result.current_test_lines += lines
        if err:
            result.warnings.append(err)

    # -- 2. Generate migrated test --
    try:
        mod = _resolve_module(base_module)
    except ImportError as exc:
        if test_files:
            result.current_coverage = _measure_coverage(test_files, base_module)
        else:
            result.current_coverage = CoverageMeasurement(
                Status.FAILED,
                error="no test files found",
            )
        result.warnings.append(f"cannot import {base_module}: {exc}")
        return result

    scannable, skipped, discovered_callables = _normalize_audit_function_collection(
        _collect_audit_functions(
            mod,
            target_specs=target_specs,
        )
    )
    result.gap_functions = skipped
    result.total_functions = len(discovered_callables)
    result.contract_findings = _audit_contract_findings(
        discovered_callables,
        contract_checks=contract_checks,
        warnings=result.warnings,
    )
    result.blocking_reason = _audit_blocking_reason(
        total_functions=result.total_functions,
        gap_functions=result.gap_functions,
        discovered_functions=discovered_callables,
        min_fixture_completeness=min_fixture_completeness,
    )
    if result.blocking_reason is not None:
        if test_files:
            result.current_coverage = _measure_coverage(test_files, base_module)
        else:
            result.current_coverage = CoverageMeasurement(
                Status.FAILED,
                error="no test files found",
            )
        collected_nodeids = (
            _collect_pytest_nodeids(test_files)
            if _should_collect_pytest_nodeids(
                discovered_callables,
                current_coverage=result.current_coverage,
                test_file_evidence=test_file_evidence,
            )
            else {}
        )
        result.function_audits = _build_function_audits(
            discovered_callables,
            current_coverage=result.current_coverage,
            test_file_evidence=test_file_evidence,
            collected_nodeids=collected_nodeids,
        )
        from ordeal.mine import STRUCTURAL_LIMITATIONS

        result.not_checked = list(STRUCTURAL_LIMITATIONS)
        if state_hash is not None:
            try:
                _save_audit_cache(cache_key, state_hash, result)
            except Exception:
                pass
        return result
    mine_examples = min(max_examples, MINE_EXAMPLES_FOR_GENERATED_TEST)
    mine_results = _mine_audit_functions(
        scannable,
        max_examples=mine_examples,
        warnings=result.warnings,
    )

    generated, test_count, _skipped = _generate_migrated_test(
        base_module,
        max_examples,
        result.warnings,
        scannable_functions=scannable,
        skipped_functions=skipped,
        mine_results=mine_results,
    )
    result.generated_test = generated
    result.migrated_test_count = test_count
    result.migrated_lines = len(
        [ln for ln in generated.splitlines() if ln.strip()],
    )

    # Collect mined properties with confidence bounds
    for name, mine_result in mine_results.items():
        for p in mine_result.properties:
            if p.universal and p.total >= 5:
                lower = wilson_lower(p.holds, p.total)
                result.mined_properties.append(
                    f"{name}: {p.name} ({p.holds}/{p.total}, >={lower:.0%} CI)"
                )

    # Suggest metamorphic relations from mined properties
    result.suggested_relations = _suggest_relations(result.mined_properties)

    # Validate mined properties against mutations using standard preset
    from ordeal.mutations import validate_mined_properties

    targets: list[tuple[str, MineResult]] = []
    for name, _func in scannable:
        mine_result = mine_results.get(name)
        if mine_result is not None and _should_validate_mined_properties(mine_result):
            targets.append((f"{base_module}.{name}", mine_result))

    total_killed = total_mutants = 0
    max_validation_examples = min(max_examples, 20)
    worker_count = max(1, workers)
    kill_counts: dict[str, int] = {}

    if worker_count == 1 or len(targets) <= 1:
        for target_path, mine_result in targets:
            try:
                mr = validate_mined_properties(
                    target_path,
                    max_examples=max_validation_examples,
                    preset="standard",
                    mine_result=mine_result,
                    validation_mode=validation_mode,
                )
                total_killed += mr.killed
                total_mutants += mr.total
                _record_validation_result(result, target_path, mr, kill_counts=kill_counts)
            except Exception:
                pass
    else:
        with ThreadPoolExecutor(max_workers=min(worker_count, len(targets))) as executor:
            future_targets = {
                executor.submit(
                    validate_mined_properties,
                    target_path,
                    max_examples=max_validation_examples,
                    preset="standard",
                    mine_result=mine_result,
                    validation_mode=validation_mode,
                ): target_path
                for target_path, mine_result in targets
            }
            for future in as_completed(future_targets):
                try:
                    target_path = future_targets[future]
                    mr = future.result()
                except Exception:
                    continue
                total_killed += mr.killed
                total_mutants += mr.total
                _record_validation_result(result, target_path, mr, kill_counts=kill_counts)
    if total_mutants > 0:
        pct = total_killed / total_mutants
        result.mutation_score = f"{total_killed}/{total_mutants} ({pct:.0%})"
    if kill_counts:
        weakest = sorted(kill_counts.items(), key=lambda item: (item[1], item[0]))
        result.weakest_tests = [
            {"test": test_name, "kills": count} for test_name, count in weakest[:DISPLAY_CAP]
        ]

    # -- 3. Measure migrated test coverage --
    out_path = _generated_test_path(base_module)
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(generated, encoding="utf-8")

    result.current_coverage, result.migrated_coverage = _measure_audit_coverages(
        test_files,
        [out_path],
        module,
    )

    collected_nodeids = (
        _collect_pytest_nodeids(test_files)
        if _should_collect_pytest_nodeids(
            scannable,
            current_coverage=result.current_coverage,
            test_file_evidence=test_file_evidence,
        )
        else {}
    )
    result.function_audits = _build_function_audits(
        discovered_callables,
        current_coverage=result.current_coverage,
        test_file_evidence=test_file_evidence,
        collected_nodeids=collected_nodeids,
    )

    # -- 4. Suggest tests to close the gap --
    result.suggestions = _suggest_tests(
        base_module,
        result.current_coverage.missing_lines,
        result.migrated_coverage.missing_lines,
    )

    # -- 5. State known unknowns --
    from ordeal.mine import STRUCTURAL_LIMITATIONS

    result.not_checked = list(STRUCTURAL_LIMITATIONS)

    # -- 6. Self-verify --
    _verify_consistency(
        result.current_coverage,
        result.migrated_coverage,
        generated,
        test_count,
        result.warnings,
    )

    if state_hash is not None:
        try:
            _save_audit_cache(cache_key, state_hash, result)
        except Exception:
            pass

    return result


# ============================================================================
# Report generation
# ============================================================================


def audit_report(
    modules: list[str],
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
    workers: int = 1,
    validation_mode: AuditValidationMode = "fast",
) -> str:
    """Audit multiple modules and produce a summary report.

    Returns a formatted string suitable for terminal output.
    Every number is labeled ``[verified]`` or ``FAILED``::

        from ordeal.audit import audit_report

        print(audit_report(["myapp.scoring", "myapp.utils"]))
        # ordeal audit
        #   myapp.scoring
        #     current:  33 tests | 343 lines | 98% coverage [verified]
        #     migrated: 12 tests | 130 lines | 96% coverage [verified]
        #     saving:   64% fewer tests | 62% less code | same coverage
        #   total:
        #     current:  55 tests | 500 lines
        #     migrated: 20 tests | 200 lines

    Args:
        modules: Dotted module paths to audit (e.g. ``["myapp.scoring"]``).
        test_dir: Directory containing test files (default ``"tests"``).
        max_examples: Hypothesis examples for property mining per function.
        workers: Parallel workers for mutation validation in each module audit.
        validation_mode: ``"fast"`` replay or ``"deep"`` replay + re-mining.
    """
    validation_mode = _normalize_validation_mode(validation_mode)
    results = [
        audit(
            mod,
            test_dir=test_dir,
            max_examples=max_examples,
            workers=workers,
            validation_mode=validation_mode,
        )
        for mod in modules
    ]
    return _render_audit_results(results)
