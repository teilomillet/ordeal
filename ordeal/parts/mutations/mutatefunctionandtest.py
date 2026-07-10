from __future__ import annotations


# ruff: noqa
def mutate_function_and_test(
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
    """Mutate a single function and run tests against each mutant.

    This is the **recommended** entry point for mutation testing. It uses
    :class:`PatchFault` to swap the function at its module attribute, so any
    code that accesses it via ``mod.func()`` will see the mutant.

    Example — minimal (auto-discovers tests via pytest)::

        from ordeal import mutate_function_and_test

        result = mutate_function_and_test("myapp.scoring.compute", preset="standard")
        print(result.summary())

    Example — AI assistant writes extra mutants directly::

        result = mutate_function_and_test(
            "myapp.scoring.compute",
            preset="standard",
            extra_mutants=[
                ("off-by-one", "def compute(a, b):\\n    if a <= 0: ..."),
                ("wrong var", "def compute(a, b):\\n    if b < 0: ..."),
            ],
        )

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        test_fn: Zero-arg callable; should raise on failure.  When ``None``
            (default), auto-discovers and runs relevant tests via pytest
            in-process (``pytest -x -k <module_name>``).
        operators: Explicit list of operator names to apply. See
            ``OPERATORS.keys()`` for all available operators. Mutually
            exclusive with *preset*.
        preset: Named operator group — pick one:

            - ``"essential"`` — 4 operators, fast feedback.
            - ``"standard"`` — 8 operators, good CI default.
            - ``"thorough"`` — all 14 operators, comprehensive.

            Mutually exclusive with *operators*. When neither is given,
            all operators are used.
        workers: Number of parallel worker processes. ``0`` (default)
            selects from mutant count, collected tests, and prior observed
            timing. A positive value is an explicit override.
        filter_equivalent: If ``True`` (default), skip mutants that produce
            identical output to the original on random sample inputs.
            Reduces noise from equivalent mutants that always survive.
        equivalence_samples: Number of random inputs for equivalence
            filtering.  Default ``10``.
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            written by an AI assistant, human, or any author.  Validated
            (parse, compile, dedup) and added alongside rule-based mutants.
            This is the primary way to supply custom mutants — no API call
            needed, just write the code.
        llm: Optional callable ``(prompt: str) -> str`` for automated
            mutant generation.  Convenience for pipelines — under the hood
            it feeds results through the same validation as *extra_mutants*.
        llm_equivalence: If ``True`` and *llm* is provided, use the LLM
            to filter surviving mutants that are semantically equivalent.
        test_filter: Pytest ``-k`` expression to narrow which tests run
            against each mutant. When ``None`` (default), ranks likely killer
            tests and retains a broad fallback for surviving mutants.
        mutant_timeout: Maximum seconds for the mutant generation step.
            When exceeded, returns whatever mutants have been generated so
            far.  Prevents hanging on complex AST expressions (numpy, cv2).
    """
    started_at = time.perf_counter()
    timings: dict[str, float] = {}
    # Try auto-discovering tests; fall back to mine()-based oracle
    disk_mutation = _resolve_disk_mutation(disk_mutation, target)
    # Resume: check cache before doing any work
    used_preset = preset
    operators = _resolve_operators(operators, preset)
    auto_discovered_tests = test_fn is None
    if test_fn is None:
        test_fn = _auto_test_fn(target, test_filter=test_filter)
    target_spec = _resolve_mutation_target(target)
    module = target_spec.module
    func_name = target_spec.leaf_name
    if func_name is None:
        raise ValueError(f"Function-level mutation target expected, got module {target!r}")
    func = _unwrap_func(_resolved_target_callable(target_spec))
    source = _get_source(func)

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

    assert test_fn is not None
    result = MutationResult(
        target=target,
        operators_used=operators,
        preset_used=used_preset,
        concern=concern,
        promote_clusters_only=promote_clusters_only,
        cluster_min_size=cluster_min_size,
        contract_context=dict(contract_context or {}),
    )

    if auto_discovered_tests and mutant_pairs:
        with _timed_phase(timings, "equivalence_seconds"):
            mutant_pairs = _filter_function_mutant_pairs(
                func,
                module,
                func_name,
                mutant_pairs,
                filter_equivalent=filter_equivalent,
                equivalence_samples=equivalence_samples,
                stats=stats,
            )
        if not mutant_pairs:
            result.diagnostics.update(stats)
            result.diagnostics["tested"] = 0
            result.timings.update(timings)
            result.timings["total_seconds"] = time.perf_counter() - started_at
            return result
        selection = _mutation_test_selection(target, test_filter=test_filter)
        profile = _load_mutation_execution_profile(target)
        selected_test_count = _selected_mutation_test_count(selection, profile)
        effective_workers = _resolve_mutation_worker_count(
            workers,
            mutant_count=len(mutant_pairs),
            selected_test_count=selected_test_count,
            profile=profile,
            disk_mutation=disk_mutation,
        )
        try:
            with _timed_phase(timings, "test_execution_seconds"):
                batch_results = []
                remaining_pairs = mutant_pairs
                if _needs_mutation_worker_preflight(
                    workers,
                    preliminary_workers=effective_workers,
                    profile=profile,
                    disk_mutation=disk_mutation,
                ):
                    batch_results = _batch_function_test(
                        target,
                        mutant_pairs[:1],
                        test_filter=test_filter,
                        disk_mutation=disk_mutation,
                        stats=stats,
                        timings=timings,
                    )
                    remaining_pairs = mutant_pairs[1:]
                    profile = _load_mutation_execution_profile(target)
                    selection = _mutation_test_selection(target, test_filter=test_filter)
                    selected_test_count = _selected_mutation_test_count(selection, profile)
                    effective_workers = _resolve_mutation_worker_count(
                        workers,
                        mutant_count=len(mutant_pairs),
                        selected_test_count=selected_test_count,
                        profile=profile,
                        disk_mutation=disk_mutation,
                    )
                stats["workers_selected"] = effective_workers
                if effective_workers > 1 and len(remaining_pairs) > 1:
                    batch_results.extend(
                        _parallel_function_batch_test(
                            target,
                            remaining_pairs,
                            effective_workers,
                            test_filter=test_filter,
                            disk_mutation=disk_mutation,
                            stats=stats,
                            timings=timings,
                        )
                    )
                elif remaining_pairs:
                    batch_results.extend(
                        _batch_function_test(
                            target,
                            remaining_pairs,
                            test_filter=test_filter,
                            disk_mutation=disk_mutation,
                            stats=stats,
                            timings=timings,
                        )
                    )
        except NoTestsFoundError:
            with _timed_phase(timings, "mine_fallback_seconds"):
                mine_result = _mine_based_mutation_test(
                    target,
                    func,
                    func_name,
                    module,
                    mutant_pairs,
                    filter_equivalent=filter_equivalent,
                    equivalence_samples=equivalence_samples,
                    operators_used=operators,
                    preset_used=used_preset,
                    contract_context=contract_context,
                    _stats=stats,
                )
            import warnings

            warnings.warn(
                f"{target!r}: tests killed 0/0 mutants because no matching tests "
                f"were collected; mine oracle killed "
                f"{mine_result.killed}/{mine_result.total}. "
                "Falling back to mine oracle evidence.",
                UserWarning,
                stacklevel=2,
            )
            mine_result.diagnostics["fallback_reason"] = "no_tests"
            mine_result.diagnostics.update(stats)
            mine_result.diagnostics["tested"] = mine_result.total
            mine_result.timings.update(timings)
            mine_result.timings["total_seconds"] = time.perf_counter() - started_at
            return mine_result
        for mutant, killed, error, killer in batch_results:
            mutant.killed = killed
            mutant.error = error
            mutant.killed_by = killer
            result.mutants.append(mutant)
    elif workers > 1 and not disk_mutation:
        with _timed_phase(timings, "test_execution_seconds"):
            result = _parallel_function_test(
                target,
                test_fn,
                mutant_pairs,
                module,
                func_name,
                workers,
            )
        result.preset_used = used_preset
        result.operators_used = operators
        result.concern = concern
    else:
        with _timed_phase(timings, "test_execution_seconds"):
            for mutant, mutated_tree in mutant_pairs:
                # Compile the mutated function in the module's namespace
                try:
                    code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
                    namespace = dict(module.__dict__)
                    exec(code, namespace)
                    mutated_func = namespace.get(func_name)
                    if mutated_func is None:
                        stats["compilation_failed"] = stats.get("compilation_failed", 0) + 1
                        continue
                except Exception:
                    stats["compilation_failed"] = stats.get("compilation_failed", 0) + 1
                    continue

                # Runtime equivalence filter: skip if outputs match on samples
                if filter_equivalent and _is_runtime_equivalent(
                    func, mutated_func, n_samples=equivalence_samples
                ):
                    stats["filtered_runtime_equivalent"] = (
                        stats.get("filtered_runtime_equivalent", 0) + 1
                    )
                    continue

                # Swap via PatchFault (in-process) + optional disk mutation (subprocesses)
                fn_name = getattr(test_fn, "__qualname__", getattr(test_fn, "__name__", "test_fn"))
                fault = PatchFault(target, lambda orig, mf=mutated_func: mf)
                disk_cm = (
                    _function_mutated_on_disk(target_spec, mutated_tree)
                    if disk_mutation
                    else contextlib.nullcontext()
                )
                with disk_cm:
                    fault.activate()
                    try:
                        test_fn()
                        mutant.killed = False
                    except Exception as e:
                        mutant.killed = True
                        mutant.error = str(e)[:200]
                        property_names = getattr(e, "property_names", None)
                        if property_names:
                            names = [str(name) for name in property_names]
                            mutant.metadata["killed_by_properties"] = names
                            mutant.killed_by = f"property:{names[0]}"
                        else:
                            mutant.killed_by = fn_name
                    finally:
                        fault.deactivate()

                result.mutants.append(mutant)

    # 0% score fallback: if auto-discovered tests killed nothing, try mine
    # oracle directly.  This catches process-isolation (Ray workers, long-lived
    # pools) where PatchFault / disk mutation are invisible to test workers.
    # Only for auto-discovered tests — if the user provided test_fn, the 0%
    # score is the correct result (their test may intentionally be weak).
    if result.total > 0 and result.killed == 0 and auto_discovered_tests:
        with _timed_phase(timings, "mine_fallback_seconds"):
            mine_result = _mine_based_mutation_test(
                target,
                func,
                func_name,
                module,
                # Re-generate mutant pairs (originals were consumed)
                generate_mutants(
                    source,
                    operators,
                    extra_mutants=extra_mutants,
                    concern=concern,
                    timeout=mutant_timeout,
                ),
                filter_equivalent=filter_equivalent,
                equivalence_samples=equivalence_samples,
                operators_used=operators,
                preset_used=used_preset,
                contract_context=contract_context,
                _stats={},
            )
        if mine_result.killed > 0:
            import warnings

            warnings.warn(
                f"{target!r}: tests killed 0/{result.total} mutants but mine oracle "
                f"killed {mine_result.killed}/{mine_result.total} — your tests likely "
                "exercise this function through a process boundary (Ray, multiprocessing) "
                "where in-memory mutations are invisible. "
                "Falling back to mine oracle for accurate results.",
                stacklevel=2,
            )
            mine_result.diagnostics["fallback_reason"] = "process_isolation"
            mine_result.diagnostics.update(stats)
            mine_result.diagnostics["tested"] = mine_result.total
            mine_result.timings.update(timings)
            mine_result.timings["total_seconds"] = time.perf_counter() - started_at
            return mine_result

    # LLM equivalence filter on surviving mutants
    if llm_equivalence and llm is not None:
        for mutant in result.survived:
            if mutant._mutant_source:
                try:
                    if _is_llm_equivalent(source, mutant._mutant_source, llm):
                        mutant.killed = True
                        mutant.error = "equivalent (LLM-detected)"
                        mutant.killed_by = "llm_equivalence"
                except Exception:
                    pass

    result.diagnostics.update(stats)
    result.diagnostics["tested"] = result.total
    result.timings.update(timings)
    result.timings["total_seconds"] = time.perf_counter() - started_at
    return result


def _mine_based_mutation_test(
    target: str,
    func: Callable,
    func_name: str,
    module: types.ModuleType,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    *,
    filter_equivalent: bool,
    equivalence_samples: int,
    operators_used: list[str] | None,
    preset_used: str | None,
    contract_context: Mapping[str, Any] | None = None,
    _stats: dict[str, int] | None = None,
) -> MutationResult:
    """Kill mutants using mine()-discovered properties as the test oracle.

    When no human-written tests exist, mine the original function to
    discover properties (bounded, no NaN, deterministic, etc.), then
    check whether each mutant violates any of those properties.
    """
    from ordeal.mine import mine

    # Mine the original function
    mine_result = mine(func, max_examples=50)
    universal = [p for p in mine_result.properties if p.universal and p.total > 0]
    if not universal:
        short = _split_mutation_target(target)[0].rsplit(".", 1)[-1]
        raise NoTestsFoundError(
            f"No tests found for {target!r} and mine() discovered no properties. "
            "Cannot validate mutations.\n"
            f"  Generate: generate_starter_tests({target!r})\n"
            f"  CLI:      ordeal init {target}",
            target=target,
            suggested_file=f"tests/test_{short}.py",
        )

    # Collect sample inputs from mining
    sample_inputs = mine_result.collected_inputs[:50]
    if not sample_inputs:
        # Generate fresh inputs
        from ordeal.auto import _infer_strategies

        strats = _infer_strategies(func, None)
        if strats:
            for _ in range(50):
                try:
                    sample_inputs.append({k: v.example() for k, v in strats.items()})
                except Exception:
                    break

    st = _stats  # alias for brevity
    result = MutationResult(
        target=target,
        operators_used=operators_used,
        preset_used=preset_used,
        contract_context=dict(contract_context or {}),
    )

    for mutant, mutated_tree in mutant_pairs:
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)  # noqa: S102
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                if st is not None:
                    st["compilation_failed"] = st.get("compilation_failed", 0) + 1
                continue
        except Exception:
            if st is not None:
                st["compilation_failed"] = st.get("compilation_failed", 0) + 1
            continue

        if filter_equivalent and _is_runtime_equivalent(
            func, mutated_func, n_samples=equivalence_samples
        ):
            if st is not None:
                st["filtered_runtime_equivalent"] = st.get("filtered_runtime_equivalent", 0) + 1
            continue

        # Check if the mutant violates any mined property
        killed = False
        for inputs in sample_inputs:
            if killed:
                break
            try:
                orig_out = func(**inputs)
                mut_out = mutated_func(**inputs)
                # Different output = mutant detected
                if orig_out != mut_out:
                    killed = True
                    mutant.killed = True
                    mutant.error = f"mine oracle: output differs on {inputs}"
                    mutant.killed_by = "mine()"
            except Exception:
                # Mutant crashes = killed
                killed = True
                mutant.killed = True
                mutant.error = "mine oracle: mutant raised exception"
                mutant.killed_by = "mine()"

        if not killed:
            mutant.killed = False

        result.mutants.append(mutant)

    return result
