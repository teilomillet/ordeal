from __future__ import annotations
# ruff: noqa
# ============================================================================
# 1. scan_module
# ============================================================================


def scan_module(
    module: str | ModuleType,
    *,
    max_examples: int | dict[str, int] = 50,
    check_return_type: bool = True,
    targets: Sequence[str] | None = None,
    include_private: bool = False,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    expected_failures: list[str] | None = None,
    expected_preconditions: dict[str, list[str]] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    expected_properties: dict[str, list[str]] | None = None,
    expected_relations: dict[str, list[str]] | None = None,
    contract_checks: dict[str, list[ContractCheck]] | None = None,
    ignore_contracts: list[str] | None = None,
    contract_overrides: dict[str, list[str]] | None = None,
    mode: ScanMode = "evidence",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: Sequence[str] | None = None,
    shell_injection_check: bool = False,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    security_focus: bool = False,
    minimize_findings: bool = False,
) -> ScanResult:
    """Smoke-test every public callable in *module*.

    For each callable with type hints, generates random inputs and checks:

    - **No crash**: calling with valid inputs doesn't raise
    - **Return type**: if annotated, the return value matches (optional)

    Simple::

        result = scan_module("myapp.scoring")
        assert result.passed

    With fixtures for params that can't be inferred::

        result = scan_module("myapp", fixtures={"model": model_strategy})

    With per-function example budgets::

        result = scan_module("myapp", max_examples={
            "compute": 200,    # fuzz this one harder
            "__default__": 50, # everything else
        })

    With expected failures for known-broken functions::

        result = scan_module("myapp", expected_failures=["broken_func"])
        assert result.passed  # broken_func failure won't count

    Args:
        module: Module path or object to scan.
        max_examples: Hypothesis examples per function. Either a single int
            (same budget for all functions) or a dict mapping function names
            to budgets, with ``"__default__"`` as fallback (default: 50).
        check_return_type: Verify return type annotations.
        targets: Optional explicit callable targets within the module. Accepts
            local names like ``"Env.build_env_vars"`` or explicit targets like
            ``"pkg.mod:Env.build_env_vars"``.
        include_private: Also include single-underscore names.
        fixtures: Strategy overrides for specific parameter names.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_scenarios: Optional per-class collaborator scenarios run after setup.
        object_state_factories: Optional per-class state factories for methods that take
            a runtime ``state`` parameter.
        object_teardowns: Optional per-class teardown hooks for stateful harnesses.
        object_harnesses: Per-class harness mode (``fresh`` or ``stateful``).
        expected_failures: Function names that are expected to fail.
            Failures from these functions are tracked separately and
            do not cause ``result.passed`` to be ``False``.
        expected_preconditions: Per-function exception/docstring tokens that
            should count as expected contract preconditions instead of bugs.
        ignore_properties: Property names to suppress in mined warnings.
        ignore_relations: Relation names to suppress in mined warnings.
        property_overrides: Per-function property suppressions.
        relation_overrides: Per-function relation suppressions.
        expected_properties: Per-function expected property annotations. These
            are merged into ``property_overrides`` as suppressions.
        expected_relations: Per-function expected relation annotations. These
            are merged into ``relation_overrides`` as suppressions.
        contract_checks: Explicit semantic contract probes keyed by
            callable name. Each probe runs with explicit ``kwargs`` and
            reports a contract violation when its predicate fails.
        ignore_contracts: Auto-inferred contract probes to suppress globally.
        contract_overrides: Per-function auto-contract suppressions.
        mode: ``"evidence"`` surfaces broad exploratory findings;
            ``"candidate"`` keeps only stricter high-fit candidates.
            ``"coverage_gap"`` and ``"real_bug"`` remain compatibility aliases.
        seed_from_tests: Learn valid input shapes from adjacent pytest files.
        seed_from_fixtures: Mine literal pytest fixture returns as seed inputs.
        seed_from_docstrings: Mine doctest-like examples from docstrings.
        seed_from_code: Mine boundary values from code patterns.
        seed_from_call_sites: Mine literal examples from adjacent call sites.
        treat_any_as_weak: Penalize broad or missing hints instead of trusting them.
        proof_bundles: Attach structured proof payloads to crash findings.
        auto_contracts: Auto-enable sink-aware semantic checks for shell/path/env/json/http.
        shell_injection_check: Run a static shell-injection oracle before execution.
        require_replayable: Require replayability before promoting a bug candidate.
        min_contract_fit: Minimum inferred contract-fit score to promote.
        min_reachability: Minimum reachability score to promote.
        min_realism: Minimum semantic realism score to promote.
        security_focus: Opt into trust-boundary-biased sink detection, scoring,
            and deterministic low-side-effect security probes.
        minimize_findings: Shrink and replay suspicious property witnesses.
    """
    if mode not in _VALID_SCAN_MODES:
        raise ValueError(f"mode must be one of {_VALID_SCAN_MODES}, got {mode!r}")
    mode = _normalize_scan_mode(mode)
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__
    result = ScanResult(
        module=mod_name,
        expected_failure_names=list(expected_failures) if expected_failures else [],
    )

    # Resolve per-function example budgets
    if isinstance(max_examples, int):
        default_examples = max_examples
        examples_map: dict[str, int] = {}
    else:
        default_examples = max_examples.get("__default__", 50)
        examples_map = max_examples

    selected_functions = _selected_public_functions(
        mod,
        targets=targets,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )
    evidence_index = (
        ProjectEvidenceIndex(str(mod_name))
        if any((seed_from_tests, seed_from_fixtures, seed_from_call_sites))
        else None
    )

    for name, func in selected_functions:
        try:
            strategies = _infer_strategies(func, fixtures)
        except Exception as exc:
            result.functions.append(
                FunctionResult(
                    name=name,
                    passed=True,
                    execution_ok=False,
                    verdict="blocked",
                    error=str(exc)[:300],
                    error_type=type(exc).__name__,
                    limitation_kind="strategy_generation",
                    blocking_reason=(
                        f"input strategy could not be inferred: {type(exc).__name__}: {exc}"
                    )[:500],
                )
            )
            continue
        if strategies is None:
            reason = _callable_skip_reason(func) or "missing type hints"
            result.skipped.append((name, reason))
            continue

        return_type = safe_get_annotations(func).get("return")

        func_examples = examples_map.get(name, default_examples)
        func_result = _test_one_function(
            name,
            func,
            strategies,
            return_type,
            max_examples=func_examples,
            check_return_type=check_return_type,
            fixtures=fixtures,
            ignore_properties=sorted(
                {
                    *(ignore_properties or []),
                    *(expected_properties or {}).get("*", []),
                    *(property_overrides or {}).get(name, []),
                    *(expected_properties or {}).get(name, []),
                }
            ),
            ignore_relations=sorted(
                {
                    *(ignore_relations or []),
                    *(expected_relations or {}).get("*", []),
                    *(relation_overrides or {}).get(name, []),
                    *(expected_relations or {}).get(name, []),
                }
            ),
            property_overrides=property_overrides,
            relation_overrides=relation_overrides,
            contract_checks=(contract_checks or {}).get(name),
            expected_preconditions=sorted(
                {
                    *((expected_preconditions or {}).get("*", [])),
                    *((expected_preconditions or {}).get(name, [])),
                }
            ),
            ignore_contracts=sorted(
                {
                    *(ignore_contracts or []),
                    *(contract_overrides or {}).get("*", []),
                    *(contract_overrides or {}).get(name, []),
                }
            ),
            mode=mode,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            treat_any_as_weak=treat_any_as_weak,
            proof_bundles=proof_bundles,
            auto_contracts=auto_contracts,
            shell_injection_check=shell_injection_check,
            require_replayable=require_replayable,
            min_contract_fit=min_contract_fit,
            min_reachability=min_reachability,
            min_realism=min_realism,
            security_focus=security_focus,
            minimize_findings=minimize_findings,
            evidence_index=evidence_index,
        )
        try:
            source = inspect.getsource(_unwrap(func))
        except (OSError, TypeError):
            func_result.source_sha256 = None
        else:
            func_result.source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        result.functions.append(func_result)

    return result
