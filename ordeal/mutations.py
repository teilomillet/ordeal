"""Mutation testing — validate that your tests catch real bugs.

Generates mutated versions of target code and runs tests against each.
If a mutant survives (tests still pass), the tests are missing something.

Quick start
-----------

Pick a preset and go — tests are auto-discovered via pytest::

    from ordeal import mutate_function_and_test

    result = mutate_function_and_test("myapp.scoring.compute", preset="standard")
    print(result.summary())   # shows test gaps + how to fix them

Or from the command line::

    ordeal mutate myapp.scoring.compute                # standard preset
    ordeal mutate myapp.scoring.compute -p essential    # fast check (4 operators)
    ordeal mutate myapp.scoring.compute -p thorough     # all 14 operators

Presets
-------

Each preset is a curated set of mutation operators — pick the level
that matches your situation:

- ``"essential"`` (4 ops) — arithmetic, comparison, negate, return_none.
  Catches wrong math, wrong comparisons, flipped conditions, and missing
  return values. Fast; good for first-time use and quick feedback loops.

- ``"standard"`` (8 ops) — essential + boundary, constant, logical,
  delete_statement. Adds off-by-one errors, magic numbers, and/or logic,
  and dead code detection. **Recommended default for CI.**

- ``"thorough"`` (14 ops) — every operator. Adds exception swallowing,
  argument swaps, break/continue swaps, and more. Use before releases
  or when you want comprehensive validation.

You can also pass ``operators=["arithmetic", "comparison"]`` for full
control — but ``preset`` and ``operators`` are mutually exclusive.

Entry points
------------

1. **Function-level** (recommended) — ``mutate_function_and_test()``
2. **Module-level** — ``mutate_and_test()``
3. **CLI** — ``ordeal mutate <target>``
4. **Config** — ``[mutations]`` section in ``ordeal.toml``

Reading the output
------------------

``result.summary()`` prints each surviving mutant with:

- **Location** — file line and column of the mutation.
- **Description** — what was changed (e.g. ``+ -> -``).
- **Fix guidance** — exactly what test to write to kill this mutant.

Discover all operators and presets programmatically::

    from ordeal.mutations import catalog
    for entry in catalog():
        print(f"{entry['name']} ({entry['type']})  -- {entry['doc']}")
"""

from __future__ import annotations

import ast
import contextlib
import copy
import functools
import hashlib
import importlib
import inspect
import json
import pkgutil
import sys
import textwrap
import time
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from ordeal.faults import PatchFault

# ============================================================================
# Helpers
# ============================================================================


def _unwrap_func(func: object) -> object:
    """Unwrap decorated/wrapped functions to reach the original source.

    Handles ``inspect.unwrap`` (follows ``__wrapped__`` chains),
    Ray's ``@ray.remote`` (stores the real function in ``._function``),
    Celery-style ``task.run`` patterns, staticmethod/classmethod
    (``__func__``), property (``fget``), and ``functools.partial``.
    """
    # Ray @ray.remote stores the original in ._function
    if hasattr(func, "_function"):
        func = func._function
    # staticmethod / classmethod → __func__
    if hasattr(func, "__func__"):
        func = func.__func__
    # property → fget
    if isinstance(func, property) and func.fget is not None:
        func = func.fget
    # functools.partial → .func
    if hasattr(func, "func") and isinstance(func, functools.partial):
        func = func.func
    # Celery-style task.run
    if hasattr(func, "run") and callable(getattr(func, "run", None)):
        candidate = func.run
        if hasattr(candidate, "__code__"):
            func = candidate
    # Standard unwrap (__wrapped__ chains from functools.wraps)
    try:
        func = inspect.unwrap(func)
    except (ValueError, TypeError):
        pass
    return func


def _get_source(func: object) -> str:
    """Extract source code for *func*, with file-based fallback.

    Tries ``inspect.getsource`` first.  When that fails (common for
    decorated callables whose wrapper is defined in C or lacks source
    metadata), falls back to reading the source file directly using
    ``__code__`` attributes.
    """
    try:
        return textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        pass

    # Fallback: read from __code__.co_filename / co_firstlineno
    code = getattr(func, "__code__", None)
    if code is None:
        raise OSError(
            f"Cannot retrieve source for {func!r}: "
            "inspect.getsource failed and object has no __code__ attribute"
        )

    filename = code.co_filename
    first_line = code.co_firstlineno  # 1-based

    try:
        with open(filename) as fh:
            lines = fh.readlines()
    except (OSError, TypeError) as exc:
        raise OSError(
            f"Cannot retrieve source for {func!r}: "
            f"inspect.getsource failed and could not read {filename!r}"
        ) from exc

    # Walk from the def/async def line until dedent signals end of function
    start = first_line - 1  # 0-based
    if start >= len(lines):
        raise OSError(f"Source line {first_line} is past end of {filename}")

    func_lines = [lines[start]]
    # Determine the indentation of the def line
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            func_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        func_lines.append(line)

    return textwrap.dedent("".join(func_lines))


class NoTestsFoundError(RuntimeError):
    """Raised when auto-discovery finds no tests for a mutation target.

    Attributes:
        target: The dotted path that was being tested.
        suggested_file: Recommended filename to save starter tests to.
    """

    def __init__(self, message: str, *, target: str = "", suggested_file: str = ""):
        super().__init__(message)
        self.target = target
        self.suggested_file = suggested_file


# ============================================================================
# Data structures
# ============================================================================


_REMEDIATION: dict[str, str] = {
    "arithmetic": (
        "Add an assertion that checks the exact numeric result of this expression.\n"
        "    Example: assert compute(3, 4) == 7  # catches + -> -\n"
        "    The surviving mutant changes the arithmetic operator, so a test that\n"
        "    verifies the precise output value (not just sign or range) will kill it."
    ),
    "comparison": (
        "Add a boundary test using the exact threshold value.\n"
        "    Example: test with x == boundary to distinguish < from <=\n"
        "    The surviving mutant shifts a comparison boundary, so test the\n"
        "    value exactly at the boundary where < and <= differ."
    ),
    "negate": (
        "Add a test that exercises the opposite branch of this condition.\n"
        "    The surviving mutant flips an if-condition; add a test case where\n"
        "    the condition is True and verify different behavior from when False."
    ),
    "return_none": (
        "Add an assertion that checks the return value is not None.\n"
        "    Example: result = func(...); assert result is not None\n"
        "    Also verify the return value's type or contents."
    ),
    "boundary": (
        "Add a test using the exact integer constant and its neighbors.\n"
        "    Example: if the code uses limit=10, test with 9, 10, and 11.\n"
        "    The surviving mutant shifts an integer by ±1."
    ),
    "constant": (
        "Add a test that verifies the exact constant value matters.\n"
        "    The surviving mutant replaces a number with 0, 1, or -1.\n"
        "    Test with inputs where the original constant produces a\n"
        "    meaningfully different result from the replacement."
    ),
    "delete_statement": (
        "Add a test that depends on the side effect of this statement.\n"
        "    The surviving mutant removes the statement entirely.\n"
        "    Verify the observable effect: updated state, return value,\n"
        "    or accumulated result that this statement contributes to."
    ),
    "logical": (
        "Add a test where exactly one of the two conditions is True.\n"
        "    The surviving mutant swaps 'and' with 'or' (or vice versa).\n"
        "    When both are True or both False, and/or are equivalent;\n"
        "    test with mixed True/False to distinguish them."
    ),
    "swap_if_else": (
        "Add a test that verifies the if-branch produces different output\n"
        "    from the else-branch, then assert the correct one is taken.\n"
        "    The surviving mutant swaps the two branches."
    ),
    "remove_not": (
        "Add a test where the negation changes the outcome.\n"
        "    The surviving mutant removes a 'not' operator.\n"
        "    Test with a value where the condition is True, ensuring the\n"
        "    negated version (False) produces different behavior."
    ),
    "exception_swallow": (
        "Add a test that verifies the except handler's body executes.\n"
        "    The surviving mutant replaces the handler body with 'pass'.\n"
        "    Assert on any side effect of the error handling logic."
    ),
    "argument_swap": (
        "Add a test where the first two arguments are different values\n"
        "    and the function is not commutative.\n"
        "    Example: assert f(a, b) != f(b, a), then check the correct one."
    ),
    "break_continue_swap": (
        "Add a test that verifies the loop exits (break) or continues\n"
        "    at the right point. Check the number of iterations or the\n"
        "    accumulated result to distinguish break from continue."
    ),
    "unary_negate": (
        "Add a test where the sign of the value matters.\n"
        "    The surviving mutant removes a unary minus.\n"
        "    Assert the exact (negative) value, not just its magnitude."
    ),
    "extra": (
        "This mutant was provided externally (by an AI assistant or human).\n"
        "    It may represent a subtle logic error, missed edge case, or wrong\n"
        "    variable usage that rule-based operators cannot produce.\n"
        "    Read the mutant description for the specific change, then add a test\n"
        "    that exercises the affected code path with an input that distinguishes\n"
        "    the original behavior from the mutated version."
    ),
    "llm": (
        "This mutant was generated by an LLM to mimic a realistic developer bug.\n"
        "    It may represent a subtle logic error, missed edge case, or wrong\n"
        "    variable usage that rule-based operators cannot produce.\n"
        "    Read the mutant description for the specific change, then add a test\n"
        "    that exercises the affected code path with an input that distinguishes\n"
        "    the original behavior from the mutated version."
    ),
}


@dataclass
class Mutant:
    """A single code mutation."""

    operator: str
    description: str
    line: int
    col: int
    killed: bool = False
    error: str | None = None
    source_line: str = ""
    killed_by: str | None = None
    _mutant_source: str | None = field(default=None, repr=False)

    @property
    def location(self) -> str:
        """Source location as ``L<line>:<col>``."""
        return f"L{self.line}:{self.col}"

    @property
    def remediation(self) -> str:
        """Actionable guidance for killing this mutant."""
        advice = _REMEDIATION.get(self.operator, "")
        if not advice:
            return f"Add a test that distinguishes the original from: {self.description}"
        return advice


# Multiple candidate values per type — distinct values so that a != b.
# Each list is ordered: [interior, interior, interior, boundary].
# Consecutive params get different values via index offset.
_TYPE_VALUES: dict[str, list[object]] = {
    "int": [2, 3, 7, 0, -1, 100, 5],
    "float": [2.5, 0.7, 3.14, 0.0, -1.0, 0.001, 100.0],
    "str": ["hello", "world", "abc", "", "x", "a b c"],
    "bool": [True, False, True, False],
    "list": [[1.0, 2.0, 3.0], [4.0, 5.0], [7.0], [], [0.0, 0.0]],
    "dict": [{"a": 1}, {"x": 2, "y": 3}, {}, {"k": 0}],
    "bytes": [b"abc", b"xyz", b"", b"\x00\xff"],
    "None": [None],
    "NoneType": [None],
}

# Legacy single-value mapping used by generate_test_stubs (surviving mutants).
_TYPE_EXAMPLES: dict[str, str] = {
    k: repr(v[0]) if k != "str" else repr(v[0]) for k, v in _TYPE_VALUES.items()
}


def _resolve_signature(target: str) -> tuple[str, str]:
    """Resolve a function's signature into display and call forms.

    Returns ``(sig_str, call_args)`` where *sig_str* is the repr-based
    call string using the first value set, and *call_args* uses the
    first candidate per parameter.

    Falls back to ``("(...)", "...")``) when the function can't be resolved.
    """
    try:
        parts = target.rsplit(".", 1)
        if len(parts) < 2:
            return "(...)", "..."
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        sig = inspect.signature(func)
    except Exception:
        return "(...)", "..."

    sig_str = str(sig)
    kwargs = _build_kwargs(func, value_set=0)
    call_args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items()) if kwargs else "..."
    return sig_str, call_args


def _build_kwargs(func: object, value_set: int = 0) -> dict[str, object]:
    """Build a dict of example kwargs for *func*.

    *value_set* offsets into the candidate list so each call produces
    distinct inputs.  Parameter index also offsets so a != b.
    """
    sig = inspect.signature(func)  # type: ignore[arg-type]
    hints: dict[str, type] = {}
    try:
        from typing import get_type_hints

        hints = get_type_hints(func)  # type: ignore[arg-type]
    except Exception:
        pass

    kwargs: dict[str, object] = {}
    param_idx = 0
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        hint = hints.get(name)
        hint_name = getattr(hint, "__name__", str(hint)) if hint else ""
        candidates = _TYPE_VALUES.get(hint_name, [None])
        # Pick a different value for each parameter
        idx = (value_set + param_idx) % len(candidates)
        kwargs[name] = candidates[idx]
        param_idx += 1

    return kwargs


def _build_multiple_kwargs(target: str, n: int = 3) -> list[dict[str, object]]:
    """Build *n* distinct sets of kwargs for a function."""
    try:
        parts = target.rsplit(".", 1)
        if len(parts) < 2:
            return []
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
    except Exception:
        return []
    return [_build_kwargs(func, value_set=i) for i in range(n)]


@dataclass
class MutationResult:
    """Aggregated mutation testing results.

    Key attributes and methods::

        result.score              # 0.625 (kill ratio, 1.0 = all caught)
        result.survived           # list of Mutant objects — test gaps
        result.summary()          # formatted report with gaps + fix guidance
        result.generate_test_stubs()  # Python test file with real signatures

    Per-gap data (each item in ``result.survived``)::

        m.operator      # "arithmetic"
        m.description   # "+ -> -"
        m.location      # "L12:4"
        m.source_line   # "return a + b"
        m.remediation   # what test to write to close this gap

    Metadata::

        result.target           # "myapp.scoring.compute"
        result.operators_used   # ["arithmetic", ...] or None
        result.preset_used      # "standard" or None
    """

    target: str
    mutants: list[Mutant] = field(default_factory=list)
    operators_used: list[str] | None = None
    preset_used: str | None = None
    concern: str | None = None
    diagnostics: dict[str, int] = field(
        default_factory=lambda: {
            "generated": 0,
            "filtered_ast_equivalent": 0,
            "filtered_runtime_equivalent": 0,
            "filtered_module_equivalent": 0,
            "compilation_failed": 0,
            "skipped_display_method": 0,
            "tested": 0,
        }
    )

    @property
    def total(self) -> int:
        """Total number of mutants generated."""
        return len(self.mutants)

    @property
    def killed(self) -> int:
        """Number of mutants detected (killed) by the tests."""
        return sum(1 for m in self.mutants if m.killed)

    @property
    def survived(self) -> list[Mutant]:
        """Mutants that the tests failed to detect — potential test gaps."""
        return [m for m in self.mutants if not m.killed]

    def kill_attribution(self) -> dict[str, list[Mutant]]:
        """Group killed mutants by the test/property that killed them.

        Returns a dict mapping test names to the mutants they caught.
        Shows which tests carry their weight and which are redundant::

            attr = result.kill_attribution()
            for test, mutants in attr.items():
                print(f"{test}: killed {len(mutants)} mutant(s)")
        """
        groups: dict[str, list[Mutant]] = {}
        for m in self.mutants:
            if m.killed and m.killed_by:
                groups.setdefault(m.killed_by, []).append(m)
        return groups

    @property
    def score(self) -> float:
        """Kill ratio: 1.0 means every mutant was caught."""
        return self.killed / self.total if self.total > 0 else 1.0

    def filter_report(self) -> str:
        """Structured breakdown of the mutation pipeline for AI assistants.

        Shows how many mutants were generated and where they were filtered,
        so the consumer can understand *why* the result looks the way it does.

        Returns an empty string when diagnostics are not populated.
        """
        d = self.diagnostics
        generated = d.get("generated", 0)
        if generated == 0 and self.total == 0:
            return "No mutants were generated from the source code."

        lines = [f"Pipeline: {generated} mutant(s) generated"]
        for key, label in [
            ("skipped_display_method", "skipped (display method)"),
            ("filtered_ast_equivalent", "filtered (AST equivalent)"),
            ("filtered_runtime_equivalent", "filtered (runtime equivalent)"),
            ("filtered_module_equivalent", "filtered (module equivalent)"),
            ("compilation_failed", "dropped (compilation failed)"),
        ]:
            count = d.get(key, 0)
            if count > 0:
                lines.append(f"  - {count} {label}")
        if d.get("generation_timed_out"):
            lines.append("  ⚠ generation timed out — results are partial")
        lines.append(f"  → {d.get('tested', self.total)} tested")
        if self.total > 0:
            lines.append(f"  → {self.killed} killed, {len(self.survived)} survived")
        return "\n".join(lines)

    def summary(self, remediation: bool = True) -> str:
        """Report with test gaps and per-gap fix guidance.

        Each surviving mutant is a **test gap** — a real code change
        that the test suite fails to detect.  The output names each gap,
        shows the affected source line, and explains the specific fix
        (what kind of test would close the gap).

        Args:
            remediation: If True (default), include per-gap fix guidance
                explaining what test to write.
        """
        parts = [f"target: {self.target}"]
        if self.preset_used:
            parts.append(f"preset: {self.preset_used}")
        if self.operators_used:
            parts.append(f"operators: {len(self.operators_used)}/{len(OPERATORS)}")
        if self.concern:
            parts.append(f"concern: {self.concern}")
        meta = ", ".join(parts)

        # When no mutants survived filtering, explain why instead of "100%"
        if self.total == 0:
            lines = [f"Mutation score: 0/0 (no mutants to test)  [{meta}]"]
            report = self.filter_report()
            if report:
                lines.append(f"  {report}")
            d = self.diagnostics
            generated = d.get("generated", 0)
            filtered = sum(
                d.get(k, 0)
                for k in (
                    "filtered_ast_equivalent",
                    "filtered_runtime_equivalent",
                    "filtered_module_equivalent",
                )
            )
            if generated > 0 and filtered == generated:
                lines.append(
                    "  All mutants were filtered as equivalent. "
                    "Try filter_equivalent=False to inspect them."
                )
            elif generated == 0:
                lines.append(
                    "  No mutation sites found in the source. "
                    "Check that the target is correct and contains mutable code."
                )
            return "\n".join(lines)

        lines = [f"Mutation score: {self.killed}/{self.total} ({self.score:.0%})  [{meta}]"]
        if self.survived:
            lines.append(
                f"  {len(self.survived)} test gap(s) — "
                "each is a code change your tests fail to catch:"
            )
        for m in self.survived:
            header = f"  GAP {m.location} [{m.operator}] {m.description}"
            if m.source_line:
                header += f"  |  {m.source_line}"
            lines.append(header)
            if remediation:
                lines.append(f"    Cause: mutant changes {m.description} and tests still pass.")
                lines.append(f"    Fix: {m.remediation}")
        # Kill attribution — which tests carry their weight
        attr = self.kill_attribution()
        if attr:
            lines.append("")
            lines.append("  Kill attribution (which tests caught which mutations):")
            for test, mutants in sorted(attr.items(), key=lambda x: -len(x[1])):
                ops = ", ".join(sorted({m.operator for m in mutants}))
                lines.append(f"    {test}: {len(mutants)} kill(s) [{ops}]")
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(f"\n{avail}")
        return "\n".join(lines)

    def generate_test_stubs(self) -> str:
        """Generate a Python test file for surviving mutants.

        Uses ``inspect.signature`` to produce stubs with real parameter
        names and typed example values.  Each surviving mutant gets a
        test function with:

        - A docstring explaining the mutation and the remediation.
        - A call using the real function signature with example args.
        - An assertion placeholder.

        Returns an empty string when all mutants are killed.
        """
        if not self.survived:
            return ""

        parts = self.target.rsplit(".", 1)
        module_path = parts[0]
        func_name = parts[-1] if len(parts) > 1 else self.target
        safe_target = self.target.replace(".", "_")

        # Try to resolve the function signature for better stubs
        sig_str, call_args = _resolve_signature(self.target)

        lines = [
            f'"""Tests to close mutation gaps in {self.target}.',
            "",
            f"Generated by ordeal — {len(self.survived)} surviving mutant(s).",
            f"Function signature: {func_name}{sig_str}",
            '"""',
            "",
            f"from {module_path} import {func_name}",
            "",
        ]

        for i, m in enumerate(self.survived, 1):
            test_name = f"test_{safe_target}_kill_{m.operator}_{i}"
            lines.append("")
            lines.append(f"def {test_name}():")
            lines.append(f'    """{m.operator}: {m.description} at {m.location}.')
            lines.append("")
            lines.append(f"    Source: {m.source_line}")
            lines.append(f"    {m.remediation}")
            lines.append('    """')
            lines.append(f"    result = {func_name}({call_args})")
            inv = _suggest_invariant(self.target, func_name)
            if inv:
                lines.append(f"    {inv}")
            else:
                lines.append("    assert result == ...  # expected value")
            lines.append("")

        return "\n".join(lines)

    def harden(
        self,
        extra_tests: list[str],
    ) -> HardeningResult:
        """Verify that test code actually kills surviving mutants (Meta ACH pattern).

        Takes source strings of test functions written by an AI assistant
        or human, and runs the **three-assurance verification loop** for
        each:

        1. **Buildable** — the test source parses and compiles.
        2. **Valid regression** — the test passes on the original code.
        3. **Hardening** — the test fails (kills) at least one surviving mutant.

        Tests that pass all three assurances are *verified* — they provably
        close a mutation gap.  Tests that fail on the original are *invalid*.
        Tests that pass but don't kill any mutant are *ineffective*.

        Example — AI assistant writes tests, ordeal verifies::

            result = mutate("myapp.scoring.compute", preset="standard")

            hardened = result.harden([
                '''
            def test_boundary():
                from myapp.scoring import compute
                assert compute(0, 5) == 5
                ''',
                '''
            def test_negative():
                from myapp.scoring import compute
                assert compute(-1, 5) == 0
                ''',
            ])

            print(f"Verified: {len(hardened.verified)}")
            print(f"Invalid: {len(hardened.invalid)}")
            print(f"Ineffective: {len(hardened.ineffective)}")

        Args:
            extra_tests: Source strings, each containing one or more
                ``def test_*()`` functions.  The tests should import
                the target function themselves.

        Returns:
            A :class:`HardeningResult` with verified, invalid, and
            ineffective tests.
        """
        if not self.survived:
            return HardeningResult()
        if not extra_tests:
            return HardeningResult()

        # Resolve target function for PatchFault swapping
        parts = self.target.rsplit(".", 1)
        if len(parts) < 2:
            return HardeningResult()
        module_path, func_name = parts

        try:
            module = importlib.import_module(module_path)
            _unwrap_func(getattr(module, func_name))
        except Exception:
            return HardeningResult()

        # Collect surviving mutants that have source for re-compilation
        swappable: list[tuple[Mutant, Callable]] = []
        for m in self.survived:
            if not m._mutant_source:
                continue
            try:
                tree = ast.parse(m._mutant_source)
                code = compile(tree, f"<mutant:{m.description}>", "exec")
                ns = dict(module.__dict__)
                exec(code, ns)  # noqa: S102
                mf = ns.get(func_name)
                if mf is not None:
                    swappable.append((m, mf))
            except Exception:
                continue

        if not swappable:
            return HardeningResult()

        result = HardeningResult()

        for test_source in extra_tests:
            test_source = textwrap.dedent(test_source)

            # 1. Buildable — parse and compile
            try:
                test_tree = ast.parse(test_source)
                test_code = compile(test_tree, "<harden-test>", "exec")
            except Exception:
                result.invalid.append(test_source)
                continue

            # Extract test functions from the compiled source
            test_ns: dict[str, object] = {}
            try:
                exec(test_code, test_ns)  # noqa: S102
            except Exception:
                result.invalid.append(test_source)
                continue

            test_fns = [
                (name, fn)
                for name, fn in test_ns.items()
                if name.startswith("test_") and callable(fn)
            ]
            if not test_fns:
                result.invalid.append(test_source)
                continue

            source_verified = False
            for test_name, test_fn in test_fns:
                # 2. Valid regression — passes on original code
                try:
                    test_fn()
                except Exception:
                    result.invalid.append(test_source)
                    source_verified = False
                    break

                # 3. Hardening — kills at least one surviving mutant
                kills: list[Mutant] = []
                for mutant, mutant_fn in swappable:
                    fault = PatchFault(self.target, lambda orig, mf=mutant_fn: mf)
                    fault.activate()
                    try:
                        test_fn()
                        # Test passed on mutant — didn't kill it
                    except Exception:
                        kills.append(mutant)
                    finally:
                        fault.deactivate()

                if kills:
                    result.verified.append(
                        VerifiedTest(name=test_name, source=test_source, kills=kills)
                    )
                    source_verified = True

            if not source_verified and test_source not in result.invalid:
                result.ineffective.append(test_source)

        return result


# ============================================================================
# Mutation cache — resume support
# ============================================================================


def _module_source_hash(target: str) -> str:
    """Hash that captures everything that could affect mutation results.

    Combines:
    1. **Module source** — any change to the target file
    2. **Test files** — ``tests/test_<module>.py`` and ``tests/conftest.py``
    3. **Lockfile** — ``uv.lock``, ``poetry.lock``, or ``requirements.txt``

    If any of these change, the hash changes and the cache is invalidated.
    This prevents stale results when tests are improved (#1) or
    dependencies are upgraded (#2).
    """
    h = hashlib.sha256()

    # 1. Module source
    parts = target.rsplit(".", 1)
    module_name = parts[0] if len(parts) >= 2 and _is_function_target(target) else target
    source_file = None
    try:
        module = importlib.import_module(module_name)
        source_file = getattr(module, "__file__", None)
    except ImportError:
        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin:
            source_file = spec.origin
    if source_file is None:
        raise ValueError(f"Cannot locate source for {target!r}")
    with open(source_file, "rb") as f:
        h.update(f.read())

    # 2. Test files — tests/test_<module>.py, tests/conftest.py, conftest.py
    #    Search from cwd AND from the module's parent directories (covers
    #    both standard and src/ layouts).
    short_name = module_name.split(".")[-1]
    search_roots = [Path.cwd()]
    source_parent = Path(source_file).resolve().parent
    for ancestor in [source_parent, *source_parent.parents]:
        if (ancestor / "tests").is_dir() or (ancestor / "pyproject.toml").exists():
            if ancestor.resolve() not in {r.resolve() for r in search_roots}:
                search_roots.append(ancestor)
            break

    seen_test_files: set[str] = set()
    for root in search_roots:
        candidates = [
            root / "tests" / f"test_{short_name}.py",
            root / "tests" / "conftest.py",
            root / f"test_{short_name}.py",
            root / "conftest.py",
        ]
        for p in sorted(candidates):
            rp = str(p.resolve())
            if p.exists() and rp not in seen_test_files:
                seen_test_files.add(rp)
                h.update(p.read_bytes())

    # 3. Lockfile — dependency version changes
    for lockfile in ["uv.lock", "poetry.lock", "requirements.txt"]:
        p = Path(lockfile)
        if p.exists():
            h.update(p.read_bytes())
            break  # only use the first one found

    return h.hexdigest()[:16]


def _cache_path(target: str) -> Path:
    safe = target.replace(".", "_")
    return Path(".ordeal") / "mutate" / f"{safe}.json"


def _mutant_to_dict(m: Mutant) -> dict:
    return {
        "operator": m.operator,
        "description": m.description,
        "line": m.line,
        "col": m.col,
        "killed": m.killed,
        "error": m.error,
        "source_line": m.source_line,
        "killed_by": m.killed_by,
    }


def _mutant_from_dict(d: dict) -> Mutant:
    return Mutant(
        operator=d["operator"],
        description=d["description"],
        line=d.get("line", 0),
        col=d.get("col", 0),
        killed=d.get("killed", False),
        error=d.get("error"),
        source_line=d.get("source_line", ""),
        killed_by=d.get("killed_by"),
    )


def _save_cache(target: str, result: MutationResult, module_hash: str) -> None:
    """Persist a mutation result to .ordeal/mutate/<target>.json."""
    data = {
        "target": target,
        "module_source_hash": module_hash,
        "preset_used": result.preset_used,
        "operators_used": result.operators_used,
        "mutants": [_mutant_to_dict(m) for m in result.mutants],
        "diagnostics": result.diagnostics,
        "timestamp": time.time(),
    }
    p = _cache_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(p)  # atomic on POSIX


def _load_cache(
    target: str,
    module_hash: str,
    preset: str | None,
    operators: list[str] | None,
) -> MutationResult | None:
    """Load cached mutation result if valid.

    Returns ``None`` (cache miss) when:
    - No cache file exists
    - Module source hash changed (any code modification)
    - Preset or operators changed (different mutation config)
    """
    p = _cache_path(target)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None

    # Validate: same source + same config
    if data.get("module_source_hash") != module_hash:
        return None
    if data.get("preset_used") != preset:
        return None
    if data.get("operators_used") != operators:
        return None

    result = MutationResult(
        target=target,
        operators_used=data.get("operators_used"),
        preset_used=data.get("preset_used"),
    )
    result.mutants = [_mutant_from_dict(m) for m in data.get("mutants", [])]
    result.diagnostics = data.get("diagnostics", {})
    result.diagnostics["cached"] = result.total
    result.diagnostics["retested"] = 0
    return result


@dataclass
class VerifiedTest:
    """A test that provably kills one or more surviving mutants.

    Attributes:
        name: The test function name (e.g. ``"test_boundary"``).
        source: The full test source code.
        kills: Mutants this test kills — the hardening guarantee.
    """

    name: str
    source: str
    kills: list[Mutant] = field(default_factory=list)


@dataclass
class HardeningResult:
    """Result of the hardening verification loop (Meta ACH 3-assurance pattern).

    Three categories:

    - **verified** — tests that pass all 3 assurances: buildable, valid
      regression (passes on original), and hardening (kills at least one
      surviving mutant).  Each has a machine-verified kill guarantee.
    - **invalid** — tests that don't compile or fail on the original code.
    - **ineffective** — tests that pass on the original but don't kill
      any surviving mutant (they test something, just not a gap).
    """

    verified: list[VerifiedTest] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    ineffective: list[str] = field(default_factory=list)

    @property
    def total_kills(self) -> int:
        """Total number of unique mutants killed across all verified tests."""
        seen: set[tuple[str, int, str]] = set()
        for vt in self.verified:
            for m in vt.kills:
                seen.add((m.operator, m.line, m.description))
        return len(seen)

    def summary(self) -> str:
        """Human-readable summary of hardening results."""
        lines = [
            f"Hardening: {len(self.verified)} verified, "
            f"{len(self.invalid)} invalid, "
            f"{len(self.ineffective)} ineffective"
        ]
        if self.verified:
            lines.append(f"  Unique mutants killed: {self.total_kills}")
            for vt in self.verified:
                descs = ", ".join(m.description for m in vt.kills)
                lines.append(f"  {vt.name}: kills {len(vt.kills)} — {descs}")
        return "\n".join(lines)


def _suggest_invariant(target: str, func_name: str) -> str:
    """Suggest an invariant assertion based on function name and return type."""
    try:
        parts = target.rsplit(".", 1)
        if len(parts) < 2:
            return ""
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        hints = inspect.get_annotations(func)
        ret = hints.get("return")
    except Exception:
        ret = None

    name_lower = func_name.lower()

    # Name-based heuristics
    if any(kw in name_lower for kw in ("score", "rate", "ratio", "prob", "confidence")):
        return "from ordeal.invariants import bounded; bounded(0, 1)(result)"
    if any(kw in name_lower for kw in ("distance", "norm", "magnitude", "loss")):
        return "assert result >= 0"
    if any(kw in name_lower for kw in ("embed", "vector", "matrix", "weight")):
        return "from ordeal.invariants import finite; finite(result)"

    # Return-type heuristics
    ret_name = getattr(ret, "__name__", str(ret)) if ret else ""
    if "float" in ret_name:
        return "from ordeal.invariants import finite; finite(result)"
    if "ndarray" in ret_name or "array" in ret_name or "Tensor" in ret_name:
        return "from ordeal.invariants import finite; finite(result)"

    return ""


def generate_starter_tests(target: str, *, dry_run: bool = False) -> str:
    """Generate a smoke-test file for a target that has no tests yet.

    Introspects the target (function or module) via ``inspect`` and
    produces one smoke test per public callable — real imports, real
    parameter names, typed example values.  No assertions beyond
    ``assert result is not None``; the goal is a runnable file that
    gives mutation testing something to work with.

    When *dry_run* is ``True``, generates tests from signatures and type
    hints only — **no modules are imported and no functions are executed**.
    Discovery uses filesystem scanning and AST parsing.  This prevents
    all side effects when previewing what ``ordeal init --dry-run`` would
    create.

    Returns an empty string if the target cannot be resolved.
    """
    if dry_run:
        # Static check: never call _is_function_target — it imports the target.
        return _starter_static(target, is_func=False)

    is_func = _is_function_target(target)
    if is_func:
        return _starter_for_function(target)
    else:
        return _starter_for_module(target)


@dataclass
class _CallResult:
    """Result of trying to call a function with example args."""

    value: object = None
    value_repr: str = "None"
    kwargs: dict[str, object] = field(default_factory=dict)
    call_repr: str = ""
    error: str = ""
    error_type: str = ""


def _try_call_with_kwargs(target: str, kwargs: dict[str, object]) -> _CallResult:
    """Call a function with specific kwargs and return what happened."""
    try:
        parts = target.rsplit(".", 1)
        if len(parts) < 2:
            return _CallResult(error="cannot resolve", error_type="ImportError")
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        result = func(**kwargs)
        call_repr = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return _CallResult(
            value=result, value_repr=repr(result), kwargs=kwargs, call_repr=call_repr
        )
    except Exception as exc:
        call_repr = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return _CallResult(
            kwargs=kwargs,
            call_repr=call_repr,
            error=str(exc)[:80],
            error_type=type(exc).__name__,
        )


def _try_multiple_calls(target: str) -> list[_CallResult]:
    """Try multiple input sets, return all results (successes and failures)."""
    all_kwargs = _build_multiple_kwargs(target, n=3)
    if not all_kwargs:
        return []
    return [_try_call_with_kwargs(target, kw) for kw in all_kwargs]


@dataclass
class _MineFindings:
    """Everything mine() discovered about a function."""

    pinned: list[tuple[dict[str, object], object]]  # (kwargs, output) pairs
    property_lines: list[str]  # generated test lines
    prop_names: list[str]  # human-readable property names


def _run_mine(target: str, safe_name: str, func_name: str) -> _MineFindings | None:
    """Run mine() on a function. Return all findings or None."""
    try:
        parts = target.rsplit(".", 1)
        if len(parts) < 2:
            return None
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])

        from ordeal.mine import mine

        hints = inspect.get_annotations(func)
        is_void = hints.get("return") is None

        result = mine(func, max_examples=50)

        # --- Extract representative (input, output) pairs ---
        # Pick diverse examples: spread across the range of outputs
        pinned: list[tuple[dict[str, object], object]] = []
        if result.collected_inputs and result.collected_outputs:
            pairs = list(zip(result.collected_inputs, result.collected_outputs))
            pinned = _pick_diverse(pairs, n=3)

        if is_void:
            return _MineFindings(pinned=pinned, property_lines=[], prop_names=[])

        if not result.universal:
            return _MineFindings(pinned=pinned, property_lines=[], prop_names=[])

        # --- Build property assertions ---
        type_props = [p.name for p in result.universal if p.name.startswith("output type is ")]
        is_float = any("float" in t for t in type_props)
        prop_name_set = {p.name for p in result.universal}

        assertions = []
        for prop in result.universal:
            a = _property_to_assert(prop.name, is_float=is_float)
            if a:
                assertions.append(a)

        param_call = _param_call(target)
        param_names = param_call.split(", ") if param_call != "..." else []

        if "commutative" in prop_name_set and len(param_names) == 2:
            a, b = param_names
            assertions.append(f"assert {func_name}({b}, {a}) == result")
        if "involution" in prop_name_set and len(param_names) == 1:
            assertions.append(f"assert {func_name}(result) == {param_names[0]}")
        if "idempotent" in prop_name_set and len(param_names) == 1:
            assertions.append(f"assert {func_name}(result) == result")

        doc_parts = [p.name for p in result.universal[:5]]

        if not assertions:
            return _MineFindings(pinned=pinned, property_lines=[], prop_names=doc_parts)

        module_path = parts[0]
        param_sig = _param_sig(target)
        needs_math = any("math." in a for a in assertions)

        lines: list[str] = []
        lines.append("")
        lines.append(f"def test_{safe_name}_properties():")
        lines.append(f'    """Discovered: {", ".join(doc_parts)}."""')
        if needs_math:
            lines.append("    import math")
        lines.append("    from ordeal.quickcheck import quickcheck")
        lines.append(f"    from {module_path} import {func_name}")
        lines.append("")
        lines.append("    @quickcheck")
        lines.append(f"    def check({param_sig}):")
        lines.append(f"        result = {func_name}({param_call})")
        for a in assertions:
            lines.append(f"        {a}")
        lines.append("")

        return _MineFindings(pinned=pinned, property_lines=lines, prop_names=doc_parts)
    except Exception:
        return None


def _pick_diverse(
    pairs: list[tuple[dict[str, object], object]], n: int = 3
) -> list[tuple[dict[str, object], object]]:
    """Pick *n* diverse (input, output) pairs from mine's collected data.

    Prefers simple inputs with diverse outputs. Machine-discovered,
    not hand-crafted.
    """
    if len(pairs) <= n:
        return pairs

    def _complexity(kwargs: dict[str, object]) -> int:
        return sum(len(repr(v)) for v in kwargs.values())

    # Sort by simplicity first — prefer short, readable inputs
    ranked = sorted(pairs, key=lambda p: _complexity(p[0]))

    selected: list[tuple[dict[str, object], object]] = []
    seen_outputs: set[str] = set()

    for kwargs, output in ranked:
        out_repr = repr(output)
        if out_repr not in seen_outputs:
            selected.append((kwargs, output))
            seen_outputs.add(out_repr)
            if len(selected) >= n:
                break

    # Pad if needed
    if len(selected) < n:
        for kwargs, output in ranked:
            if (kwargs, output) not in selected:
                selected.append((kwargs, output))
                if len(selected) >= n:
                    break

    return selected[:n]


def _param_sig(target: str) -> str:
    """Build a parameter signature for a quickcheck function."""
    try:
        parts = target.rsplit(".", 1)
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        sig = inspect.signature(func)
        params = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            ann = p.annotation
            if ann is not inspect.Parameter.empty:
                ann_name = getattr(ann, "__name__", str(ann))
                params.append(f"{name}: {ann_name}")
            else:
                params.append(name)
        return ", ".join(params)
    except Exception:
        return "..."


def _param_call(target: str) -> str:
    """Build a call expression using parameter names."""
    try:
        parts = target.rsplit(".", 1)
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        sig = inspect.signature(func)
        names = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            names.append(name)
        return ", ".join(names)
    except Exception:
        return "..."


_PROPERTY_ASSERTIONS: dict[str, str | None] = {
    "never None": "assert result is not None",
    "output >= 0": "assert result >= 0",
    "output in [0, 1]": "assert 0 <= result <= 1",
    "no NaN": "assert not math.isnan(result)",
    "never empty": "assert len(result) > 0",
    "deterministic": None,  # covered by pinned tests
}

# Properties that need the function reference — generated as extra lines
_ALGEBRAIC_ASSERTIONS: dict[str, str] = {
    "commutative": "assert {func}({b}={args}[0], {a}={args}[1]) == result",
    "involution": "assert {func}(result) == {first_arg}",
    "idempotent": "assert {func}(result) == result",
}


def _property_to_assert(prop_name: str, *, is_float: bool = False) -> str | None:
    """Convert a mined property name to an assertion line."""
    # Exact match
    if prop_name in _PROPERTY_ASSERTIONS:
        a = _PROPERTY_ASSERTIONS[prop_name]
        # no NaN only makes sense for float
        if prop_name == "no NaN" and not is_float:
            return None
        return a
    # Type consistency: "output type is float"
    if prop_name.startswith("output type is "):
        type_name = prop_name.replace("output type is ", "")
        if type_name == "NoneType":
            return "assert result is None"
        return f"assert isinstance(result, {type_name})"
    # Length relationships: "len(output) == len(xs)"
    if prop_name.startswith("len(output) "):
        return f"assert {prop_name.replace('output', 'result')}"
    return None


def _returns_none(target: str) -> bool:
    """Check if a function's return annotation is None."""
    try:
        parts = target.rsplit(".", 1)
        if len(parts) < 2:
            return False
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        hints = inspect.get_annotations(func)
        ret = hints.get("return", _SENTINEL)
        return ret is None or ret is type(None)
    except Exception:
        return False


_SENTINEL = object()


def _needs_approx(value: object) -> bool:
    """Check if a value needs pytest.approx for comparison."""
    if isinstance(value, float):
        return True
    if isinstance(value, (list, tuple)):
        return any(isinstance(v, float) for v in value)
    return False


def _pin_assertion(call_expr: str, value: object, value_repr: str) -> str:
    """Generate the right assertion for a pinned value."""
    if _needs_approx(value):
        return f"assert {call_expr} == pytest.approx({value_repr})"
    return f"assert {call_expr} == {value_repr}"


def _starter_static(target: str, *, is_func: bool) -> str:
    """Generate test stubs via AST only — no imports, no function execution.

    Used by ``--dry-run`` to preview test structure without **any** side
    effects.  Does not call ``importlib.import_module`` — discovers
    callables and signatures by parsing source files on disk.
    """
    pkg_dir = _package_dir(target)
    if pkg_dir is None:
        return ""

    # Find the source file for the target module
    parts = target.split(".")
    root = pkg_dir
    while root.name != parts[0] and root.parent != root:
        root = root.parent
    root = root.parent  # parent of top-level package

    mod_path = root
    for part in parts:
        mod_path = mod_path / part
    source_file = None
    if mod_path.is_dir() and (mod_path / "__init__.py").exists():
        source_file = str(mod_path / "__init__.py")
    elif mod_path.with_suffix(".py").exists():
        source_file = str(mod_path.with_suffix(".py"))

    if source_file is None:
        return ""

    callables = _discover_callables_static(source_file)
    if not callables:
        return ""

    safe_mod = target.replace(".", "_")
    lines = [
        f'"""Tests for {target} — generated by ordeal init (dry-run).',
        "",
        f"{len(callables)} callable(s). Stub tests from signatures only.",
        "Run without --dry-run for full discovery with pinned values and mined properties.",
        '"""',
        "",
        f"import {target}",
        "",
    ]

    for name, sig_str in callables:
        lines.append("")
        lines.append(f"def test_{safe_mod}_{name}():")
        lines.append(f'    """{name}{sig_str}"""')
        lines.append(f"    result = {target}.{name}(...)")
        lines.append("    assert result is not None")
        lines.append("")

    return "\n".join(lines)


def _starter_for_function(target: str) -> str:
    """Generate real tests for a single function.

    Calls the function with multiple distinct inputs, pins actual return
    values, then runs mine() to discover properties and turn them into
    assertions.
    """
    parts = target.rsplit(".", 1)
    if len(parts) < 2:
        return ""
    module_path, func_name = parts
    safe_name = target.replace(".", "_")

    sig_str, _ = _resolve_signature(target)
    returns_none = _returns_none(target)

    lines = [
        f'"""Tests for {target} — generated by ordeal init.',
        "",
        "Pinned return values and discovered properties.",
        "Pinned values freeze CURRENT behavior — verify they match INTENDED behavior.",
        "If a pinned test fails, the function changed.",
        "If a value looks wrong, the code has a bug.",
        '"""',
        "",
        f"from {module_path} import {func_name}",
        "",
    ]

    # Try multiple inputs and pin each successful result
    results = _try_multiple_calls(target)
    successes = [r for r in results if not r.error]
    failures = [r for r in results if r.error]

    uses_approx = any(_needs_approx(r.value) for r in successes)
    uses_pytest = bool(failures) or uses_approx

    if uses_pytest:
        lines.append("import pytest")
        lines.append("")

    if successes:
        lines.append("")
        lines.append(f"def test_{safe_name}_pinned():")
        lines.append(f'    """{func_name}{sig_str} — pinned return values."""')
        for r in successes:
            if returns_none:
                lines.append(f"    assert {func_name}({r.call_repr}) is None")
            else:
                a = _pin_assertion(f"{func_name}({r.call_repr})", r.value, r.value_repr)
                lines.append(f"    {a}")
        lines.append("")

    if failures:
        lines.append("")
        lines.append(f"def test_{safe_name}_crashes():")
        err = failures[0]
        lines.append(f'    """Crashes on some inputs: {err.error_type}."""')
        for f in failures:
            lines.append(f"    with pytest.raises({f.error_type}):")
            lines.append(f"        {func_name}({f.call_repr})")
        lines.append("")

    # Mine properties
    mine_result = _run_mine(target, safe_name, func_name)
    if mine_result and mine_result.property_lines:
        lines.extend(mine_result.property_lines)

    lines.append("")
    return "\n".join(lines)


def _starter_for_module(target: str) -> str:
    """Generate real tests for every public callable in a module.

    Uses the full ordeal toolkit:
    1. scan_module — smoke-test with random inputs, find crashes
    2. Pinned values — call with distinct args, record results
    3. mine — discover properties (commutativity, bounds, etc.)
    4. fuzz — deep-fuzz typed functions, capture crash inputs
    5. chaos_for — generate a stateful ChaosTest class
    """
    try:
        mod = importlib.import_module(target)
    except ImportError:
        return ""

    callables: list[str] = []
    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name, None)
        if not callable(obj):
            continue
        obj_mod = getattr(obj, "__module__", None)
        if obj_mod and not obj_mod.startswith(target):
            continue
        callables.append(name)

    if not callables:
        return ""

    safe_mod = target.replace(".", "_")

    # --- Phase 1: mine() each function (the discovery engine) ---
    # mine() runs the function with random inputs and discovers everything:
    # (input, output) pairs + universal properties. No hand-crafted values.
    mine_findings: dict[str, _MineFindings | None] = {}
    for name in callables:
        dotted = f"{target}.{name}"
        mine_findings[name] = _run_mine(dotted, f"{safe_mod}_{name}", name)

    # --- Phase 2: scan_module for crash discovery ---
    scan_crashes: dict[str, str] = {}
    try:
        from ordeal.auto import scan_module

        scan = scan_module(target, max_examples=30)
        for fr in scan.functions:
            if not fr.passed and fr.error:
                scan_crashes[fr.name] = fr.error[:80]
    except Exception:
        pass

    # --- Phase 3: fallback for functions mine() couldn't handle ---
    fallback: dict[str, tuple[list[_CallResult], list[_CallResult]]] = {}
    for name in callables:
        f = mine_findings.get(name)
        if f and f.pinned:
            continue  # mine handled it
        dotted = f"{target}.{name}"
        results = _try_multiple_calls(dotted)
        successes = [r for r in results if not r.error]
        failures = [r for r in results if r.error]
        if successes or failures:
            fallback[name] = (successes, failures)

    # --- Determine imports ---
    needs_pytest = bool(scan_crashes)
    for name, f in mine_findings.items():
        if f and f.pinned:
            for _, output in f.pinned:
                if _needs_approx(output):
                    needs_pytest = True
    for _, (_, failures) in fallback.items():
        if failures:
            needs_pytest = True

    lines = [
        f'"""Tests for {target} — generated by ordeal init.',
        "",
        f"{len(callables)} callable(s). All values discovered by ordeal, not hand-written.",
        "Pinned values freeze CURRENT behavior — verify they match INTENDED behavior.",
        "If a pinned test fails, the function changed.",
        "If a value looks wrong, the code has a bug.",
        '"""',
        "",
        f"import {target}",
    ]
    if needs_pytest:
        lines.append("import pytest")
    lines.append("")

    # --- Emit tests per function ---
    for name in callables:
        dotted = f"{target}.{name}"
        sig_str, _ = _resolve_signature(dotted)
        is_void = _returns_none(dotted)
        f = mine_findings.get(name)

        # Pinned values from mine (machine-discovered inputs)
        if f and f.pinned:
            lines.append("")
            lines.append(f"def test_{safe_mod}_{name}_pinned():")
            lines.append(f'    """{name}{sig_str} — discovered by ordeal."""')
            for kwargs, output in f.pinned:
                call_repr = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
                call = f"{target}.{name}({call_repr})"
                if is_void:
                    lines.append(f"    assert {call} is None")
                else:
                    a = _pin_assertion(call, output, repr(output))
                    lines.append(f"    {a}")
            lines.append("")
        elif name in fallback:
            # Fallback for functions mine couldn't handle
            successes, failures = fallback[name]
            if successes:
                lines.append("")
                lines.append(f"def test_{safe_mod}_{name}_pinned():")
                lines.append(f'    """{name}{sig_str} — pinned values."""')
                for r in successes:
                    if is_void:
                        lines.append(f"    assert {target}.{name}({r.call_repr}) is None")
                    else:
                        call = f"{target}.{name}({r.call_repr})"
                        a = _pin_assertion(call, r.value, r.value_repr)
                        lines.append(f"    {a}")
                lines.append("")
            if failures:
                lines.append("")
                lines.append(f"def test_{safe_mod}_{name}_crashes():")
                lines.append(f'    """Crashes: {failures[0].error_type}."""')
                for fail in failures:
                    lines.append(f"    with pytest.raises({fail.error_type}):")
                    lines.append(f"        {target}.{name}({fail.call_repr})")
                lines.append("")

        # Property tests from mine
        if f and f.property_lines:
            lines.extend(f.property_lines)

    # --- Scan crash findings not already covered ---
    for name, error in scan_crashes.items():
        has_test = name in mine_findings and mine_findings[name] and mine_findings[name].pinned
        if has_test or name in fallback:
            continue
        lines.append("")
        lines.append(f"def test_{safe_mod}_{name}_crash():")
        lines.append(f'    """scan found: {name} crashes on random input: {error}."""')
        lines.append("    pass")
        lines.append("")

    # --- Emit chaos test ---
    has_typed = False
    for name in callables:
        obj = getattr(mod, name, None)
        if obj:
            try:
                ann = inspect.get_annotations(obj)
                if len(ann) > 1:  # at least one param + return
                    has_typed = True
                    break
            except Exception:
                pass

    if has_typed:
        lines.append("")
        lines.append("")
        lines.append("# --- Stateful chaos test (ordeal explores rule interleavings) ---")
        lines.append("from ordeal.auto import chaos_for")
        lines.append("")
        lines.append(f'Test{safe_mod.title().replace("_", "")}Chaos = chaos_for("{target}")')
        lines.append("")

    lines.append("")
    return "\n".join(lines)


# ============================================================================
# Project init — bootstrap tests for untested modules
# ============================================================================

_SKIP_DIRS = {
    "tests",
    "test",
    "docs",
    "doc",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".git",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    ".eggs",
    "*.egg-info",
}


def _ensure_importable(target: str) -> None:
    """Add src/ or project root to sys.path if needed for import."""
    cwd = Path.cwd()
    # src/ layout: src/myapp/__init__.py
    src = cwd / "src"
    if src.is_dir() and str(src) not in sys.path:
        top = target.split(".")[0]
        if (src / top).is_dir():
            sys.path.insert(0, str(src))
            return
    # Flat layout: myapp/__init__.py at project root
    if str(cwd) not in sys.path:
        sys.path.insert(0, str(cwd))


def _package_dir(target: str) -> Path | None:
    """Resolve a dotted package name to its directory on disk (no import).

    Searches cwd, cwd/src, and ``sys.path`` for a directory matching
    *target* with an ``__init__.py``.
    """
    top = target.split(".")[0]
    cwd = Path.cwd()
    search_roots = [cwd / "src", cwd]
    for p in sys.path:
        pp = Path(p)
        if pp.is_dir() and pp not in search_roots:
            search_roots.append(pp)

    for root in search_roots:
        candidate = root / top
        if (candidate / "__init__.py").exists():
            parts = target.split(".")
            path = root
            for part in parts:
                path = path / part
            if path.is_dir() and (path / "__init__.py").exists():
                return path
            if path.with_suffix(".py").exists():
                return path.parent
            return candidate
    return None


def _discover_modules_static(target: str) -> list[str]:
    """Find all modules under *target* by scanning the filesystem (no import).

    Walks the package directory for ``.py`` files and ``__init__.py``
    sub-packages.  Skips private modules (leading ``_``).
    """
    pkg_dir = _package_dir(target)
    if pkg_dir is None:
        return []

    top = target.split(".")[0]
    top_dir = pkg_dir
    # Walk up to the top-level package
    while top_dir.name != top and top_dir.parent != top_dir:
        top_dir = top_dir.parent

    # If target is a single .py file, not a directory
    single = pkg_dir / (target.rsplit(".", 1)[-1] + ".py") if "." in target else None
    if single and single.exists() and not pkg_dir.is_dir():
        return [target]

    modules = []
    root_parent = top_dir.parent
    for py_file in sorted(pkg_dir.rglob("*.py")):
        name = py_file.stem
        if name.startswith("_") and name != "__init__":
            continue
        # Convert path to dotted module name
        rel = py_file.relative_to(root_parent)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].removesuffix(".py")
        if not parts:
            continue
        mod_name = ".".join(parts)
        if mod_name.startswith(target) or target.startswith(mod_name):
            modules.append(mod_name)

    return modules or [target]


def _discover_callables_static(source_path: str) -> list[tuple[str, str]]:
    """Discover public callables from a source file via AST (no import).

    Returns list of ``(name, signature_str)`` pairs.
    """
    try:
        with open(source_path) as f:
            tree = ast.parse(f.read())
    except Exception:
        return []

    results = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            sig = _signature_from_ast(node)
            results.append((node.name, sig))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            results.append((node.name, ""))
    return results


def _signature_from_ast(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a signature string from an AST function definition."""
    args = node.args
    parts = []
    # Positional args (skip 'self'/'cls')
    all_args = args.posonlyargs + args.args
    defaults_offset = len(all_args) - len(args.defaults)
    for i, arg in enumerate(all_args):
        if arg.arg in ("self", "cls"):
            continue
        param = arg.arg
        if arg.annotation:
            try:
                param += f": {ast.unparse(arg.annotation)}"
            except Exception:
                pass
        if i >= defaults_offset:
            try:
                default = ast.unparse(args.defaults[i - defaults_offset])
                param += f" = {default}"
            except Exception:
                pass
        parts.append(param)
    # *args
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    # keyword-only
    kw_defaults_map = {i: d for i, d in enumerate(args.kw_defaults) if d is not None}
    for i, arg in enumerate(args.kwonlyargs):
        param = arg.arg
        if arg.annotation:
            try:
                param += f": {ast.unparse(arg.annotation)}"
            except Exception:
                pass
        if i in kw_defaults_map:
            try:
                param += f" = {ast.unparse(kw_defaults_map[i])}"
            except Exception:
                pass
        parts.append(param)
    # **kwargs
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    sig = f"({', '.join(parts)})"
    if node.returns:
        try:
            sig += f" -> {ast.unparse(node.returns)}"
        except Exception:
            pass
    return sig


def _discover_modules(target: str) -> list[str]:
    """Find all importable modules under *target* (package or single module)."""
    _ensure_importable(target)
    try:
        mod = importlib.import_module(target)
    except ImportError:
        return []

    if not hasattr(mod, "__path__"):
        return [target]

    modules = [target]
    for info in pkgutil.walk_packages(mod.__path__, prefix=target + "."):
        if info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        modules.append(info.name)
    return modules


# Common test directory names and patterns
_TEST_DIRS = ["tests", "test", "src/tests", "src/test"]


def _find_test_dirs() -> list[Path]:
    """Discover all directories that contain test files."""
    cwd = Path.cwd()
    found: list[Path] = []
    # Check well-known locations
    for name in _TEST_DIRS:
        d = cwd / name
        if d.is_dir():
            found.append(d)
    # Also check for test files at project root (rare but valid)
    if list(cwd.glob("test_*.py")):
        found.append(cwd)
    return found or [cwd / "tests"]  # default to tests/ even if missing


def _has_tests(module_name: str, test_dir: str = "tests") -> str | None:
    """Return the existing test file path if one exists, else None.

    Searches the specified *test_dir* and also auto-discovers common
    test directory layouts (``tests/``, ``test/``, ``src/tests/``, nested
    subdirectories).
    """
    short = module_name.rsplit(".", 1)[-1]

    # Build list of directories to search
    dirs_to_check: list[Path] = []
    specified = Path(test_dir)
    if specified.is_dir():
        dirs_to_check.append(specified)
    for d in _find_test_dirs():
        if d not in dirs_to_check:
            dirs_to_check.append(d)

    for d in dirs_to_check:
        # Exact match: test_{name}.py
        exact = d / f"test_{short}.py"
        if exact.exists():
            return str(exact)
        # Prefix match: test_{name}_*.py (e.g. test_mutations_presets.py)
        for match in d.glob(f"test_{short}_*.py"):
            return str(match)
        # Also search subdirectories (tests/unit/test_X.py, tests/integration/test_X.py)
        for match in d.rglob(f"test_{short}.py"):
            return str(match)
        for match in d.rglob(f"test_{short}_*.py"):
            return str(match)

    return None


def init_project(
    target: str | None = None,
    *,
    output_dir: str = "tests",
    dry_run: bool = False,
) -> list[dict[str, str]]:
    """Bootstrap test files for a Python package.

    Scans *target* for public modules, checks which ones already have
    tests, and generates starter smoke tests for the rest.

    Args:
        target: Dotted package path (e.g. ``"myapp"``).  When ``None``,
            auto-detects from the current directory.
        output_dir: Directory to write test files to (default ``"tests"``).
        dry_run: If True, no files are written and no functions are executed.
            Generates stub tests from signatures and type hints only, so
            ``--dry-run`` is safe even on packages with side effects.

    Returns:
        List of dicts with keys ``module``, ``status``, ``path``, ``content``.
        Status is one of ``"generated"``, ``"exists"``, ``"empty"``.
    """
    if target is None:
        target = _detect_package()
        if target is None:
            return []

    modules = _discover_modules_static(target) if dry_run else _discover_modules(target)
    results: list[dict[str, str]] = []
    out = Path(output_dir)

    for mod_name in modules:
        existing = _has_tests(mod_name, output_dir)
        if existing:
            results.append(
                {
                    "module": mod_name,
                    "status": "exists",
                    "path": existing,
                    "content": "",
                }
            )
            continue

        content = generate_starter_tests(mod_name, dry_run=dry_run)
        if not content:
            results.append(
                {
                    "module": mod_name,
                    "status": "empty",
                    "path": "",
                    "content": "",
                }
            )
            continue

        short = mod_name.rsplit(".", 1)[-1]
        dest = out / f"test_{short}.py"

        if not dry_run:
            out.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

        results.append(
            {
                "module": mod_name,
                "status": "generated",
                "path": str(dest),
                "content": content,
            }
        )

    # Generate ordeal.toml if it doesn't exist
    if not dry_run and not Path("ordeal.toml").exists():
        generated_mods = [r["module"] for r in results if r["status"] == "generated"]
        all_mods = [r["module"] for r in results]
        if all_mods:
            _generate_toml(target, all_mods, generated_mods, output_dir)

    return results


def _generate_toml(
    target: str,
    modules: list[str],
    generated_modules: list[str],
    test_dir: str,
) -> None:
    """Generate ordeal.toml with explorer + mutation config."""
    # Find chaos test classes in generated test files
    test_classes: list[str] = []
    for mod in generated_modules:
        short = mod.rsplit(".", 1)[-1]
        test_file = Path(test_dir) / f"test_{short}.py"
        if test_file.exists():
            content = test_file.read_text()
            for line in content.splitlines():
                if "= chaos_for(" in line:
                    cls_name = line.split("=")[0].strip()
                    test_mod = f"{test_dir}.test_{short}".replace("/", ".")
                    test_classes.append(f"{test_mod}:{cls_name}")

    top_pkg = target.split(".")[0]

    lines = [
        f"# ordeal.toml — generated by ordeal init for {target}",
        "#",
        "# Run:  ordeal explore     (coverage-guided state exploration)",
        "#       ordeal mutate      (mutation testing)",
        "#       ordeal audit <mod> (test coverage audit)",
        "",
        "[explorer]",
        f'target_modules = ["{top_pkg}"]',
        "max_time = 30",
        "seed = 42",
        "rule_swarm = true",
        "verbose = true",
        "",
    ]

    for cls in test_classes:
        lines.append("[[tests]]")
        lines.append(f'class = "{cls}"')
        lines.append("")

    # Mutation targets: all function-containing modules
    func_targets = [m for m in modules if m != target]
    if not func_targets:
        func_targets = modules
    target_strs = ", ".join(f'"{m}"' for m in func_targets[:10])

    lines.extend(
        [
            "[mutations]",
            f"targets = [{target_strs}]",
            'preset = "standard"',
            "threshold = 0.8",
            "",
            "[report]",
            'format = "text"',
            "verbose = true",
            "",
        ]
    )

    Path("ordeal.toml").write_text("\n".join(lines))


def _detect_package() -> str | None:
    """Auto-detect the top-level package from the current directory.

    Checks (in order):
    1. ``pyproject.toml`` ``[project] name`` (PEP 621)
    2. ``setup.cfg`` ``[metadata] name``
    3. ``setup.py`` ``name=`` argument
    4. Directories with ``__init__.py`` (flat layout)
    5. ``src/`` subdirectories with ``__init__.py``

    For each candidate, verifies the directory actually exists before returning.
    """
    cwd = Path.cwd()

    candidates = _candidates_from_pyproject(cwd)
    candidates.extend(_candidates_from_setup_cfg(cwd))
    candidates.extend(_candidates_from_setup_py(cwd))

    # Verify each candidate exists as a real package
    for name in candidates:
        pkg = name.replace("-", "_")
        if _verify_package(cwd, pkg):
            return pkg

    # Fall back: scan for directories with __init__.py
    for search_root in [cwd, cwd / "src"]:
        if not search_root.is_dir():
            continue
        for child in sorted(search_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            if (child / "__init__.py").exists():
                return child.name

    return None


def _candidates_from_pyproject(cwd: Path) -> list[str]:
    """Extract package name from pyproject.toml [project] section."""
    path = cwd / "pyproject.toml"
    if not path.exists():
        return []
    text = path.read_text()
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if stripped.startswith("[") and in_project:
            break  # left [project] section
        if in_project and stripped.startswith("name"):
            _, _, val = stripped.partition("=")
            val = val.strip().strip("\"'")
            if val:
                return [val]
    return []


def _candidates_from_setup_cfg(cwd: Path) -> list[str]:
    """Extract package name from setup.cfg [metadata] section."""
    path = cwd / "setup.cfg"
    if not path.exists():
        return []
    text = path.read_text()
    in_metadata = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[metadata]":
            in_metadata = True
            continue
        if stripped.startswith("[") and in_metadata:
            break
        if in_metadata and stripped.startswith("name"):
            _, _, val = stripped.partition("=")
            val = val.strip()
            if val:
                return [val]
    return []


def _candidates_from_setup_py(cwd: Path) -> list[str]:
    """Extract package name from setup.py (best-effort regex)."""
    path = cwd / "setup.py"
    if not path.exists():
        return []
    import re

    text = path.read_text()
    m = re.search(r"""name\s*=\s*["']([^"']+)["']""", text)
    return [m.group(1)] if m else []


def _verify_package(cwd: Path, name: str) -> bool:
    """Check that *name* exists as a package directory in cwd or cwd/src."""
    for root in [cwd, cwd / "src"]:
        pkg_dir = root / name
        if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
            return True
    return False


# ============================================================================
# AST mutation operators
# ============================================================================


class _Applicator(ast.NodeTransformer):
    """Apply exactly the Nth possible mutation of a specific type."""

    def __init__(self, target_idx: int):
        self.target_idx = target_idx
        self.current_idx = 0
        self.description = ""
        self.line = 0
        self.col = 0
        self.applied = False


class _ArithmeticApplicator(_Applicator):
    SWAPS: dict[type, tuple[type, str, str]] = {
        ast.Add: (ast.Sub, "+", "-"),
        ast.Sub: (ast.Add, "-", "+"),
        ast.Mult: (ast.Div, "*", "/"),
        ast.Div: (ast.Mult, "/", "*"),
        ast.Mod: (ast.Mult, "%", "*"),
    }

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        entry = self.SWAPS.get(type(node.op))
        if entry and not self.applied:
            if self.current_idx == self.target_idx:
                new_cls, old_sym, new_sym = entry
                node = copy.deepcopy(node)
                node.op = new_cls()
                self.description = f"{old_sym} -> {new_sym}"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


class _ComparisonApplicator(_Applicator):
    SWAPS: dict[type, tuple[type, str, str]] = {
        ast.Lt: (ast.LtE, "<", "<="),
        ast.LtE: (ast.Lt, "<=", "<"),
        ast.Gt: (ast.GtE, ">", ">="),
        ast.GtE: (ast.Gt, ">=", ">"),
        ast.Eq: (ast.NotEq, "==", "!="),
        ast.NotEq: (ast.Eq, "!=", "=="),
    }

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            entry = self.SWAPS.get(type(op))
            if entry and not self.applied:
                if self.current_idx == self.target_idx:
                    new_cls, old_sym, new_sym = entry
                    node = copy.deepcopy(node)
                    node.ops[i] = new_cls()
                    self.description = f"{old_sym} -> {new_sym}"
                    self.line = node.lineno
                    self.col = node.col_offset
                    self.applied = True
                self.current_idx += 1
        return node


class _NegateApplicator(_Applicator):
    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
                ast.fix_missing_locations(node)
                self.description = "negate if-condition"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        self.generic_visit(node)
        if not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
                ast.fix_missing_locations(node)
                self.description = "negate while-condition"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


class _ReturnNoneApplicator(_Applicator):
    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if node.value is not None and not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.value = ast.Constant(value=None)
                self.description = "return None"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


# -- Counters (same traversal logic, just counting) --


class _Counter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.count = 0


class _ArithmeticCounter(_Counter):
    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.generic_visit(node)
        if type(node.op) in _ArithmeticApplicator.SWAPS:
            self.count += 1


class _ComparisonCounter(_Counter):
    def visit_Compare(self, node: ast.Compare) -> None:
        self.generic_visit(node)
        for op in node.ops:
            if type(op) in _ComparisonApplicator.SWAPS:
                self.count += 1


class _NegateCounter(_Counter):
    def visit_If(self, node: ast.If) -> None:
        self.generic_visit(node)
        self.count += 1

    def visit_While(self, node: ast.While) -> None:
        self.generic_visit(node)
        self.count += 1


class _ReturnNoneCounter(_Counter):
    def visit_Return(self, node: ast.Return) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self.count += 1


class _BoundaryApplicator(_Applicator):
    """Mutate integer constants: ``n`` -> ``n + 1`` and ``n`` -> ``n - 1``.

    Both directions are needed: ``<= 10`` with n=10+1 catches upper
    bound errors, n=10-1 catches lower bound errors.
    """

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, int) and not isinstance(node.value, bool) and not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                original = node.value
                node.value = original + 1
                self.description = f"{original} -> {original + 1}"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            elif self.current_idx == self.target_idx - 1:
                # n-1 direction handled by next index
                pass
            self.current_idx += 1
            # Second mutation: n - 1
            if not self.applied:
                if self.current_idx == self.target_idx:
                    node = copy.deepcopy(node)
                    original = node.value
                    node.value = original - 1
                    self.description = f"{original} -> {original - 1}"
                    self.line = node.lineno
                    self.col = node.col_offset
                    self.applied = True
                self.current_idx += 1
        return node


class _BoundaryCounter(_Counter):
    def visit_Constant(self, node: ast.Constant) -> None:
        self.generic_visit(node)
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            self.count += 2  # both +1 and -1


class _ConstantApplicator(_Applicator):
    """Replace numeric constants with 0, 1, or -1."""

    REPLACEMENTS = [0, 1, -1]

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if not self.applied:
                if self.current_idx == self.target_idx:
                    node = copy.deepcopy(node)
                    original = node.value
                    for replacement in self.REPLACEMENTS:
                        if replacement != original:
                            node.value = replacement
                            break
                    self.description = f"{original} -> {node.value}"
                    self.line = node.lineno
                    self.col = node.col_offset
                    self.applied = True
                self.current_idx += 1
        return node


class _ConstantCounter(_Counter):
    def visit_Constant(self, node: ast.Constant) -> None:
        self.generic_visit(node)
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            self.count += 1


class _DeleteStatementApplicator(_Applicator):
    """Replace a statement with ``pass``.

    Skips docstrings (``Expr(Constant(str))``) to avoid equivalent mutants.
    """

    @staticmethod
    def _is_docstring(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )

    def _try_delete(self, node: ast.AST) -> ast.AST:
        if not self.applied:
            if self.current_idx == self.target_idx:
                pass_node = ast.Pass()
                ast.copy_location(node, pass_node)
                self.description = "delete statement"
                self.line = getattr(node, "lineno", 0)
                self.col = getattr(node, "col_offset", 0)
                self.applied = True
                return pass_node
            self.current_idx += 1
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        self.generic_visit(node)
        return self._try_delete(node)

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        self.generic_visit(node)
        if self._is_docstring(node):
            return node  # skip docstrings — always equivalent
        return self._try_delete(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        self.generic_visit(node)
        return self._try_delete(node)


class _DeleteStatementCounter(_Counter):
    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        self.count += 1

    def visit_Expr(self, node: ast.Expr) -> None:
        self.generic_visit(node)
        if not _DeleteStatementApplicator._is_docstring(node):
            self.count += 1

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.generic_visit(node)
        self.count += 1


# -- Logical operator swap: and <-> or --


class _LogicalApplicator(_Applicator):
    """Swap ``and`` with ``or`` and vice versa."""

    SWAPS: dict[type, tuple[type, str, str]] = {
        ast.And: (ast.Or, "and", "or"),
        ast.Or: (ast.And, "or", "and"),
    }

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        entry = self.SWAPS.get(type(node.op))
        if entry and not self.applied:
            if self.current_idx == self.target_idx:
                new_cls, old_sym, new_sym = entry
                node = copy.deepcopy(node)
                node.op = new_cls()
                self.description = f"{old_sym} -> {new_sym}"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


class _LogicalCounter(_Counter):
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.generic_visit(node)
        if type(node.op) in _LogicalApplicator.SWAPS:
            self.count += 1


# -- Swap if/else branches --


class _SwapIfElseApplicator(_Applicator):
    """Swap the if-body and else-body of an if statement."""

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if node.orelse and not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.body, node.orelse = node.orelse, node.body
                self.description = "swap if/else branches"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


class _SwapIfElseCounter(_Counter):
    def visit_If(self, node: ast.If) -> None:
        self.generic_visit(node)
        if node.orelse:
            self.count += 1


# -- Remove not: ``not x`` -> ``x`` --


class _RemoveNotApplicator(_Applicator):
    """Remove ``not`` from unary-not expressions."""

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and not self.applied:
            if self.current_idx == self.target_idx:
                self.description = "remove not"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
                return node.operand  # unwrap: not x -> x
            self.current_idx += 1
        return node


class _RemoveNotCounter(_Counter):
    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            self.count += 1


# -- Exception swallow: replace except body with pass --


class _ExceptionSwallowApplicator(_Applicator):
    """Replace except handler body with ``pass`` (swallow the exception)."""

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
        self.generic_visit(node)
        if not self.applied:
            # Skip handlers that already just contain pass
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                return node
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.body = [ast.Pass()]
                ast.fix_missing_locations(node)
                self.description = "swallow exception"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


class _ExceptionSwallowCounter(_Counter):
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.generic_visit(node)
        if not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
            self.count += 1


# -- Argument swap: f(a, b) -> f(b, a) --


class _ArgumentSwapApplicator(_Applicator):
    """Swap the first two arguments of a function call."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if len(node.args) >= 2 and not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.args[0], node.args[1] = node.args[1], node.args[0]
                self.description = "swap arguments"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node


class _ArgumentSwapCounter(_Counter):
    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        if len(node.args) >= 2:
            self.count += 1


# -- Break/continue swap: break <-> continue --


class _BreakContinueSwapApplicator(_Applicator):
    """Swap ``break`` ↔ ``continue`` in loops."""

    def visit_Break(self, node: ast.Break) -> ast.AST:
        if not self.applied:
            if self.current_idx == self.target_idx:
                new_node = ast.Continue()
                ast.copy_location(node, new_node)
                self.description = "break -> continue"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
                return new_node
            self.current_idx += 1
        return node

    def visit_Continue(self, node: ast.Continue) -> ast.AST:
        if not self.applied:
            if self.current_idx == self.target_idx:
                new_node = ast.Break()
                ast.copy_location(node, new_node)
                self.description = "continue -> break"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
                return new_node
            self.current_idx += 1
        return node


class _BreakContinueSwapCounter(_Counter):
    def visit_Break(self, node: ast.Break) -> None:
        self.count += 1

    def visit_Continue(self, node: ast.Continue) -> None:
        self.count += 1


# -- Unary negate: -x -> x --


class _UnaryNegateApplicator(_Applicator):
    """Remove unary minus: ``-x`` → ``x``."""

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.USub) and not self.applied:
            if self.current_idx == self.target_idx:
                self.description = "-x -> x"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
                return node.operand
            self.current_idx += 1
        return node


class _UnaryNegateCounter(_Counter):
    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.generic_visit(node)
        if isinstance(node.op, ast.USub):
            self.count += 1


def _discover_operators() -> dict[str, tuple[type[_Counter], type[_Applicator]]]:
    """Auto-discover mutation operators by scanning for _*Counter/_*Applicator pairs."""
    import re as _re

    g = globals()
    counters: dict[str, type[_Counter]] = {}
    applicators: dict[str, type[_Applicator]] = {}
    for name, obj in g.items():
        if not isinstance(obj, type) or not name.startswith("_"):
            continue
        if name.endswith("Counter") and issubclass(obj, _Counter) and obj is not _Counter:
            stem = _re.sub(r"(?<!^)(?=[A-Z])", "_", name[1:].removesuffix("Counter")).lower()
            counters[stem] = obj
        elif (
            name.endswith("Applicator") and issubclass(obj, _Applicator) and obj is not _Applicator
        ):
            stem = _re.sub(r"(?<!^)(?=[A-Z])", "_", name[1:].removesuffix("Applicator")).lower()
            applicators[stem] = obj
    return {
        stem: (counters[stem], applicators[stem])
        for stem in sorted(counters)
        if stem in applicators
    }


OPERATORS: dict[str, tuple[type[_Counter], type[_Applicator]]] = _discover_operators()


# ============================================================================
# Operator presets — named groups for different thoroughness levels
# ============================================================================


#: Valid preset names for the ``preset`` parameter.
PresetName = Literal["essential", "standard", "thorough"]

PRESETS: dict[str, list[str]] = {
    # Fast feedback — catches wrong math, wrong comparisons,
    # flipped conditions, and missing return values.
    "essential": ["arithmetic", "comparison", "negate", "return_none"],
    # Good CI default — adds off-by-one, magic numbers, and/or logic,
    # and dead code detection on top of essential.
    "standard": [
        "arithmetic",
        "comparison",
        "negate",
        "return_none",
        "boundary",
        "constant",
        "logical",
        "delete_statement",
    ],
    # Comprehensive — every operator. Use before releases.
    "thorough": list(OPERATORS.keys()),
}


def _resolve_operators(
    operators: list[str] | None = None,
    preset: PresetName | None = None,
) -> list[str] | None:
    """Resolve operators from an explicit list or a preset name.

    - Both ``None``: returns ``None`` (caller uses all operators).
    - Only *preset*: returns ``PRESETS[preset]``.
    - Only *operators*: returns *operators* as-is.
    - Both specified: raises ``ValueError``.
    """
    if operators is not None and preset is not None:
        raise ValueError("Cannot specify both 'operators' and 'preset'. Use one or the other.")
    if preset is not None:
        if preset not in PRESETS:
            raise ValueError(f"Unknown preset: {preset!r}. Available: {list(PRESETS.keys())}")
        return PRESETS[preset]
    return operators


# ============================================================================
# Catalog — introspect mutation operators at runtime
# ============================================================================


def catalog() -> list[dict[str, str]]:
    """Discover all mutation operators and presets via runtime introspection.

    Returns a list of dicts with ``name``, ``type`` (``"operator"`` or
    ``"preset"``), ``doc`` (remediation guidance for operators, operator
    list for presets), and ``operators`` (list of operator names for presets).
    Derived from :data:`OPERATORS`, :data:`PRESETS`, and :data:`_REMEDIATION`
    — new operators appear automatically.
    """
    entries: list[dict[str, str]] = []
    for name in sorted(OPERATORS):
        entries.append(
            {
                "name": name,
                "type": "operator",
                "doc": (_REMEDIATION.get(name, "") or "").split("\n")[0],
            }
        )
    for name, ops in PRESETS.items():
        entries.append(
            {
                "name": name,
                "type": "preset",
                "doc": f"{len(ops)} operators: {', '.join(ops)}",
                "operators": ops,
            }
        )
    return entries


# ============================================================================
# Mutant generation
# ============================================================================


_SKIP_METHODS = frozenset(
    {
        "__repr__",
        "__str__",
        "__format__",
        "__hash__",
        "__sizeof__",
        "__reduce__",
        "__reduce_ex__",
    }
)


# ============================================================================
# Equivalence filtering — skip mutants that are semantically identical
# ============================================================================

# Algebraic identities: operator + constant operand = no-op.
# e.g. x + 0 == x, x * 1 == x, x ** 1 == x, x // 1 == x, x - 0 == x.
# When the arithmetic mutator swaps + to - (or * to /) and the other
# operand is a neutral element, the mutation is equivalent.
_IDENTITY_OPS: dict[type, set[int | float]] = {
    ast.Add: {0, 0.0},  # x + 0 == x
    ast.Sub: {0, 0.0},  # x - 0 == x
    ast.Mult: {1, 1.0},  # x * 1 == x
    ast.Div: {1, 1.0},  # x / 1 == x
    ast.FloorDiv: {1, 1.0},  # x // 1 == x
    ast.Pow: {1, 1.0},  # x ** 1 == x
}


def _is_algebraic_identity(node: ast.BinOp) -> bool:
    """Check if a BinOp is an algebraic identity (result == one operand).

    Detects patterns like ``x + 0``, ``x * 1``, ``x ** 1`` where the
    operation has no effect.  Mutations of these nodes produce equivalent
    mutants (e.g. ``x + 0`` -> ``x - 0`` — both equal ``x``).
    """
    neutrals = _IDENTITY_OPS.get(type(node.op))
    if neutrals is None:
        return False
    # Check right operand (most common: x + 0, x * 1)
    if isinstance(node.right, ast.Constant) and node.right.value in neutrals:
        return True
    # Check left operand for commutative ops (0 + x, 1 * x)
    if type(node.op) in (ast.Add, ast.Mult) and isinstance(node.left, ast.Constant):
        if node.left.value in neutrals:
            return True
    return False


def _bytecode_equal(a: types.CodeType, b: types.CodeType) -> bool:
    """Recursively compare bytecode of two code objects.

    The top-level module code just defines functions, so we must also
    compare the bytecode of nested code objects (the actual functions).
    """
    if a.co_code != b.co_code:
        return False
    # Compare nested code objects (functions, classes, comprehensions)
    a_inner = [c for c in a.co_consts if isinstance(c, types.CodeType)]
    b_inner = [c for c in b.co_consts if isinstance(c, types.CodeType)]
    if len(a_inner) != len(b_inner):
        return False
    return all(_bytecode_equal(ai, bi) for ai, bi in zip(a_inner, b_inner))


def _is_equivalent_mutant(
    original_tree: ast.Module,
    mutated_tree: ast.Module,
    operator: str,
    description: str,
    line: int,
) -> bool:
    """Detect mutants that are semantically equivalent to the original.

    Checks:
    1. Algebraic identities in the *mutated* tree (the result of the
       mutation is a no-op expression like ``x + 0``).
    2. Algebraic identities in the *original* tree at the mutation site
       (mutating a no-op is still a no-op for the swapped neutral).
    3. Duplicate AST output (original and mutated compile to identical code).
    """
    # 1+2: Check for algebraic identities at the mutation site
    if operator == "arithmetic":
        for tree in (original_tree, mutated_tree):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.BinOp)
                    and hasattr(node, "lineno")
                    and node.lineno == line
                    and _is_algebraic_identity(node)
                ):
                    return True

    # 3: AST-level deduplication — compile both and compare bytecode
    try:
        orig_code = compile(original_tree, "<orig>", "exec")
        mut_code = compile(mutated_tree, "<mut>", "exec")
        if _bytecode_equal(orig_code, mut_code):
            return True
    except Exception:
        pass

    return False


def _is_inside_skip_method(tree: ast.Module, line: int) -> bool:
    """Check if a mutation line falls inside a method we should skip."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _SKIP_METHODS:
                if node.lineno <= line <= node.end_lineno:
                    return True
    return False


def generate_mutants(
    source: str,
    operators: list[str] | None = None,
    *,
    extra_mutants: list[str | tuple[str, str]] | None = None,
    llm: Callable[[str], str] | None = None,
    concern: str | None = None,
    _stats: dict[str, int] | None = None,
    timeout: float | None = None,
) -> list[tuple[Mutant, ast.Module]]:
    """Generate mutants from source code, filtering out noise.

    Skips mutations inside ``__repr__``, ``__str__``, and other display
    methods (they produce equivalent mutants that always survive).

    When *extra_mutants* is provided, the given source strings are
    validated (parse, compile, dedup) and appended after rule-based ones.
    This is the primary interface for AI assistants and humans to supply
    mutants they wrote directly — no API call needed::

        result = generate_mutants(source, extra_mutants=[
            "def compute(a, b):\\n    if a <= 0: return 0\\n    return a + b",
        ])

    When *llm* is provided, it is called to generate additional mutant
    source strings automatically.  This is a convenience for automated
    pipelines — under the hood it feeds the results through the same
    validation as *extra_mutants*.

    Args:
        source: Python source code to mutate.
        operators: Operator names to use (default: all rule-based operators).
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            to validate and add alongside rule-based mutants.  Written by
            an AI assistant, a human, or any other author.
        llm: Optional callable ``(prompt: str) -> str`` for automated
            mutant generation.  ordeal crafts the prompt; the user
            provides any LLM backend.
        _stats: Optional mutable dict for diagnostic counters.  When
            provided, keys ``generated``, ``filtered_ast_equivalent``,
            and ``skipped_display_method`` are incremented in-place.
        timeout: Maximum seconds for mutant generation.  When exceeded,
            returns whatever mutants have been generated so far instead
            of hanging on complex AST expressions (numpy, cv2, etc.).

    Returns a list of ``(Mutant, mutated_AST)`` pairs.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None
    tree = ast.parse(source)
    source_lines = source.splitlines()
    ops = operators or list(OPERATORS.keys())
    results: list[tuple[Mutant, ast.Module]] = []
    st = _stats  # alias for brevity

    timed_out = False
    for op_name in ops:
        if timed_out:
            break
        if op_name not in OPERATORS:
            raise ValueError(f"Unknown operator: {op_name!r}. Available: {list(OPERATORS)}")
        counter_cls, applicator_cls = OPERATORS[op_name]

        counter = counter_cls()
        counter.visit(tree)

        for i in range(counter.count):
            # Check deadline before each mutant (catches hangs on complex ASTs)
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                if st is not None:
                    st["generation_timed_out"] = 1
                break

            mutated_tree = ast.parse(source)
            applicator = applicator_cls(target_idx=i)
            applicator.visit(mutated_tree)
            ast.fix_missing_locations(mutated_tree)

            if applicator.applied:
                if st is not None:
                    st["generated"] = st.get("generated", 0) + 1
                # Skip mutations in display/repr methods
                if _is_inside_skip_method(tree, applicator.line):
                    if st is not None:
                        st["skipped_display_method"] = st.get("skipped_display_method", 0) + 1
                    continue
                # Skip semantically equivalent mutants
                if _is_equivalent_mutant(
                    tree, mutated_tree, op_name, applicator.description, applicator.line
                ):
                    if st is not None:
                        st["filtered_ast_equivalent"] = st.get("filtered_ast_equivalent", 0) + 1
                    continue
                # Capture the source line for context
                src_line = ""
                if 0 < applicator.line <= len(source_lines):
                    src_line = source_lines[applicator.line - 1].strip()
                try:
                    msrc = ast.unparse(mutated_tree)
                except Exception:
                    msrc = None
                mutant = Mutant(
                    operator=op_name,
                    description=applicator.description,
                    line=applicator.line,
                    col=applicator.col,
                    source_line=src_line,
                    _mutant_source=msrc,
                )
                results.append((mutant, mutated_tree))

    # --- Extra mutants: from AI assistant, human, or LLM ---
    additional: list[tuple[Mutant, ast.Module]] = []
    if extra_mutants is not None:
        additional.extend(_validate_extra_mutants(source, extra_mutants))
    if llm is not None:
        additional.extend(_generate_llm_mutants(source, llm, concern=concern))

    if additional:
        _dedup_into(results, additional)

    return results


def _dedup_into(
    existing: list[tuple[Mutant, ast.Module]],
    new: list[tuple[Mutant, ast.Module]],
) -> None:
    """Append *new* mutants to *existing*, skipping bytecode duplicates."""
    existing_codes: list[bytes] = []
    for _, etree in existing:
        try:
            code = compile(etree, "<existing>", "exec")
            inner = [c for c in code.co_consts if isinstance(c, types.CodeType)]
            existing_codes.append(inner[0].co_code if inner else code.co_code)
        except Exception:
            pass
    for mutant, mtree in new:
        try:
            mcode = compile(mtree, "<new>", "exec")
            inner = [c for c in mcode.co_consts if isinstance(c, types.CodeType)]
            key = inner[0].co_code if inner else mcode.co_code
            if key not in existing_codes:
                existing.append((mutant, mtree))
                existing_codes.append(key)
        except Exception:
            pass


# ============================================================================
# Extra mutants — accept source code from any author (AI assistant, human, LLM)
# ============================================================================


def _strip_python_comments(source: str) -> str:
    """Remove comments and normalize whitespace for diff comparison.

    Strips ``# ...`` comments, blank lines, and trailing whitespace so that
    comment-only mutations (a known failure mode — 61% of Meta ACH's
    false equivalents were comment-only) are detected as identical.
    """
    import re

    lines = []
    for line in source.splitlines():
        # Remove inline comments (but not inside strings — good enough heuristic)
        stripped = re.sub(r"#[^\"']*$", "", line).rstrip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


def _validate_extra_mutants(
    source: str,
    extra_mutants: list[str | tuple[str, str]],
) -> list[tuple[Mutant, ast.Module]]:
    """Validate externally-provided mutant source code.

    Accepts raw source strings — written by an AI assistant, a human, or
    any other author — and runs the full validation pipeline:

    1. ``ast.parse()`` — reject syntax errors
    2. ``compile()`` — reject semantic errors
    3. Comment-strip diff — reject comment-only changes
    4. ``_bytecode_equal()`` — reject identical-to-original
    5. Deduplicate against each other

    Each item in *extra_mutants* is either a source string or a
    ``(description, source)`` tuple.

    Returns ``(Mutant, ast.Module)`` pairs with ``operator="extra"``.
    """
    # Compile the original for comparison
    try:
        original_tree = ast.parse(source)
        original_code = compile(original_tree, "<orig>", "exec")
    except Exception:
        return []

    original_stripped = _strip_python_comments(source)

    results: list[tuple[Mutant, ast.Module]] = []
    seen_bytecodes: list[bytes] = []

    for item in extra_mutants:
        if isinstance(item, tuple):
            desc, mutant_source = item
        else:
            desc = "extra mutant"
            mutant_source = item

        # 1. Parse
        try:
            mutant_tree = ast.parse(mutant_source)
        except SyntaxError:
            continue

        # 2. Compile
        try:
            mutant_code = compile(mutant_tree, "<extra-mutant>", "exec")
        except Exception:
            continue

        # 3. Comment-strip diff — reject comment-only changes
        mutant_stripped = _strip_python_comments(mutant_source)
        if mutant_stripped == original_stripped:
            continue

        # 4. Bytecode dedup — reject identical to original
        if _bytecode_equal(original_code, mutant_code):
            continue

        # 5. Deduplicate against other extra mutants
        inner = [c for c in mutant_code.co_consts if isinstance(c, types.CodeType)]
        bytecode_key = inner[0].co_code if inner else mutant_code.co_code
        if bytecode_key in seen_bytecodes:
            continue
        seen_bytecodes.append(bytecode_key)

        # Find the mutation line by diffing source lines
        orig_lines = source.splitlines()
        mut_lines = mutant_source.splitlines()
        line = 1
        col = 0
        for i, (ol, ml) in enumerate(zip(orig_lines, mut_lines)):
            if ol != ml:
                line = i + 1
                for j, (oc, mc) in enumerate(zip(ol, ml)):
                    if oc != mc:
                        col = j
                        break
                break

        src_line = mut_lines[line - 1].strip() if line <= len(mut_lines) else ""

        mutant = Mutant(
            operator="extra",
            description=desc,
            line=line,
            col=col,
            source_line=src_line,
            _mutant_source=mutant_source,
        )
        results.append((mutant, mutant_tree))

    return results


# ============================================================================
# LLM-automated mutant generation (convenience wrapper over extra_mutants)
# ============================================================================

#: Prompt template for LLM mutant generation.  Follows Meta ACH pattern:
#: show the source, ask for realistic bugs, forbid trivial/comment-only changes.
_LLM_MUTANT_PROMPT = """\
Given this Python function:

```python
{source}
```
{concern_block}
Generate {n} mutated versions that each introduce a single, subtle, realistic bug — \
the kind a developer might introduce that passes code review but causes failures in production.

Types of bugs to introduce (vary across mutants):
- Off-by-one errors (< vs <=, wrong range bound)
- Wrong variable used (similar names swapped)
- Missing edge case handling (None, empty, zero, negative)
- Incorrect operator (+/-, and/or, ==/is)
- Swapped arguments in a function call
- Wrong return value on one code path
- Missing or extra negation
- Incorrect default value
- Type coercion error (int vs float, str vs bytes)

Rules:
- Each mutant must be the COMPLETE function (same signature, same name)
- Change only 1-2 lines per mutant — subtle, not obvious
- Do NOT add or change comments
- Do NOT change the function signature
- Do NOT make trivially detectable changes (like always returning None)
- Do NOT import new modules

Output format — use exactly this delimiter between mutants:

---MUTANT---
Description: <one line: what changed and why it is a realistic bug>
```python
<the full mutated function>
```
"""

_LLM_EQUIVALENCE_PROMPT = """\
Are these two Python functions semantically equivalent? \
That is, do they produce the same output for ALL possible inputs?

Original:
```python
{original}
```

Mutant:
```python
{mutant}
```

Answer with exactly YES or NO on the first line, then a one-sentence reason.
"""


def _parse_llm_response(response: str) -> list[tuple[str, str]]:
    """Parse LLM response into ``(description, source_code)`` pairs.

    Expects ``---MUTANT---`` delimiters separating blocks, each with a
    ``Description:`` line and a fenced Python code block.
    """
    import re

    blocks = re.split(r"---\s*MUTANT\s*---", response)
    results: list[tuple[str, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        desc_match = re.search(r"Description:\s*(.+?)(?:\n|$)", block)
        desc = desc_match.group(1).strip() if desc_match else "LLM-generated mutant"
        code_match = re.search(r"```(?:python)?\s*\n(.*?)```", block, re.DOTALL)
        if code_match:
            results.append((desc, code_match.group(1).strip()))
    return results


def _generate_llm_mutants(
    source: str,
    llm: Callable[[str], str],
    *,
    n: int = 5,
    concern: str | None = None,
) -> list[tuple[Mutant, ast.Module]]:
    """Generate mutants via an LLM, with full validation pipeline.

    Convenience wrapper: calls *llm* to produce source strings, then
    feeds them through :func:`_validate_extra_mutants`.  The LLM is just
    one way to produce extra mutants — an AI assistant or human can also
    pass source strings directly via ``extra_mutants``.

    Args:
        source: The original function source code.
        llm: Callable that takes a prompt string and returns a response string.
        n: Number of mutants to request from the LLM.  Default ``5``.
        concern: Optional free-text concern description (Meta ACH pattern).
            When provided, the LLM is asked to generate mutations that
            target this specific concern (e.g. "privacy: user data should
            not leak into error messages").
    """
    concern_block = ""
    if concern:
        concern_block = (
            f"\nCONCERN: {concern}\n"
            "Focus mutations on bugs that would manifest this concern. "
            "The mutations should be realistic instances of this class of fault.\n"
        )
    prompt = _LLM_MUTANT_PROMPT.format(source=source, n=n, concern_block=concern_block)
    try:
        response = llm(prompt)
    except Exception:
        return []  # LLM failure is non-fatal

    parsed = _parse_llm_response(response)
    if not parsed:
        return []

    # Feed through the same validation as extra_mutants
    validated = _validate_extra_mutants(source, parsed)
    # Relabel operator as "llm" to distinguish from hand-written extras
    for mutant, _ in validated:
        mutant.operator = "llm"
    return validated


def _is_llm_equivalent(
    original_source: str,
    mutant_source: str,
    llm: Callable[[str], str],
) -> bool:
    """Ask an LLM whether a mutant is semantically equivalent to the original.

    Used as an additional equivalence filter for surviving mutants.  The LLM
    sees both versions and answers YES (equivalent, skip) or NO (genuine
    gap, keep).  Falls back to ``False`` (keep the mutant) on any error.

    Evidence: Meta ACH achieved 0.95 precision / 0.96 recall with this
    approach after trivial preprocessing (ISSTA 2024).
    """
    prompt = _LLM_EQUIVALENCE_PROMPT.format(original=original_source, mutant=mutant_source)
    try:
        response = llm(prompt)
    except Exception:
        return False  # on error, assume not equivalent (safe default)

    first_line = response.strip().split("\n")[0].strip().upper()
    return first_line.startswith("YES")


# ============================================================================
# Module-level mutation testing
# ============================================================================


@contextmanager
def _mutated_module(module_name: str, mutated_tree: ast.Module):
    """Temporarily replace a module's contents with mutated code.

    Patches the original module object in-place so that both
    ``import mod; mod.func(...)`` and ``from mod import func``
    (captured before the swap) see the mutated definitions.

    .. note::

       This is **process-local** — subprocesses (Ray workers,
       ``multiprocessing`` spawn) reimport from disk and will not see
       the mutation.  Use :func:`_mutated_module_on_disk` when the
       test suite may spawn child processes.
    """
    original = sys.modules.get(module_name)
    if original is None:
        raise ImportError(f"Module {module_name!r} not in sys.modules")

    # Save original dict contents
    saved = dict(original.__dict__)

    # Compile and exec mutated code into a temp namespace
    code = compile(mutated_tree, getattr(original, "__file__", "<mutated>"), "exec")
    mutated_ns: dict[str, object] = {}
    exec(code, mutated_ns)  # noqa: S102

    # Patch original module in-place: replace only the names that the
    # mutated code defines (preserves __name__, __file__, etc.)
    for name, value in mutated_ns.items():
        if not name.startswith("__"):
            setattr(original, name, value)

    try:
        yield original
    finally:
        # Restore original contents
        # Remove names that the mutation added
        for name in mutated_ns:
            if not name.startswith("__") and name not in saved:
                try:
                    delattr(original, name)
                except AttributeError:
                    pass
        # Restore original values
        for name, value in saved.items():
            if not name.startswith("__"):
                setattr(original, name, value)


_CROSS_PROCESS_IMPORTS = frozenset(
    {
        "ray",
        "multiprocessing",
        "subprocess",
        "concurrent",
        "celery",
        "dask",
        "joblib",
    }
)

_CROSS_PROCESS_CALLS = frozenset(
    {
        "ray.remote",
        "ray.get",
        "ray.put",
        "multiprocessing.Pool",
        "multiprocessing.Process",
        "concurrent.futures.ProcessPoolExecutor",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
    }
)


def _has_cross_process_imports(source: str) -> bool:
    """Check whether source code contains cross-process imports or decorators."""
    try:
        tree = ast.parse(source)
    except Exception:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _CROSS_PROCESS_IMPORTS:
                    return True
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _CROSS_PROCESS_IMPORTS:
                return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                dec_name = _decorator_name(dec)
                if dec_name and any(dec_name.startswith(p) for p in _CROSS_PROCESS_IMPORTS):
                    return True
    return False


def _read_module_source(module_name: str) -> str | None:
    """Read source for a module — works even when the module can't be imported."""
    source_file = None
    try:
        module = importlib.import_module(module_name)
        source_file = getattr(module, "__file__", None)
    except Exception:
        try:
            spec = importlib.util.find_spec(module_name)
            if spec and spec.origin:
                source_file = spec.origin
        except Exception:
            pass
    if source_file is None:
        return None
    try:
        with open(source_file) as f:
            return f.read()
    except Exception:
        return None


def _needs_disk_mutation(target: str) -> bool:
    """Auto-detect whether *target* or its tests use cross-process patterns.

    Scans both the target module's AST and the corresponding test files
    for imports of Ray, multiprocessing, subprocess, etc.  The target
    module itself may be pure Python while the *tests* call it through
    Ray ``.remote()`` — scanning only the target would miss this.
    """
    parts = target.rsplit(".", 1)
    module_name = parts[0] if len(parts) >= 2 else target

    # Scan the target module
    source = _read_module_source(module_name)
    if source and _has_cross_process_imports(source):
        return True

    # Scan likely test files (tests/test_<module>.py, tests/conftest.py)
    short_name = module_name.split(".")[-1]
    test_candidates = [
        f"tests/test_{short_name}.py",
        "tests/conftest.py",
        f"test_{short_name}.py",
    ]
    for test_path in test_candidates:
        try:
            with open(test_path) as f:
                test_source = f.read()
            if _has_cross_process_imports(test_source):
                return True
        except FileNotFoundError:
            continue
        except Exception:
            continue

    return False


def _decorator_name(node: ast.expr) -> str | None:
    """Extract dotted name from a decorator AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _resolve_disk_mutation(disk_mutation: bool | None, target: str) -> bool:
    """Resolve disk_mutation: None means auto-detect."""
    if disk_mutation is not None:
        return disk_mutation
    needed = _needs_disk_mutation(target)
    if needed:
        import warnings

        warnings.warn(
            f"Auto-enabling disk_mutation for {target!r} — "
            "cross-process imports detected (Ray/multiprocessing/subprocess). "
            "Mutations will be written to disk so subprocesses see them. "
            "Suppress with disk_mutation=False.",
            stacklevel=3,
        )
    return needed


def _clear_pyc(source_path: str) -> None:
    """Remove ``__pycache__`` bytecode for *source_path*.

    Python caches compiled bytecode in ``__pycache__/<stem>.cpython-*.pyc``.
    After rewriting a ``.py`` file the stale ``.pyc`` must be removed,
    otherwise the interpreter may load the old bytecode instead of
    re-compiling from the modified source.
    """
    from pathlib import Path

    source = Path(source_path)
    cache_dir = source.parent / "__pycache__"
    if cache_dir.exists():
        stem = source.stem
        for pyc in cache_dir.glob(f"{stem}.*.pyc"):
            pyc.unlink(missing_ok=True)


@contextmanager
def _mutated_source_file(source_path: str, mutated_source: str):
    """Write mutated source to disk, clear bytecode, restore on exit.

    Subprocesses (Ray workers, ``multiprocessing`` spawn, ``subprocess``)
    reimport from disk, so in-memory patching alone is invisible to them.
    This writes the mutation to the actual ``.py`` file so **any** process
    that imports the module picks up the mutated code.
    """
    with open(source_path) as f:
        original_source = f.read()

    _clear_pyc(source_path)
    with open(source_path, "w") as f:
        f.write(mutated_source)

    try:
        yield
    finally:
        with open(source_path, "w") as f:
            f.write(original_source)
        _clear_pyc(source_path)


@contextmanager
def _mutated_module_on_disk(module_name: str, mutated_tree: ast.Module):
    """Mutate a module both in-memory and on disk.

    Combines :func:`_mutated_module` (for in-process visibility) with
    :func:`_mutated_source_file` (for subprocess visibility).  This is
    the correct strategy when the test suite may spawn child processes
    — e.g. Ray workers, ``multiprocessing`` pools, or subprocess calls.
    """
    original = sys.modules.get(module_name)
    if original is None:
        raise ImportError(f"Module {module_name!r} not in sys.modules")

    source_path = getattr(original, "__file__", None)
    if source_path is None:
        raise ValueError(f"Cannot locate source file for {module_name!r}")

    try:
        mutated_source = ast.unparse(mutated_tree)
    except Exception:
        # Fallback: in-memory only if unparse fails (rare)
        with _mutated_module(module_name, mutated_tree) as mod:
            yield mod
        return

    with (
        _mutated_source_file(source_path, mutated_source),
        _mutated_module(module_name, mutated_tree) as mod,
    ):
        importlib.invalidate_caches()
        yield mod


@contextmanager
def _function_mutated_on_disk(
    module_path: str,
    func_name: str,
    mutated_func_tree: ast.Module,
):
    """Rewrite a single function on disk inside its module.

    Reads the full module source, replaces the target function's AST
    node with the mutated version, writes to disk, and restores on exit.
    Used alongside :class:`PatchFault` for full cross-process coverage.
    """
    module = importlib.import_module(module_path)
    source_path = getattr(module, "__file__", None)
    if source_path is None:
        yield  # nothing to do — no source file
        return

    with open(source_path) as f:
        module_source = f.read()

    # Extract the mutated FunctionDef from the single-function AST
    mutated_func_node = None
    for node in ast.walk(mutated_func_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mutated_func_node = node
            break

    if mutated_func_node is None:
        yield  # no function found — skip disk mutation
        return

    # Parse full module, replace the target function
    module_tree = ast.parse(module_source)
    replaced = False
    for i, node in enumerate(module_tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            # Preserve the original line number for ast.fix_missing_locations
            mutated_func_node.lineno = node.lineno
            mutated_func_node.col_offset = node.col_offset
            module_tree.body[i] = mutated_func_node
            replaced = True
            break
        # Also check inside class definitions
        if isinstance(node, ast.ClassDef):
            for j, method in enumerate(node.body):
                if (
                    isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and method.name == func_name
                ):
                    mutated_func_node.lineno = method.lineno
                    mutated_func_node.col_offset = method.col_offset
                    node.body[j] = mutated_func_node
                    replaced = True
                    break
            if replaced:
                break

    if not replaced:
        yield  # function not found in module — skip disk mutation
        return

    ast.fix_missing_locations(module_tree)
    try:
        mutated_module_source = ast.unparse(module_tree)
    except Exception:
        yield  # unparse failed — skip disk mutation
        return

    with _mutated_source_file(source_path, mutated_module_source):
        importlib.invalidate_caches()
        yield


def _module_is_equivalent(
    original: types.ModuleType,
    mutated_tree: ast.Module,
    n_samples: int = 10,
) -> bool:
    """Heuristic: compare all public callables in original vs mutated module.

    Returns ``True`` (skip) when every callable produces identical outputs
    on random inputs.  Falls back to ``False`` (test it) on any error.
    """
    try:
        mutated = types.ModuleType(original.__name__)
        mutated.__file__ = getattr(original, "__file__", "<mutated>")
        code = compile(mutated_tree, mutated.__file__, "exec")
        exec(code, mutated.__dict__)  # noqa: S102
    except Exception:
        return False

    for name in sorted(dir(original)):
        if name.startswith("_"):
            continue
        orig_fn = getattr(original, name, None)
        mut_fn = getattr(mutated, name, None)
        if not callable(orig_fn) or inspect.isclass(orig_fn):
            continue
        if mut_fn is None:
            return False
        if not _is_runtime_equivalent(orig_fn, mut_fn, n_samples):
            return False
    return True


def _batch_module_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    *,
    disk_mutation: bool = False,
) -> list[tuple[Mutant, bool, str | None]]:
    """Run all mutants in a single pytest session via a custom plugin.

    Instead of starting a new pytest session per mutant, collects tests
    once and replays them for each mutant — cutting out repeated startup.
    """
    import pytest

    parts = target.rsplit(".", 1)
    module_name = parts[0] if len(parts) >= 2 else target
    short_name = module_name.split(".")[-1]

    results: list[tuple[Mutant, bool, str | None]] = []

    class _BatchPlugin:
        """Pytest plugin that tests multiple mutants in one session."""

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session: pytest.Session) -> bool:
            """Override the default test loop to iterate mutants."""
            if not session.config.option.collectonly:
                if not session.items:
                    short = target.rsplit(".", 1)[-1]
                    suggested = f"tests/test_{short}.py"
                    raise NoTestsFoundError(
                        f"No tests found for {target!r}. "
                        "Mutation score is meaningless without tests.\n"
                        f"  Generate: generate_starter_tests({target!r})\n"
                        f"  CLI:      ordeal init {target}\n"
                        f"  Save to:  {suggested}",
                        target=target,
                        suggested_file=suggested,
                    )
                for mutant, mutated_tree in mutant_pairs:
                    killed = False
                    error = None
                    killer = None
                    try:
                        cm = (
                            _mutated_module_on_disk(target, mutated_tree)
                            if disk_mutation
                            else _mutated_module(target, mutated_tree)
                        )
                        with cm:
                            if not disk_mutation:
                                importlib.invalidate_caches()
                            for i, item in enumerate(session.items):
                                nxt = session.items[i + 1] if i + 1 < len(session.items) else None
                                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nxt)
                                if item.session.testsfailed:
                                    killed = True
                                    killer = item.nodeid
                                    error = f"{item.nodeid} failed"
                                    break
                    except Exception as e:
                        killed = True
                        error = str(e)[:200]
                    # Reset failures for next mutant
                    session.testsfailed = 0
                    results.append((mutant, killed, error, killer))
            return True  # prevent default loop from running

    plugin = _BatchPlugin()
    pytest.main(
        ["-x", "-q", "--tb=no", "--no-header", "--chaos", "-o", "addopts=", "-k", short_name],
        plugins=[plugin],
    )
    return results


def _parallel_module_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    workers: int,
) -> list[tuple[Mutant, bool, str | None]]:
    """Run module-level mutants in parallel, each worker batch-testing a chunk.

    Divides *mutant_pairs* across *workers* processes.  Each worker runs
    a single pytest session that iterates its chunk — combining the startup
    savings of batch mode with parallelism.
    """
    import multiprocessing as mp

    # Serialize mutant pairs: ast.Module doesn't pickle, so send source text
    serialized: list[tuple[Mutant, str]] = []
    for mutant, tree in mutant_pairs:
        try:
            serialized.append((mutant, ast.unparse(tree)))
        except Exception:
            continue

    # Chunk work across workers
    chunk_size = max(1, len(serialized) // workers)
    chunks: list[list[tuple[Mutant, str]]] = []
    for i in range(0, len(serialized), chunk_size):
        chunks.append(serialized[i : i + chunk_size])

    def _worker_fn(chunk: list[tuple[Mutant, str]]) -> list[tuple[int, bool, str | None]]:
        """Worker: re-parse ASTs and batch-test a chunk of mutants."""
        reparsed = []
        for mutant, source_text in chunk:
            try:
                tree = ast.parse(source_text)
                reparsed.append((mutant, tree))
            except Exception:
                continue
        return _batch_module_test(target, reparsed)

    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    with ctx.Pool(min(workers, len(chunks))) as pool:
        chunk_results = pool.map(_worker_fn, chunks)

    # Flatten results
    results: list[tuple[Mutant, bool, str | None]] = []
    for chunk_result in chunk_results:
        results.extend(chunk_result)
    return results


def _module_mine_oracle_fallback(
    target: str,
    module: types.ModuleType,
    original_result: MutationResult,
    operators: list[str],
    stats: dict[str, int],
    *,
    filter_equivalent: bool,
    equivalence_samples: int,
    preset_used: str | None,
    mutant_timeout: float | None,
) -> MutationResult | None:
    """Run mine oracle per-function when module-level tests killed 0 mutants.

    Iterates public functions in the module, re-generates mutants for each,
    and tests them via :func:`_mine_based_mutation_test`.  If the mine oracle
    catches mutations that tests missed, warns about process isolation and
    returns the combined result.
    """
    combined = MutationResult(target=target, operators_used=operators, preset_used=preset_used)
    any_killed = False

    for name in sorted(dir(module)):
        if name.startswith("__"):
            continue
        func = getattr(module, name, None)
        if not callable(func) or inspect.isclass(func):
            continue
        func = _unwrap_func(func)
        obj_mod = getattr(func, "__module__", None)
        if obj_mod and not obj_mod.startswith(target):
            continue
        try:
            source = _get_source(func)
        except Exception:
            continue

        func_target = f"{target}.{name}"
        try:
            func_mutants = generate_mutants(source, operators, timeout=mutant_timeout)
        except Exception:
            continue

        if not func_mutants:
            continue

        try:
            mine_result = _mine_based_mutation_test(
                func_target,
                func,
                name,
                module,
                func_mutants,
                filter_equivalent=filter_equivalent,
                equivalence_samples=equivalence_samples,
                operators_used=operators,
                preset_used=preset_used,
                _stats={},
            )
            combined.mutants.extend(mine_result.mutants)
            if mine_result.killed > 0:
                any_killed = True
        except (NoTestsFoundError, Exception):
            continue

    if any_killed:
        import warnings

        warnings.warn(
            f"{target!r}: tests killed 0/{original_result.total} mutants but "
            f"mine oracle killed {combined.killed}/{combined.total} across "
            f"module functions — your tests likely exercise this module through "
            "a process boundary (Ray, multiprocessing) where in-memory mutations "
            "are invisible. Falling back to mine oracle for accurate results.",
            stacklevel=3,
        )
        combined.diagnostics["fallback_reason"] = "process_isolation"
        combined.diagnostics.update(stats)
        combined.diagnostics["tested"] = combined.total
        return combined
    return None


def mutate_and_test(
    target: str,
    test_fn: Callable[[], None] | None = None,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
    workers: int = 1,
    filter_equivalent: bool = True,
    equivalence_samples: int = 10,
    extra_mutants: list[str | tuple[str, str]] | None = None,
    llm: Callable[[str], str] | None = None,
    llm_equivalence: bool = False,
    concern: str | None = None,
    test_filter: str | None = None,
    mutant_timeout: float | None = None,
    disk_mutation: bool | None = None,
) -> MutationResult:
    """Apply mutations to an entire module and run *test_fn* against each.

    A mutant is **killed** if *test_fn* raises.
    A mutant **survives** if *test_fn* passes — meaning your tests miss the bug.

    Note: this swaps ``sys.modules[target]``.  Code that cached individual
    functions via ``from target import func`` will not see the mutant.
    Prefer :func:`mutate_function_and_test` for precise single-function targeting.

    Args:
        target: Module path (e.g. ``"myapp.scoring"``).
        test_fn: Zero-arg callable; should raise on failure.  When ``None``
            (default), auto-discovers tests via pytest in-process.
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
        workers: Parallel workers for testing mutants. Default ``1``.
        filter_equivalent: Drop mutants that produce identical outputs on
            random inputs.  Default ``True``.
        equivalence_samples: Number of random inputs for equivalence
            filtering.  Default ``10``.
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            to validate and add alongside rule-based mutants.
        llm: Optional callable for automated mutant generation.
        llm_equivalence: If ``True`` and *llm* is provided, use the LLM
            to filter surviving mutants for semantic equivalence.
        test_filter: Pytest ``-k`` expression to narrow which tests run
            against each mutant.  When ``None`` (default), derives a filter
            from the target module name.
        mutant_timeout: Maximum seconds for the mutant generation step.
            When exceeded, returns whatever mutants have been generated so
            far.  Prevents hanging on complex AST expressions (numpy, cv2).
    """
    disk_mutation = _resolve_disk_mutation(disk_mutation, target)
    used_preset = preset
    operators = _resolve_operators(operators, preset)
    use_batch = test_fn is None  # batch when auto-discovering tests
    if test_fn is None:
        test_fn = _auto_test_fn(target, test_filter=test_filter)
    module = importlib.import_module(target)
    source_file = getattr(module, "__file__", None)
    if source_file is None:
        raise ValueError(f"Cannot locate source for {target!r}")
    with open(source_file) as f:
        source = f.read()

    stats: dict[str, int] = {}
    mutant_pairs = generate_mutants(
        source,
        operators,
        extra_mutants=extra_mutants,
        llm=llm,
        concern=concern,
        _stats=stats,
        timeout=mutant_timeout,
    )

    # Filter equivalent mutants (same outputs on random inputs)
    if filter_equivalent:
        filtered = []
        for mutant, tree in mutant_pairs:
            if _module_is_equivalent(module, tree, equivalence_samples):
                mutant.killed = True
                mutant.error = "equivalent (filtered)"
                stats["filtered_module_equivalent"] = (
                    stats.get("filtered_module_equivalent", 0) + 1
                )
            else:
                filtered.append((mutant, tree))
        mutant_pairs = filtered

    result = MutationResult(
        target=target, operators_used=operators, preset_used=used_preset, concern=concern
    )

    # Batch mode: single pytest session for all mutants (much faster)
    if use_batch and mutant_pairs:
        if workers > 1 and len(mutant_pairs) > 1:
            batch_results = _parallel_module_test(target, mutant_pairs, workers)
        else:
            batch_results = _batch_module_test(target, mutant_pairs, disk_mutation=disk_mutation)
        for item in batch_results:
            mutant, killed, error = item[0], item[1], item[2]
            killer = item[3] if len(item) > 3 else None
            mutant.killed = killed
            mutant.error = error
            mutant.killed_by = killer
            result.mutants.append(mutant)
        # 0% score fallback for module-level: if auto-discovered tests killed
        # nothing, try calling each public function directly (mine oracle).
        # Same logic as mutate_function_and_test's fallback.
        if result.total > 0 and result.killed == 0:
            mine_kills = _module_mine_oracle_fallback(
                target,
                module,
                result,
                operators,
                stats,
                filter_equivalent=filter_equivalent,
                equivalence_samples=equivalence_samples,
                preset_used=used_preset,
                mutant_timeout=mutant_timeout,
            )
            if mine_kills is not None:
                return mine_kills

        result.diagnostics.update(stats)
        result.diagnostics["tested"] = result.total
        return result

    # Fallback: serial per-mutant testing (custom test_fn)
    test_name = getattr(test_fn, "__qualname__", getattr(test_fn, "__name__", "test_fn"))
    for mutant, mutated_tree in mutant_pairs:
        try:
            cm = (
                _mutated_module_on_disk(target, mutated_tree)
                if disk_mutation
                else _mutated_module(target, mutated_tree)
            )
            with cm:
                if not disk_mutation:
                    importlib.invalidate_caches()
                test_fn()
            mutant.killed = False
        except Exception as e:
            mutant.killed = True
            mutant.error = str(e)[:200]
            mutant.killed_by = test_name

        result.mutants.append(mutant)

    result.diagnostics.update(stats)
    result.diagnostics["tested"] = result.total
    return result


# ============================================================================
# Function-level mutation testing (recommended)
# ============================================================================


def validate_mined_properties(
    target: str,
    max_examples: int = 100,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
) -> MutationResult:
    """Mine properties of *target*, then mutate it and check the properties catch the mutations.

    This answers: "are the properties mine() found strong enough to detect real bugs?"
    Surviving mutants reveal properties that are too weak — the mined invariants
    pass even on broken code.

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        max_examples: Examples for mine() property discovery.
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
    """
    operators = _resolve_operators(operators, preset)
    from ordeal.mine import mine

    module_path, func_name = target.rsplit(".", 1)
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)

    # Mine the original function's properties
    mine_result = mine(func, max_examples=max_examples)
    universal = mine_result.universal
    if not universal:
        return MutationResult(target=target)  # nothing to validate

    # Build a test function from the mined properties
    def mined_test() -> None:
        current_func = getattr(importlib.import_module(module_path), func_name)
        re_mined = mine(current_func, max_examples=max(20, max_examples // 5))
        for original_prop in universal:
            match = next((p for p in re_mined.properties if p.name == original_prop.name), None)
            if match is None or not match.universal:
                raise AssertionError(f"Property {original_prop.name!r} no longer holds on mutant")

    return mutate_function_and_test(target, mined_test, operators)


_BOUNDARY_VALUES: dict[type, list] = {
    int: [0, 1, -1, 2, -2],
    float: [0.0, 1.0, -1.0, 0.5, -0.5],
    bool: [True, False],
    str: ["", "a", "ab"],
    bytes: [b"", b"\x00", b"ab"],
}


def _is_runtime_equivalent(
    original: Callable,
    mutant_fn: Callable,
    n_samples: int = 10,
) -> bool:
    """Heuristic: run both functions on random inputs, skip if outputs match.

    Tests boundary values first (0, 1, -1, etc.) to catch mutations that
    only differ at edges (e.g. ``<`` → ``<=``), then *n_samples* random
    draws from type-driven strategies.

    Returns ``True`` (skip) when all samples agree.  Falls back to ``False``
    (test it) when inputs can't be generated or any sample disagrees.
    """
    import warnings

    try:
        sig = inspect.signature(original)
    except (ValueError, TypeError):
        return False

    params = [
        p
        for p in sig.parameters.values()
        if p.name != "self" and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if not params:
        try:
            return original() == mutant_fn()
        except Exception:
            return False

    try:
        from typing import get_type_hints

        from ordeal.quickcheck import strategy_for_type

        hints = get_type_hints(original)
    except Exception:
        return False

    param_hints = []
    strategies = []
    for p in params:
        if p.name not in hints:
            return False
        hint = hints[p.name]
        param_hints.append(hint)
        try:
            strategies.append(strategy_for_type(hint))
        except Exception:
            return False

    # --- Phase 1: boundary values (catches < vs <=, off-by-one, etc.) ---
    boundary_lists = []
    for hint in param_hints:
        origin = getattr(hint, "__origin__", hint)
        values = _BOUNDARY_VALUES.get(origin, [])
        boundary_lists.append(values)

    # Build boundary arg combos: for each param pick each boundary value
    # while using the first boundary (or a random draw) for other params.
    def _check(args: list) -> bool:
        try:
            return original(*args) == mutant_fn(*args)
        except Exception:
            return False

    # Defaults: first boundary value per param, or a random draw
    defaults = []
    for i, bl in enumerate(boundary_lists):
        defaults.append(bl[0] if bl else strategies[i].example())

    for i, bl in enumerate(boundary_lists):
        for val in bl:
            args = list(defaults)
            args[i] = val
            if not _check(args):
                return False

    # --- Phase 2: random samples ---
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(n_samples):
            try:
                args = [s.example() for s in strategies]
                if original(*args) != mutant_fn(*args):
                    return False
            except Exception:
                return False

    return True


def _auto_test_fn(target: str, test_filter: str | None = None) -> Callable[[], None]:
    """Create a test function that runs pytest in-process for *target*.

    When *test_filter* is provided, it is passed as ``-k`` to pytest,
    replacing the default broad module-name filter.  This avoids running
    the entire test suite for each mutant when only a few tests are
    relevant (e.g. ``test_filter="test_postprocess"`` instead of
    matching all 1555 tests).

    Runs in-process so PatchFault swaps are visible to the test code.
    """

    def run_tests() -> None:
        import pytest

        if test_filter is not None:
            k_filter = test_filter
        else:
            parts = target.rsplit(".", 1)
            module_name = parts[0] if len(parts) >= 2 else target
            k_filter = module_name.split(".")[-1]
        rc = pytest.main(
            [
                "-x",
                "-q",
                "--tb=short",
                "--no-header",
                "--chaos",
                "-o",
                "addopts=",
                "-k",
                k_filter,
            ]
        )
        if rc == 5:
            short = target.rsplit(".", 1)[-1]
            suggested = f"tests/test_{short}.py"
            raise NoTestsFoundError(
                f"No tests found for {target!r}. "
                "Mutation score is meaningless without tests.\n"
                f"  Generate: generate_starter_tests({target!r})\n"
                f"  CLI:      ordeal init {target}\n"
                f"  Save to:  {suggested}",
                target=target,
                suggested_file=suggested,
            )
        if rc != 0:
            raise AssertionError(f"pytest returned exit code {rc}")

    return run_tests


def mutate_function_and_test(
    target: str,
    test_fn: Callable[[], None] | None = None,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
    workers: int = 1,
    filter_equivalent: bool = True,
    equivalence_samples: int = 10,
    extra_mutants: list[str | tuple[str, str]] | None = None,
    llm: Callable[[str], str] | None = None,
    llm_equivalence: bool = False,
    concern: str | None = None,
    test_filter: str | None = None,
    mutant_timeout: float | None = None,
    disk_mutation: bool | None = None,
) -> MutationResult:
    """Mutate a single function and run tests against each mutant.

    This is the **recommended** entry point for mutation testing. It uses
    :class:`PatchFault` to swap the function at its module attribute, so any
    code that accesses it via ``mod.func()`` will see the mutant.

    Example — minimal (auto-discovers tests via pytest)::

        from ordeal import mutate_function_and_test

        result = mutate_function_and_test("myapp.scoring.compute", preset="standard")
        print(result.summary())

    Example — only run relevant tests (fast)::

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            preset="standard",
            test_filter="test_compute",
        )

    Example — AI assistant writes extra mutants directly::

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            preset="standard",
            extra_mutants=[
                ("off-by-one", "def compute(a, b):\\n    if a <= 0: ..."),
                ("wrong var", "def compute(a, b):\\n    if b < 0: ..."),
            ],
        )

    Example — custom operators + parallel::

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            operators=["arithmetic", "comparison", "boundary"],
            workers=4,
        )

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        test_fn: Zero-arg callable; should raise on failure.  When ``None``
            (default), auto-discovers and runs relevant tests via pytest
            in-process (``pytest -x -k <module_name>``).
        operators: Explicit list of operator names to apply. See
            ``OPERATORS.keys()`` for all available operators. Mutually
            exclusive with *preset*.
        preset: Named operator group — pick one:

            - ``"essential"`` — 4 operators, fast feedback.
            - ``"standard"`` — 8 operators, good CI default.
            - ``"thorough"`` — all 14 operators, comprehensive.

            Mutually exclusive with *operators*. When neither is given,
            all operators are used.
        workers: Number of parallel worker processes. ``1`` (default)
            runs sequentially.  Higher values give near-linear speedup
            since each mutant is tested independently.
        filter_equivalent: If ``True`` (default), skip mutants that produce
            identical output to the original on random sample inputs.
            Reduces noise from equivalent mutants that always survive.
        equivalence_samples: Number of random inputs for equivalence
            filtering.  Default ``10``.
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            written by an AI assistant, human, or any author.  Validated
            (parse, compile, dedup) and added alongside rule-based mutants.
            This is the primary way to supply custom mutants — no API call
            needed, just write the code.
        llm: Optional callable ``(prompt: str) -> str`` for automated
            mutant generation.  Convenience for pipelines — under the hood
            it feeds results through the same validation as *extra_mutants*.
        llm_equivalence: If ``True`` and *llm* is provided, use the LLM
            to filter surviving mutants that are semantically equivalent.
        test_filter: Pytest ``-k`` expression to narrow which tests run
            against each mutant.  When ``None`` (default), derives a filter
            from the target module name.  Set this to avoid running the
            entire test suite per mutant (e.g. ``"test_compute"``).
        mutant_timeout: Maximum seconds for the mutant generation step.
            When exceeded, returns whatever mutants have been generated so
            far.  Prevents hanging on complex AST expressions (numpy, cv2).
    """
    # Try auto-discovering tests; fall back to mine()-based oracle
    disk_mutation = _resolve_disk_mutation(disk_mutation, target)
    # Resume: check cache before doing any work
    used_preset = preset
    operators = _resolve_operators(operators, preset)
    auto_discovered_tests = test_fn is None
    use_mine_oracle = False
    if test_fn is None:
        try:
            test_fn = _auto_test_fn(target, test_filter=test_filter)
            # Probe for tests — if NoTestsFoundError, switch to mine oracle
            test_fn()
        except NoTestsFoundError:
            use_mine_oracle = True
            test_fn = None
        except Exception:
            pass  # tests exist but failed — that's fine for mutation testing
    module_path, func_name = target.rsplit(".", 1)
    module = importlib.import_module(module_path)
    func = _unwrap_func(getattr(module, func_name))
    source = _get_source(func)

    stats: dict[str, int] = {}
    mutant_pairs = generate_mutants(
        source,
        operators,
        extra_mutants=extra_mutants,
        llm=llm,
        concern=concern,
        _stats=stats,
        timeout=mutant_timeout,
    )

    # Mine-based oracle: mine the original, use properties to kill mutants
    if use_mine_oracle:
        result = _mine_based_mutation_test(
            target,
            func,
            func_name,
            module,
            mutant_pairs,
            filter_equivalent=filter_equivalent,
            equivalence_samples=equivalence_samples,
            operators_used=operators,
            preset_used=used_preset,
            _stats=stats,
        )
        result.diagnostics.update(stats)
        result.diagnostics["tested"] = result.total
        return result

    assert test_fn is not None

    if workers > 1:
        result = _parallel_function_test(target, test_fn, mutant_pairs, module, func_name, workers)
        result.preset_used = used_preset
        result.operators_used = operators
        result.diagnostics.update(stats)
        result.diagnostics["tested"] = result.total
        return result

    result = MutationResult(
        target=target, operators_used=operators, preset_used=used_preset, concern=concern
    )

    for mutant, mutated_tree in mutant_pairs:
        # Compile the mutated function in the module's namespace
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                stats["compilation_failed"] = stats.get("compilation_failed", 0) + 1
                continue
        except Exception:
            stats["compilation_failed"] = stats.get("compilation_failed", 0) + 1
            continue

        # Runtime equivalence filter: skip if outputs match on samples
        if filter_equivalent and _is_runtime_equivalent(
            func, mutated_func, n_samples=equivalence_samples
        ):
            stats["filtered_runtime_equivalent"] = stats.get("filtered_runtime_equivalent", 0) + 1
            continue

        # Swap via PatchFault (in-process) + optional disk mutation (subprocesses)
        fn_name = getattr(test_fn, "__qualname__", getattr(test_fn, "__name__", "test_fn"))
        fault = PatchFault(target, lambda orig, mf=mutated_func: mf)
        disk_cm = (
            _function_mutated_on_disk(module_path, func_name, mutated_tree)
            if disk_mutation
            else contextlib.nullcontext()
        )
        with disk_cm:
            fault.activate()
            try:
                test_fn()
                mutant.killed = False
            except Exception as e:
                mutant.killed = True
                mutant.error = str(e)[:200]
                mutant.killed_by = fn_name
            finally:
                fault.deactivate()

        result.mutants.append(mutant)

    # 0% score fallback: if auto-discovered tests killed nothing, try mine
    # oracle directly.  This catches process-isolation (Ray workers, long-lived
    # pools) where PatchFault / disk mutation are invisible to test workers.
    # Only for auto-discovered tests — if the user provided test_fn, the 0%
    # score is the correct result (their test may intentionally be weak).
    if result.total > 0 and result.killed == 0 and auto_discovered_tests:
        mine_result = _mine_based_mutation_test(
            target,
            func,
            func_name,
            module,
            # Re-generate mutant pairs (originals were consumed)
            generate_mutants(
                source,
                operators,
                extra_mutants=extra_mutants,
                concern=concern,
                timeout=mutant_timeout,
            ),
            filter_equivalent=filter_equivalent,
            equivalence_samples=equivalence_samples,
            operators_used=operators,
            preset_used=used_preset,
            _stats={},
        )
        if mine_result.killed > 0:
            import warnings

            warnings.warn(
                f"{target!r}: tests killed 0/{result.total} mutants but mine oracle "
                f"killed {mine_result.killed}/{mine_result.total} — your tests likely "
                "exercise this function through a process boundary (Ray, multiprocessing) "
                "where in-memory mutations are invisible. "
                "Falling back to mine oracle for accurate results.",
                stacklevel=2,
            )
            mine_result.diagnostics["fallback_reason"] = "process_isolation"
            mine_result.diagnostics.update(stats)
            mine_result.diagnostics["tested"] = mine_result.total
            return mine_result

    # LLM equivalence filter on surviving mutants
    if llm_equivalence and llm is not None:
        for mutant in result.survived:
            if mutant._mutant_source:
                try:
                    if _is_llm_equivalent(source, mutant._mutant_source, llm):
                        mutant.killed = True
                        mutant.error = "equivalent (LLM-detected)"
                        mutant.killed_by = "llm_equivalence"
                except Exception:
                    pass

    result.diagnostics.update(stats)
    result.diagnostics["tested"] = result.total
    return result


def _mine_based_mutation_test(
    target: str,
    func: Callable,
    func_name: str,
    module: types.ModuleType,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    *,
    filter_equivalent: bool,
    equivalence_samples: int,
    operators_used: list[str] | None,
    preset_used: str | None,
    _stats: dict[str, int] | None = None,
) -> MutationResult:
    """Kill mutants using mine()-discovered properties as the test oracle.

    When no human-written tests exist, mine the original function to
    discover properties (bounded, no NaN, deterministic, etc.), then
    check whether each mutant violates any of those properties.
    """
    from ordeal.mine import mine

    # Mine the original function
    mine_result = mine(func, max_examples=50)
    universal = [p for p in mine_result.properties if p.universal and p.total > 0]
    if not universal:
        short = target.rsplit(".", 1)[-1]
        raise NoTestsFoundError(
            f"No tests found for {target!r} and mine() discovered no properties. "
            "Cannot validate mutations.\n"
            f"  Generate: generate_starter_tests({target!r})\n"
            f"  CLI:      ordeal init {target}",
            target=target,
            suggested_file=f"tests/test_{short}.py",
        )

    # Collect sample inputs from mining
    sample_inputs = mine_result.collected_inputs[:50]
    if not sample_inputs:
        # Generate fresh inputs
        from ordeal.auto import _infer_strategies

        strats = _infer_strategies(func, None)
        if strats:
            for _ in range(50):
                try:
                    sample_inputs.append({k: v.example() for k, v in strats.items()})
                except Exception:
                    break

    st = _stats  # alias for brevity
    result = MutationResult(target=target, operators_used=operators_used, preset_used=preset_used)

    for mutant, mutated_tree in mutant_pairs:
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)  # noqa: S102
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                if st is not None:
                    st["compilation_failed"] = st.get("compilation_failed", 0) + 1
                continue
        except Exception:
            if st is not None:
                st["compilation_failed"] = st.get("compilation_failed", 0) + 1
            continue

        if filter_equivalent and _is_runtime_equivalent(
            func, mutated_func, n_samples=equivalence_samples
        ):
            if st is not None:
                st["filtered_runtime_equivalent"] = st.get("filtered_runtime_equivalent", 0) + 1
            continue

        # Check if the mutant violates any mined property
        killed = False
        for inputs in sample_inputs:
            if killed:
                break
            try:
                orig_out = func(**inputs)
                mut_out = mutated_func(**inputs)
                # Different output = mutant detected
                if orig_out != mut_out:
                    killed = True
                    mutant.killed = True
                    mutant.error = f"mine oracle: output differs on {inputs}"
                    mutant.killed_by = "mine()"
            except Exception:
                # Mutant crashes = killed
                killed = True
                mutant.killed = True
                mutant.error = "mine oracle: mutant raised exception"
                mutant.killed_by = "mine()"

        if not killed:
            mutant.killed = False

        result.mutants.append(mutant)

    return result


def _parallel_function_test(
    target: str,
    test_fn: Callable[[], None],
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    module: types.ModuleType,
    func_name: str,
    workers: int,
) -> MutationResult:
    """Run mutant tests in parallel using a process pool.

    Pre-compiles all mutants, then distributes the test execution
    across *workers* processes.  Each worker activates one mutant
    via PatchFault, runs test_fn, and returns the result.
    """
    import multiprocessing as mp

    # Pre-compile mutants into (Mutant, callable) — filter out failures
    compiled: list[tuple[Mutant, Callable]] = []
    for mutant, mutated_tree in mutant_pairs:
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                continue
            compiled.append((mutant, mutated_func))
        except Exception:
            continue

    # Worker function: test one mutant
    def _test_one(args: tuple[int, str, str]) -> tuple[int, bool, str | None]:
        idx, op, desc = args
        _, mf = compiled[idx]
        fault = PatchFault(target, lambda orig, mf=mf: mf)
        fault.activate()
        try:
            test_fn()
            return (idx, False, None)
        except Exception as e:
            return (idx, True, str(e)[:200])
        finally:
            fault.deactivate()

    # Build work items
    work = [(i, m.operator, m.description) for i, (m, _) in enumerate(compiled)]

    # Execute in pool (use fork to share compiled state)
    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    with ctx.Pool(workers) as pool:
        outcomes = pool.map(_test_one, work)

    result = MutationResult(target=target)
    for idx, killed, error in outcomes:
        mutant, _ = compiled[idx]
        mutant.killed = killed
        mutant.error = error
        result.mutants.append(mutant)

    return result


def _is_function_target(target: str) -> bool:
    """Determine if a dotted path refers to a callable (vs a module)."""
    try:
        importlib.import_module(target)
        return False
    except ImportError:
        pass
    parts = target.rsplit(".", 1)
    if len(parts) < 2:
        return False
    try:
        mod = importlib.import_module(parts[0])
        attr = getattr(mod, parts[1], None)
        return callable(attr)
    except ImportError:
        return False


def mutate(
    target: str,
    test_fn: Callable[[], None] | None = None,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
    workers: int = 1,
    filter_equivalent: bool = True,
    equivalence_samples: int = 10,
    extra_mutants: list[str | tuple[str, str]] | None = None,
    llm: Callable[[str], str] | None = None,
    llm_equivalence: bool = False,
    concern: str | None = None,
    test_filter: str | None = None,
    mutant_timeout: float | None = None,
    disk_mutation: bool | None = None,
    resume: bool = False,
) -> MutationResult:
    """Unified mutation testing entry point — auto-detects function vs module.

    Inspects *target* to decide whether it names a callable or a module,
    then delegates to :func:`mutate_function_and_test` or
    :func:`mutate_and_test` respectively.

    This is the function used by the ``@pytest.mark.mutate`` fixture and
    is the simplest way to run mutation testing programmatically::

        from ordeal.mutations import mutate

        result = mutate("myapp.scoring.compute", preset="standard")
        print(result.summary())

    Run only relevant tests per mutant (fast)::

        result = mutate("myapp.scoring.compute", test_filter="test_compute")

    With extra mutants written by an AI assistant or human::

        result = mutate("myapp.scoring.compute", preset="standard", extra_mutants=[
            ("off-by-one", "def compute(a, b):\\n    if a <= 0: ..."),
        ])

    Args:
        target: Dotted path to a function (e.g. ``"myapp.scoring.compute"``)
            or module (e.g. ``"myapp.scoring"``).
        test_fn: Zero-arg callable; should raise on failure.  When ``None``
            (default), auto-discovers tests via pytest in-process.
        operators: Explicit list of operator names. Mutually exclusive with
            *preset*.
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
        workers: Parallel worker processes. Default ``1``.
        filter_equivalent: Skip equivalent mutants. Default ``True``.
        equivalence_samples: Samples for equivalence filtering. Default ``10``.
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            to validate and add alongside rule-based mutants.
        llm: Optional callable for automated mutant generation.
        llm_equivalence: If ``True`` and *llm* is provided, use the LLM
            to filter surviving mutants for semantic equivalence.
        test_filter: Pytest ``-k`` expression to narrow which tests run
            against each mutant.  When ``None`` (default), derives a filter
            from the target module name.
        mutant_timeout: Maximum seconds for the mutant generation step.
            Prevents hanging on complex AST expressions (numpy, cv2).
        disk_mutation: Write the mutated source to disk so subprocesses
            (Ray workers, ``multiprocessing`` spawn) see the mutation.
            Default ``False`` (in-memory only, safe for parallel tests).
        resume: Reuse cached results when the module source hasn't changed.
            Cache is invalidated when **any** line in the module changes
            (not just the target function) or when the preset/operators
            change.  Default ``False`` (always run fresh).
    """
    # Resume: check cache before dispatching
    resolved_operators = _resolve_operators(operators, preset)
    if resume:
        try:
            module_hash = _module_source_hash(target)
            cached = _load_cache(target, module_hash, preset, resolved_operators)
            if cached is not None:
                return cached
        except Exception:
            pass

    dispatch = mutate_function_and_test if _is_function_target(target) else mutate_and_test
    result = dispatch(
        target,
        test_fn=test_fn,
        operators=operators,
        preset=preset,
        workers=workers,
        filter_equivalent=filter_equivalent,
        equivalence_samples=equivalence_samples,
        extra_mutants=extra_mutants,
        llm=llm,
        llm_equivalence=llm_equivalence,
        concern=concern,
        test_filter=test_filter,
        mutant_timeout=mutant_timeout,
        disk_mutation=disk_mutation,
    )

    # Save cache after fresh run — but NOT if the result came from the
    # mine oracle (primary or fallback), which is stochastic. mine() uses
    # random inputs, so re-running can discover more properties.
    mine_used = any(m.killed_by == "mine()" for m in result.mutants)
    if resume and not mine_used:
        try:
            module_hash = _module_source_hash(target)
            _save_cache(target, result, module_hash)
        except Exception:
            pass

    return result


def mutation_faults(
    target: str,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
) -> list[tuple[Mutant, PatchFault]]:
    """Generate :class:`PatchFault` objects for each mutant of a function.

    Each fault, when activated, replaces the target function with a mutated
    version.  Use with the Explorer to let the nemesis toggle mutations
    during coverage-guided exploration::

        explorer = Explorer(MyTest, mutation_targets=["myapp.scoring.compute"])

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.

    Returns:
        List of ``(Mutant, PatchFault)`` pairs.
    """
    operators = _resolve_operators(operators, preset)
    module_path, func_name = target.rsplit(".", 1)
    module = importlib.import_module(module_path)
    func = _unwrap_func(getattr(module, func_name))
    source = _get_source(func)

    results: list[tuple[Mutant, PatchFault]] = []
    for mutant, mutated_tree in generate_mutants(source, operators):
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)  # noqa: S102
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                continue
        except Exception:
            continue

        fault = PatchFault(
            target,
            lambda orig, mf=mutated_func: mf,
            name=f"mutant({mutant.operator}@L{mutant.line}:{mutant.description})",
        )
        results.append((mutant, fault))

    return results
