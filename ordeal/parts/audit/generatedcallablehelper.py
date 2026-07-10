from __future__ import annotations
# ruff: noqa
def _generated_callable_helper(
    module: str,
    func: object,
    name: str,
    param_names: list[str],
    param_decls: list[str],
    call_args: str,
) -> tuple[list[str], set[str], str]:
    """Generate a helper wrapper for a callable used in migrated tests."""
    safe_name = re.sub(r"[^0-9a-zA-Z_]", "_", name.replace(".", "_"))
    helper_name = f"_ordeal_target_{safe_name}"
    imports: set[str] = {
        "from ordeal.audit import _call_with_async_support",
        "from ordeal.auto import _prepare_bound_method_call",
    }
    lines = [f"def {helper_name}({', '.join(param_decls)}):"]

    kind = str(getattr(func, "__ordeal_kind__", "function"))
    owner_value = getattr(func, "__ordeal_owner__", None)
    owner = (
        getattr(owner_value, "__qualname__", getattr(owner_value, "__name__", None))
        if owner_value is not None and not isinstance(owner_value, str)
        else owner_value
    )
    method = getattr(func, "__ordeal_method__", None) or getattr(
        func,
        "__ordeal_method_name__",
        None,
    )
    factory = getattr(func, "__ordeal_factory__", None)
    setup = getattr(func, "__ordeal_setup__", None)
    scenarios = list(getattr(func, "__ordeal_scenarios__", []) or [])
    state_factory = getattr(func, "__ordeal_state_factory__", None)
    state_param = getattr(func, "__ordeal_state_param__", None)
    teardown = getattr(func, "__ordeal_teardown__", None)
    arg_suffix = f", {call_args}" if call_args else ""
    call_kwargs_expr = "{" + ", ".join(f"{item!r}: {item}" for item in param_names) + "}"

    if not owner or not method:
        target_expr = f"{module}.{name}"
        lines.append(f"    return _call_with_async_support({target_expr}{arg_suffix})")
        return lines, imports, helper_name

    if kind in {"static", "class"}:
        target_expr = f"{module}.{owner}.{method}"
        lines.append(f"    return _call_with_async_support({target_expr}{arg_suffix})")
        return lines, imports, helper_name

    factory_expr = None
    if factory:
        hook_import = _generated_hook_import(factory, f"_ordeal_factory_mod_{safe_name}")
        if hook_import is not None:
            import_line, factory_expr = hook_import
            imports.add(import_line)

    if factory_expr is None:
        lines.append(f"    instance = {module}.{owner}()")
    else:
        lines.append(f"    instance = _call_with_async_support({factory_expr})")
    if setup:
        hook_import = _generated_hook_import(setup, f"_ordeal_setup_mod_{safe_name}")
        if hook_import is not None:
            import_line, setup_expr = hook_import
            imports.add(import_line)
            lines.append(f"    setup_result = _call_with_async_support({setup_expr}, instance)")
            lines.append("    if setup_result is not None:")
            lines.append("        instance = setup_result")
    for idx, scenario in enumerate(scenarios, 1):
        hook_import = _generated_hook_import(scenario, f"_ordeal_scenario_mod_{safe_name}_{idx}")
        if hook_import is None:
            continue
        import_line, scenario_expr = hook_import
        imports.add(import_line)
        lines.append(f"    scenario_result = _call_with_async_support({scenario_expr}, instance)")
        lines.append("    if scenario_result is not None:")
        lines.append("        instance = scenario_result")
    lines.append("    try:")
    if state_factory and state_param:
        hook_import = _generated_hook_import(state_factory, f"_ordeal_state_mod_{safe_name}")
        if hook_import is not None:
            import_line, state_factory_expr = hook_import
            imports.add(import_line)
            lines.append(f"        _ordeal_bound = instance.{method}")
            lines.append(
                "        _ordeal_call_args, _ordeal_call_kwargs = _prepare_bound_method_call("
            )
            lines.append("            _ordeal_bound,")
            lines.append("            (),")
            lines.append(f"            {call_kwargs_expr},")
            lines.append("            instance=instance,")
            lines.append(f"            state_factory={state_factory_expr},")
            lines.append(f"            state_param={state_param!r},")
            lines.append("        )")
            lines.append(
                "        return _call_with_async_support("
                "_ordeal_bound, *_ordeal_call_args, **_ordeal_call_kwargs)"
            )
        else:
            lines.append(f"        return _call_with_async_support(instance.{method}{arg_suffix})")
    else:
        lines.append(f"        return _call_with_async_support(instance.{method}{arg_suffix})")
    lines.append("    finally:")
    if teardown:
        hook_import = _generated_hook_import(teardown, f"_ordeal_teardown_mod_{safe_name}")
        if hook_import is not None:
            import_line, teardown_expr = hook_import
            imports.add(import_line)
            lines.append(f"        _call_with_async_support({teardown_expr}, instance)")
        else:
            lines.append("        pass")
    else:
        lines.append("        pass")
    return lines, imports, helper_name
def _property_to_assertion(
    prop_name: str,
    call_expr: str,
    param_names: list[str],
) -> str | None:
    """Map a mined property name to an assertion line, or *None*."""
    call = call_expr

    if prop_name == "never None":
        return "assert result is not None"
    if prop_name.startswith("output type is "):
        tp = prop_name.removeprefix("output type is ")
        return f"assert type(result).__name__ == {tp!r}"
    if prop_name == "no NaN":
        return "assert not (isinstance(result, float) and math.isnan(result))"
    if prop_name == "output >= 0":
        return "assert result >= 0"
    if prop_name == "output in [0, 1]":
        return "assert 0 <= result <= 1"
    if prop_name == "never empty":
        return "assert len(result) > 0"
    if prop_name == "deterministic":
        return f"assert {call}({', '.join(param_names)}) == result"
    if prop_name == "idempotent":
        if len(param_names) == 1:
            return f"assert {call}(result) == result"
        if len(param_names) >= 2:
            rest = ", ".join(param_names[1:])
            return f"assert {call}(result, {rest}) == result"
    if prop_name == "involution":
        if len(param_names) == 1:
            return f"assert {call}(result) == {param_names[0]}"
        if len(param_names) >= 2:
            rest = ", ".join(param_names[1:])
            return f"assert {call}(result, {rest}) == {param_names[0]}"
    if prop_name == "commutative" and len(param_names) == 2:
        return f"assert {call}({param_names[1]}, {param_names[0]}) == result"
    for op in ("==", "<=", ">="):
        prefix = f"len(output) {op} len("
        if prop_name.startswith(prefix):
            param = prop_name.removeprefix(prefix).rstrip(")")
            if param in param_names:
                return f"assert len(result) {op} len({param})"

    return None
def _generated_hook_import(
    hook: str | Any | None,
    alias: str,
) -> tuple[str, str] | None:
    """Return ``(import_line, expr)`` for a generated helper hook."""
    if hook is None:
        return None
    if isinstance(hook, str):
        module_name, sep, attr_path = hook.partition(":")
        if not sep:
            module_name, _, attr_path = hook.rpartition(".")
        if module_name.endswith(".py"):
            resolved_module = _python_source_path_to_module_name(module_name)
            if resolved_module is not None:
                module_name = resolved_module
    else:
        module_name = getattr(hook, "__module__", "")
        attr_path = getattr(hook, "__qualname__", getattr(hook, "__name__", ""))
        if module_name.startswith("_ordeal_symbol_"):
            source_file = inspect.getsourcefile(hook) or inspect.getfile(hook)
            if source_file:
                resolved_module = _python_source_path_to_module_name(source_file)
                if resolved_module is not None:
                    module_name = resolved_module
    if not module_name or not attr_path or "<locals>" in attr_path:
        return None
    return (f"import {module_name} as {alias}", f"{alias}.{attr_path}")
def _generate_migrated_test(
    module: str,
    max_examples: int,
    warnings: list[str],
    *,
    scannable_functions: list[tuple[str, object]] | None = None,
    skipped_functions: list[str] | None = None,
    mine_results: dict[str, MineResult] | None = None,
) -> tuple[str, int, list[str]]:
    """Generate a consolidated test file: ordeal fuzz + mined property assertions.

    Returns ``(source_code, test_count, skipped_functions)``.

    The generated file has two layers per function:

    - ``fuzz()`` test — crash safety (does NOT verify correctness).
    - ``@quickcheck`` test — asserts mined properties with random inputs.
      Falls back to informational comments when type hints are missing.

    Args:
        module: Dotted module path.
        max_examples: Hypothesis examples for fuzz and mine.
        warnings: Mutable list — mining failures are appended here.
        scannable_functions: Optional pre-filtered ``(name, func)`` pairs.
        skipped_functions: Optional names missing inferred strategies.
        mine_results: Optional precomputed mine outputs keyed by function name.
    """
    mod = _resolve_module(module)

    base_imports = [
        "from ordeal.auto import fuzz",
        f"import {module}",
    ]
    extra_imports: set[str] = set()

    header = [
        f'"""Auto-generated ordeal test for {module}.',
        "",
        "fuzz() tests crash safety only — it does NOT verify correctness.",
        "Property tests assert mined invariants (confirmed by sampling).",
        '"""',
    ]
    body: list[str] = []

    if scannable_functions is None or skipped_functions is None:
        scannable, skipped, _discovered = _normalize_audit_function_collection(
            _collect_audit_functions(mod)
        )
    else:
        scannable = list(scannable_functions)
        skipped = list(skipped_functions)

    test_count = 0

    # Mine properties and generate assertion tests
    for name, func in scannable:
        safe_name = re.sub(r"[^0-9a-zA-Z_]", "_", name.replace(".", "_"))
        mine_result = None if mine_results is None else mine_results.get(name)
        if mine_result is None:
            try:
                cap = min(max_examples, MINE_EXAMPLES_FOR_GENERATED_TEST)
                mine_result = mine(func, max_examples=cap)
            except Exception as exc:
                warnings.append(f"mining failed for {name}: {type(exc).__name__}: {exc}")
                continue

        strong = [
            p
            for p in mine_result.properties
            if p.universal and p.total >= MIN_SAMPLES_FOR_PROPERTY
        ]

        sig_info = _func_sig_for_codegen(func)
        if strong and sig_info:
            param_names, param_decls, call_args, sig_imports = sig_info
            extra_imports.update(sig_imports)
            helper_lines, helper_imports, helper_name = _generated_callable_helper(
                module,
                func,
                name,
                param_names,
                param_decls,
                call_args,
            )
            extra_imports.update(helper_imports)
            extra_imports.add("from ordeal.quickcheck import quickcheck")
            body.extend(helper_lines)
            body.append("")
            assertions = [
                (p, _property_to_assertion(p.name, helper_name, param_names)) for p in strong
            ]
            has_any = any(a for _, a in assertions)
        else:
            helper_name = f"{module}.{name}"
            has_any = False

        test_count += 1
        body.append(f"def test_{safe_name}_no_crash():")
        body.append(f'    """Crash safety: {module}.{name} does not raise."""')
        body.append(f"    result = fuzz({helper_name}, max_examples={max_examples})")
        body.append("    assert result.passed, result.summary()")
        body.append("")

        if not strong:
            continue

        test_count += 1

        if has_any:
            if any(a and "math." in a for _, a in assertions):
                extra_imports.add("import math")

            body.append(f"@quickcheck(max_examples={max_examples})")
            body.append(f"def test_{safe_name}_properties({', '.join(param_decls)}):")
            body.append(f'    """Mined properties for {module}.{name}."""')
            body.append(f"    result = {helper_name}({call_args})")

            for prop, assertion in assertions:
                lower = wilson_lower(prop.holds, prop.total)
                ci = f">={lower:.1%} CI"
                if assertion:
                    body.append(f"    {assertion}  # {ci}")
                else:
                    body.append(f"    # {prop.name}: {prop.holds}/{prop.total} ({ci})")
            body.append("")
        else:
            # Fallback: comment-only (no type hints or no expressible assertions)
            body.append(f"def test_{safe_name}_properties():")
            body.append(f'    """Mined properties for {module}.{name}."""')
            for prop in strong:
                lower = wilson_lower(prop.holds, prop.total)
                body.append(
                    f"    # {prop.name}: {prop.holds}/{prop.total} (>={lower:.1%} at 95% CI)"
                )
            body.append(f"    result = fuzz({helper_name}, max_examples={max_examples})")
            body.append("    assert result.passed")
            body.append("")

    all_imports = base_imports + sorted(extra_imports)
    full = header + [""] + all_imports + ["", ""] + body
    return "\n".join(full), test_count, skipped
# ============================================================================
# Self-verification
# ============================================================================


def _verify_consistency(
    current: CoverageMeasurement,
    migrated: CoverageMeasurement,
    generated_test: str,
    migrated_test_count: int,
    warnings: list[str],
) -> None:
    """Cross-check audit outputs for internal consistency.

    Appends warnings for any inconsistency found.  Does NOT change
    measurement status — just flags concerns.

    **Checks performed:**

    1. If both measurements succeeded, ``total_statements`` should match
       (same module, same source file).
    2. ``migrated_test_count`` should match ``def test_`` count in the
       generated file.
    """
    if current.status == Status.VERIFIED and migrated.status == Status.VERIFIED:
        cur_stmts = current.result.total_statements  # type: ignore[union-attr]
        mig_stmts = migrated.result.total_statements  # type: ignore[union-attr]
        if cur_stmts != mig_stmts:
            warnings.append(
                f"statement count mismatch: current={cur_stmts}, "
                f"migrated={mig_stmts} (expected same source)"
            )

    actual_count = generated_test.count("def test_")
    if actual_count != migrated_test_count:
        warnings.append(
            f"test count mismatch: reported={migrated_test_count}, "
            f"actual in generated file={actual_count}"
        )
def _should_validate_mined_properties(mine_result: MineResult) -> bool:
    """Return whether mutation validation is likely to be useful.

    Validation is expensive because it runs mutation tests. We only run it
    when mining found at least one high-confidence universal property that
    can become a concrete assertion.
    """
    return any(p.universal and p.total >= 5 for p in mine_result.properties)
