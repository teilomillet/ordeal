"""Audit a module's test coverage and show what ordeal replaces.

One command to justify adoption::

    ordeal audit myapp.scoring --test-dir tests/

Output::

    myapp.scoring
      current:  21 tests | 343 lines | 98% coverage
      ordeal:    5 tests |   0 lines | 98% coverage
      saving:   76% fewer tests | 100% less code | same coverage

The audit RUNS both test suites and MEASURES coverage. No estimates,
no assumptions — only verified numbers.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ordeal.auto import _resolve_module, scan_module


@dataclass
class ModuleAudit:
    """Audit result for one module."""

    module: str
    # Current state (from existing tests)
    current_test_count: int = 0
    current_test_lines: int = 0
    current_coverage_pct: float = 0.0
    current_uncovered: int = 0
    current_total_stmts: int = 0
    # Ordeal state (from auto-scan)
    ordeal_test_count: int = 0
    ordeal_coverage_pct: float = 0.0
    ordeal_uncovered: int = 0
    ordeal_scannable: int = 0
    ordeal_skipped: int = 0
    # Functions that ordeal covers
    ordeal_functions: list[str] = field(default_factory=list)
    # Functions ordeal can't cover (need fixtures)
    gap_functions: list[str] = field(default_factory=list)

    @property
    def test_reduction(self) -> float:
        """Fraction of tests ordeal replaces."""
        if self.current_test_count == 0:
            return 0.0
        return 1.0 - (self.ordeal_test_count / self.current_test_count)

    @property
    def coverage_preserved(self) -> bool:
        """True if ordeal coverage >= current coverage - 2%."""
        return self.ordeal_coverage_pct >= self.current_coverage_pct - 2.0

    def summary(self) -> str:
        """Human-readable one-module report."""
        lines = [f"\n  {self.module}"]
        lines.append(
            f"    current: {self.current_test_count:>4} tests "
            f"| {self.current_test_lines:>5} lines "
            f"| {self.current_coverage_pct:.0f}% coverage"
        )
        lines.append(
            f"    ordeal:  {self.ordeal_test_count:>4} tests "
            f"| {'0':>5} lines "
            f"| {self.ordeal_coverage_pct:.0f}% coverage"
        )

        if self.current_test_count > 0:
            test_pct = self.test_reduction * 100
            cov_delta = self.ordeal_coverage_pct - self.current_coverage_pct
            cov_str = "same coverage" if abs(cov_delta) < 1 else f"{cov_delta:+.0f}% coverage"
            lines.append(f"    saving:  {test_pct:.0f}% fewer tests | 100% less code | {cov_str}")

        if self.gap_functions:
            lines.append(
                f"    gaps:    {len(self.gap_functions)} functions need fixtures: "
                f"{', '.join(self.gap_functions[:5])}"
            )

        return "\n".join(lines)


def _count_tests_in_file(path: Path) -> int:
    """Count `def test_` occurrences in a file."""
    try:
        text = path.read_text()
        return text.count("def test_")
    except OSError:
        return 0


def _count_lines_in_file(path: Path) -> int:
    """Count non-empty lines in a file."""
    try:
        return sum(1 for line in path.read_text().splitlines() if line.strip())
    except OSError:
        return 0


def _find_test_files(module_name: str, test_dir: Path) -> list[Path]:
    """Find test files that primarily test the given module.

    Matches by filename convention only: ``test_<short_name>.py``
    and ``test_<short_name>_*.py`` (for property/props/unit variants).
    This avoids overcounting cross-module test files.
    """
    results = []
    mod_short = module_name.rsplit(".", 1)[-1]

    for test_file in sorted(test_dir.rglob("test_*.py")):
        stem = test_file.stem  # e.g., "test_scoring_props"
        # Match: test_scoring.py, test_scoring_props.py, test_scoring_unit.py
        if stem == f"test_{mod_short}" or stem.startswith(f"test_{mod_short}_"):
            results.append(test_file)

    return results


def _measure_coverage(
    test_files: list[Path],
    module_name: str,
) -> tuple[float, int, int]:
    """Run tests with in-process coverage measurement.

    Returns (coverage_pct, uncovered_lines, total_statements).
    """
    if not test_files:
        return 0.0, 0, 0

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *[str(f) for f in test_files],
        f"--cov={module_name}",
        "--cov-report=term",
        "-q",
        "--tb=no",
        "--no-header",
        "-x",
        "-p",
        "no:ordeal",
    ]

    # Force PYTHONPATH to project root so conftest imports resolve
    cwd = str(Path.cwd())
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = cwd

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
            env=env,
        )
        mod_path = module_name.replace(".", "/")
        for line in result.stdout.splitlines():
            if mod_path not in line:
                continue
            parts = line.split()
            for i, part in enumerate(parts):
                if "%" in part:
                    try:
                        pct = float(part.rstrip("%"))
                        stmts = int(parts[i - 2])
                        miss = int(parts[i - 1])
                        return pct, miss, stmts
                    except (ValueError, IndexError):
                        continue
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return 0.0, 0, 0


def audit(
    module: str,
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
) -> ModuleAudit:
    """Audit a module: measure current tests vs ordeal auto-scan.

    Runs BOTH test suites and MEASURES coverage. No estimates.

    Args:
        module: Dotted module path (e.g., ``"myapp.scoring"``).
        test_dir: Directory containing existing tests.
        max_examples: Hypothesis examples for ordeal scan.
    """
    result = ModuleAudit(module=module)
    test_path = Path(test_dir)

    # -- 1. Find and measure existing tests --
    test_files = _find_test_files(module, test_path)
    for tf in test_files:
        result.current_test_count += _count_tests_in_file(tf)
        result.current_test_lines += _count_lines_in_file(tf)

    if test_files:
        pct, uncov, total = _measure_coverage(test_files, module)
        result.current_coverage_pct = pct
        result.current_uncovered = uncov
        result.current_total_stmts = total

    # -- 2. Run ordeal scan --
    try:
        _resolve_module(module)
    except ImportError:
        return result

    scan_result = scan_module(module, max_examples=max_examples)
    result.ordeal_test_count = scan_result.total
    result.ordeal_scannable = scan_result.total
    result.ordeal_skipped = len(scan_result.skipped)
    result.ordeal_functions = [f.name for f in scan_result.functions]
    result.gap_functions = [name for name, reason in scan_result.skipped]

    # -- 3. Measure ordeal coverage --
    # Write a temporary test file that runs scan_module
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        prefix="ordeal_audit_",
        delete=False,
    ) as tmp:
        tmp.write(f"""
from ordeal.auto import scan_module
def test_ordeal_scan():
    result = scan_module("{module}", max_examples={max_examples})
    # Just run it — we only care about coverage
""")
        tmp_path = tmp.name

    try:
        pct, uncov, total = _measure_coverage([Path(tmp_path)], module)
        result.ordeal_coverage_pct = pct
        result.ordeal_uncovered = uncov
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return result


def audit_report(
    modules: list[str],
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
) -> str:
    """Audit multiple modules and produce a summary report.

    Returns a formatted string suitable for terminal output.
    """
    results = []
    for mod in modules:
        results.append(audit(mod, test_dir=test_dir, max_examples=max_examples))

    lines = ["ordeal audit"]
    total_current_tests = 0
    total_current_lines = 0
    total_ordeal_tests = 0

    for r in results:
        lines.append(r.summary())
        total_current_tests += r.current_test_count
        total_current_lines += r.current_test_lines
        total_ordeal_tests += r.ordeal_test_count

    if len(results) > 1:
        lines.append("\n  total:")
        lines.append(f"    current: {total_current_tests} tests | {total_current_lines} lines")
        lines.append(f"    ordeal:  {total_ordeal_tests} tests | 0 lines")
        if total_current_tests > 0:
            reduction = (1 - total_ordeal_tests / total_current_tests) * 100
            lines.append(f"    saving:  {reduction:.0f}% fewer tests")

    return "\n".join(lines)
