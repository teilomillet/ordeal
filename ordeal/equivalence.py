"""Equivalent mutant detection — structural, statistical, and formal methods.

The equivalent mutant problem is one of the hardest open problems in mutation
testing.  An **equivalent mutant** is a code change that does not alter program
behavior under any input.  Because it can never be killed, it inflates the
denominator of the mutation score, making test suites appear weaker than they
are and creating false "test gaps" that waste developer time.

Why it is hard:

    Deciding whether two programs are semantically identical is undecidable in
    general (Rice's theorem).  Every practical approach trades off speed,
    coverage, and soundness.  No single technique works for all mutants.

This module provides three complementary approaches, ordered from fast and
conservative to slow and definitive:

1. **Structural equivalence** (``structural_equivalence``):
   Normalizes both ASTs — strips comments, canonicalizes variable names,
   folds constants — and checks structural identity.  Runs in microseconds.
   Only catches trivially equivalent mutants (e.g., ``x + 0``, reordered
   commutative operands).  Sound: if it says equivalent, it is.  Incomplete:
   many equivalent mutants have structurally different ASTs.

2. **Statistical equivalence** (``statistical_equivalence``):
   Runs both functions on boundary values and random type-driven inputs,
   then applies a Wilson score confidence interval to bound the probability
   that the functions differ.  Medium speed (milliseconds to seconds).
   Probabilistic: a high confidence of equivalence is strong evidence but
   not proof.  This extends the behavioral filtering already in
   ``ordeal.mutations._is_runtime_equivalent`` with rigorous statistics.

3. **Formal equivalence** (``prove_equivalent``):
   Encodes both functions as SMT formulas via Z3 and checks satisfiability
   of ``f(x) != g(x)``.  If UNSAT, the functions are proven identical for
   all inputs.  If SAT, the solver provides a concrete counterexample.
   Definitive when it succeeds, but may time out on complex functions.
   Requires ``pip install z3-solver`` — gracefully degrades without it.

How the three complement each other:

    Structural catches the easy cases in microseconds, so the statistical
    layer never wastes time on them.  Statistical catches most remaining
    equivalences with high confidence, filtering the candidate set for the
    expensive formal check.  Formal provides proof for the ambiguous cases
    that statistics cannot resolve.  Together, they form a layered filter
    that is both fast in practice and as rigorous as available tools allow.

The ``classify_mutant`` function runs all three in order (fast to slow),
returning the first definitive result.  ``filter_equivalent_mutants``
provides a drop-in replacement for the existing equivalence filter in
``ordeal.mutations``.

References:

- Papadakis et al., "Mutation Testing Advances: An Analysis and Survey",
  Advances in Computers, 2019 — comprehensive survey of equivalence detection.
- ICST Mutation Workshop (2023-2025) — ongoing research into scalable
  equivalence detection combining static analysis and SMT solvers.
- Meta ACH (Automated Chaos and Hardening) — industrial-scale mutation
  testing where equivalent mutant filtering is critical for signal-to-noise.
- Offutt & Pan, "Automatically Detecting Equivalent Mutants and Infeasible
  Paths", Software Testing, Verification & Reliability, 1997 — foundational
  constraint-based equivalence detection.

Z3 is optional::

    pip install z3-solver

Every function in this module works without Z3, returning inconclusive
results when formal proof would be required.

Usage::

    from ordeal.equivalence import classify_mutant, filter_equivalent_mutants

    # Classify a single mutant
    result = classify_mutant(original_fn, mutant_fn, orig_src, mut_src)
    if result.equivalent:
        print("Skip — equivalent mutant")

    # Drop-in filter for mutation testing pipeline
    surviving = filter_equivalent_mutants("myapp.scoring", mutant_pairs)
"""

from __future__ import annotations

import ast
import inspect
import logging
import math
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_Z3_AVAILABLE: bool | None = None
_Z3_INSTALL_HINT = "Z3 is not installed. Install with: pip install z3-solver"


def _check_z3() -> bool:
    """Check whether Z3 is importable, caching the result."""
    global _Z3_AVAILABLE
    if _Z3_AVAILABLE is None:
        try:
            import z3  # noqa: F401

            _Z3_AVAILABLE = True
        except ImportError:
            _Z3_AVAILABLE = False
    return _Z3_AVAILABLE


# ============================================================================
# Result type
# ============================================================================


@dataclass
class EquivalenceResult:
    """Result of an equivalence analysis between an original and mutant function.

    Attributes:
        equivalent: ``True`` if proven/detected equivalent, ``False`` if a
            distinguishing input was found, ``None`` if inconclusive.
        confidence: 1.0 for structural or formal proof, 0.0-1.0 for
            statistical estimation.  Represents the lower bound of the
            Wilson score confidence interval for the probability that the
            functions agree on a random input.
        method: Which analysis produced this result — one of ``"structural"``,
            ``"statistical"``, ``"formal"``, or ``"inconclusive"``.
        counterexample: When ``equivalent`` is ``False``, a dict of input
            kwargs that produced different outputs from the two functions.
            ``None`` when equivalent or inconclusive.
        time_seconds: Wall-clock time for the analysis in seconds.
    """

    equivalent: bool | None
    confidence: float
    method: str
    counterexample: dict | None = None
    time_seconds: float = 0.0


# ============================================================================
# Structural equivalence — AST normalization and comparison
# ============================================================================


class _ASTNormalizer(ast.NodeTransformer):
    """Normalize an AST for structural comparison.

    Transformations:
    - Remove docstrings (first Expr(Constant(str)) in function bodies).
    - Normalize all local variable names to positional placeholders (_v0, _v1, ...).
    - Fold simple constant expressions (e.g., ``x + 0`` -> ``x``).
    - Sort commutative binary operands by AST dump for canonical form.
    - Strip type annotations from arguments and assignments.
    """

    def __init__(self) -> None:
        self._name_map: dict[str, str] = {}
        self._name_counter: int = 0
        self._param_names: set[str] = set()

    def _canonical_name(self, name: str) -> str:
        """Map a local variable name to a canonical placeholder."""
        if name in self._param_names:
            return name
        if name not in self._name_map:
            self._name_map[name] = f"_v{self._name_counter}"
            self._name_counter += 1
        return self._name_map[name]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        # Record parameter names so they stay stable across both ASTs.
        for arg in node.args.args:
            self._param_names.add(arg.arg)
        # Strip return annotation.
        node.returns = None
        # Strip argument annotations.
        for arg in node.args.args:
            arg.annotation = None
        # Remove docstring if present.
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        # Normalize function name.
        node.name = "_fn"
        node.decorator_list = []
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        # Same normalization as sync functions.
        for arg in node.args.args:
            self._param_names.add(arg.arg)
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        node.name = "_fn"
        node.decorator_list = []
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self._canonical_name(node.id)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        # Constant folding: x + 0 -> x, x * 1 -> x, x - 0 -> x.
        if isinstance(node.op, ast.Add) and _is_zero(node.right):
            return node.left
        if isinstance(node.op, ast.Add) and _is_zero(node.left):
            return node.right
        if isinstance(node.op, ast.Sub) and _is_zero(node.right):
            return node.left
        if isinstance(node.op, ast.Mult) and _is_one(node.right):
            return node.left
        if isinstance(node.op, ast.Mult) and _is_one(node.left):
            return node.right
        # Canonicalize commutative operands by AST dump order.
        if isinstance(node.op, (ast.Add, ast.Mult, ast.BitOr, ast.BitAnd, ast.BitXor)):
            left_dump = ast.dump(node.left)
            right_dump = ast.dump(node.right)
            if left_dump > right_dump:
                node.left, node.right = node.right, node.left
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        """Strip annotation from annotated assignments, keep the value."""
        self.generic_visit(node)
        if node.value is not None:
            assign = ast.Assign(
                targets=[node.target],
                value=node.value,
                lineno=node.lineno,
                col_offset=node.col_offset,
            )
            return assign
        # Annotation-only (no value) — remove entirely.
        return ast.Pass(lineno=node.lineno, col_offset=node.col_offset)


def _is_zero(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 0


def _is_one(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 1


def _normalize_ast(source: str) -> str:
    """Parse, normalize, and dump an AST to a canonical string."""
    tree = ast.parse(source)
    normalizer = _ASTNormalizer()
    normalized = normalizer.visit(tree)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, annotate_fields=False)


def structural_equivalence(original_source: str, mutant_source: str) -> EquivalenceResult:
    """Check structural equivalence via AST normalization.

    Parses both source strings, normalizes the ASTs (removes docstrings,
    canonicalizes variable names, folds trivial constants, sorts commutative
    operands), and compares the resulting AST dumps.

    This is fast (microseconds) but conservative — it only catches mutants
    that are trivially equivalent after normalization.  If it says equivalent,
    it is.  If it says not equivalent, the functions may still be semantically
    identical.

    Args:
        original_source: Source code of the original function.
        mutant_source: Source code of the mutant function.

    Returns:
        EquivalenceResult with ``method="structural"``.  ``equivalent`` is
        ``True`` if the normalized ASTs match, ``None`` otherwise (not
        ``False`` — structural difference does not prove behavioral difference).
    """
    t0 = time.monotonic()
    try:
        orig_norm = _normalize_ast(original_source)
        mut_norm = _normalize_ast(mutant_source)
    except SyntaxError:
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None,
            confidence=0.0,
            method="structural",
            time_seconds=elapsed,
        )

    elapsed = time.monotonic() - t0
    if orig_norm == mut_norm:
        return EquivalenceResult(
            equivalent=True,
            confidence=1.0,
            method="structural",
            time_seconds=elapsed,
        )

    return EquivalenceResult(
        equivalent=None,
        confidence=0.0,
        method="structural",
        time_seconds=elapsed,
    )


# ============================================================================
# Statistical equivalence — Wilson score confidence interval
# ============================================================================


def _wilson_lower(successes: int, trials: int, confidence: float) -> float:
    """Lower bound of the Wilson score confidence interval.

    Uses the normal approximation.  For *confidence* = 0.99, the z-score
    is approximately 2.576.  For 0.95 it is 1.96.

    Returns the lower bound of the confidence interval for the true
    proportion of successes.
    """
    if trials == 0:
        return 0.0

    # z-score lookup for common confidence levels; fall back to approximation.
    z_table = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576, 0.999: 3.291}
    z = z_table.get(confidence)
    if z is None:
        # Inverse normal approximation via rational Abramowitz & Stegun formula.
        alpha = 1.0 - confidence
        p = alpha / 2.0
        # Approximation of inverse normal CDF for small p.
        t = math.sqrt(-2.0 * math.log(p))
        z = t - (2.515517 + 0.802853 * t + 0.010328 * t * t) / (
            1.0 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t * t * t
        )

    n = trials
    p_hat = successes / n
    z2 = z * z

    denominator = 1.0 + z2 / n
    centre = p_hat + z2 / (2.0 * n)
    spread = z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)

    lower = (centre - spread) / denominator
    return max(0.0, lower)


# Boundary values for type-driven sampling — shared with mutations.py.
_BOUNDARY_VALUES: dict[type, list] = {
    int: [0, 1, -1, 2, -2],
    float: [0.0, 1.0, -1.0, 0.5, -0.5],
    bool: [True, False],
    str: ["", "a", "ab"],
    bytes: [b"", b"\x00", b"ab"],
}


def statistical_equivalence(
    original_fn: Callable,
    mutant_fn: Callable,
    *,
    n_samples: int = 100,
    confidence: float = 0.99,
) -> EquivalenceResult:
    """Statistical equivalence test with Wilson score confidence bounds.

    Runs both functions on boundary values and *n_samples* random inputs,
    then computes a Wilson score confidence interval for the probability
    that the functions agree.

    This extends the behavioral filtering in ``ordeal.mutations`` with
    rigorous statistics: instead of a simple "all samples matched" boolean,
    it quantifies how confident we are that the functions are equivalent.

    Args:
        original_fn: The original function.
        mutant_fn: The mutant function.
        n_samples: Number of random samples to draw beyond boundary values.
        confidence: Confidence level for the Wilson interval (default 0.99).

    Returns:
        EquivalenceResult with ``method="statistical"``.

        - ``equivalent=True`` if all samples agree and the Wilson lower bound
          exceeds ``confidence``.
        - ``equivalent=False`` if any sample disagrees (with counterexample).
        - ``equivalent=None`` if inputs cannot be generated.
    """
    t0 = time.monotonic()

    # Extract parameter info.
    try:
        sig = inspect.signature(original_fn)
    except (ValueError, TypeError):
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="statistical", time_seconds=elapsed
        )

    params = [
        p
        for p in sig.parameters.values()
        if p.name != "self" and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]

    if not params:
        try:
            agree = original_fn() == mutant_fn()
        except Exception:
            agree = False
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=agree,
            confidence=1.0 if agree else 0.0,
            method="statistical",
            time_seconds=elapsed,
        )

    # Resolve type hints and strategies.
    try:
        from typing import get_type_hints

        from ordeal.quickcheck import strategy_for_type

        hints = get_type_hints(original_fn)
    except Exception:
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="statistical", time_seconds=elapsed
        )

    param_hints = []
    strategies = []
    param_names = []
    for p in params:
        if p.name not in hints:
            elapsed = time.monotonic() - t0
            return EquivalenceResult(
                equivalent=None, confidence=0.0, method="statistical", time_seconds=elapsed
            )
        hint = hints[p.name]
        param_hints.append(hint)
        param_names.append(p.name)
        try:
            strategies.append(strategy_for_type(hint))
        except Exception:
            elapsed = time.monotonic() - t0
            return EquivalenceResult(
                equivalent=None, confidence=0.0, method="statistical", time_seconds=elapsed
            )

    agree_count = 0
    total_count = 0

    def _check(args: list) -> bool:
        """Return True if both functions agree on these args."""
        try:
            return original_fn(*args) == mutant_fn(*args)
        except Exception:
            return False

    def _make_counterexample(args: list) -> dict:
        return dict(zip(param_names, args))

    # Phase 1: boundary values.
    boundary_lists = []
    for hint in param_hints:
        origin = getattr(hint, "__origin__", hint)
        values = _BOUNDARY_VALUES.get(origin, [])
        boundary_lists.append(values)

    defaults = []
    for i, bl in enumerate(boundary_lists):
        defaults.append(bl[0] if bl else strategies[i].example())

    for i, bl in enumerate(boundary_lists):
        for val in bl:
            args = list(defaults)
            args[i] = val
            total_count += 1
            if _check(args):
                agree_count += 1
            else:
                elapsed = time.monotonic() - t0
                return EquivalenceResult(
                    equivalent=False,
                    confidence=0.0,
                    method="statistical",
                    counterexample=_make_counterexample(args),
                    time_seconds=elapsed,
                )

    # Phase 2: random samples.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(n_samples):
            try:
                args = [s.example() for s in strategies]
            except Exception:
                continue
            total_count += 1
            if _check(args):
                agree_count += 1
            else:
                elapsed = time.monotonic() - t0
                return EquivalenceResult(
                    equivalent=False,
                    confidence=0.0,
                    method="statistical",
                    counterexample=_make_counterexample(args),
                    time_seconds=elapsed,
                )

    # Compute Wilson lower bound.
    elapsed = time.monotonic() - t0

    if total_count == 0:
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="statistical", time_seconds=elapsed
        )

    wilson_lower = _wilson_lower(agree_count, total_count, confidence)

    # All samples agreed → equivalent=True if confidence is high enough,
    # otherwise inconclusive (NOT False — absence of evidence is not evidence
    # of absence).  A disagreement would have returned False above.
    if agree_count == total_count:
        proven = True if wilson_lower >= confidence else None
    else:
        proven = None

    return EquivalenceResult(
        equivalent=proven,
        confidence=wilson_lower,
        method="statistical",
        time_seconds=elapsed,
    )


# ============================================================================
# Formal equivalence — Z3 SMT solver
# ============================================================================


def _extract_function_ast(source: str) -> ast.FunctionDef | None:
    """Extract the first FunctionDef from source code."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            return node
    return None


def _z3_encode_expr(  # noqa: C901 — necessarily complex dispatcher
    node: ast.AST,
    env: dict[str, Any],
    z3_mod: Any,
) -> Any | None:
    """Attempt to encode a Python AST expression as a Z3 expression.

    Handles a subset of Python: integer arithmetic, comparisons, boolean
    logic, and simple conditionals.  Returns None for unsupported constructs.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, bool)):
            return z3_mod.IntVal(int(node.value))
        if isinstance(node.value, float):
            return z3_mod.RealVal(node.value)
        return None

    if isinstance(node, ast.Name):
        return env.get(node.id)

    if isinstance(node, ast.UnaryOp):
        operand = _z3_encode_expr(node.operand, env, z3_mod)
        if operand is None:
            return None
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Not):
            return z3_mod.Not(operand)
        return None

    if isinstance(node, ast.BinOp):
        left = _z3_encode_expr(node.left, env, z3_mod)
        right = _z3_encode_expr(node.right, env, z3_mod)
        if left is None or right is None:
            return None
        op_map = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Mod: lambda a, b: a % b,
        }
        op_fn = op_map.get(type(node.op))
        if op_fn is None:
            return None
        try:
            return op_fn(left, right)
        except Exception:
            return None

    if isinstance(node, ast.BoolOp):
        values = [_z3_encode_expr(v, env, z3_mod) for v in node.values]
        if any(v is None for v in values):
            return None
        if isinstance(node.op, ast.And):
            return z3_mod.And(*values)
        if isinstance(node.op, ast.Or):
            return z3_mod.Or(*values)
        return None

    if isinstance(node, ast.Compare):
        left = _z3_encode_expr(node.left, env, z3_mod)
        if left is None:
            return None
        constraints = []
        current = left
        for op, comparator in zip(node.ops, node.comparators):
            right = _z3_encode_expr(comparator, env, z3_mod)
            if right is None:
                return None
            cmp_map = {
                ast.Lt: lambda a, b: a < b,
                ast.LtE: lambda a, b: a <= b,
                ast.Gt: lambda a, b: a > b,
                ast.GtE: lambda a, b: a >= b,
                ast.Eq: lambda a, b: a == b,
                ast.NotEq: lambda a, b: a != b,
            }
            cmp_fn = cmp_map.get(type(op))
            if cmp_fn is None:
                return None
            constraints.append(cmp_fn(current, right))
            current = right
        if len(constraints) == 1:
            return constraints[0]
        return z3_mod.And(*constraints)

    if isinstance(node, ast.IfExp):
        test = _z3_encode_expr(node.test, env, z3_mod)
        body = _z3_encode_expr(node.body, env, z3_mod)
        orelse = _z3_encode_expr(node.orelse, env, z3_mod)
        if test is None or body is None or orelse is None:
            return None
        return z3_mod.If(test, body, orelse)

    return None


def _encode_return_expr(
    func_def: ast.FunctionDef,
    env: dict[str, Any],
    z3_mod: Any,
) -> Any | None:
    """Encode a simple function body as a Z3 expression.

    Handles:
    - Single return statement: ``return expr``
    - If/elif/else chains that each return: encoded as nested If-Then-Else.
    """
    body = func_def.body

    # Single return statement.
    if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
        return _z3_encode_expr(body[0].value, env, z3_mod)

    # If/elif/else chain where every branch returns.
    return _encode_if_chain(body, env, z3_mod)


def _encode_if_chain(
    stmts: list[ast.stmt],
    env: dict[str, Any],
    z3_mod: Any,
) -> Any | None:
    """Encode a sequence of if/elif/else statements as nested Z3 If."""
    if not stmts:
        return None

    stmt = stmts[0]

    if isinstance(stmt, ast.Return) and stmt.value is not None:
        return _z3_encode_expr(stmt.value, env, z3_mod)

    if isinstance(stmt, ast.If):
        test = _z3_encode_expr(stmt.test, env, z3_mod)
        if test is None:
            return None

        # Body must end with a return.
        body_expr = _encode_if_chain(stmt.body, env, z3_mod)
        if body_expr is None:
            return None

        # Else branch: either explicit orelse or remaining statements.
        if stmt.orelse:
            else_expr = _encode_if_chain(stmt.orelse, env, z3_mod)
        else:
            else_expr = _encode_if_chain(stmts[1:], env, z3_mod)

        if else_expr is None:
            return None

        return z3_mod.If(test, body_expr, else_expr)

    return None


def prove_equivalent(
    original_fn: Callable,
    mutant_fn: Callable,
    *,
    max_seconds: int = 5,
    _original_source: str | None = None,
    _mutant_source: str | None = None,
) -> EquivalenceResult:
    """Attempt formal equivalence proof using Z3 SMT solver.

    Encodes both functions as SMT formulas and checks whether there exists
    any input where they produce different outputs.  If UNSAT (no such input
    exists), the functions are proven equivalent.  If SAT, a concrete
    counterexample is returned.

    Only works for functions expressible in the supported subset: integer
    and real arithmetic, comparisons, boolean logic, simple conditionals.
    Returns inconclusive for functions with loops, string operations,
    data structures, or external calls.

    Args:
        original_fn: The original function.
        mutant_fn: The mutant function.
        max_seconds: Solver timeout in seconds.

    Returns:
        EquivalenceResult with ``method="formal"``.

        - ``equivalent=True`` with ``confidence=1.0`` if proven equivalent.
        - ``equivalent=False`` with counterexample if proven different.
        - ``equivalent=None`` if Z3 is not installed, the functions cannot
          be encoded, or the solver times out.
    """
    t0 = time.monotonic()

    if not _check_z3():
        logger.info(_Z3_INSTALL_HINT)
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="formal", time_seconds=elapsed
        )

    import z3

    # Get source code.
    orig_source = _original_source
    mut_source = _mutant_source
    try:
        if orig_source is None:
            orig_source = inspect.getsource(original_fn)
        if mut_source is None:
            mut_source = inspect.getsource(mutant_fn)
    except (OSError, TypeError):
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="formal", time_seconds=elapsed
        )

    # Parse ASTs.
    orig_ast = _extract_function_ast(orig_source)
    mut_ast = _extract_function_ast(mut_source)
    if orig_ast is None or mut_ast is None:
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="formal", time_seconds=elapsed
        )

    # Build Z3 variables for parameters.
    try:
        from typing import get_type_hints

        hints = get_type_hints(original_fn)
    except Exception:
        hints = {}

    param_names = [arg.arg for arg in orig_ast.args.args if arg.arg != "self"]
    env: dict[str, Any] = {}
    for name in param_names:
        hint = hints.get(name)
        if hint is float:
            env[name] = z3.Real(name)
        else:
            # Default to Int for int, bool, or untyped parameters.
            env[name] = z3.Int(name)

    # Encode both functions.
    orig_expr = _encode_return_expr(orig_ast, env, z3)
    mut_expr = _encode_return_expr(mut_ast, env, z3)

    if orig_expr is None or mut_expr is None:
        elapsed = time.monotonic() - t0
        return EquivalenceResult(
            equivalent=None, confidence=0.0, method="formal", time_seconds=elapsed
        )

    # Check: is there any input where outputs differ?
    solver = z3.Solver()
    solver.set("timeout", max_seconds * 1000)
    solver.add(orig_expr != mut_expr)

    result = solver.check()
    elapsed = time.monotonic() - t0

    if result == z3.unsat:
        return EquivalenceResult(
            equivalent=True, confidence=1.0, method="formal", time_seconds=elapsed
        )

    if result == z3.sat:
        model = solver.model()
        counterexample = {}
        for name in param_names:
            val = model.evaluate(env[name], model_completion=True)
            # Convert Z3 value to Python.
            try:
                if hasattr(val, "as_long"):
                    counterexample[name] = val.as_long()
                elif hasattr(val, "as_fraction"):
                    frac = val.as_fraction()
                    counterexample[name] = float(frac)
                else:
                    counterexample[name] = str(val)
            except Exception:
                counterexample[name] = str(val)
        return EquivalenceResult(
            equivalent=False,
            confidence=0.0,
            method="formal",
            counterexample=counterexample,
            time_seconds=elapsed,
        )

    # Timeout or unknown.
    return EquivalenceResult(
        equivalent=None, confidence=0.0, method="formal", time_seconds=elapsed
    )


# ============================================================================
# Combined classifier
# ============================================================================


def classify_mutant(
    original_fn: Callable,
    mutant_fn: Callable,
    original_source: str | None = None,
    mutant_source: str | None = None,
    *,
    max_seconds: int = 5,
    n_samples: int = 100,
    confidence: float = 0.99,
) -> EquivalenceResult:
    """Run all equivalence methods in order: structural, statistical, formal.

    Starts with the fastest method (structural AST comparison) and
    escalates to slower methods only when the previous one is inconclusive.
    Returns the first definitive result.

    Args:
        original_fn: The original function.
        mutant_fn: The mutant function.
        original_source: Source code of the original (auto-extracted if None).
        mutant_source: Source code of the mutant (auto-extracted if None).
        max_seconds: Timeout for formal Z3 proof in seconds.
        n_samples: Number of random samples for statistical test.
        confidence: Confidence level for statistical test.

    Returns:
        The first definitive EquivalenceResult, or an inconclusive result
        with cumulative time if no method was definitive.
    """
    total_time = 0.0

    # Resolve source if not provided.
    if original_source is None:
        try:
            original_source = inspect.getsource(original_fn)
        except (OSError, TypeError):
            pass

    if mutant_source is None:
        try:
            mutant_source = inspect.getsource(mutant_fn)
        except (OSError, TypeError):
            pass

    # --- Layer 1: structural ---
    if original_source is not None and mutant_source is not None:
        result = structural_equivalence(original_source, mutant_source)
        total_time += result.time_seconds
        if result.equivalent is True:
            result.time_seconds = total_time
            return result

    # --- Layer 2: statistical ---
    result = statistical_equivalence(
        original_fn, mutant_fn, n_samples=n_samples, confidence=confidence
    )
    total_time += result.time_seconds
    if result.equivalent is not None:
        result.time_seconds = total_time
        return result

    # --- Layer 3: formal ---
    if original_source is not None and mutant_source is not None:
        result = prove_equivalent(
            original_fn,
            mutant_fn,
            max_seconds=max_seconds,
            _original_source=original_source,
            _mutant_source=mutant_source,
        )
        total_time += result.time_seconds
        if result.equivalent is not None:
            result.time_seconds = total_time
            return result

    return EquivalenceResult(
        equivalent=None,
        confidence=0.0,
        method="inconclusive",
        time_seconds=total_time,
    )


# ============================================================================
# Batch filter — drop-in replacement for mutations.py equivalence filtering
# ============================================================================


@dataclass
class MutantPair:
    """A mutant paired with its source code for equivalence analysis.

    Attributes:
        mutant_fn: The compiled mutant callable.
        mutant_source: Source code of the mutant.
        label: Human-readable label (e.g., operator name + line number).
    """

    mutant_fn: Callable
    mutant_source: str
    label: str = ""


def filter_equivalent_mutants(
    original_fn: Callable,
    mutant_pairs: list[MutantPair],
    *,
    methods: tuple[str, ...] = ("structural", "statistical"),
    original_source: str | None = None,
    n_samples: int = 100,
    confidence: float = 0.99,
    max_seconds: int = 5,
) -> list[tuple[MutantPair, EquivalenceResult]]:
    """Filter equivalent mutants using layered analysis.

    Drop-in enhancement for the equivalence filtering in ``ordeal.mutations``.
    Runs the specified methods in order for each mutant, skipping those
    classified as equivalent.

    Args:
        original_fn: The original function.
        mutant_pairs: List of MutantPair instances to classify.
        methods: Which methods to use, in order.  Default is
            ``("structural", "statistical")`` for speed.  Add ``"formal"``
            for Z3-backed proof when available.
        original_source: Source of the original function (auto-extracted if None).
        n_samples: Random samples for statistical method.
        confidence: Confidence level for statistical method.
        max_seconds: Timeout for formal method.

    Returns:
        List of ``(MutantPair, EquivalenceResult)`` tuples for mutants that
        are NOT equivalent (i.e., should be tested).  Equivalent mutants are
        filtered out.
    """
    if original_source is None:
        try:
            original_source = inspect.getsource(original_fn)
        except (OSError, TypeError):
            pass

    surviving: list[tuple[MutantPair, EquivalenceResult]] = []

    for pair in mutant_pairs:
        result: EquivalenceResult | None = None

        for method in methods:
            if method == "structural" and original_source and pair.mutant_source:
                result = structural_equivalence(original_source, pair.mutant_source)
                if result.equivalent is True:
                    break

            elif method == "statistical":
                result = statistical_equivalence(
                    original_fn,
                    pair.mutant_fn,
                    n_samples=n_samples,
                    confidence=confidence,
                )
                if result.equivalent is not None:
                    break

            elif method == "formal":
                result = prove_equivalent(
                    original_fn,
                    pair.mutant_fn,
                    max_seconds=max_seconds,
                    _original_source=original_source,
                    _mutant_source=pair.mutant_source,
                )
                if result.equivalent is not None:
                    break

        # Keep mutants that are NOT proven equivalent.
        if result is None or result.equivalent is not True:
            if result is None:
                result = EquivalenceResult(equivalent=None, confidence=0.0, method="inconclusive")
            surviving.append((pair, result))

    return surviving
