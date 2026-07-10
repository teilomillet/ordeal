from __future__ import annotations
# ruff: noqa
# ============================================================================
# 2. fuzz
# ============================================================================


def fuzz(
    fn: Any,
    *,
    max_examples: int = 1000,
    check_return_type: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> FuzzResult:
    """Deep-fuzz a single function with auto-inferred strategies.

    Simple::

        result = fuzz(myapp.scoring.compute)
        assert result.passed

    With fixture overrides (strategies or plain values)::

        result = fuzz(myapp.scoring.compute, model=model_strategy)
        result = fuzz(myapp.scoring.compute, max_tokens=5)  # auto-wrapped

    Args:
        fn: The function to fuzz.
        max_examples: Number of random inputs to try.
        check_return_type: Verify return type annotation.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_scenarios: Optional per-class collaborator scenarios run after setup.
        object_state_factories: Optional per-class state factories for methods that take
            a runtime ``state`` parameter.
        **fixtures: Strategy overrides or plain values (auto-wrapped in st.just).
    """
    fn_name = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    if isinstance(fn, str):
        fn_name, fn = _resolve_explicit_target(
            fn,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
        )

    # Auto-wrap plain values in st.just()
    normalized: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized = {}
        for k, v in fixtures.items():
            if isinstance(v, st.SearchStrategy):
                normalized[k] = v
            else:
                normalized[k] = st.just(v)
    strategies = _infer_strategies(fn, normalized)
    if strategies is None:
        reason = _callable_skip_reason(fn)
        if reason is not None:
            raise ValueError(f"Cannot fuzz {fn_name}: {reason}")
        raise ValueError(
            f"Cannot infer strategies for {fn_name}. Provide fixtures for untyped parameters."
        )

    return_type = safe_get_annotations(fn).get("return")

    failures: list[Exception] = []
    last_kwargs: dict[str, Any] = {}
    try:
        for kwargs in _boundary_smoke_inputs(fn, fixtures=normalized):
            last_kwargs = dict(kwargs)
            result = _call_sync(fn, **dict(kwargs))
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            nonlocal last_kwargs
            last_kwargs = dict(kwargs)
            result = _call_sync(fn, **kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

        test()
    except Exception as e:
        failures.append(e)

    failing_args = last_kwargs if failures and last_kwargs else None
    return FuzzResult(
        function=fn.__qualname__ or fn.__name__,
        examples=max_examples,
        failures=failures,
        failing_args=failing_args,
    )
# ============================================================================
# 3. chaos_for — auto-infer faults + invariants
# ============================================================================

# Patterns in function ASTs that map to specific fault types.
# Keys are (module_attr, func_name) pairs found in ast.Call nodes.
_FAULT_PATTERNS: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
    # pattern → [(fault_module, fault_factory, kwargs), ...]
    "subprocess.run": [
        ("io", "subprocess_timeout", {}),
        ("io", "subprocess_delay", {}),
        ("io", "corrupt_stdout", {}),
    ],
    "subprocess.check_output": [
        ("io", "subprocess_timeout", {}),
    ],
    "subprocess.Popen": [
        ("io", "subprocess_timeout", {}),
    ],
    "open": [
        ("io", "disk_full", {}),
        ("io", "permission_denied", {}),
    ],
}
def _source_bound_subprocess_match(node: ast.Call) -> str | None:
    """Resolve a complete subprocess command from literals and ``sys.executable``."""
    command: ast.AST | None = node.args[0] if node.args else None
    if command is None:
        command = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "args"),
            None,
        )
    if isinstance(command, ast.Constant) and isinstance(command.value, str):
        return command.value.strip() or None
    if not isinstance(command, (ast.List, ast.Tuple)) or not command.elts:
        return None
    tokens: list[str] = []
    for item in command.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value:
            tokens.append(item.value)
            continue
        if (
            isinstance(item, ast.Attribute)
            and item.attr == "executable"
            and isinstance(item.value, ast.Name)
            and item.value.id == "sys"
        ):
            tokens.append(sys.executable)
            continue
        return None
    return " ".join(tokens)
def _ml_data_fault_specs(call_str: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Infer ML/data seam faults from one dotted call target string."""
    parts = [part.lower() for part in call_str.split(".") if part]
    if not parts:
        return []
    leaf = parts[-1]
    has_model_token = bool(
        {
            "model",
            "model_client",
            "predictor",
            "scorer",
            "embedder",
            "encoder",
            "classifier",
            "reranker",
        }
        & set(parts)
    )
    has_feature_token = bool(
        {
            "feature_store",
            "vector_store",
            "embedding_store",
            "retriever",
            "feature_client",
        }
        & set(parts)
    )
    if has_model_token and leaf in {"predict", "infer", "run"}:
        return [
            ("numerical", "nan_injection", {}),
            ("numerical", "partial_batch", {}),
            ("numerical", "dtype_drift", {}),
        ]
    if has_model_token and leaf in {"predict_proba", "transform", "embed", "encode"}:
        return [
            ("numerical", "partial_batch", {}),
            ("numerical", "feature_order_drift", {}),
            ("numerical", "dtype_drift", {}),
        ]
    if has_feature_token and leaf in {
        "get",
        "fetch",
        "lookup",
        "get_features",
        "fetch_features",
        "lookup_features",
    }:
        return [
            ("numerical", "missing_feature", {}),
            ("numerical", "dtype_drift", {}),
        ]
    return []
def _infer_faults(
    mod: ModuleType,
    mod_name: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> list[Fault]:
    """Auto-discover faults by scanning function ASTs for risky calls.

    Detects subprocess, file I/O, and cross-function calls, then
    generates appropriate fault instances.
    """
    import ast
    import textwrap

    faults: list[Fault] = []
    seen: set[tuple[str, str, str, tuple[tuple[str, Any], ...]]] = set()

    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        try:
            source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Extract call target as string (e.g. "subprocess.run", "open")
            call_str = _call_to_string(node)
            if not call_str:
                continue

            # Check against known patterns
            for pattern, fault_specs in _FAULT_PATTERNS.items():
                if pattern not in call_str:
                    continue
                for fault_mod, fault_fn, kwargs in fault_specs:
                    key = (
                        name,
                        fault_mod,
                        fault_fn,
                        tuple(sorted(kwargs.items())),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    fault_module = importlib.import_module(f"ordeal.faults.{fault_mod}")
                    factory = getattr(fault_module, fault_fn)
                    # Faults that need a target get the module name
                    params = inspect.signature(factory).parameters
                    if "target" in params:
                        inferred_fault = factory(f"{mod_name}.{name}", **kwargs)
                    else:
                        inferred_fault = factory(**kwargs)
                    setattr(inferred_fault, "__ordeal_operation__", name)
                    setattr(inferred_fault, "__ordeal_fault_kind__", fault_fn)
                    faults.append(inferred_fault)

            for fault_mod, fault_fn, kwargs in _ml_data_fault_specs(call_str):
                key = (
                    name,
                    fault_mod,
                    fault_fn,
                    tuple(sorted(kwargs.items())),
                )
                if key in seen:
                    continue
                seen.add(key)
                fault_module = importlib.import_module(f"ordeal.faults.{fault_mod}")
                factory = getattr(fault_module, fault_fn)
                params = inspect.signature(factory).parameters
                if "target" in params:
                    inferred_fault = factory(f"{mod_name}.{name}", **kwargs)
                else:
                    inferred_fault = factory(**kwargs)
                setattr(inferred_fault, "__ordeal_operation__", name)
                setattr(inferred_fault, "__ordeal_fault_kind__", fault_fn)
                faults.append(inferred_fault)

            # Cross-function calls → error_on_call
            if (
                call_str.startswith(mod_name + ".")
                and (
                    name,
                    "io",
                    call_str,
                    (),
                )
                not in seen
            ):
                seen.add((name, "io", call_str, ()))
                from ordeal.faults.io import error_on_call

                inferred_fault = error_on_call(call_str)
                setattr(inferred_fault, "__ordeal_operation__", name)
                setattr(inferred_fault, "__ordeal_fault_kind__", "error_on_call")
                faults.append(inferred_fault)

    return faults
def _call_to_string(node: Any) -> str | None:
    """Extract a dotted string from an ast.Call node's func attribute."""
    import ast

    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        current = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None
def _infer_invariants(
    mod: ModuleType,
    fixtures: dict[str, Any] | None,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[dict[str, list[Invariant]], list[Invariant]]:
    """Auto-discover invariants by mining function properties.

    Runs mine() on each function with a small example count,
    then maps universal properties to invariant objects.
    """
    from ordeal.invariants import bounded, finite, no_nan, non_empty
    from ordeal.mine import mine

    # Map mined property names to invariant constructors
    _PROPERTY_TO_INVARIANT: dict[str, Invariant | None] = {
        "no NaN": no_nan,
        "output >= 0": bounded(0, float("inf")),
        "output in [0, 1]": bounded(0, 1),
        "never empty": non_empty(),
    }

    invariant_map: dict[str, list[Invariant]] = {}
    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        strats = _infer_strategies(func, fixtures)
        if strats is None:
            continue
        try:
            result = mine(func, max_examples=30)
        except Exception:
            continue

        func_invs: list[Invariant] = []
        has_numeric = False
        for prop in result.universal:
            inv = _PROPERTY_TO_INVARIANT.get(prop.name)
            if inv is not None:
                func_invs.append(inv)
            if "output >= 0" in prop.name or "output in [" in prop.name:
                has_numeric = True

        # If function returns numeric values and no specific bound was found,
        # at least check for finite
        if has_numeric and not any(isinstance(i, type(finite)) for i in func_invs):
            func_invs.append(finite)

        if func_invs:
            invariant_map[name] = func_invs

    return invariant_map, []
