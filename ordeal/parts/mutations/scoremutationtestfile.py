from __future__ import annotations


# ruff: noqa
def _score_mutation_test_file(
    path: str,
    *,
    target: str,
    module_name: str,
    func_name: str | None,
) -> int:
    """Score how likely a test file is to execute the mutation target."""
    try:
        source = Path(path).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=path)
    except Exception:
        return 0

    def _iter_test_nodes(module_tree: ast.Module) -> list[ast.AST]:
        tests: list[ast.AST] = []
        for node in module_tree.body:
            is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_func and node.name.startswith("test_"):
                tests.append(node)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for item in node.body:
                    is_fn = isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    if is_fn and item.name.startswith("test_"):
                        tests.append(item)
        return tests

    def _walk_test_body(node: ast.AST) -> list[ast.AST]:
        stack = list(ast.iter_child_nodes(node))
        walked: list[ast.AST] = []
        while stack:
            current = stack.pop()
            walked.append(current)
            is_scope = isinstance(
                current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            )
            if is_scope:
                continue
            stack.extend(ast.iter_child_nodes(current))
        return walked

    module_scope_aliases: set[str] = set()
    module_scope_direct_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_scope_aliases.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom) and node.module == module_name:
            for alias in node.names:
                if alias.name == "*" or func_name is None or alias.name == func_name:
                    module_scope_direct_names.add(alias.asname or alias.name)

    score = 0
    for test_node in _iter_test_nodes(tree):
        module_aliases = set(module_scope_aliases)
        direct_names = set(module_scope_direct_names)
        for current in _walk_test_body(test_node):
            if isinstance(current, ast.Import):
                for alias in current.names:
                    if alias.name == module_name:
                        module_aliases.add(alias.asname or alias.name.split(".")[-1])
            elif isinstance(current, ast.ImportFrom) and current.module == module_name:
                for alias in current.names:
                    if alias.name == "*" or func_name is None or alias.name == func_name:
                        direct_names.add(alias.asname or alias.name)
            elif isinstance(current, ast.Call):
                call = current.func
                if func_name is None:
                    if isinstance(call, ast.Name) and call.id in direct_names:
                        score += 4
                    elif (
                        isinstance(call, ast.Attribute)
                        and isinstance(call.value, ast.Name)
                        and call.value.id in module_aliases
                    ):
                        score += 4
                else:
                    if isinstance(call, ast.Name) and call.id in direct_names:
                        score += 12
                    elif (
                        isinstance(call, ast.Attribute)
                        and isinstance(call.value, ast.Name)
                        and call.value.id in module_aliases
                        and call.attr == func_name
                    ):
                        score += 10
    return score


@functools.lru_cache(maxsize=128)
def _mutation_test_selection(
    target: str,
    test_filter: str | None = None,
) -> _MutationTestSelection:
    """Choose pytest file/path arguments for mutation testing."""
    module_name, func_name = _split_mutation_target(target)
    named = list(_named_mutation_test_candidates(module_name))
    profile = _load_mutation_execution_profile(target)
    observed_path_scores: dict[str, int] = {}
    if profile is not None:
        known_paths = {str(Path(path).resolve()) for path in named}
        for nodeid, count in profile.kill_counts.items():
            path = nodeid.split("::", 1)[0]
            resolved = str(Path(path).resolve())
            observed_path_scores[resolved] = observed_path_scores.get(resolved, 0) + (
                max(1, count) * 1_000
            )
            if Path(path).exists() and resolved not in known_paths:
                named.append(path)
                known_paths.add(resolved)
        for nodeid in profile.coverage_hits:
            path = nodeid.split("::", 1)[0]
            resolved = str(Path(path).resolve())
            observed_path_scores[resolved] = observed_path_scores.get(resolved, 0) + 100
            if Path(path).exists() and resolved not in known_paths:
                named.append(path)
                known_paths.add(resolved)

    scored: list[tuple[int, str]] = []
    ast_scores: list[tuple[str, int]] = []
    for path in named:
        score = _score_mutation_test_file(
            path,
            target=target,
            module_name=module_name,
            func_name=func_name,
        )
        score += observed_path_scores.get(str(Path(path).resolve()), 0)
        if score > 0:
            scored.append((score, path))
            ast_scores.extend(
                _score_mutation_test_nodes(
                    path,
                    module_name=module_name,
                    func_name=func_name,
                )
            )

    if not scored and func_name is not None:
        for path in _additional_mutation_test_candidates(module_name):
            score = _score_mutation_test_file(
                path,
                target=target,
                module_name=module_name,
                func_name=func_name,
            )
            if score > 0:
                scored.append((score, path))
                ast_scores.extend(
                    _score_mutation_test_nodes(
                        path,
                        module_name=module_name,
                        func_name=func_name,
                    )
                )

    if scored:
        seen: set[str] = set()
        ranked_paths: list[str] = []
        for _, path in sorted(scored, key=lambda item: (-item[0], item[1])):
            if path not in seen:
                seen.add(path)
                ranked_paths.append(path)
        return _MutationTestSelection(
            paths=tuple(ranked_paths),
            k_filter=test_filter,
            ast_scores=tuple(ast_scores),
        )

    if test_filter is not None:
        return _MutationTestSelection(
            paths=tuple(named),
            k_filter=test_filter,
            ast_scores=(),
        )

    broad_paths = tuple(named) or _all_test_files()
    return _MutationTestSelection(paths=broad_paths, k_filter=None, ast_scores=())


def _raise_no_tests_found(target: str) -> None:
    """Raise :class:`NoTestsFoundError` with the standard guidance."""
    short = _split_mutation_target(target)[0].rsplit(".", 1)[-1]
    suggested = f"tests/test_{short}.py"
    raise NoTestsFoundError(
        f"No tests found for {target!r}. "
        "Mutation score is meaningless without tests.\n"
        f"  Generate: generate_starter_tests({target!r})\n"
        f"  CLI:      ordeal init {target}\n"
        f"  Save to:  {suggested}",
        target=target,
        suggested_file=suggested,
    )


def _batch_module_test(
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
    """Run all mutants in a single pytest session via a custom plugin.

    Instead of starting a new pytest session per mutant, collects tests
    once and replays them for each mutant — cutting out repeated startup.
    """
    import pytest

    results: list[tuple[Mutant, bool, str | None, str | None]] = []
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

    class _BatchPlugin:
        """Pytest plugin that tests multiple mutants in one session."""

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
            """Override the default test loop to iterate mutants."""
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
                for mutant, mutated_tree in mutant_pairs:
                    killed = False
                    error = None
                    killer = None
                    try:
                        cm = (
                            _mutated_module_on_disk(target, mutated_tree)
                            if disk_mutation
                            else _mutated_module(target, mutated_tree)
                        )
                        with cm:
                            if not disk_mutation:
                                importlib.invalidate_caches()
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
                    except Exception as e:
                        killed = True
                        error = str(e)[:200]
                    # Reset failures for next mutant
                    session.testsfailed = 0
                    results.append((mutant, killed, error, killer))
            return True  # prevent default loop from running

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
            fallback_results = _batch_module_test(
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
            mutant_count=len(mutant_pairs),
            pytest_seconds=phase_timings.get("pytest_seconds", 0.0),
            workers=1,
            baseline_fingerprint=baseline_fingerprint,
        )
    return results


def _parallel_module_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    workers: int,
    *,
    test_filter: str | None = None,
    disk_mutation: bool = False,
    timings: dict[str, float] | None = None,
    baseline_validated: bool = False,
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Run module-level mutants in parallel, each worker batch-testing a chunk.

    Divides *mutant_pairs* across *workers* processes.  Each worker runs
    a single pytest session that iterates its chunk — combining the startup
    savings of batch mode with parallelism.
    """
    import multiprocessing as mp

    # Serialize mutant pairs: ast.Module doesn't pickle, so send source text
    serialized: list[tuple[Mutant, str]] = []
    for mutant, tree in mutant_pairs:
        try:
            serialized.append((mutant, ast.unparse(tree)))
        except Exception:
            continue

    # Chunk work across workers
    chunk_size = max(1, (len(serialized) + workers - 1) // workers)
    chunks: list[list[tuple[Mutant, str]]] = []
    for i in range(0, len(serialized), chunk_size):
        chunks.append(serialized[i : i + chunk_size])

    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    phase_timings = timings if timings is not None else {}
    with _timed_phase(phase_timings, "pytest_seconds"):
        with ctx.Pool(min(workers, len(chunks))) as pool:
            chunk_results = pool.map(
                _parallel_module_batch_worker,
                [
                    (target, chunk, test_filter, disk_mutation, baseline_validated)
                    for chunk in chunks
                ],
            )

    # Flatten results
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
            fallback_results = _batch_module_test(
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


def _parallel_module_batch_worker(
    args: tuple[str, list[tuple[Mutant, str]], str | None, bool, bool],
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Re-parse ASTs and batch-test one module-level chunk."""
    target, chunk, test_filter, disk_mutation, baseline_validated = args
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
    )
