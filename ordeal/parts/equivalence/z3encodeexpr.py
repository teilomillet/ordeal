from __future__ import annotations
# ruff: noqa
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
