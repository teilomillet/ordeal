from __future__ import annotations
# ruff: noqa
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
    "thorough": [
        "arithmetic",
        "comparison",
        "negate",
        "return_none",
        "boundary",
        "constant",
        "logical",
        "delete_statement",
        *[
            op
            for op in OPERATORS.keys()
            if op
            not in {
                "arithmetic",
                "comparison",
                "negate",
                "return_none",
                "boundary",
                "constant",
                "logical",
                "delete_statement",
            }
        ],
    ],
}
def _default_operator_order() -> list[str]:
    """Return the default operator order for progressive mutation testing."""
    return list(PRESETS["thorough"])
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
def _code_fingerprint(code: types.CodeType) -> tuple[Any, ...]:
    """Return a location-independent semantic fingerprint for *code*."""
    constants = tuple(
        ("code", _code_fingerprint(value))
        if isinstance(value, types.CodeType)
        else (type(value).__qualname__, repr(value))
        for value in code.co_consts
    )
    return (
        code.co_code,
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        constants,
    )
