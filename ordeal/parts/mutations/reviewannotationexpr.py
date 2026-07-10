from __future__ import annotations
# ruff: noqa
def _review_annotation_expr(tp: object, *, current_module: str) -> str | None:
    """Render an annotation for review, keeping module names explicit."""
    if isinstance(tp, str):
        return tp
    if tp is type(None):
        return "None"
    if tp is Any:
        return "Any"

    origin = get_origin(tp)
    if origin is Literal:
        return f"Literal[{', '.join(repr(arg) for arg in get_args(tp))}]"

    if origin is Union or (hasattr(types, "UnionType") and origin is types.UnionType):
        parts = []
        for arg in get_args(tp):
            part = _review_annotation_expr(arg, current_module=current_module)
            if part is None:
                return None
            parts.append(part)
        return " | ".join(parts)

    if origin in {list, set, frozenset}:
        args = get_args(tp)
        if len(args) != 1:
            return origin.__name__
        inner = _review_annotation_expr(args[0], current_module=current_module)
        if inner is None:
            return None
        return f"{origin.__name__}[{inner}]"

    if origin is dict:
        args = get_args(tp)
        if len(args) != 2:
            return "dict"
        key = _review_annotation_expr(args[0], current_module=current_module)
        value = _review_annotation_expr(args[1], current_module=current_module)
        if key is None or value is None:
            return None
        return f"dict[{key}, {value}]"

    if origin is tuple:
        rendered: list[str] = []
        for arg in get_args(tp):
            if arg is Ellipsis:
                rendered.append("...")
                continue
            part = _review_annotation_expr(arg, current_module=current_module)
            if part is None:
                return None
            rendered.append(part)
        return f"tuple[{', '.join(rendered)}]"

    if origin is not None:
        origin_expr = _review_annotation_expr(origin, current_module=current_module)
        if origin_expr is None:
            return None
        rendered_args: list[str] = []
        for arg in get_args(tp):
            part = _review_annotation_expr(arg, current_module=current_module)
            if part is None:
                return None
            rendered_args.append(part)
        if not rendered_args:
            return origin_expr
        return f"{origin_expr}[{', '.join(rendered_args)}]"

    module = getattr(tp, "__module__", None)
    qualname = getattr(tp, "__qualname__", None) or getattr(tp, "__name__", None)
    if qualname is None:
        return None
    if module in {None, "builtins"}:
        return qualname
    return f"{module}.{qualname}"
def _review_signature(target: str) -> str:
    """Render a function signature with explicit module qualification."""
    try:
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return "(...)"
        func = _resolved_target_callable(target_spec)
        sig = inspect.signature(func)
        hints = safe_get_annotations(func)
    except Exception:
        return "(...)"

    rendered: list[str] = []
    current_module = getattr(func, "__module__", "")
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        piece = name
        if name in hints:
            ann = _review_annotation_expr(
                hints[name],
                current_module=current_module,
            )
            if ann is not None:
                piece += f": {ann}"
        if param.default is not inspect.Parameter.empty:
            piece += f" = {param.default!r}"
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            piece = f"*{piece}"
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            piece = f"**{piece}"
        elif param.kind is inspect.Parameter.KEYWORD_ONLY and not rendered:
            piece = f"*, {piece}"
        rendered.append(piece)

    sig_str = f"({', '.join(rendered)})"
    ret = hints.get("return")
    if ret is not None:
        ann = _review_annotation_expr(ret, current_module=current_module)
        if ann is not None:
            sig_str += f" -> {ann}"
    qual_parts: list[str] = []
    if target_spec.qualname_parts:
        qual_parts.append(target_spec.module_name)
    qualname = ".".join([*qual_parts, *target_spec.qualname_parts, target_spec.leaf_name])
    return f"{qualname}{sig_str}"
def _comment_lines(text: str, *, indent: str = "    # ") -> list[str]:
    """Render multiline text as review comments."""
    return [f"{indent}{line}" if line else indent.rstrip() for line in text.splitlines()]
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

    Args:
        target: Dotted path to a function (``"myapp.scoring.compute"``)
            or module (``"myapp.scoring"``).
        dry_run: If ``True``, no imports or execution — AST-only stubs.

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
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return _CallResult(error="cannot resolve", error_type="ImportError")
        func = _resolved_target_callable(target_spec)
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
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return None
        func = _resolved_target_callable(target_spec)

        from ordeal.mine import mine

        hints = safe_get_annotations(func)
        is_void = annotation_is_none(hints.get("return"))

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

        module_path = target_spec.module_name
        if target_spec.qualname_parts:
            call_target = ".".join([*target_spec.qualname_parts, func_name])
        else:
            call_target = func_name
        param_sig, param_imports = _param_sig(target)
        needs_math = any("math." in a for a in assertions)

        lines: list[str] = []
        lines.append("")
        lines.append(f"def test_{safe_name}_properties():")
        lines.append(f'    """Discovered: {", ".join(doc_parts)}."""')
        if needs_math:
            lines.append("    import math")
        lines.append("    from ordeal.quickcheck import quickcheck")
        for import_line in param_imports:
            lines.append(f"    {import_line}")
        lines.append(f"    import {module_path} as _ordeal_target")
        lines.append("")
        lines.append("    @quickcheck")
        lines.append(f"    def check({param_sig}):")
        lines.append(f"        result = _ordeal_target.{call_target}({param_call})")
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
def _annotation_expr(
    tp: object,
    *,
    imports: set[str],
) -> str | None:
    """Render a type annotation and collect the imports it needs."""
    if isinstance(tp, str):
        return tp
    if tp is type(None):
        return "None"
    if tp is Any:
        imports.add("from typing import Any")
        return "Any"

    origin = get_origin(tp)
    if origin is Literal:
        imports.add("from typing import Literal")
        return f"Literal[{', '.join(repr(arg) for arg in get_args(tp))}]"

    if origin is Union or origin is types.UnionType:
        parts = []
        for arg in get_args(tp):
            rendered = _annotation_expr(arg, imports=imports)
            if rendered is None:
                return None
            parts.append(rendered)
        return " | ".join(parts)

    if origin in {list, set, frozenset}:
        args = get_args(tp)
        if len(args) != 1:
            return origin.__name__
        inner = _annotation_expr(args[0], imports=imports)
        if inner is None:
            return None
        return f"{origin.__name__}[{inner}]"

    if origin is dict:
        args = get_args(tp)
        if len(args) != 2:
            return "dict"
        key = _annotation_expr(args[0], imports=imports)
        value = _annotation_expr(args[1], imports=imports)
        if key is None or value is None:
            return None
        return f"dict[{key}, {value}]"

    if origin is tuple:
        rendered_parts: list[str] = []
        for arg in get_args(tp):
            if arg is Ellipsis:
                rendered_parts.append("...")
                continue
            rendered = _annotation_expr(arg, imports=imports)
            if rendered is None:
                return None
            rendered_parts.append(rendered)
        return f"tuple[{', '.join(rendered_parts)}]"

    if origin is not None:
        origin_expr = _annotation_expr(origin, imports=imports)
        if origin_expr is None:
            return None
        rendered_args: list[str] = []
        for arg in get_args(tp):
            rendered = _annotation_expr(arg, imports=imports)
            if rendered is None:
                return None
            rendered_args.append(rendered)
        if not rendered_args:
            return origin_expr
        return f"{origin_expr}[{', '.join(rendered_args)}]"

    module = getattr(tp, "__module__", None)
    qualname = getattr(tp, "__qualname__", None) or getattr(tp, "__name__", None)
    if qualname is None:
        return None
    if module in {None, "builtins"}:
        return qualname
    imports.add(f"import {module}")
    return f"{module}.{qualname}"
def _param_sig(target: str) -> tuple[str, list[str]]:
    """Build a parameter signature for a quickcheck function."""
    try:
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return "...", []
        func = _resolved_target_callable(target_spec)
        sig = inspect.signature(func)
        annotations = safe_get_annotations(func)
        imports: set[str] = set()
        params = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            ann = annotations.get(name, p.annotation)
            if ann is not inspect.Parameter.empty:
                ann_name = _annotation_expr(ann, imports=imports)
                if ann_name is None:
                    params.append(name)
                else:
                    params.append(f"{name}: {ann_name}")
            else:
                params.append(name)
        return ", ".join(params), sorted(imports)
    except Exception:
        return "...", []
def _param_call(target: str) -> str:
    """Build a call expression using parameter names."""
    try:
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return "..."
        func = _resolved_target_callable(target_spec)
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
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return False
        func = _resolved_target_callable(target_spec)
        hints = safe_get_annotations(func)
        ret = hints.get("return", _SENTINEL)
        return annotation_is_none(ret)
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
