from __future__ import annotations
# ruff: noqa
import ast
import asyncio
import contextlib
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
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Literal, Mapping, Sequence, Union, get_args, get_origin
from ordeal.auto import (
    _active_contract_faults,
    _active_instance_probe,
    _evaluate_contract_checks,
    _get_public_functions,
    _infer_strategies,
    _instance_probe_result,
    _lifecycle_fault_runtime,
    _mine_object_harness_hints,
    _prepare_bound_method_call,
    _python_source_path_to_module_name,
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
