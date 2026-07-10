from __future__ import annotations


# ruff: noqa
def _filter_function_mutant_pairs(
    func: Callable,
    module: types.ModuleType,
    func_name: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    *,
    filter_equivalent: bool,
    equivalence_samples: int,
    stats: dict[str, int],
) -> list[tuple[Mutant, ast.Module]]:
    """Filter function mutants before the batched pytest fast path."""
    if not filter_equivalent:
        return mutant_pairs

    filtered: list[tuple[Mutant, ast.Module]] = []
    for mutant, mutated_tree in mutant_pairs:
        try:
            code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
            namespace = dict(module.__dict__)
            exec(code, namespace)  # noqa: S102
            mutated_func = namespace.get(func_name)
            if mutated_func is None:
                stats["compilation_failed"] = stats.get("compilation_failed", 0) + 1
                continue
        except Exception:
            stats["compilation_failed"] = stats.get("compilation_failed", 0) + 1
            continue

        if _is_runtime_equivalent(func, mutated_func, n_samples=equivalence_samples):
            stats["filtered_runtime_equivalent"] = stats.get("filtered_runtime_equivalent", 0) + 1
            continue
        filtered.append((mutant, mutated_tree))

    return filtered


def _batch_function_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    *,
    test_filter: str | None = None,
    disk_mutation: bool = False,
    stats: dict[str, int] | None = None,
    timings: dict[str, float] | None = None,
    persist_profile: bool = True,
    selection_override: _MutationTestSelection | None = None,
    allow_broad_fallback: bool = True,
    baseline_validated: bool = False,
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Run function mutants in a single pytest session.

    This keeps function-level mutation testing on the same fast path as
    module-level mutation testing when tests are auto-discovered via
    pytest: collect once, replay for each mutant, and avoid paying
    pytest startup cost per mutant.
    """
    import pytest

    target_spec = _resolve_mutation_target(target)
    module = target_spec.module
    func_name = target_spec.leaf_name
    if func_name is None:
        raise ValueError(f"Function-level mutation target expected, got module {target!r}")
    phase_timings = timings if timings is not None else {}
    with _timed_phase(phase_timings, "test_selection_seconds"):
        selection = selection_override or _mutation_test_selection(
            target,
            test_filter=test_filter,
        )
    profile = _load_mutation_execution_profile(target)
    baseline_fingerprint = _mutation_test_baseline_fingerprint(target, selection)
    baseline_validated = baseline_validated or bool(
        profile and profile.baseline_fingerprint == baseline_fingerprint
    )
    if stats is not None:
        stats["selected_test_files"] = len(selection.paths)
        stats["selected_test_nodes"] = len(selection.ast_scores)

    compiled: list[tuple[Mutant, Callable, ast.Module]] = []
    with _timed_phase(phase_timings, "compile_seconds"):
        for mutant, mutated_tree in mutant_pairs:
            try:
                code = compile(mutated_tree, f"<mutant:{mutant.description}>", "exec")
                namespace = dict(module.__dict__)
                exec(code, namespace)  # noqa: S102
                mutated_func = namespace.get(func_name)
                if mutated_func is None:
                    continue
                compiled.append((mutant, mutated_func, mutated_tree))
            except Exception:
                continue

    results: list[tuple[Mutant, bool, str | None, str | None]] = []

    class _BatchPlugin:
        """Pytest plugin that replays a collected session across mutants."""

        def __init__(self) -> None:
            self.no_tests_found = False
            self.no_target_tests = False
            self.baseline_failed = False
            self.collected_tests = 0
            self.coverage_hits: set[str] = set()
            self.coverage_calibrated = False
            self.executed_tests = 0

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session: pytest.Session) -> bool:
            if not session.config.option.collectonly:
                self.collected_tests = len(session.items)
                if not session.items:
                    self.no_tests_found = True
                    return True
                prior_coverage = set(profile.coverage_hits) if profile is not None else set()
                self.coverage_hits = {
                    str(item.nodeid)
                    for item in session.items
                    if str(item.nodeid) in prior_coverage
                }
                self.coverage_calibrated = bool(profile and profile.coverage_calibrated)
                stale_coverage = bool(
                    profile
                    and profile.coverage_calibrated
                    and profile.coverage_hits
                    and not self.coverage_hits
                )
                calibrated_now = False
                if persist_profile and (not self.coverage_calibrated or stale_coverage):
                    self.coverage_hits = _calibrate_mutation_test_coverage(session, target)
                    self.coverage_calibrated = True
                    calibrated_now = True
                    self.baseline_failed = bool(
                        getattr(session, "_ordeal_mutation_baseline_failed", False)
                    )
                    if self.baseline_failed:
                        return True
                elif not baseline_validated:
                    self.baseline_failed = _mutation_test_baseline_fails(session)
                    if self.baseline_failed:
                        return True
                if (
                    persist_profile
                    and calibrated_now
                    and not self.coverage_hits
                    and not selection.ast_scores
                    and not disk_mutation
                ):
                    self.no_target_tests = True
                    return True
                for mutant, mutated_func, mutated_tree in compiled:
                    killed = False
                    error = None
                    killer = None
                    fault = PatchFault(target, lambda orig, mf=mutated_func: mf)
                    disk_cm = (
                        _function_mutated_on_disk(target_spec, mutated_tree)
                        if disk_mutation
                        else contextlib.nullcontext()
                    )
                    with disk_cm:
                        fault.activate()
                        try:
                            ordered_items = _order_mutation_test_items(
                                session.items,
                                mutant=mutant,
                                selection=selection,
                                coverage_hits=self.coverage_hits,
                                profile=profile,
                            )
                            for i, item in enumerate(ordered_items):
                                nxt = ordered_items[i + 1] if i + 1 < len(ordered_items) else None
                                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nxt)
                                self.executed_tests += 1
                                if item.session.testsfailed:
                                    killed = True
                                    killer = item.nodeid
                                    error = f"{item.nodeid} failed"
                                    break
                        except Exception as exc:
                            killed = True
                            error = str(exc)[:200]
                        finally:
                            fault.deactivate()
                    session.testsfailed = 0
                    results.append((mutant, killed, error, killer))
            return True

    plugin = _BatchPlugin()
    with _timed_phase(phase_timings, "pytest_seconds"):
        with _disable_seed_replay():
            pytest.main(
                [
                    "-x",
                    "-q",
                    "--tb=no",
                    "--no-header",
                    "--chaos",
                    "-o",
                    "addopts=",
                    *selection.pytest_args(),
                ],
                plugins=[plugin],
            )
    if stats is not None:
        stats["collected_tests"] = plugin.collected_tests
        stats["coverage_selected_tests"] = len(plugin.coverage_hits)
        stats["executed_tests"] = plugin.executed_tests
    if plugin.no_tests_found:
        _raise_no_tests_found(target)
    if plugin.baseline_failed:
        raise RuntimeError(
            f"Selected tests for {target!r} fail before mutation; "
            "cannot attribute a reliable mutant kill"
        )
    if plugin.no_target_tests:
        _raise_no_tests_found(target)
    if allow_broad_fallback and test_filter is None:
        fallback_pairs = _surviving_mutant_pairs(mutant_pairs, results)
        broad_selection = (
            _broad_mutation_test_selection(target, selection) if fallback_pairs else None
        )
        if broad_selection is not None and fallback_pairs:
            fallback_results = _batch_function_test(
                target,
                fallback_pairs,
                test_filter=None,
                disk_mutation=disk_mutation,
                timings=phase_timings,
                persist_profile=False,
                selection_override=broad_selection,
                allow_broad_fallback=False,
            )
            results = _merge_mutation_batch_results(results, fallback_results)
    if persist_profile:
        _record_mutation_execution_profile(
            target,
            results,
            coverage_hits=plugin.coverage_hits,
            coverage_calibrated=plugin.coverage_calibrated,
            collected_tests=plugin.collected_tests,
            mutant_count=len(compiled),
            pytest_seconds=phase_timings.get("pytest_seconds", 0.0),
            workers=1,
            baseline_fingerprint=baseline_fingerprint,
        )
    return results


def _parallel_function_batch_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    workers: int,
    *,
    test_filter: str | None = None,
    disk_mutation: bool = False,
    stats: dict[str, int] | None = None,
    timings: dict[str, float] | None = None,
    baseline_validated: bool = False,
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Run function-level mutation batches in parallel worker processes."""
    import multiprocessing as mp

    serialized: list[tuple[Mutant, str]] = []
    for mutant, tree in mutant_pairs:
        try:
            serialized.append((mutant, ast.unparse(tree)))
        except Exception:
            continue
    if stats is not None:
        selection = _mutation_test_selection(target, test_filter=test_filter)
        stats["selected_test_files"] = len(selection.paths)

    chunk_size = max(1, (len(serialized) + workers - 1) // workers)
    chunks: list[list[tuple[Mutant, str]]] = []
    for i in range(0, len(serialized), chunk_size):
        chunks.append(serialized[i : i + chunk_size])

    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    phase_timings = timings if timings is not None else {}
    with _timed_phase(phase_timings, "pytest_seconds"):
        with ctx.Pool(min(workers, len(chunks))) as pool:
            chunk_results = pool.map(
                _parallel_function_batch_worker,
                [
                    (target, chunk, test_filter, disk_mutation, baseline_validated)
                    for chunk in chunks
                ],
            )

    results: list[tuple[Mutant, bool, str | None, str | None]] = []
    for chunk_result in chunk_results:
        results.extend(chunk_result)
    selection = _mutation_test_selection(target, test_filter=test_filter)
    if test_filter is None:
        fallback_pairs = _surviving_mutant_pairs(mutant_pairs, results)
        broad_selection = (
            _broad_mutation_test_selection(target, selection) if fallback_pairs else None
        )
        if broad_selection is not None and fallback_pairs:
            fallback_results = _batch_function_test(
                target,
                fallback_pairs,
                test_filter=None,
                disk_mutation=disk_mutation,
                timings=phase_timings,
                persist_profile=False,
                selection_override=broad_selection,
                allow_broad_fallback=False,
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
        baseline_fingerprint=_mutation_test_baseline_fingerprint(target, selection),
    )
    return results


def _parallel_function_batch_worker(
    args: tuple[str, list[tuple[Mutant, str]], str | None, bool, bool],
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Re-parse ASTs and batch-test one function-level chunk."""
    target, chunk, test_filter, disk_mutation, baseline_validated = args
    reparsed = []
    for mutant, source_text in chunk:
        try:
            reparsed.append((mutant, ast.parse(source_text)))
        except Exception:
            continue
    return _batch_function_test(
        target,
        reparsed,
        test_filter=test_filter,
        disk_mutation=disk_mutation,
        persist_profile=False,
        allow_broad_fallback=False,
        baseline_validated=baseline_validated,
    )
