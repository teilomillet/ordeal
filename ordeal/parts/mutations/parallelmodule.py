from __future__ import annotations


# ruff: noqa
def _parallel_module_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    workers: int,
    *,
    test_filter: str | None = None,
    disk_mutation: bool = False,
    timings: dict[str, float] | None = None,
    baseline_validated: bool = False,
    excluded_test: _PytestItemIdentity | None = None,
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Run module-level mutants in parallel, each worker batch-testing a chunk.

    Divides *mutant_pairs* across *workers* processes. Each worker runs
    a single pytest session that iterates its chunk, combining the startup
    savings of batch mode with parallelism.
    """
    import multiprocessing as mp

    excluded_test = excluded_test or _current_pytest_item_identity()
    serialized: list[tuple[Mutant, str]] = []
    for mutant, tree in mutant_pairs:
        try:
            serialized.append((mutant, ast.unparse(tree)))
        except Exception:
            continue

    chunk_size = max(1, (len(serialized) + workers - 1) // workers)
    chunks = [
        serialized[index : index + chunk_size]
        for index in range(0, len(serialized), chunk_size)
    ]

    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    phase_timings = timings if timings is not None else {}
    with _timed_phase(phase_timings, "pytest_seconds"):
        with ctx.Pool(min(workers, len(chunks))) as pool:
            chunk_results = pool.map(
                _parallel_module_batch_worker,
                [
                    (
                        target,
                        chunk,
                        test_filter,
                        disk_mutation,
                        baseline_validated,
                        excluded_test,
                    )
                    for chunk in chunks
                ],
            )

    results = [result for chunk in chunk_results for result in chunk]
    selection = _mutation_test_selection(target, test_filter=test_filter)
    if test_filter is None:
        fallback_pairs = _surviving_mutant_pairs(mutant_pairs, results)
        broad_selection = (
            _broad_mutation_test_selection(target, selection)
            if fallback_pairs
            else None
        )
        if broad_selection is not None and fallback_pairs:
            fallback_results = _batch_module_test(
                target,
                fallback_pairs,
                test_filter=None,
                disk_mutation=disk_mutation,
                timings=phase_timings,
                persist_profile=False,
                selection_override=broad_selection,
                allow_broad_fallback=False,
                excluded_test=excluded_test,
            )
            results = _merge_mutation_batch_results(results, fallback_results)
    prior = _load_mutation_execution_profile(target)
    collected_hint = (
        prior.collected_tests
        if prior is not None and prior.collected_tests > 0
        else max(1, len(selection.ast_scores), len(selection.paths))
    )
    _record_mutation_execution_profile(
        target,
        results,
        coverage_hits=set(),
        coverage_calibrated=False,
        collected_tests=collected_hint,
        mutant_count=len(serialized),
        pytest_seconds=phase_timings.get("pytest_seconds", 0.0),
        workers=min(workers, len(chunks)),
        baseline_fingerprint=_mutation_test_baseline_fingerprint(
            target,
            selection,
            excluded_test=excluded_test,
        ),
    )
    return results


def _parallel_module_batch_worker(
    args: tuple[
        str,
        list[tuple[Mutant, str]],
        str | None,
        bool,
        bool,
        _PytestItemIdentity | None,
    ],
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Re-parse ASTs and batch-test one module-level chunk."""
    (
        target,
        chunk,
        test_filter,
        disk_mutation,
        baseline_validated,
        excluded_test,
    ) = args
    reparsed = []
    for mutant, source_text in chunk:
        try:
            reparsed.append((mutant, ast.parse(source_text)))
        except Exception:
            continue
    return _batch_module_test(
        target,
        reparsed,
        test_filter=test_filter,
        disk_mutation=disk_mutation,
        persist_profile=False,
        allow_broad_fallback=False,
        baseline_validated=baseline_validated,
        excluded_test=excluded_test,
    )
