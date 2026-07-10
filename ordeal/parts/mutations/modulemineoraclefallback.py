from __future__ import annotations


# ruff: noqa
def _module_mine_oracle_fallback(
    target: str,
    module: types.ModuleType,
    original_result: MutationResult,
    operators: list[str],
    stats: dict[str, int],
    *,
    filter_equivalent: bool,
    equivalence_samples: int,
    preset_used: str | None,
    mutant_timeout: float | None,
    contract_context: Mapping[str, Any] | None = None,
) -> MutationResult | None:
    """Run mine oracle per-function when module-level tests killed 0 mutants.

    Iterates public functions in the module, re-generates mutants for each,
    and tests them via :func:`_mine_based_mutation_test`.  If the mine oracle
    catches mutations that tests missed, warns about process isolation and
    returns the combined result.
    """
    combined = MutationResult(
        target=target,
        operators_used=operators,
        preset_used=preset_used,
        contract_context=dict(contract_context or {}),
    )
    any_killed = False

    for name, obj in sorted(vars(module).items()):
        if name.startswith("__"):
            continue
        if not callable(obj) or isinstance(obj, type):
            continue
        func = _unwrap_func(obj)
        if getattr(func, "__ordeal_requires_factory__", False):
            continue
        if getattr(func, "__module__", None) not in {module.__name__, None}:
            continue
        try:
            source = _get_source(func)
        except Exception:
            continue

        func_target = f"{target}.{name}"
        try:
            func_mutants = generate_mutants(source, operators, timeout=mutant_timeout)
        except Exception:
            continue

        if not func_mutants:
            continue

        try:
            mine_result = _mine_based_mutation_test(
                func_target,
                func,
                name,
                module,
                func_mutants,
                filter_equivalent=filter_equivalent,
                equivalence_samples=equivalence_samples,
                operators_used=operators,
                preset_used=preset_used,
                contract_context=contract_context,
                _stats={},
            )
            combined.mutants.extend(mine_result.mutants)
            if mine_result.killed > 0:
                any_killed = True
        except (NoTestsFoundError, Exception):
            continue

    if any_killed:
        import warnings

        warnings.warn(
            f"{target!r}: tests killed 0/{original_result.total} mutants but "
            f"mine oracle killed {combined.killed}/{combined.total} across "
            f"module functions — your tests likely exercise this module through "
            "a process boundary (Ray, multiprocessing) where in-memory mutations "
            "are invisible. Falling back to mine oracle for accurate results.",
            stacklevel=3,
        )
        combined.diagnostics["fallback_reason"] = "process_isolation"
        combined.diagnostics.update(stats)
        combined.diagnostics["tested"] = combined.total
        return combined
    return None


def mutate_and_test(
    target: str,
    test_fn: Callable[[], None] | None = None,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
    workers: int = 0,
    filter_equivalent: bool = True,
    equivalence_samples: int = 10,
    extra_mutants: list[str | tuple[str, str]] | None = None,
    llm: Callable[[str], str] | None = None,
    llm_equivalence: bool = False,
    concern: str | None = None,
    test_filter: str | None = None,
    mutant_timeout: float | None = None,
    disk_mutation: bool | None = None,
    promote_clusters_only: bool = True,
    cluster_min_size: int = 2,
    contract_context: Mapping[str, Any] | None = None,
) -> MutationResult:
    """Apply mutations to an entire module and run *test_fn* against each.

    A mutant is **killed** if *test_fn* raises.
    A mutant **survives** if *test_fn* passes — meaning your tests miss the bug.

    Note: this swaps ``sys.modules[target]``.  Code that cached individual
    functions via ``from target import func`` will not see the mutant.
    Prefer :func:`mutate_function_and_test` for precise single-function targeting.

    Args:
        target: Module path (e.g. ``"myapp.scoring"``).
        test_fn: Zero-arg callable; should raise on failure.  When ``None``
            (default), auto-discovers tests via pytest in-process.
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
        workers: Parallel workers for testing mutants. ``0`` (default)
            selects adaptively; a positive value is an explicit override.
        filter_equivalent: Drop mutants that produce identical outputs on
            random inputs.  Default ``True``.
        equivalence_samples: Number of random inputs for equivalence
            filtering.  Default ``10``.
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            to validate and add alongside rule-based mutants.
        llm: Optional callable for automated mutant generation.
        llm_equivalence: If ``True`` and *llm* is provided, use the LLM
            to filter surviving mutants for semantic equivalence.
        test_filter: Pytest ``-k`` expression to narrow which tests run
            against each mutant.  When ``None`` (default), derives a filter
            from the target module name.
        mutant_timeout: Maximum seconds for the mutant generation step.
            When exceeded, returns whatever mutants have been generated so
            far.  Prevents hanging on complex AST expressions (numpy, cv2).
    """
    started_at = time.perf_counter()
    timings: dict[str, float] = {}
    disk_mutation = _resolve_disk_mutation(disk_mutation, target)
    used_preset = preset
    operators = _resolve_operators(operators, preset)
    use_batch = test_fn is None  # batch when auto-discovering tests
    if test_fn is None:
        test_fn = _auto_test_fn(target, test_filter=test_filter)
    module = importlib.import_module(target)
    source_file = getattr(module, "__file__", None)
    if source_file is None:
        raise ValueError(f"Cannot locate source for {target!r}")
    with open(source_file) as f:
        source = f.read()

    stats: dict[str, int] = {}
    with _timed_phase(timings, "generate_seconds"):
        mutant_pairs = generate_mutants(
            source,
            operators,
            extra_mutants=extra_mutants,
            llm=llm,
            concern=concern,
            _stats=stats,
            timeout=mutant_timeout,
        )

    # Filter equivalent mutants (same outputs on random inputs)
    if filter_equivalent:
        with _timed_phase(timings, "equivalence_seconds"):
            filtered = []
            for mutant, tree in mutant_pairs:
                if _module_is_equivalent(module, tree, equivalence_samples):
                    mutant.killed = True
                    mutant.error = "equivalent (filtered)"
                    stats["filtered_module_equivalent"] = (
                        stats.get("filtered_module_equivalent", 0) + 1
                    )
                else:
                    filtered.append((mutant, tree))
            mutant_pairs = filtered

    result = MutationResult(
        target=target,
        operators_used=operators,
        preset_used=used_preset,
        concern=concern,
        promote_clusters_only=promote_clusters_only,
        cluster_min_size=cluster_min_size,
        contract_context=dict(contract_context or {}),
    )

    # Batch mode: single pytest session for all mutants (much faster)
    if use_batch and mutant_pairs:
        selection = _mutation_test_selection(target, test_filter=test_filter)
        profile = _load_mutation_execution_profile(target)
        selected_test_count = (
            profile.collected_tests
            if profile is not None and profile.collected_tests > 0
            else max(1, len(selection.ast_scores), len(selection.paths))
        )
        effective_workers = _resolve_mutation_worker_count(
            workers,
            mutant_count=len(mutant_pairs),
            selected_test_count=selected_test_count,
            profile=profile,
            disk_mutation=disk_mutation,
        )
        stats["workers_selected"] = effective_workers
        with _timed_phase(timings, "test_execution_seconds"):
            if effective_workers > 1 and len(mutant_pairs) > 1:
                batch_results = _parallel_module_test(
                    target,
                    mutant_pairs,
                    effective_workers,
                    test_filter=test_filter,
                    disk_mutation=disk_mutation,
                    timings=timings,
                )
            else:
                batch_results = _batch_module_test(
                    target,
                    mutant_pairs,
                    test_filter=test_filter,
                    disk_mutation=disk_mutation,
                    stats=stats,
                    timings=timings,
                )
        for item in batch_results:
            mutant, killed, error = item[0], item[1], item[2]
            killer = item[3] if len(item) > 3 else None
            mutant.killed = killed
            mutant.error = error
            mutant.killed_by = killer
            result.mutants.append(mutant)
        # 0% score fallback for module-level: if auto-discovered tests killed
        # nothing, try calling each public function directly (mine oracle).
        # Same logic as mutate_function_and_test's fallback.
        if result.total > 0 and result.killed == 0:
            with _timed_phase(timings, "mine_fallback_seconds"):
                mine_kills = _module_mine_oracle_fallback(
                    target,
                    module,
                    result,
                    operators,
                    stats,
                    filter_equivalent=filter_equivalent,
                    equivalence_samples=equivalence_samples,
                    preset_used=used_preset,
                    mutant_timeout=mutant_timeout,
                    contract_context=contract_context,
                )
            if mine_kills is not None:
                mine_kills.timings.update(timings)
                mine_kills.timings["total_seconds"] = time.perf_counter() - started_at
                return mine_kills

        result.diagnostics.update(stats)
        result.diagnostics["tested"] = result.total
        result.timings.update(timings)
        result.timings["total_seconds"] = time.perf_counter() - started_at
        return result

    # Fallback: serial per-mutant testing (custom test_fn)
    test_name = getattr(test_fn, "__qualname__", getattr(test_fn, "__name__", "test_fn"))
    with _timed_phase(timings, "test_execution_seconds"):
        for mutant, mutated_tree in mutant_pairs:
            try:
                cm = (
                    _mutated_module_on_disk(target, mutated_tree)
                    if disk_mutation
                    else _mutated_module(target, mutated_tree)
                )
                with cm:
                    if not disk_mutation:
                        importlib.invalidate_caches()
                    test_fn()
                    mutant.killed = False
            except Exception as e:
                mutant.killed = True
                mutant.error = str(e)[:200]
                mutant.killed_by = test_name

            result.mutants.append(mutant)

    result.diagnostics.update(stats)
    result.diagnostics["tested"] = result.total
    result.timings.update(timings)
    result.timings["total_seconds"] = time.perf_counter() - started_at
    return result


# ============================================================================
# Function-level mutation testing (recommended)
# ============================================================================


class _MinedPropertyFailure(AssertionError):
    """Internal failure carrying every mined property that rejected a mutant."""

    def __init__(self, property_names: list[str]) -> None:
        self.property_names = property_names
        joined = ", ".join(repr(name) for name in property_names)
        super().__init__(f"Mined properties no longer hold on mutant: {joined}")


def validate_mined_properties(
    target: str,
    max_examples: int = 100,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
    mine_result: "MineResult | None" = None,
    validation_mode: ValidationMode = "fast",
    contract_context: Mapping[str, Any] | None = None,
    _disk_mutation: bool | None = None,
) -> MutationResult:
    """Mine properties of *target*, then mutate it and check the properties catch the mutations.

    This answers: "are the properties mine() found strong enough to detect real bugs?"
    Surviving mutants reveal properties that are too weak — the mined invariants
    pass even on broken code.

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        max_examples: Examples for mine() property discovery.
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
        mine_result: Optional precomputed ``mine()`` result for the original
            function. Reusing it avoids re-mining when a caller already has
            those properties (for example, inside ``ordeal.audit``).
        validation_mode: ``"fast"`` replays mined inputs against mutants.
            ``"deep"`` replays mined inputs, then re-runs ``mine()`` on each mutant.
    """
    operators = _resolve_operators(operators, preset)
    validation_mode = _normalize_validation_mode(validation_mode)
    from ordeal.mine import mine

    target_spec = _resolve_mutation_target(target)
    func_name = target_spec.leaf_name
    if func_name is None:
        raise ValueError(f"Function-level mutation target expected, got module {target!r}")
    func = _resolved_target_callable(target_spec)

    # Mine the original function's properties
    original_mine: MineResult = mine_result or mine(func, max_examples=max_examples)
    universal = original_mine.universal
    if not universal:
        return MutationResult(target=target, contract_context=dict(contract_context or {}))

    sample_matrix = _validation_sample_matrix(func, original_mine, max_examples)
    property_names = tuple(str(prop.name) for prop in universal)
    evaluate_properties = _compile_mined_property_evaluator(property_names)

    # Build a test function from the mined properties
    def mined_test() -> None:
        current_func = _resolved_target_callable(target_spec)
        sample_inputs = sample_matrix.replay()
        if sample_inputs:
            failed = evaluate_properties(current_func, sample_inputs)
            if failed:
                raise _MinedPropertyFailure(failed)
        elif validation_mode == "fast":
            props = mine(current_func, max_examples=max(max_examples, 20)).properties
            failed = []
            for property_name in property_names:
                match = next((prop for prop in props if prop.name == property_name), None)
                if match is None or not match.universal:
                    failed.append(property_name)
            if failed:
                raise _MinedPropertyFailure(failed)

        if validation_mode == "deep":
            original_examples = getattr(original_mine, "examples", 0)
            deep_examples = max(
                max_examples,
                original_examples,
                len(sample_inputs),
                20,
            )
            props = mine(current_func, max_examples=deep_examples).properties
            failed = []
            for property_name in property_names:
                match = next((prop for prop in props if prop.name == property_name), None)
                if match is None or not match.universal:
                    failed.append(property_name)
            if failed:
                raise _MinedPropertyFailure(failed)

    mutate_kwargs: dict[str, Any] = {}
    if contract_context:
        mutate_kwargs["contract_context"] = dict(contract_context)
    if _disk_mutation is not None:
        mutate_kwargs["disk_mutation"] = _disk_mutation
    result = mutate_function_and_test(
        target,
        mined_test,
        operators,
        **mutate_kwargs,
    )
    result.validation_sample_matrix_sha256 = sample_matrix.sha256
    result.property_observations = [
        {
            "name": str(prop.name),
            "holds": int(getattr(prop, "holds", 0)),
            "total": int(getattr(prop, "total", 0)),
        }
        for prop in universal
    ]
    return result


_BOUNDARY_VALUES: dict[type, list] = {
    int: [0, 1, -1, 2, -2],
    float: [0.0, 1.0, -1.0, 0.5, -0.5],
    bool: [True, False],
    str: ["", "a", "ab"],
    bytes: [b"", b"\x00", b"ab"],
}
