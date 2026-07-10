from __future__ import annotations
# ruff: noqa
# ============================================================================
# Main audit function
# ============================================================================


def audit(
    module: str,
    *,
    targets: Sequence[Any] | None = None,
    test_dir: str = "tests",
    max_examples: int = 20,
    workers: int = 1,
    validation_mode: AuditValidationMode = "fast",
    contract_checks: Mapping[str, Sequence[Any]] | None = None,
    min_fixture_completeness: float = 0.0,
) -> ModuleAudit:
    """Audit a module: measure current tests vs ordeal-migrated tests.

    Runs BOTH test suites and MEASURES coverage.  Every number in the
    result is either ``[verified]`` or ``FAILED: reason``.

    The "migrated" test combines ordeal ``fuzz()`` (crash safety) with
    mined property descriptions.  The generated file is written to
    ``.ordeal/test_<mod>_migrated.py`` for inspection and debugging.

    Args:
        module: Dotted module path (e.g., ``"myapp.scoring"``).
        test_dir: Directory containing existing tests.
        max_examples: Hypothesis examples per function.
        workers: Isolated processes for mutation validation. ``1`` (default)
            keeps validation serial; higher values preserve target order and
            use deterministic per-target seeds without promising a speedup.
        validation_mode: ``"fast"`` replays mined inputs against mutants.
            ``"deep"`` replays mined inputs, then re-mines each mutant.
        contract_checks: Explicit semantic contract probes keyed by callable name.
        min_fixture_completeness: Minimum runnable-target fraction required before
            audit spends time on migrated-test generation and mutation checks.

    Returns:
        A ``ModuleAudit`` with verified or explicitly-failed measurements.
    """
    validation_mode = _normalize_validation_mode(validation_mode)
    raw_target = module
    base_module, owner_path, _method_name = _split_audit_target_spec(module)
    result = ModuleAudit(module=base_module, validation_mode=validation_mode)
    test_path = Path(test_dir)
    state_hash: str | None = None
    target_specs = list(targets or [])
    if owner_path is not None:
        target_specs = [raw_target, *target_specs]
    cache_key = _audit_target_cache_key(base_module, target_specs)

    try:
        state_hash = _audit_state_hash(
            base_module,
            test_dir=test_dir,
            max_examples=max_examples,
            validation_mode=validation_mode,
            target_specs=target_specs,
        )
        cached = _load_audit_cache(cache_key, state_hash)
        if cached is not None:
            out_path = _generated_test_path(base_module)
            out_path.parent.mkdir(exist_ok=True)
            if cached.generated_test:
                out_path.write_text(cached.generated_test, encoding="utf-8")
            return cached
    except Exception:
        state_hash = None

    # -- 1. Find and measure existing tests --
    test_files = _find_test_files(base_module, test_path)
    test_file_evidence = _find_test_file_evidence(base_module, test_path)
    for tf in test_files:
        count, err = _count_tests_in_file(tf)
        result.current_test_count += count
        if err:
            result.warnings.append(err)

        lines, err = _count_lines_in_file(tf)
        result.current_test_lines += lines
        if err:
            result.warnings.append(err)

    # -- 2. Generate migrated test --
    try:
        mod = _resolve_module(base_module)
    except ImportError as exc:
        if test_files:
            result.current_coverage = _measure_coverage(test_files, base_module)
        else:
            result.current_coverage = CoverageMeasurement(
                Status.FAILED,
                error="no test files found",
            )
        result.warnings.append(f"cannot import {base_module}: {exc}")
        return result

    scannable, skipped, discovered_callables = _normalize_audit_function_collection(
        _collect_audit_functions(
            mod,
            target_specs=target_specs,
        )
    )
    result.gap_functions = skipped
    result.total_functions = len(discovered_callables)
    result.harness_hints = _audit_harness_hints(discovered_callables, result.gap_functions)
    hints_by_function: dict[str, list[dict[str, Any]]] = {}
    for hint in result.harness_hints:
        hints_by_function.setdefault(str(hint.get("function", "")), []).append(dict(hint))
    skipped_names = set(skipped)
    result.surface = [
        {
            "module": base_module,
            "name": name,
            "target": f"{base_module}.{name}",
            "kind": str(getattr(func, "__ordeal_kind__", "function")),
            "runnable": name not in skipped_names,
            "skip_reason": None if name not in skipped_names else name,
            "harness_hints": hints_by_function.get(name, []),
        }
        for name, func in discovered_callables
    ]
    result.contract_findings = _audit_contract_findings(
        discovered_callables,
        contract_checks=contract_checks,
        warnings=result.warnings,
    )
    result.blocking_reason = _audit_blocking_reason(
        total_functions=result.total_functions,
        gap_functions=result.gap_functions,
        discovered_functions=discovered_callables,
        min_fixture_completeness=min_fixture_completeness,
    )
    if result.blocking_reason is not None:
        if test_files:
            result.current_coverage = _measure_coverage(test_files, base_module)
        else:
            result.current_coverage = CoverageMeasurement(
                Status.FAILED,
                error="no test files found",
            )
        collected_nodeids = (
            _collect_pytest_nodeids(test_files)
            if _should_collect_pytest_nodeids(
                discovered_callables,
                current_coverage=result.current_coverage,
                test_file_evidence=test_file_evidence,
            )
            else {}
        )
        result.function_audits = _build_function_audits(
            discovered_callables,
            current_coverage=result.current_coverage,
            test_file_evidence=test_file_evidence,
            collected_nodeids=collected_nodeids,
        )
        from ordeal.mine import STRUCTURAL_LIMITATIONS

        result.not_checked = list(STRUCTURAL_LIMITATIONS)
        if state_hash is not None:
            try:
                _save_audit_cache(cache_key, state_hash, result)
            except Exception:
                pass
        return result
    mine_examples = min(max_examples, MINE_EXAMPLES_FOR_GENERATED_TEST)
    mine_results = _mine_audit_functions(
        scannable,
        max_examples=mine_examples,
        warnings=result.warnings,
    )

    generated, test_count, _skipped = _generate_migrated_test(
        base_module,
        max_examples,
        result.warnings,
        scannable_functions=scannable,
        skipped_functions=skipped,
        mine_results=mine_results,
    )
    result.generated_test = generated
    result.migrated_test_count = test_count
    result.migrated_lines = len(
        [ln for ln in generated.splitlines() if ln.strip()],
    )

    # Collect mined properties with confidence bounds
    for name, mine_result in mine_results.items():
        for p in mine_result.properties:
            if p.universal and p.total >= 5:
                lower = wilson_lower(p.holds, p.total)
                result.mined_properties.append(
                    f"{name}: {p.name} ({p.holds}/{p.total}, >={lower:.0%} CI)"
                )

    # Suggest metamorphic relations from mined properties
    result.suggested_relations = _suggest_relations(result.mined_properties)

    # Validate mined properties against mutations using standard preset
    from ordeal.mutations import mutation_contract_context

    targets: list[tuple[str, MineResult, dict[str, Any]]] = []
    for name, func in scannable:
        mine_result = mine_results.get(name)
        if mine_result is not None and _should_validate_mined_properties(mine_result):
            targets.append(
                (
                    f"{base_module}.{name}",
                    mine_result,
                    mutation_contract_context(
                        list((contract_checks or {}).get(name, [])),
                        harness=str(getattr(func, "__ordeal_harness__", "") or "") or None,
                    ),
                )
            )

    max_validation_examples = min(max_examples, 20)
    kill_counts: dict[str, int] = {}
    validation_evidence = _validate_audit_targets(
        targets,
        max_examples=max_validation_examples,
        workers=workers,
        validation_mode=validation_mode,
        warnings=result.warnings,
    )
    total_killed = sum(item.killed for item in validation_evidence)
    total_mutants = sum(item.total for item in validation_evidence)
    for item in validation_evidence:
        _record_validation_evidence(result, item, kill_counts=kill_counts)
    if total_mutants > 0:
        pct = total_killed / total_mutants
        result.mutation_score = f"{total_killed}/{total_mutants} ({pct:.0%})"
    if kill_counts:
        weakest = sorted(kill_counts.items(), key=lambda item: (item[1], item[0]))
        result.weakest_tests = [
            {"test": test_name, "kills": count} for test_name, count in weakest[:DISPLAY_CAP]
        ]

    # -- 3. Measure migrated test coverage --
    out_path = _generated_test_path(base_module)
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(generated, encoding="utf-8")

    result.current_coverage, result.migrated_coverage = _measure_audit_coverages(
        test_files,
        [out_path],
        module,
    )

    collected_nodeids = (
        _collect_pytest_nodeids(test_files)
        if _should_collect_pytest_nodeids(
            scannable,
            current_coverage=result.current_coverage,
            test_file_evidence=test_file_evidence,
        )
        else {}
    )
    result.function_audits = _build_function_audits(
        discovered_callables,
        current_coverage=result.current_coverage,
        test_file_evidence=test_file_evidence,
        collected_nodeids=collected_nodeids,
    )

    # -- 4. Suggest tests to close the gap --
    result.suggestions = _suggest_tests(
        base_module,
        result.current_coverage.missing_lines,
        result.migrated_coverage.missing_lines,
    )

    # -- 5. State known unknowns --
    from ordeal.mine import STRUCTURAL_LIMITATIONS

    result.not_checked = list(STRUCTURAL_LIMITATIONS)

    # -- 6. Self-verify --
    _verify_consistency(
        result.current_coverage,
        result.migrated_coverage,
        generated,
        test_count,
        result.warnings,
    )

    if state_hash is not None:
        try:
            _save_audit_cache(cache_key, state_hash, result)
        except Exception:
            pass

    return result
# ============================================================================
# Report generation
# ============================================================================


def audit_report(
    modules: list[str],
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
    workers: int = 1,
    validation_mode: AuditValidationMode = "fast",
) -> str:
    """Audit multiple modules and produce a summary report.

    Returns a formatted string suitable for terminal output.
    Every number is labeled ``[verified]`` or ``FAILED``::

        from ordeal.audit import audit_report

        print(audit_report(["myapp.scoring", "myapp.utils"]))
        # ordeal audit
        #   myapp.scoring
        #     current:  33 tests | 343 lines | 98% coverage [verified]
        #     migrated: 12 tests | 130 lines | 96% coverage [verified]
        #     saving:   64% fewer tests | 62% less code | same coverage
        #   total:
        #     current:  55 tests | 500 lines
        #     migrated: 20 tests | 200 lines

    Args:
        modules: Dotted module paths to audit (e.g. ``["myapp.scoring"]``).
        test_dir: Directory containing test files (default ``"tests"``).
        max_examples: Hypothesis examples for property mining per function.
        workers: Parallel workers for mutation validation in each module audit.
        validation_mode: ``"fast"`` replay or ``"deep"`` replay + re-mining.
    """
    validation_mode = _normalize_validation_mode(validation_mode)
    results = [
        audit(
            mod,
            test_dir=test_dir,
            max_examples=max_examples,
            workers=workers,
            validation_mode=validation_mode,
        )
        for mod in modules
    ]
    return _render_audit_results(results)
