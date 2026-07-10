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

    scored: list[tuple[int, str]] = []
    for path in named:
        score = _score_mutation_test_file(
            path,
            target=target,
            module_name=module_name,
            func_name=func_name,
        )
        if score > 0:
            scored.append((score, path))

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

    if not scored and func_name is not None:
        fallback_filter = test_filter if test_filter is not None else func_name
        return _MutationTestSelection(paths=tuple(named), k_filter=fallback_filter)

    if scored:
        seen: set[str] = set()
        ranked_paths: list[str] = []
        for _, path in sorted(scored, key=lambda item: (-item[0], item[1])):
            if path not in seen:
                seen.add(path)
                ranked_paths.append(path)
        return _MutationTestSelection(paths=tuple(ranked_paths), k_filter=test_filter)

    short = module_name.split(".")[-1]
    fallback_filter = test_filter if test_filter is not None else (func_name or f"test_{short}")
    return _MutationTestSelection(paths=(), k_filter=fallback_filter)
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
    disk_mutation: bool = False,
    stats: dict[str, int] | None = None,
    timings: dict[str, float] | None = None,
) -> list[tuple[Mutant, bool, str | None]]:
    """Run all mutants in a single pytest session via a custom plugin.

    Instead of starting a new pytest session per mutant, collects tests
    once and replays them for each mutant — cutting out repeated startup.
    """
    import pytest

    results: list[tuple[Mutant, bool, str | None]] = []
    phase_timings = timings if timings is not None else {}
    with _timed_phase(phase_timings, "test_selection_seconds"):
        selection = _mutation_test_selection(target)
    if stats is not None:
        stats["selected_test_files"] = len(selection.paths)

    class _BatchPlugin:
        """Pytest plugin that tests multiple mutants in one session."""

        def __init__(self) -> None:
            self.no_tests_found = False
            self.collected_tests = 0

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session: pytest.Session) -> bool:
            """Override the default test loop to iterate mutants."""
            if not session.config.option.collectonly:
                self.collected_tests = len(session.items)
                if not session.items:
                    self.no_tests_found = True
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
                            for i, item in enumerate(session.items):
                                nxt = session.items[i + 1] if i + 1 < len(session.items) else None
                                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nxt)
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
    if plugin.no_tests_found:
        _raise_no_tests_found(target)
    return results
def _parallel_module_test(
    target: str,
    mutant_pairs: list[tuple[Mutant, ast.Module]],
    workers: int,
) -> list[tuple[Mutant, bool, str | None]]:
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
    with ctx.Pool(min(workers, len(chunks))) as pool:
        chunk_results = pool.map(
            _parallel_module_batch_worker,
            [(target, chunk) for chunk in chunks],
        )

    # Flatten results
    results: list[tuple[Mutant, bool, str | None]] = []
    for chunk_result in chunk_results:
        results.extend(chunk_result)
    return results
def _parallel_module_batch_worker(
    args: tuple[str, list[tuple[Mutant, str]]],
) -> list[tuple[Mutant, bool, str | None]]:
    """Re-parse ASTs and batch-test one module-level chunk."""
    target, chunk = args
    reparsed = []
    for mutant, source_text in chunk:
        try:
            reparsed.append((mutant, ast.parse(source_text)))
        except Exception:
            continue
    return _batch_module_test(target, reparsed)
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
        selection = _mutation_test_selection(target, test_filter=test_filter)
    if stats is not None:
        stats["selected_test_files"] = len(selection.paths)

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
            self.collected_tests = 0

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session: pytest.Session) -> bool:
            if not session.config.option.collectonly:
                self.collected_tests = len(session.items)
                if not session.items:
                    self.no_tests_found = True
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
                            for i, item in enumerate(session.items):
                                nxt = session.items[i + 1] if i + 1 < len(session.items) else None
                                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nxt)
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
    if plugin.no_tests_found:
        _raise_no_tests_found(target)
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
    with _timed_phase(timings if timings is not None else {}, "pytest_seconds"):
        with ctx.Pool(min(workers, len(chunks))) as pool:
            chunk_results = pool.map(
                _parallel_function_batch_worker,
                [(target, chunk, test_filter, disk_mutation) for chunk in chunks],
            )

    results: list[tuple[Mutant, bool, str | None, str | None]] = []
    for chunk_result in chunk_results:
        results.extend(chunk_result)
    return results
def _parallel_function_batch_worker(
    args: tuple[str, list[tuple[Mutant, str]], str | None, bool],
) -> list[tuple[Mutant, bool, str | None, str | None]]:
    """Re-parse ASTs and batch-test one function-level chunk."""
    target, chunk, test_filter, disk_mutation = args
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
    )
