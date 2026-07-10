from __future__ import annotations
# ruff: noqa
def _append_seed_example(
    bucket: list[SeedExample],
    *,
    kwargs: dict[str, Any],
    source: str,
    evidence: str,
) -> None:
    """Append one seed example when its kwargs are not already present."""
    if not kwargs:
        return
    for existing in bucket:
        if existing.kwargs == kwargs:
            return
    bucket.append(SeedExample(kwargs=dict(kwargs), source=source, evidence=evidence))
def _source_file_for_callable(func: Any) -> Path | None:
    """Return the source file for *func* when available."""
    with contextlib.suppress(OSError, TypeError):
        source_file = inspect.getsourcefile(_unwrap(func)) or inspect.getfile(_unwrap(func))
        if source_file:
            return Path(source_file).resolve()
    return None
@functools.lru_cache(maxsize=128)
def _candidate_seed_files_cached(
    module_name: str,
    workspace_root: str,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return likely test files and project call-site files for *module_name*."""
    root_path = Path(workspace_root)
    test_files: list[Path] = []
    project_files: list[Path] = []

    tests_dir = root_path / "tests"
    if tests_dir.is_dir():
        with contextlib.suppress(Exception):
            from ordeal.audit import _find_test_file_evidence

            test_files = [
                Path(item.path)
                for item in _find_test_file_evidence(module_name, tests_dir)
                if Path(item.path).is_file()
            ]

    module_path_parts = module_name.split(".")
    roots = [root_path, root_path / "src"]
    module_path: Path | None = None
    for root in roots:
        candidate = root.joinpath(*module_path_parts)
        if candidate.with_suffix(".py").exists():
            module_path = candidate.with_suffix(".py")
            break
        if (candidate / "__init__.py").exists():
            module_path = candidate / "__init__.py"
            break
    if module_path is None:
        return tuple(test_files), tuple(project_files)

    package_root = module_path.parent
    for path in package_root.rglob("*.py"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == module_path.resolve():
            continue
        if any(part in {"tests", ".venv", "site-packages"} for part in resolved.parts):
            continue
        project_files.append(resolved)
        if len(project_files) >= 24:
            break
    return tuple(test_files), tuple(project_files)
def _candidate_seed_files(module_name: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return likely test files and project call-site files for *module_name*."""
    return _candidate_seed_files_cached(module_name, str(Path.cwd().resolve()))
def _fixture_literals_for_params(
    param_names: set[str],
    files: Sequence[Path],
) -> dict[str, list[Any]]:
    """Return literal pytest fixture values keyed by matching parameter name."""
    fixtures: dict[str, list[Any]] = {}
    catalog = _pytest_fixture_catalog(
        tuple(str(path.resolve()) for path in files if path.exists())
    )
    for name in sorted(param_names):
        values = list(catalog.get(name, {}).get("values", ()))
        if values:
            fixtures[name] = values
    return fixtures
def _seed_value_from_node(
    node: ast.AST,
    *,
    bindings: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve a seed value from a literal node or a bound local name."""
    if isinstance(node, ast.Name) and bindings and node.id in bindings:
        return bindings[node.id]
    return _literal_ast_value(node)
def _parametrize_arg_names(node: ast.AST) -> list[str]:
    """Return parameter names from a ``pytest.mark.parametrize`` decorator."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [item.strip() for item in node.value.split(",") if item.strip()]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                text = item.value.strip()
                if text:
                    names.append(text)
        return names
    return []
def _parametrize_bindings(values_node: ast.AST, names: Sequence[str]) -> list[dict[str, Any]]:
    """Return literal binding rows from a ``pytest.mark.parametrize`` value node."""
    if not names:
        return []
    rows_value = _literal_ast_value(values_node)
    if rows_value is _MISSING:
        return []
    if len(names) == 1 and not isinstance(rows_value, (list, tuple)):
        return [{names[0]: rows_value}]
    rows = list(rows_value) if isinstance(rows_value, (list, tuple)) else [rows_value]
    bindings: list[dict[str, Any]] = []
    for row in rows:
        if len(names) == 1:
            bindings.append({names[0]: row})
            continue
        if not isinstance(row, (list, tuple)) or len(row) != len(names):
            continue
        bindings.append(dict(zip(names, row, strict=False)))
    return bindings
def _function_parametrize_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict[str, Any]]:
    """Return merged literal binding rows for one parametrized test function."""
    cases: list[dict[str, Any]] = [{}]
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if _call_name(decorator.func) not in {"pytest.mark.parametrize", "mark.parametrize"}:
            continue
        if len(decorator.args) < 2:
            continue
        names = _parametrize_arg_names(decorator.args[0])
        rows = _parametrize_bindings(decorator.args[1], names) if names else []
        if not names or not rows:
            continue
        expanded: list[dict[str, Any]] = []
        for base in cases:
            for row in rows:
                merged = dict(base)
                merged.update(row)
                expanded.append(merged)
        if expanded:
            cases = expanded
    return cases or [{}]
def _call_seed_examples_from_files(
    func: Any,
    files: Sequence[Path],
    *,
    source: str,
) -> list[SeedExample]:
    """Extract literal call examples for *func* from Python files."""
    callable_obj = func
    target = _unwrap(callable_obj)
    try:
        sig = inspect.signature(target)
    except Exception:
        return []

    module_name = getattr(target, "__module__", "")
    leaf_name = getattr(target, "__name__", "")
    if not module_name or not leaf_name:
        return []
    method_name = str(getattr(callable_obj, "__ordeal_method_name__", leaf_name) or leaf_name)
    owner = getattr(callable_obj, "__ordeal_owner__", None)
    callable_kind = getattr(callable_obj, "__ordeal_kind__", None)
    class_tokens = {
        token.lower()
        for token in (
            [getattr(owner, "__name__", "")] + _camel_case_tokens(getattr(owner, "__name__", ""))
        )
        if token
    }

    param_names = [name for name in sig.parameters if name not in {"self", "cls"}]
    if not param_names:
        return []
    hidden_state_param = None
    if getattr(callable_obj, "__ordeal_state_factory__", None) is not None:
        hidden_state_param = getattr(callable_obj, "__ordeal_state_param__", None)

    examples: list[SeedExample] = []
    fixture_paths: set[Path] = {path.resolve() for path in files if path.exists()}
    for path in list(fixture_paths):
        for parent in [path.parent, *path.parents]:
            candidate = parent / "conftest.py"
            if candidate.exists():
                fixture_paths.add(candidate.resolve())
            if parent.resolve() == Path.cwd().resolve():
                break
    fixture_catalog = _pytest_fixture_catalog(tuple(str(path) for path in sorted(fixture_paths)))
    for path in files:
        tree = _parse_python_source(str(path))
        if tree is None:
            continue

        module_aliases, imported_names, class_aliases = _class_import_aliases(
            tree,
            module_name=module_name,
            class_name=getattr(owner, "__name__", None),
        )
        factory_names = _factory_like_helper_names(
            tree,
            class_tokens=class_tokens,
            fixture_catalog=fixture_catalog,
        )

        scopes: list[tuple[ast.AST, list[dict[str, Any]]]] = [(tree, [{}])]
        scopes.extend(
            (node, _function_parametrize_bindings(node))
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for scope, bindings_list in scopes:
            base_bindings = _scope_literal_bindings(scope, fixture_catalog)
            instance_names = _scope_instance_names(
                scope,
                class_tokens=class_tokens,
                class_aliases=class_aliases,
                factory_names=factory_names,
                fixture_catalog=fixture_catalog,
            )
            for stmt in _iter_scope_statements(scope):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for node in ast.walk(stmt):
                    if not isinstance(node, ast.Call):
                        continue
                    if callable_kind == "instance":
                        matched = _call_matches_bound_method(
                            node,
                            method_name=method_name,
                            instance_names=instance_names,
                            class_aliases=class_aliases,
                            factory_names=factory_names,
                        )
                    else:
                        func_name = _call_name(node.func)
                        if func_name is None:
                            continue
                        matched = func_name == leaf_name or func_name.split(".")[-1] == leaf_name
                        if not matched:
                            continue
                        if (
                            "." in func_name
                            and func_name.split(".", 1)[0] not in module_aliases
                            and func_name.split(".")[-1] != leaf_name
                        ):
                            continue
                        if (
                            "." not in func_name
                            and imported_names
                            and func_name not in imported_names
                        ):
                            continue

                    if not matched:
                        continue

                    for bindings in bindings_list or [{}]:
                        merged_bindings = dict(base_bindings)
                        merged_bindings.update(bindings)
                        kwargs: dict[str, Any] = {}
                        positional_params = iter(param_names)
                        supported = True
                        call_args = list(node.args)
                        if hidden_state_param and len(call_args) == len(param_names) + 1:
                            call_args = call_args[1:]
                        for arg in call_args:
                            param_name = next(positional_params, None)
                            if param_name is None:
                                supported = False
                                break
                            value = _seed_value_from_node(arg, bindings=merged_bindings)
                            if value is _MISSING:
                                supported = False
                                break
                            kwargs[param_name] = value
                        if not supported:
                            continue
                        for kw in node.keywords:
                            if kw.arg is None:
                                supported = False
                                break
                            if hidden_state_param and kw.arg == hidden_state_param:
                                continue
                            value = _seed_value_from_node(kw.value, bindings=merged_bindings)
                            if value is _MISSING:
                                supported = False
                                break
                            kwargs[kw.arg] = value
                        if not supported or not kwargs:
                            continue
                        _append_seed_example(
                            examples,
                            kwargs=kwargs,
                            source=source,
                            evidence=f"{path.name}:{getattr(node, 'lineno', 0)}",
                        )
    return examples
def _doctest_seed_examples(func: Any) -> list[SeedExample]:
    """Extract literal call examples from doctest-style docstrings."""
    target = _unwrap(func)
    doc = inspect.getdoc(target) or ""
    name = getattr(target, "__name__", "")
    if not doc or not name:
        return []

    examples: list[SeedExample] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped.startswith(">>> ") or f"{name}(" not in stripped:
            continue
        expr = stripped.removeprefix(">>> ").strip()
        try:
            node = ast.parse(expr, mode="eval").body
        except SyntaxError:
            continue
        if not isinstance(node, ast.Call):
            continue
        try:
            sig = inspect.signature(target)
        except Exception:
            continue
        param_names = [param for param in sig.parameters if param not in {"self", "cls"}]
        kwargs: dict[str, Any] = {}
        supported = True
        for param_name, arg in zip(param_names, node.args, strict=False):
            value = _literal_ast_value(arg)
            if value is _MISSING:
                supported = False
                break
            kwargs[param_name] = value
        for kw in node.keywords:
            if kw.arg is None:
                supported = False
                break
            value = _literal_ast_value(kw.value)
            if value is _MISSING:
                supported = False
                break
            kwargs[kw.arg] = value
        if supported and kwargs:
            _append_seed_example(
                examples,
                kwargs=kwargs,
                source="docstring",
                evidence=stripped,
            )
    return examples
def _numeric_boundary_neighbors(value: int | float) -> list[Any]:
    """Return nearby values for one numeric boundary witness."""
    if isinstance(value, bool):
        return [value]
    if isinstance(value, int):
        return [value, value + 1, value - 1]
    return [value, value + 1.0, value - 1.0]
def _source_boundary_examples(func: Any) -> list[SeedExample]:
    """Mine explicit boundary constants from comparisons in the function body."""
    target = _unwrap(func)
    source_file = _source_file_for_callable(target)
    if source_file is None:
        return []
    try:
        source_text = inspect.getsource(target)
    except (OSError, TypeError):
        return []
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []

    try:
        sig = inspect.signature(target)
    except Exception:
        return []
    params = [name for name in sig.parameters if name not in {"self", "cls"}]
    if not params:
        return []

    hints = safe_get_annotations(target)
    base_kwargs: dict[str, Any] = {}
    for name in params:
        values = []
        if name in hints:
            values.extend(_boundary_values_for_hint(hints[name]))
        if values:
            base_kwargs[name] = values[0]
    if set(base_kwargs) != set(params):
        return []
    examples: list[SeedExample] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        lhs = _call_name(node.left)
        if lhs not in params:
            continue
        for comp in node.comparators:
            value = _literal_ast_value(comp)
            if value is _MISSING:
                continue
            values = (
                _numeric_boundary_neighbors(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else [value]
            )
            for candidate_value in values:
                kwargs = dict(base_kwargs)
                kwargs[lhs] = candidate_value
                _append_seed_example(
                    examples,
                    kwargs=kwargs,
                    source="source_boundary",
                    evidence=f"{source_file.name}:{getattr(node, 'lineno', 0)}",
                )
    return examples
