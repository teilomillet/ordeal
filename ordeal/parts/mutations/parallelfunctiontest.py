from __future__ import annotations
# ruff: noqa
def _parallel_function_test(
    target: str,
    test_fn: Callable[[], None],
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    module: types.ModuleType,
    func_name: str,
    workers: int,
) -> MutationResult:
    """Run mutant tests in parallel using a process pool.

    Pre-compiles all mutants, then distributes the test execution
    across *workers* processes.  Each worker activates one mutant
    via PatchFault, runs test_fn, and returns the result.
    """
    import multiprocessing as mp

    # Pre-compile mutants into (Mutant, callable) — filter out failures
    compiled: list[tuple[Mutant, Callable]] = []
    for mutant, mutated_tree in mutant_pairs:
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                continue
            compiled.append((mutant, mutated_func))
        except Exception:
            continue

    # Worker function: test one mutant
    def _test_one(args: tuple[int, str, str]) -> tuple[int, bool, str | None]:
        idx, op, desc = args
        _, mf = compiled[idx]
        fault = PatchFault(target, lambda orig, mf=mf: mf)
        fault.activate()
        try:
            test_fn()
            return (idx, False, None)
        except Exception as e:
            return (idx, True, str(e)[:200])
        finally:
            fault.deactivate()

    # Build work items
    work = [(i, m.operator, m.description) for i, (m, _) in enumerate(compiled)]

    # Execute in pool (use fork to share compiled state)
    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    with ctx.Pool(workers) as pool:
        outcomes = pool.map(_test_one, work)

    result = MutationResult(target=target)
    for idx, killed, error in outcomes:
        mutant, _ = compiled[idx]
        mutant.killed = killed
        mutant.error = error
        result.mutants.append(mutant)

    return result
def _is_function_target(target: str) -> bool:
    """Determine if a dotted path refers to a callable (vs a module)."""
    try:
        resolved = _resolve_mutation_target(target)
    except Exception:
        return False
    return resolved.leaf_name is not None
def mutate(
    target: str,
    test_fn: Callable[[], None] | None = None,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
    workers: int = 1,
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
    resume: bool = False,
    contract_context: Mapping[str, Any] | None = None,
) -> MutationResult:
    """Unified mutation testing entry point — auto-detects function vs module.

    Inspects *target* to decide whether it names a callable or a module,
    then delegates to :func:`mutate_function_and_test` or
    :func:`mutate_and_test` respectively.

    This is the function used by the ``@pytest.mark.mutate`` fixture and
    is the simplest way to run mutation testing programmatically::

        from ordeal.mutations import mutate

        result = mutate("myapp.scoring.compute", preset="standard")
        print(result.summary())

    Run only relevant tests per mutant (fast)::

        result = mutate("myapp.scoring.compute", test_filter="test_compute")

    With extra mutants written by an AI assistant or human::

        result = mutate("myapp.scoring.compute", preset="standard", extra_mutants=[
            ("off-by-one", "def compute(a, b):\\n    if a <= 0: ..."),
        ])

    Args:
        target: Dotted path to a function (e.g. ``"myapp.scoring.compute"``)
            or module (e.g. ``"myapp.scoring"``).
        test_fn: Zero-arg callable; should raise on failure.  When ``None``
            (default), auto-discovers tests via pytest in-process.
        operators: Explicit list of operator names. Mutually exclusive with
            *preset*.
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.
        workers: Parallel worker processes. Default ``1``.
        filter_equivalent: Skip equivalent mutants. Default ``True``.
        equivalence_samples: Samples for equivalence filtering. Default ``10``.
        extra_mutants: Source strings (or ``(description, source)`` tuples)
            to validate and add alongside rule-based mutants.
        llm: Optional callable for automated mutant generation.
        llm_equivalence: If ``True`` and *llm* is provided, use the LLM
            to filter surviving mutants for semantic equivalence.
        test_filter: Pytest ``-k`` expression to narrow which tests run
            against each mutant.  When ``None`` (default), derives a filter
            from the target module name.
        mutant_timeout: Maximum seconds for the mutant generation step.
            Prevents hanging on complex AST expressions (numpy, cv2).
        disk_mutation: Write the mutated source to disk so subprocesses
            (Ray workers, ``multiprocessing`` spawn) see the mutation.
            Default ``False`` (in-memory only, safe for parallel tests).
        resume: Reuse cached results when nothing has changed.  Cache is
            invalidated when **any** of these change:

            - Module source (any line)
            - Test files (``tests/test_<module>.py``, ``test_<module>_*.py``)
            - ``tests/conftest.py`` or ``conftest.py``
            - Lockfile (``uv.lock``, ``poetry.lock``, ``requirements.txt``)
            - Preset or operators
            - Test-selection and equivalence settings
            - Extra mutants or the custom test callable fingerprint

            Mine oracle results (``killed_by='mine()'``) are **never**
            cached because mine uses random inputs — re-running can
            discover more properties.

            LLM-generated mutants are also not cached because the supplied
            generator may return a different set of mutants on each run.

            **Caveat**: test files that don't follow ``test_<module>*.py``
            naming (e.g. ``test_battle.py``) are not tracked.  If you use
            ``test_filter`` to run non-standard test files, pass
            ``resume=False``.

            Default ``False`` (always run fresh).
    """
    # Resume: check cache before dispatching
    resolved_operators = _resolve_operators(operators, preset)
    resolved_disk_mutation = _resolve_disk_mutation(disk_mutation, target)
    config_hash = _mutation_cache_config_hash(
        test_fn=test_fn,
        test_filter=test_filter,
        filter_equivalent=filter_equivalent,
        equivalence_samples=equivalence_samples,
        extra_mutants=extra_mutants,
        llm=llm,
        llm_equivalence=llm_equivalence,
        concern=concern,
        mutant_timeout=mutant_timeout,
        disk_mutation=resolved_disk_mutation,
        contract_context=contract_context,
    )
    if resume:
        try:
            module_hash = _module_source_hash(target)
            cached = _load_cache(target, module_hash, preset, resolved_operators, config_hash)
            if cached is not None:
                return cached
        except Exception:
            pass

    dispatch = mutate_function_and_test if _is_function_target(target) else mutate_and_test
    result = dispatch(
        target,
        test_fn=test_fn,
        operators=operators,
        preset=preset,
        workers=workers,
        filter_equivalent=filter_equivalent,
        equivalence_samples=equivalence_samples,
        extra_mutants=extra_mutants,
        llm=llm,
        llm_equivalence=llm_equivalence,
        concern=concern,
        test_filter=test_filter,
        mutant_timeout=mutant_timeout,
        disk_mutation=resolved_disk_mutation,
        promote_clusters_only=promote_clusters_only,
        cluster_min_size=cluster_min_size,
        contract_context=contract_context,
    )

    # Save cache after fresh run — but NOT if the result came from the
    # mine oracle (primary or fallback), which is stochastic. mine() uses
    # random inputs, so re-running can discover more properties.
    mine_used = any(m.killed_by == "mine()" for m in result.mutants)
    if resume and config_hash is not None and not mine_used:
        try:
            module_hash = _module_source_hash(target)
            _save_cache(target, result, module_hash, config_hash)
        except Exception:
            pass

    return result
def mutation_faults(
    target: str,
    operators: list[str] | None = None,
    *,
    preset: PresetName | None = None,
) -> list[tuple[Mutant, PatchFault]]:
    """Generate :class:`PatchFault` objects for each mutant of a function.

    Each fault, when activated, replaces the target function with a mutated
    version.  Use with the Explorer to let the nemesis toggle mutations
    during coverage-guided exploration::

        explorer = Explorer(MyTest, mutation_targets=["myapp.scoring.compute"])

    Args:
        target: Dotted path to the function (e.g. ``"myapp.scoring.compute"``).
        operators: Mutation operators to use (default: all).
        preset: Named operator group: ``"essential"``, ``"standard"``,
            or ``"thorough"``. Mutually exclusive with *operators*.

    Returns:
        List of ``(Mutant, PatchFault)`` pairs.
    """
    operators = _resolve_operators(operators, preset)
    target_spec = _resolve_mutation_target(target)
    module = target_spec.module
    func_name = target_spec.leaf_name
    if func_name is None:
        raise ValueError(f"Function-level mutation target expected, got module {target!r}")
    func = _unwrap_func(_resolved_target_callable(target_spec))
    source = _get_source(func)

    results: list[tuple[Mutant, PatchFault]] = []
    for mutant, mutated_tree in generate_mutants(source, operators):
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)  # noqa: S102
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                continue
        except Exception:
            continue

        fault = PatchFault(
            target,
            lambda orig, mf=mutated_func: mf,
            name=f"mutant({mutant.operator}@L{mutant.line}:{mutant.description})",
        )
        results.append((mutant, fault))

    return results
