"""Mutation testing — validate that your chaos tests catch real bugs.

Generates mutated versions of target code and runs tests against each.
If a mutant survives (tests still pass), the tests are missing something.

Two entry points:

1. **Module-level** — mutate an entire module::

       from ordeal.mutations import mutate_and_test

       result = mutate_and_test(
           target="myapp.scoring",
           test_fn=lambda: run_tests(),
       )
       print(result.summary())

2. **Function-level** (recommended) — mutate a single function, uses
   PatchFault for reliable replacement::

       from ordeal.mutations import mutate_function_and_test

       result = mutate_function_and_test(
           target="myapp.scoring.compute",
           test_fn=lambda: assert compute(1, 2) == 3,
       )

Operators:
    arithmetic   +↔-  *↔/  %→*
    comparison   <↔<=  >↔>=  ==↔!=
    negate       if cond → if not cond
    return_none  return x → return None
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
from typing import Any, Callable

from ordeal.faults import PatchFault


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class Mutant:
    """A single code mutation."""

    operator: str
    description: str
    line: int
    col: int
    killed: bool = False
    error: str | None = None

    @property
    def location(self) -> str:
        """Source location as ``L<line>:<col>``."""
        return f"L{self.line}:{self.col}"


@dataclass
class MutationResult:
    """Aggregated mutation testing results."""

    target: str
    mutants: list[Mutant] = field(default_factory=list)

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

    def summary(self) -> str:
        """Human-readable report with surviving mutants listed."""
        lines = [f"Mutation score: {self.killed}/{self.total} ({self.score:.0%})"]
        for m in self.survived:
            lines.append(f"  SURVIVED  {m.location} {m.description}")
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


OPERATORS: dict[str, tuple[type[_Counter], type[_Applicator]]] = {
    "arithmetic": (_ArithmeticCounter, _ArithmeticApplicator),
    "comparison": (_ComparisonCounter, _ComparisonApplicator),
    "negate": (_NegateCounter, _NegateApplicator),
    "return_none": (_ReturnNoneCounter, _ReturnNoneApplicator),
}


# ============================================================================
# Mutant generation
# ============================================================================

def generate_mutants(
    source: str,
    operators: list[str] | None = None,
) -> list[tuple[Mutant, ast.Module]]:
    """Generate all possible mutants from source code.

    Returns a list of ``(Mutant, mutated_AST)`` pairs.
    """
    tree = ast.parse(source)
    ops = operators or list(OPERATORS.keys())
    results: list[tuple[Mutant, ast.Module]] = []

    for op_name in ops:
        if op_name not in OPERATORS:
            raise ValueError(f"Unknown operator: {op_name!r}. Available: {list(OPERATORS)}")
        counter_cls, applicator_cls = OPERATORS[op_name]

        counter = counter_cls()
        counter.visit(tree)

        for i in range(counter.count):
            mutated_tree = copy.deepcopy(tree)
            applicator = applicator_cls(target_idx=i)
            applicator.visit(mutated_tree)
            ast.fix_missing_locations(mutated_tree)

            if applicator.applied:
                mutant = Mutant(
                    operator=op_name,
                    description=applicator.description,
                    line=applicator.line,
                    col=applicator.col,
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
) -> MutationResult:
    """Apply mutations to an entire module and run *test_fn* against each.

    A mutant is **killed** if *test_fn* raises.
    A mutant **survives** if *test_fn* passes — meaning your tests miss the bug.

    Note: this swaps ``sys.modules[target]``.  Code that cached individual
    functions via ``from target import func`` will not see the mutant.
    Prefer :func:`mutate_function_and_test` for precise single-function targeting.
    """
    module = importlib.import_module(target)
    source_file = getattr(module, "__file__", None)
    if source_file is None:
        raise ValueError(f"Cannot locate source for {target!r}")
    with open(source_file) as f:
        source = f.read()

    mutant_pairs = generate_mutants(source, operators)
    result = MutationResult(target=target)

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

def mutate_function_and_test(
    target: str,
    test_fn: Callable[[], None],
    operators: list[str] | None = None,
) -> MutationResult:
    """Mutate a single function and run *test_fn* against each mutant.

    Uses :class:`PatchFault` to swap the function, so callers that reference
    it through the module (``mod.func()``) will see the mutant.

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        test_fn: Zero-arg callable; should raise on failure.
        operators: Mutation operators to use (default: all).
    """
    module_path, func_name = target.rsplit(".", 1)
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    source = textwrap.dedent(inspect.getsource(func))

    mutant_pairs = generate_mutants(source, operators)
    result = MutationResult(target=target)

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
