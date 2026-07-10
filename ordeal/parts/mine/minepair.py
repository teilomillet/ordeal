from __future__ import annotations
# ruff: noqa
def mine_pair(
    f: Callable[..., Any],
    g: Callable[..., Any],
    *,
    max_examples: int = 200,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> MineResult:
    """Discover relational properties between two functions.

    Checks roundtrip (``g(f(x)) == x``), the reverse (``f(g(x)) == x``),
    and commutative composition (``f(g(x)) == g(f(x))``).  Strategies
    are inferred from *f*'s signature.

    Example::

        result = mine_pair(json.dumps, json.loads)
        # discovers: roundtrip g(f(x)) == x

        result = mine_pair(encode, decode)
        # discovers: roundtrip g(f(x)) == x, roundtrip f(g(x)) == x
    """
    import inspect

    from ordeal.auto import _unwrap

    f = _unwrap(f)
    g = _unwrap(g)

    # Normalize fixtures
    normalized: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized = {}
        for k, v in fixtures.items():
            normalized[k] = v if isinstance(v, st.SearchStrategy) else st.just(v)

    strategies = _infer_strategies(f, normalized)
    if strategies is None:
        fname = getattr(f, "__name__", str(f))
        raise ValueError(f"Cannot infer strategies for {fname}.")

    # Get first param name for feeding output back
    sig_f = inspect.signature(f)
    params_f = [n for n in sig_f.parameters if n not in ("self", "cls")]
    sig_g = inspect.signature(g)
    params_g = [n for n in sig_g.parameters if n not in ("self", "cls")]
    first_f = params_f[0] if params_f else None
    first_g = params_g[0] if params_g else None

    # Collect inputs and outputs of f
    inputs: list[dict[str, Any]] = []
    outputs_f: list[Any] = []

    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None, derandomize=True)
        def collect(**kwargs: Any) -> None:
            out = f(**kwargs)
            outputs_f.append(out)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    fname = getattr(f, "__name__", str(f))
    gname = getattr(g, "__name__", str(g))
    pair_name = f"{fname} <-> {gname}"
    cap = min(len(inputs), 50)

    # -- roundtrip: g(f(x)) == x --
    rt_holds = rt_total = 0
    for kwargs, out_f in zip(inputs[:cap], outputs_f[:cap]):
        if out_f is None or first_g is None:
            continue
        try:
            back = g(**{first_g: out_f})
            rt_total += 1
            if _approx_equal(back, kwargs[first_f]):
                rt_holds += 1
        except Exception:
            pass

    # -- reverse roundtrip: f(g(x)) == x --
    # Generate inputs for g, then feed through f
    rr_holds = rr_total = 0
    strategies_g = _infer_strategies(g, normalized)
    if strategies_g and first_f and first_g:
        g_inputs: list[dict[str, Any]] = []
        g_outputs: list[Any] = []
        try:

            @given(**strategies_g)
            @settings(max_examples=max_examples, database=None, derandomize=True)
            def collect_g(**kwargs: Any) -> None:
                out = g(**kwargs)
                g_outputs.append(out)
                g_inputs.append(dict(kwargs))

            collect_g()
        except Exception:
            pass

        for kwargs_g, out_g in zip(g_inputs[:cap], g_outputs[:cap]):
            if out_g is None:
                continue
            try:
                back = f(**{first_f: out_g})
                rr_total += 1
                if _approx_equal(back, kwargs_g[first_g]):
                    rr_holds += 1
            except Exception:
                pass

    # -- commutative composition: f(g(x)) == g(f(x)) --
    cc_holds = cc_total = 0
    if first_f and first_g:
        for kwargs, out_f in zip(inputs[:cap], outputs_f[:cap]):
            if out_f is None:
                continue
            try:
                fg = g(**{first_g: out_f})  # g(f(x))
                gx = g(**{first_g: kwargs[first_f]})  # g(x)
                gf = f(**{first_f: gx})  # f(g(x))
                cc_total += 1
                if _approx_equal(fg, gf):
                    cc_holds += 1
            except Exception:
                pass

    all_props = [
        MinedProperty(f"roundtrip {gname}({fname}(x)) == x", rt_holds, rt_total),
        MinedProperty(f"roundtrip {fname}({gname}(x)) == x", rr_holds, rr_total),
        MinedProperty("commutative composition", cc_holds, cc_total),
    ]

    props = [p for p in all_props if p.total > 0]
    not_applicable = [p.name for p in all_props if p.total == 0]

    return MineResult(
        function=pair_name,
        examples=max(len(inputs), 0),
        properties=props,
        not_applicable=not_applicable,
    )
# ============================================================================
# Cross-function mining
# ============================================================================


def _return_type(fn: Callable[..., Any]) -> type | None:
    """Extract the return type annotation from a function, or None if absent."""
    hints = safe_get_annotations(fn)
    return hints.get("return")
def _first_param_type(fn: Callable[..., Any]) -> tuple[str | None, type | None]:
    """Return (name, type) of the first non-self/cls parameter, or (None, None)."""
    hints = safe_get_annotations(fn)
    sig = inspect.signature(fn)
    for name, _param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        return name, hints.get(name)
    return None, None
def _types_compatible(source_type: type | None, target_type: type | None) -> bool:
    """Check whether *source_type* can plausibly be fed into *target_type*.

    Returns ``True`` when the types are identical, when the target is a
    supertype, or when both are the same generic origin (e.g. both
    ``list[...]``).  Returns ``False`` when either is ``None`` (unknown)
    to avoid false positives from untyped code.
    """
    if source_type is None or target_type is None:
        return False
    # Exact match
    if source_type is target_type:
        return True
    # Unwrap Optional / Union — check any branch
    import types as pytypes

    src_origin = get_origin(source_type)
    tgt_origin = get_origin(target_type)
    is_tgt_union = tgt_origin is type(int | str) or (
        hasattr(pytypes, "UnionType") and isinstance(target_type, pytypes.UnionType)
    )
    if is_tgt_union:
        return any(_types_compatible(source_type, a) for a in get_args(target_type))
    # Generic containers — match on origin (list[int] vs list[str] both list)
    if src_origin is not None and tgt_origin is not None:
        return src_origin is tgt_origin
    if src_origin is not None:
        try:
            return issubclass(src_origin, target_type)
        except TypeError:
            return False
    # Plain class inheritance
    try:
        return issubclass(source_type, target_type)
    except TypeError:
        return False
def _check_roundtrip(
    f: Callable[..., Any],
    g: Callable[..., Any],
    fname: str,
    gname: str,
    *,
    max_examples: int = 30,
) -> CrossFunctionProperty | None:
    """Test whether ``g(f(x)) == x`` — the roundtrip property.

    Only attempted when f's return type is compatible with g's first
    parameter type.  Returns ``None`` if the pair is not type-compatible
    or if no examples could be generated.
    """
    ret_f = _return_type(f)
    g_param_name, g_param_type = _first_param_type(g)
    if not _types_compatible(ret_f, g_param_type) or g_param_name is None:
        return None

    strategies = _infer_strategies(f)
    if strategies is None:
        return None

    f_param_name, _f_param_type = _first_param_type(f)
    if f_param_name is None:
        return None

    inputs: list[dict[str, Any]] = []
    outputs_f: list[Any] = []
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None, derandomize=True)
        def collect(**kwargs: Any) -> None:
            out = f(**kwargs)
            outputs_f.append(out)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    if not inputs:
        return None

    holds = total = 0
    counterexample: dict[str, Any] | None = None
    cap = min(len(inputs), max_examples)
    for kwargs, out_f in zip(inputs[:cap], outputs_f[:cap]):
        if out_f is None:
            continue
        try:
            back = g(**{g_param_name: out_f})
            total += 1
            if _approx_equal(back, kwargs[f_param_name]):
                holds += 1
            elif counterexample is None:
                counterexample = {
                    "input": kwargs[f_param_name],
                    f"{fname}_output": out_f,
                    f"{gname}_output": back,
                }
        except Exception:
            pass

    if total == 0:
        return None

    return CrossFunctionProperty(
        function_a=fname,
        function_b=gname,
        relation="roundtrip",
        confidence=holds / total,
        holds=holds,
        total=total,
        counterexample=counterexample,
    )
def _check_composition_commutativity(
    f: Callable[..., Any],
    g: Callable[..., Any],
    fname: str,
    gname: str,
    *,
    max_examples: int = 30,
) -> CrossFunctionProperty | None:
    """Test whether ``f(g(x)) == g(f(x))`` — commutative composition.

    Only attempted when both functions accept the same first-parameter
    type and each function's return type is compatible with the other's
    input.  Returns ``None`` if the pair is not type-compatible or if
    no examples could be generated.
    """
    f_param_name, f_param_type = _first_param_type(f)
    g_param_name, g_param_type = _first_param_type(g)
    ret_f = _return_type(f)
    ret_g = _return_type(g)

    # Both must accept the same type, and each output must feed into the other
    if not _types_compatible(f_param_type, g_param_type):
        return None
    if not _types_compatible(ret_f, g_param_type):
        return None
    if not _types_compatible(ret_g, f_param_type):
        return None
    if f_param_name is None or g_param_name is None:
        return None

    strategies = _infer_strategies(f)
    if strategies is None:
        return None

    inputs: list[dict[str, Any]] = []
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None, derandomize=True)
        def collect(**kwargs: Any) -> None:
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    if not inputs:
        return None

    holds = total = 0
    counterexample: dict[str, Any] | None = None
    cap = min(len(inputs), max_examples)
    for kwargs in inputs[:cap]:
        x = kwargs[f_param_name]
        try:
            fx = f(**{f_param_name: x})
            gx = g(**{g_param_name: x})
            g_of_fx = g(**{g_param_name: fx})  # g(f(x))
            f_of_gx = f(**{f_param_name: gx})  # f(g(x))
            total += 1
            if _approx_equal(g_of_fx, f_of_gx):
                holds += 1
            elif counterexample is None:
                counterexample = {
                    "input": x,
                    f"g({fname}(x))": g_of_fx,
                    f"f({gname}(x))": f_of_gx,
                }
        except Exception:
            pass

    if total == 0:
        return None

    return CrossFunctionProperty(
        function_a=fname,
        function_b=gname,
        relation="commutative_composition",
        confidence=holds / total,
        holds=holds,
        total=total,
        counterexample=counterexample,
    )
def _check_output_equivalence(
    f: Callable[..., Any],
    g: Callable[..., Any],
    fname: str,
    gname: str,
    *,
    max_examples: int = 30,
) -> CrossFunctionProperty | None:
    """Test whether ``f(x) == g(x)`` — output equivalence.

    Only attempted when both functions accept the same parameter types.
    Detects duplicate implementations, reference/optimized pairs, or
    accidental copies.  Returns ``None`` if the pair is not
    type-compatible or if no examples could be generated.
    """
    f_param_name, f_param_type = _first_param_type(f)
    g_param_name, g_param_type = _first_param_type(g)

    if not _types_compatible(f_param_type, g_param_type):
        return None
    if f_param_name is None or g_param_name is None:
        return None

    strategies = _infer_strategies(f)
    if strategies is None:
        return None

    inputs: list[dict[str, Any]] = []
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None, derandomize=True)
        def collect(**kwargs: Any) -> None:
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    if not inputs:
        return None

    holds = total = 0
    counterexample: dict[str, Any] | None = None
    cap = min(len(inputs), max_examples)
    for kwargs in inputs[:cap]:
        x = kwargs[f_param_name]
        try:
            out_f = f(**{f_param_name: x})
            out_g = g(**{g_param_name: x})
            total += 1
            if _approx_equal(out_f, out_g):
                holds += 1
            elif counterexample is None:
                counterexample = {
                    "input": x,
                    f"{fname}_output": out_f,
                    f"{gname}_output": out_g,
                }
        except Exception:
            pass

    if total == 0:
        return None

    return CrossFunctionProperty(
        function_a=fname,
        function_b=gname,
        relation="equivalent",
        confidence=holds / total,
        holds=holds,
        total=total,
        counterexample=counterexample,
    )
