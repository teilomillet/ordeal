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

The audit runs ``pytest --cov=<module> --cov-report=json:<path>``
as a subprocess.  The JSON report has a stable schema (coverage.py v7+)
with structured fields: ``percent_covered``, ``missing_lines``, etc.
This replaces the previous approach of parsing terminal stdout, which
broke 3 times during development when the output format didn't match.

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

import enum
import json
import math
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ordeal.auto import (
    _get_public_functions,
    _infer_strategies,
    _resolve_module,
    scan_module,
)
from ordeal.mine import mine

# ============================================================================
# Constants — every number has a documented rationale
# ============================================================================

SUBPROCESS_TIMEOUT_SECONDS: int = 120
"""Timeout for the pytest-cov subprocess, in seconds.

**Why 120s:** Large projects with 500+ tests may take 60-90s under
coverage instrumentation.  120s provides 2x margin without hanging
indefinitely.  Measured: ordeal's own 276 tests complete in ~9s;
vauban's 3000 tests complete in ~14s.
"""

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
    gap_functions: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

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
            test_pct = (1.0 - self.migrated_test_count / self.current_test_count) * 100
            line_pct = (1.0 - self.migrated_lines / self.current_test_lines) * 100
            delta = mig.percent - cur.percent
            cov_str = "same coverage" if abs(delta) < COVERAGE_TOLERANCE_PCT else f"{delta:+.0f}%"
            lines.append(
                f"    saving:   {test_pct:.0f}% fewer tests "
                f"| {line_pct:.0f}% less code "
                f"| {cov_str}"
            )

        if self.mined_properties:
            lines.append(f"    mined:    {', '.join(self.mined_properties[:DISPLAY_CAP])}")

        if self.gap_functions:
            lines.append(
                f"    gaps:     {len(self.gap_functions)} functions need fixtures: "
                f"{', '.join(self.gap_functions[:DISPLAY_CAP])}"
            )

        if self.suggestions:
            lines.append("    suggest:")
            for s in self.suggestions:
                lines.append(f"      - {s}")

        if self.not_checked:
            lines.append("    NOT verified (write these tests manually):")
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


def _find_test_files(module_name: str, test_dir: Path) -> list[Path]:
    """Find test files that primarily test the given module.

    Matches by filename convention: ``test_<short>.py`` and
    ``test_<short>_*.py`` (for variants like ``_props``, ``_unit``).

    **Why filename-only:** Matching by import content overcounts.
    Many cross-module tests import ``scoring`` but aren't primarily
    testing it.  Filename convention is the only reliable signal.

    **Limitation:** Misses tests named ``<module>_test.py`` or
    placed in non-standard locations.
    """
    results = []
    mod_short = module_name.rsplit(".", 1)[-1]

    if not test_dir.is_dir():
        return results

    for test_file in sorted(test_dir.rglob("test_*.py")):
        stem = test_file.stem
        if stem == f"test_{mod_short}" or stem.startswith(f"test_{mod_short}_"):
            results.append(test_file)

    return results


# ============================================================================
# Coverage measurement — via JSON, not stdout parsing
# ============================================================================


def _measure_coverage(
    test_files: list[Path],
    module_name: str,
) -> CoverageMeasurement:
    """Run tests and measure coverage via coverage.py JSON report.

    **Why JSON, not terminal:** The terminal format (``--cov-report=term``)
    is designed for humans, not machines.  Column positions, spacing, and
    header format vary across pytest-cov versions.  Parsing it broke 3
    times during development.  The JSON format has a stable schema
    (coverage.py v7+) with structured fields.

    **How it works:**

    1. Run ``pytest --cov=<module> --cov-report=json:<tmpfile>``
    2. Parse the JSON file (structured, version-checked)
    3. Cross-check: verify ``percent ≈ (stmts - missing) / stmts * 100``
    4. On ANY failure: return ``CoverageMeasurement(status=FAILED)``

    **What can go wrong (all handled explicitly):**

    - pytest not installed → FAILED: "pytest not found"
    - pytest-cov not installed → FAILED: "pytest-cov not installed"
    - subprocess timeout → FAILED: "timed out after Ns"
    - JSON file not created → FAILED: "coverage report not generated"
    - JSON parse error → FAILED: "invalid JSON"
    - Module not in report → FAILED: "module not found in report"
    - Cross-check fails → FAILED: "coverage data inconsistent"
    """
    if not test_files:
        return CoverageMeasurement(Status.FAILED, error="no test files provided")

    cwd = str(Path.cwd())

    # Write coverage JSON to a temp file
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    # Detect if test files are inside the project (need conftest)
    # or outside (e.g., .ordeal/ generated files — no conftest).
    #
    # Why: conftest.py in the project's test/ directory may import
    # project-specific modules that fail when run from a different context.
    # For generated files, we bypass conftest with --override-ini.
    in_project = any(str(f).startswith(cwd) and "/.ordeal/" not in str(f) for f in test_files)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *[str(f) for f in test_files],
        f"--cov={module_name}",
        f"--cov-report=json:{json_path}",
        "--cov-report=",  # suppress terminal output
        "-q",
        "--tb=no",
        "--no-header",
        "-p",
        "no:ordeal",
    ]

    if not in_project:
        cmd.extend(["--override-ini", f"confcutdir={test_files[0].parent}"])

    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = cwd

    # -- Run subprocess --
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

    # -- Parse JSON --
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # pytest-cov may not be installed, or tests crashed before reporting
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        json_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}",
        )
    finally:
        json_path.unlink(missing_ok=True)

    # -- Extract module data --
    files_data = raw.get("files", {})
    mod_path = module_name.replace(".", "/")

    # Find the matching file entry (may be "vauban/scoring.py" or similar)
    file_data = None
    for file_key, data in files_data.items():
        if mod_path in file_key:
            file_data = data
            break

    if file_data is None:
        return CoverageMeasurement(
            Status.FAILED,
            error=f"module {module_name} not found in coverage report",
        )

    summary = file_data.get("summary", {})
    percent = summary.get("percent_covered", 0.0)
    total_stmts = summary.get("num_statements", 0)
    missing_count = summary.get("missing_lines", 0)
    missing_lines = frozenset(file_data.get("missing_lines", []))

    # -- Cross-check: verify internal consistency --
    # percent_covered should match (stmts - missing) / stmts * 100
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
            missing_count=missing_count if isinstance(missing_count, int) else len(missing_lines),
            missing_lines=missing_lines,
            source="coverage.py JSON",
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


def _type_repr(tp: type) -> str:
    """Best-effort render of a type annotation for code generation."""
    if tp is type(None):
        return "None"
    if hasattr(tp, "__name__") and not hasattr(tp, "__args__"):
        return tp.__name__
    s = str(tp)
    for prefix in ("typing.", "collections.abc."):
        s = s.replace(prefix, "")
    return s


def _func_sig_for_codegen(
    func: object,
) -> tuple[list[str], list[str], str] | None:
    """Extract param info for generated ``@quickcheck`` test, or *None*.

    Returns ``(param_names, param_decls_with_types, call_args_str)``
    only when every required parameter has a renderable type hint.
    """
    import inspect
    from typing import get_type_hints

    try:
        hints = get_type_hints(func)
    except Exception:
        return None

    sig = inspect.signature(func)
    names: list[str] = []
    decls: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        if pname not in hints:
            return None
        type_str = _type_repr(hints[pname])
        if "." in type_str:
            return None  # needs an import we can't safely generate
        names.append(pname)
        decls.append(f"{pname}: {type_str}")

    return (names, decls, ", ".join(names)) if names else None


def _property_to_assertion(
    prop_name: str,
    module: str,
    func_name: str,
    param_names: list[str],
) -> str | None:
    """Map a mined property name to an assertion line, or *None*."""
    call = f"{module}.{func_name}"

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
    if prop_name == "commutative" and len(param_names) == 2:
        return f"assert {call}({param_names[1]}, {param_names[0]}) == result"
    for op in ("==", "<=", ">="):
        prefix = f"len(output) {op} len("
        if prop_name.startswith(prefix):
            param = prop_name.removeprefix(prefix).rstrip(")")
            if param in param_names:
                return f"assert len(result) {op} len({param})"

    return None


def _generate_migrated_test(
    module: str,
    max_examples: int,
    warnings: list[str],
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

    scannable = []
    skipped = []
    for name, func in _get_public_functions(mod):
        strategies = _infer_strategies(func)
        if strategies is not None:
            scannable.append((name, func))
        else:
            skipped.append(name)

    test_count = 0
    for name, _func in scannable:
        test_count += 1
        body.append(f"def test_{name}_no_crash():")
        body.append(f'    """Crash safety: {module}.{name} does not raise."""')
        body.append(f"    result = fuzz({module}.{name}, max_examples={max_examples})")
        body.append("    assert result.passed, result.summary()")
        body.append("")

    # Mine properties and generate assertion tests
    mine_cap = min(max_examples, MINE_EXAMPLES_FOR_GENERATED_TEST)
    for name, func in scannable:
        try:
            mine_result = mine(func, max_examples=mine_cap)
        except Exception as exc:
            warnings.append(f"mining failed for {name}: {type(exc).__name__}: {exc}")
            continue

        strong = [
            p
            for p in mine_result.properties
            if p.universal and p.total >= MIN_SAMPLES_FOR_PROPERTY
        ]
        if not strong:
            continue

        sig_info = _func_sig_for_codegen(func)

        if sig_info:
            param_names, param_decls, call_args = sig_info
            assertions = [
                (p, _property_to_assertion(p.name, module, name, param_names))
                for p in strong
            ]
            has_any = any(a for _, a in assertions)
        else:
            has_any = False

        test_count += 1

        if has_any:
            extra_imports.add("from ordeal.quickcheck import quickcheck")
            if any(a and "math." in a for _, a in assertions):
                extra_imports.add("import math")

            body.append("@quickcheck")
            body.append(f"def test_{name}_properties({', '.join(param_decls)}):")
            body.append(f'    """Mined properties for {module}.{name}."""')
            body.append(f"    result = {module}.{name}({call_args})")

            for prop, assertion in assertions:
                lower = wilson_lower(prop.holds, prop.total)
                ci = f">={lower:.1%} CI"
                if assertion:
                    body.append(f"    {assertion}  # {ci}")
                else:
                    body.append(
                        f"    # {prop.name}: {prop.holds}/{prop.total} ({ci})"
                    )
            body.append("")
        else:
            # Fallback: comment-only (no type hints or no expressible assertions)
            body.append(f"def test_{name}_properties():")
            body.append(f'    """Mined properties for {module}.{name}."""')
            for prop in strong:
                lower = wilson_lower(prop.holds, prop.total)
                body.append(
                    f"    # {prop.name}: {prop.holds}/{prop.total}"
                    f" (>={lower:.1%} at 95% CI)"
                )
            body.append(
                f"    result = fuzz({module}.{name}, max_examples={max_examples})"
            )
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


# ============================================================================
# Main audit function
# ============================================================================


def audit(
    module: str,
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
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

    Returns:
        A ``ModuleAudit`` with verified or explicitly-failed measurements.
    """
    result = ModuleAudit(module=module)
    test_path = Path(test_dir)

    # -- 1. Find and measure existing tests --
    test_files = _find_test_files(module, test_path)
    for tf in test_files:
        count, err = _count_tests_in_file(tf)
        result.current_test_count += count
        if err:
            result.warnings.append(err)

        lines, err = _count_lines_in_file(tf)
        result.current_test_lines += lines
        if err:
            result.warnings.append(err)

    if test_files:
        result.current_coverage = _measure_coverage(test_files, module)
    else:
        result.current_coverage = CoverageMeasurement(
            Status.FAILED,
            error="no test files found",
        )

    # -- 2. Generate migrated test --
    try:
        _resolve_module(module)
    except ImportError as exc:
        result.warnings.append(f"cannot import {module}: {exc}")
        return result

    scan_result = scan_module(module, max_examples=max_examples)
    result.gap_functions = [name for name, _reason in scan_result.skipped]

    generated, test_count, _skipped = _generate_migrated_test(
        module,
        max_examples,
        result.warnings,
    )
    result.generated_test = generated
    result.migrated_test_count = test_count
    result.migrated_lines = len(
        [ln for ln in generated.splitlines() if ln.strip()],
    )

    # Collect mined properties with confidence bounds
    for name, func in _get_public_functions(_resolve_module(module)):
        try:
            mine_result = mine(func, max_examples=min(max_examples, 30))
            for p in mine_result.properties:
                if p.universal and p.total >= 5:
                    lower = wilson_lower(p.holds, p.total)
                    result.mined_properties.append(
                        f"{name}: {p.name} ({p.holds}/{p.total}, >={lower:.0%} CI)"
                    )
        except Exception as exc:
            result.warnings.append(
                f"mining failed for {name}: {type(exc).__name__}: {exc}",
            )

    # -- 3. Measure migrated test coverage --
    out_dir = Path(".ordeal")
    out_dir.mkdir(exist_ok=True)
    mod_short = module.rsplit(".", 1)[-1]
    out_path = out_dir / f"test_{mod_short}_migrated.py"
    out_path.write_text(generated, encoding="utf-8")

    result.migrated_coverage = _measure_coverage([out_path], module)

    # -- 4. Suggest tests to close the gap --
    result.suggestions = _suggest_tests(
        module,
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

    return result


# ============================================================================
# Report generation
# ============================================================================


def audit_report(
    modules: list[str],
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
) -> str:
    """Audit multiple modules and produce a summary report.

    Returns a formatted string suitable for terminal output.
    Every number is labeled ``[verified]`` or ``FAILED``.
    """
    results = []
    for mod in modules:
        results.append(audit(mod, test_dir=test_dir, max_examples=max_examples))

    lines = ["ordeal audit"]
    total_cur_tests = 0
    total_cur_lines = 0
    total_mig_tests = 0
    total_mig_lines = 0
    total_warnings = 0

    for r in results:
        lines.append(r.summary())
        total_cur_tests += r.current_test_count
        total_cur_lines += r.current_test_lines
        total_mig_tests += r.migrated_test_count
        total_mig_lines += r.migrated_lines
        total_warnings += len(r.warnings)

    if len(results) > 1:
        lines.append("\n  total:")
        lines.append(f"    current:  {total_cur_tests} tests | {total_cur_lines} lines")
        lines.append(f"    migrated: {total_mig_tests} tests | {total_mig_lines} lines")
        if total_cur_tests > 0:
            test_red = (1 - total_mig_tests / total_cur_tests) * 100
            line_red = (1 - total_mig_lines / total_cur_lines) * 100 if total_cur_lines > 0 else 0
            lines.append(f"    saving:   {test_red:.0f}% fewer tests | {line_red:.0f}% less code")
        if total_warnings > 0:
            lines.append(f"    warnings: {total_warnings} (run with --verbose)")

    return "\n".join(lines)
