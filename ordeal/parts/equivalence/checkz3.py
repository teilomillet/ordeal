from __future__ import annotations
# ruff: noqa
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
