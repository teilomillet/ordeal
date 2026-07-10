from __future__ import annotations
# ruff: noqa


@dataclass(frozen=True)
class _IndexedEvidenceScope:
    """One scope with literal bindings and call sites indexed by leaf name."""

    node: ast.AST
    parametrized: tuple[dict[str, Any], ...]
    calls: dict[str, tuple[ast.Call, ...]]


@dataclass(frozen=True)
class _IndexedEvidenceFile:
    """Reusable structural evidence for one version of a Python file."""

    cache_key: tuple[str, int, int]
    scopes: tuple[_IndexedEvidenceScope, ...]
    functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]
    imports: tuple[tuple[str, str | None], ...]
    from_imports: tuple[tuple[str, str, str | None], ...]
    documentation_tokens: frozenset[str]


class ProjectEvidenceIndex:
    """Parse and index project evidence once for all callables in one scan."""

    def __init__(
        self,
        module_name: str,
        *,
        test_files: Sequence[Path] | None = None,
        project_files: Sequence[Path] | None = None,
    ) -> None:
        """Build the test, fixture, and project call-site index."""
        if test_files is None or project_files is None:
            found_tests, found_project = _candidate_seed_files(module_name)
            test_files = found_tests if test_files is None else test_files
            project_files = found_project if project_files is None else project_files
        self.module_name = module_name
        self.test_files = self._paths(test_files)
        self.project_files = self._paths(project_files)
        self._files: dict[Path, _IndexedEvidenceFile] = {}
        self._catalogs: dict[tuple[Path, ...], dict[str, dict[str, Any]]] = {}
        self._bindings: dict[tuple[tuple[Path, ...], Path, int], dict[str, Any]] = {}
        self._file_seeds: dict[
            tuple[int, tuple[Path, ...], str], tuple[SeedExample, ...]
        ] = {}
        self._callable_seeds: dict[tuple[Any, ...], tuple[SeedExample, ...]] = {}
        self._profiles: dict[tuple[Any, ...], dict[str, Any]] = {}
        all_paths = {
            *self._fixture_paths(self.test_files),
            *self._fixture_paths(self.project_files),
            *(
                path.resolve()
                for path in (Path.cwd() / "conftest.py", Path.cwd() / "tests" / "conftest.py")
                if path.exists()
            ),
        }
        for path in sorted(all_paths):
            self._files[path] = self._build_file(path)
        self._prepare(self.test_files)
        self._prepare(self.project_files)

    @staticmethod
    def _paths(paths: Sequence[Path]) -> tuple[Path, ...]:
        return tuple(Path(path).resolve() for path in paths if Path(path).exists())

    @staticmethod
    def _fixture_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
        found = {Path(path).resolve() for path in paths if Path(path).exists()}
        workspace = Path.cwd().resolve()
        for path in list(found):
            for parent in [path.parent, *path.parents]:
                conftest = parent / "conftest.py"
                if conftest.exists():
                    found.add(conftest.resolve())
                if parent.resolve() == workspace:
                    break
        return tuple(sorted(found))

    @staticmethod
    def _cache_key(path: Path) -> tuple[str, int, int]:
        stat = path.stat()
        return str(path.resolve()), stat.st_mtime_ns, stat.st_size

    @classmethod
    def _build_file(cls, path: Path) -> _IndexedEvidenceFile:
        cache_key = cls._cache_key(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            return _IndexedEvidenceFile(cache_key, (), (), (), (), frozenset())
        nodes = tuple(ast.walk(tree))
        functions = tuple(
            node
            for node in nodes
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        scopes: list[_IndexedEvidenceScope] = []
        for scope in (tree, *functions):
            calls: dict[str, list[ast.Call]] = {}
            for statement in _iter_scope_statements(scope):
                if isinstance(
                    statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                for node in ast.walk(statement):
                    if not isinstance(node, ast.Call):
                        continue
                    name = _call_name(node.func)
                    leaf = (
                        name.rsplit(".", 1)[-1]
                        if name
                        else node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else None
                    )
                    if leaf:
                        calls.setdefault(leaf, []).append(node)
            parametrized = (
                tuple(_function_parametrize_bindings(scope))
                if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
                else ({},)
            )
            scopes.append(
                _IndexedEvidenceScope(
                    scope,
                    parametrized,
                    {name: tuple(items) for name, items in calls.items()},
                )
            )
        imports = tuple(
            (alias.name, alias.asname)
            for node in nodes
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        from_imports = tuple(
            (node.module or "", alias.name, alias.asname)
            for node in nodes
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        )
        docs = [ast.get_docstring(tree) or ""]
        docs.extend(ast.get_docstring(node) or "" for node in functions)
        tokens = frozenset(
            token.lower()
            for doc in docs
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", doc)
        )
        return _IndexedEvidenceFile(
            cache_key, tuple(scopes), functions, imports, from_imports, tokens
        )

    def _fixture_catalog(self, files: Sequence[Path]) -> dict[str, dict[str, Any]]:
        fixture_paths = self._fixture_paths(files)
        if fixture_paths in self._catalogs:
            return self._catalogs[fixture_paths]
        catalog: dict[str, dict[str, Any]] = {}
        for path in fixture_paths:
            if path not in self._files:
                self._files[path] = self._build_file(path)
            indexed = self._files[path]
            try:
                display_path = path.relative_to(Path.cwd())
            except ValueError:
                display_path = path
            for node in indexed.functions:
                is_fixture = any(
                    _call_name(decorator.func) == "pytest.fixture"
                    if isinstance(decorator, ast.Call)
                    else _call_name(decorator) == "pytest.fixture"
                    for decorator in node.decorator_list
                )
                if not is_fixture:
                    continue
                info = catalog.setdefault(
                    node.name,
                    {
                        "values": [],
                        "return_names": set(),
                        "yield_cleanup": False,
                        "text": "",
                        "evidence": f"{display_path}:{getattr(node, 'lineno', '?')}",
                        "symbol": _symbol_hint_value(path, node.name),
                    },
                )
                annotation = ""
                with contextlib.suppress(Exception):
                    if node.returns is not None:
                        annotation = ast.unparse(node.returns)
                body = ""
                with contextlib.suppress(Exception):
                    body = " ".join(ast.unparse(stmt) for stmt in node.body[:8])
                parts = [node.name, annotation, ast.get_docstring(node) or "", body]
                info["text"] = " ".join(
                    str(part).lower() for part in parts if part
                ).strip()
                if annotation:
                    info["return_names"].add(annotation.lower())
                    info["return_names"].update(
                        token.lower()
                        for token in re.findall(
                            r"[A-Za-z_][A-Za-z0-9_]*", annotation
                        )
                    )
                saw_yield = False
                yield_index: int | None = None
                for index, statement in enumerate(node.body):
                    value_node: ast.AST | None = None
                    if isinstance(statement, ast.Return):
                        value_node = statement.value
                    elif isinstance(statement, ast.Expr) and isinstance(
                        statement.value, ast.Yield
                    ):
                        value_node = statement.value.value
                        saw_yield = True
                        yield_index = index
                    if value_node is None:
                        if saw_yield and _yield_cleanup_mentions(statement):
                            info["yield_cleanup"] = True
                        continue
                    value = _literal_ast_value(value_node)
                    if value is not _MISSING and value not in info["values"]:
                        info["values"].append(value)
                    if isinstance(value_node, ast.Call):
                        callee = _call_name(value_node.func)
                        if callee:
                            info["return_names"].update(
                                {callee.lower(), callee.rsplit(".", 1)[-1].lower()}
                            )
                if saw_yield and yield_index is not None:
                    if any(
                        _yield_cleanup_mentions(item)
                        for item in node.body[yield_index + 1 :]
                    ):
                        info["yield_cleanup"] = True
        self._catalogs[fixture_paths] = catalog
        return catalog

    def _prepare(self, files: Sequence[Path]) -> None:
        paths = self._paths(files)
        catalog = self._fixture_catalog(paths)
        for path in paths:
            for index, scope in enumerate(self._files[path].scopes):
                self._bindings[(paths, path, index)] = _scope_literal_bindings(
                    scope.node, catalog
                )

    @staticmethod
    def _aliases(
        indexed: _IndexedEvidenceFile, module_name: str, class_name: str | None
    ) -> tuple[set[str], set[str], set[str]]:
        modules: set[str] = set()
        callables: set[str] = set()
        classes: set[str] = {class_name} if class_name else set()
        for imported, alias in indexed.imports:
            if imported == module_name:
                modules.add(alias or imported.rsplit(".", 1)[-1])
        for imported, name, alias in indexed.from_imports:
            if imported == module_name:
                resolved = alias or name
                callables.add(resolved)
                if class_name and name == class_name:
                    classes.add(resolved)
            elif f"{imported}.{name}" == module_name:
                modules.add(alias or name)
        return modules, callables, classes

    @staticmethod
    def _factory_names(
        indexed: _IndexedEvidenceFile,
        class_tokens: set[str],
        catalog: Mapping[str, Mapping[str, Any]],
    ) -> set[str]:
        helpers: set[str] = set()
        for node in indexed.functions:
            info = catalog.get(node.name, {})
            text = str(info.get("text", node.name)).lower()
            returns = {str(item).lower() for item in info.get("return_names", set())}
            factory_named = node.name.startswith(("make_", "build_", "create_", "new_"))
            token_named = any(token in node.name.lower() for token in class_tokens)
            token_matched = returns & class_tokens or any(
                token in text for token in class_tokens
            )
            if (factory_named or token_named) and token_matched:
                helpers.add(node.name)
        return helpers

    def invalidate(self, path: Path | None = None) -> None:
        """Explicitly rebuild one changed file, or the complete index."""
        targets = list(self._files) if path is None else [Path(path).resolve()]
        for target in targets:
            if target.exists():
                self._files[target] = self._build_file(target)
        self._catalogs.clear()
        self._bindings.clear()
        self._file_seeds.clear()
        self._callable_seeds.clear()
        self._profiles.clear()
        self._prepare(self.test_files)
        self._prepare(self.project_files)

    def cached_seed_examples(self, key: tuple[Any, ...]) -> tuple[SeedExample, ...] | None:
        """Return combined seed examples cached for one callable and option set."""
        return self._callable_seeds.get(key)

    def store_seed_examples(
        self, key: tuple[Any, ...], examples: Sequence[SeedExample]
    ) -> None:
        """Cache combined seed examples for reuse throughout this scan."""
        self._callable_seeds[key] = tuple(examples)

    def cached_contract_profile(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        """Return a contract profile cached for one callable and option set."""
        return self._profiles.get(key)

    def store_contract_profile(self, key: tuple[Any, ...], profile: dict[str, Any]) -> None:
        """Cache one contract profile for reuse throughout this scan."""
        self._profiles[key] = profile

    def fixture_literals(
        self, param_names: set[str], files: Sequence[Path]
    ) -> dict[str, list[Any]]:
        """Return pre-indexed literal fixture values for matching parameters."""
        catalog = self._fixture_catalog(files)
        return {
            name: list(catalog.get(name, {}).get("values", ()))
            for name in sorted(param_names)
            if catalog.get(name, {}).get("values")
        }

    def call_seed_examples(
        self, func: Any, files: Sequence[Path], *, source: str
    ) -> list[SeedExample]:
        """Return literal call examples by querying the pre-indexed call sites."""
        paths = self._paths(files)
        key = (id(func), paths, source)
        if key in self._file_seeds:
            return list(self._file_seeds[key])
        callable_obj = func
        target = _unwrap(callable_obj)
        try:
            signature = inspect.signature(target)
        except Exception:
            return []
        module_name = getattr(target, "__module__", "")
        leaf_name = getattr(target, "__name__", "")
        if not module_name or not leaf_name:
            return []
        method_name = str(
            getattr(callable_obj, "__ordeal_method_name__", leaf_name) or leaf_name
        )
        owner = getattr(callable_obj, "__ordeal_owner__", None)
        kind = getattr(callable_obj, "__ordeal_kind__", None)
        class_tokens = {
            token.lower()
            for token in (
                [getattr(owner, "__name__", "")]
                + _camel_case_tokens(getattr(owner, "__name__", ""))
            )
            if token
        }
        params = [name for name in signature.parameters if name not in {"self", "cls"}]
        if not params:
            return []
        hidden = None
        if getattr(callable_obj, "__ordeal_state_factory__", None) is not None:
            hidden = getattr(callable_obj, "__ordeal_state_param__", None)
        catalog = self._fixture_catalog(paths)
        examples: list[SeedExample] = []
        for path in paths:
            indexed = self._files[path]
            modules, imported_names, classes = self._aliases(
                indexed, module_name, getattr(owner, "__name__", None)
            )
            factories = self._factory_names(indexed, class_tokens, catalog)
            for scope_index, scope in enumerate(indexed.scopes):
                base = self._bindings[(paths, path, scope_index)]
                instances = (
                    _scope_instance_names(
                        scope.node,
                        class_tokens=class_tokens,
                        class_aliases=classes,
                        factory_names=factories,
                        fixture_catalog=catalog,
                    )
                    if kind == "instance"
                    else set()
                )
                wanted = method_name if kind == "instance" else leaf_name
                for node in scope.calls.get(wanted, ()):
                    if kind == "instance":
                        matched = _call_matches_bound_method(
                            node,
                            method_name=method_name,
                            instance_names=instances,
                            class_aliases=classes,
                            factory_names=factories,
                        )
                    else:
                        name = _call_name(node.func)
                        if name is None:
                            continue
                        matched = name == leaf_name or name.split(".")[-1] == leaf_name
                        if "." not in name and imported_names and name not in imported_names:
                            matched = False
                    if not matched:
                        continue
                    for bindings in scope.parametrized or ({},):
                        merged = dict(base)
                        merged.update(bindings)
                        kwargs: dict[str, Any] = {}
                        positional = iter(params)
                        supported = True
                        args = list(node.args)
                        if hidden and len(args) == len(params) + 1:
                            args = args[1:]
                        for arg in args:
                            param = next(positional, None)
                            value = _seed_value_from_node(arg, bindings=merged)
                            if param is None or value is _MISSING:
                                supported = False
                                break
                            kwargs[param] = value
                        for keyword in node.keywords if supported else ():
                            if keyword.arg is None:
                                supported = False
                                break
                            if hidden and keyword.arg == hidden:
                                continue
                            value = _seed_value_from_node(keyword.value, bindings=merged)
                            if value is _MISSING:
                                supported = False
                                break
                            kwargs[keyword.arg] = value
                        if supported and kwargs:
                            _append_seed_example(
                                examples,
                                kwargs=kwargs,
                                source=source,
                                evidence=f"{path.name}:{getattr(node, 'lineno', 0)}",
                            )
        self._file_seeds[key] = tuple(examples)
        return list(examples)
