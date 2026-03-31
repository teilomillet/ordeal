"""Mutation testing — validate that your tests catch real bugs.

Generates mutated versions of target code and runs tests against each.
If a mutant survives (tests still pass), the tests are missing something.

Quick start
-----------

Pick a preset and go::

    from ordeal import mutate_function_and_test

    result = mutate_function_and_test(
        "myapp.scoring.compute",
        test_fn=lambda: assert compute(1, 2) == 3,
        preset="standard",    # "essential" | "standard" | "thorough"
    )
    print(result.summary())   # shows surviving mutants + how to fix them

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
"""

from __future__ import annotations

import ast
import copy
import importlib
import inspect
import sys
import textwrap
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from ordeal.faults import PatchFault

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


@dataclass
class MutationResult:
    """Aggregated mutation testing results.

    Access structured data for AI consumption::

        result.target           # "myapp.scoring.compute"
        result.score            # 0.625 (kill ratio)
        result.survived         # list of Mutant objects that tests missed
        result.operators_used   # ["arithmetic", "comparison", ...] or None
        result.preset_used      # "standard" or None

        for m in result.survived:
            print(m.operator)     # "arithmetic"
            print(m.description)  # "+ -> -"
            print(m.location)     # "L12:4"
            print(m.source_line)  # "return a + b"
            print(m.remediation)  # actionable fix guidance
    """

    target: str
    mutants: list[Mutant] = field(default_factory=list)
    operators_used: list[str] | None = None
    preset_used: str | None = None

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

    @property
    def score(self) -> float:
        """Kill ratio: 1.0 means every mutant was caught."""
        return self.killed / self.total if self.total > 0 else 1.0

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
        meta = ", ".join(parts)

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
        return "\n".join(lines)


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


OPERATORS: dict[str, tuple[type[_Counter], type[_Applicator]]] = {
    "arithmetic": (_ArithmeticCounter, _ArithmeticApplicator),
    "comparison": (_ComparisonCounter, _ComparisonApplicator),
    "negate": (_NegateCounter, _NegateApplicator),
    "return_none": (_ReturnNoneCounter, _ReturnNoneApplicator),
    "boundary": (_BoundaryCounter, _BoundaryApplicator),
    "constant": (_ConstantCounter, _ConstantApplicator),
    "delete_statement": (_DeleteStatementCounter, _DeleteStatementApplicator),
    "logical": (_LogicalCounter, _LogicalApplicator),
    "swap_if_else": (_SwapIfElseCounter, _SwapIfElseApplicator),
    "remove_not": (_RemoveNotCounter, _RemoveNotApplicator),
    "exception_swallow": (_ExceptionSwallowCounter, _ExceptionSwallowApplicator),
    "argument_swap": (_ArgumentSwapCounter, _ArgumentSwapApplicator),
    "break_continue_swap": (_BreakContinueSwapCounter, _BreakContinueSwapApplicator),
    "unary_negate": (_UnaryNegateCounter, _UnaryNegateApplicator),
}


# ============================================================================
# Operator presets — named groups for different thoroughness levels
# ============================================================================


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
    preset: str | None = None,
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
) -> list[tuple[Mutant, ast.Module]]:
    """Generate mutants from source code, filtering out noise.

    Skips mutations inside ``__repr__``, ``__str__``, and other display
    methods (they produce equivalent mutants that always survive).

    Returns a list of ``(Mutant, mutated_AST)`` pairs.
    """
    tree = ast.parse(source)
    source_lines = source.splitlines()
    ops = operators or list(OPERATORS.keys())
    results: list[tuple[Mutant, ast.Module]] = []

    for op_name in ops:
        if op_name not in OPERATORS:
            raise ValueError(f"Unknown operator: {op_name!r}. Available: {list(OPERATORS)}")
        counter_cls, applicator_cls = OPERATORS[op_name]

        counter = counter_cls()
        counter.visit(tree)

        for i in range(counter.count):
            mutated_tree = ast.parse(source)
            applicator = applicator_cls(target_idx=i)
            applicator.visit(mutated_tree)
            ast.fix_missing_locations(mutated_tree)

            if applicator.applied:
                # Skip mutations in display/repr methods
                if _is_inside_skip_method(tree, applicator.line):
                    continue
                # Skip semantically equivalent mutants
                if _is_equivalent_mutant(
                    tree, mutated_tree, op_name, applicator.description, applicator.line
                ):
                    continue
                # Capture the source line for context
                src_line = ""
                if 0 < applicator.line <= len(source_lines):
                    src_line = source_lines[applicator.line - 1].strip()
                mutant = Mutant(
                    operator=op_name,
                    description=applicator.description,
                    line=applicator.line,
                    col=applicator.col,
                    source_line=src_line,
                )
                results.append((mutant, mutated_tree))

    return results


# ============================================================================
# Module-level mutation testing
# ============================================================================


@contextmanager
def _mutated_module(module_name: str, mutated_tree: ast.Module):
    """Temporarily replace a module in ``sys.modules`` with mutated code.

    Note: code that used ``from module import func`` before the swap will
    still reference the original.  Import the module itself for full effect.
    """
    original = sys.modules.get(module_name)
    if original is None:
        raise ImportError(f"Module {module_name!r} not in sys.modules")

    mutated = types.ModuleType(module_name)
    mutated.__file__ = getattr(original, "__file__", "<mutated>")
    mutated.__package__ = getattr(original, "__package__", None)

    code = compile(mutated_tree, mutated.__file__, "exec")
    exec(code, mutated.__dict__)

    sys.modules[module_name] = mutated
    try:
        yield mutated
    finally:
        sys.modules[module_name] = original


def mutate_and_test(
    target: str,
    test_fn: Callable[[], None],
    operators: list[str] | None = None,
    *,
    preset: str | None = None,
    workers: int = 1,
) -> MutationResult:
    """Apply mutations to an entire module and run *test_fn* against each.

    A mutant is **killed** if *test_fn* raises.
    A mutant **survives** if *test_fn* passes — meaning your tests miss the bug.

    Note: this swaps ``sys.modules[target]``.  Code that cached individual
    functions via ``from target import func`` will not see the mutant.
    Prefer :func:`mutate_function_and_test` for precise single-function targeting.

    Args:
        target: Module path (e.g. ``"myapp.scoring"``).
        test_fn: Zero-arg callable; should raise on failure.
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
        workers: Parallel workers for testing mutants. Default ``1``.
    """
    used_preset = preset
    operators = _resolve_operators(operators, preset)
    module = importlib.import_module(target)
    source_file = getattr(module, "__file__", None)
    if source_file is None:
        raise ValueError(f"Cannot locate source for {target!r}")
    with open(source_file) as f:
        source = f.read()

    mutant_pairs = generate_mutants(source, operators)
    result = MutationResult(target=target, operators_used=operators, preset_used=used_preset)

    for mutant, mutated_tree in mutant_pairs:
        try:
            with _mutated_module(target, mutated_tree):
                importlib.invalidate_caches()
                test_fn()
            mutant.killed = False
        except Exception as e:
            mutant.killed = True
            mutant.error = str(e)[:200]

        result.mutants.append(mutant)

    return result


# ============================================================================
# Function-level mutation testing (recommended)
# ============================================================================


def validate_mined_properties(
    target: str,
    max_examples: int = 100,
    operators: list[str] | None = None,
    *,
    preset: str | None = None,
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


def _is_runtime_equivalent(
    original: Callable,
    mutant_fn: Callable,
    n_samples: int = 10,
) -> bool:
    """Heuristic: run both functions on random inputs, skip if outputs match.

    Generates *n_samples* sets of arguments from the function's type hints
    (via ``strategy_for_type``) and compares outputs.  If every output is
    identical, the mutant is likely equivalent — testing it is wasted work.

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

    strategies = []
    for p in params:
        if p.name not in hints:
            return False
        try:
            strategies.append(strategy_for_type(hints[p.name]))
        except Exception:
            return False

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


def mutate_function_and_test(
    target: str,
    test_fn: Callable[[], None],
    operators: list[str] | None = None,
    *,
    preset: str | None = None,
    workers: int = 1,
    filter_equivalent: bool = True,
    equivalence_samples: int = 10,
) -> MutationResult:
    """Mutate a single function and run *test_fn* against each mutant.

    This is the **recommended** entry point for mutation testing. It uses
    :class:`PatchFault` to swap the function at its module attribute, so any
    code that accesses it via ``mod.func()`` will see the mutant.

    Example — basic usage with a preset::

        from ordeal import mutate_function_and_test

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            test_fn=lambda: assert compute(1, 2) == 3,
            preset="standard",
        )
        print(result.summary())  # surviving mutants + remediation

    Example — custom operator selection::

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            test_fn=run_scoring_tests,
            operators=["arithmetic", "comparison", "boundary"],
        )

    Example — parallel execution for large codebases::

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            test_fn=run_scoring_tests,
            preset="thorough",
            workers=4,
        )

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        test_fn: Zero-arg callable; should raise on failure.
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
    """
    used_preset = preset
    operators = _resolve_operators(operators, preset)
    module_path, func_name = target.rsplit(".", 1)
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    source = textwrap.dedent(inspect.getsource(func))

    mutant_pairs = generate_mutants(source, operators)

    if workers > 1:
        result = _parallel_function_test(target, test_fn, mutant_pairs, module, func_name, workers)
        result.preset_used = used_preset
        result.operators_used = operators
        return result

    result = MutationResult(target=target, operators_used=operators, preset_used=used_preset)

    for mutant, mutated_tree in mutant_pairs:
        # Compile the mutated function in the module's namespace
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                continue
        except Exception:
            continue  # mutant doesn't compile — skip

        # Runtime equivalence filter: skip if outputs match on samples
        if filter_equivalent and _is_runtime_equivalent(
            func, mutated_func, n_samples=equivalence_samples
        ):
            continue

        # Swap via PatchFault
        fault = PatchFault(target, lambda orig, mf=mutated_func: mf)
        fault.activate()
        try:
            test_fn()
            mutant.killed = False
        except Exception as e:
            mutant.killed = True
            mutant.error = str(e)[:200]
        finally:
            fault.deactivate()

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


def mutation_faults(
    target: str,
    operators: list[str] | None = None,
    *,
    preset: str | None = None,
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
    func = getattr(module, func_name)
    source = textwrap.dedent(inspect.getsource(func))

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
