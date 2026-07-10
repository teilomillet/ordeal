from __future__ import annotations
# ruff: noqa
def _infer_strategies(
    func: Any,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
) -> dict[str, st.SearchStrategy[Any]] | None:
    """Infer strategies from fixtures → name patterns → type hints.

    Resolution order per parameter:
    1. Explicit fixture (user-provided)
    2. Common name pattern (COMMON_NAME_STRATEGIES)
    3. Type hint (via strategy_for_type)
    4. Default value → skip
    5. None → can't infer, return None for entire function
    """
    target = _unwrap(func)
    if _callable_skip_reason(target) is not None:
        return None

    hints: dict[str, Any] = {}
    for candidate in (target, getattr(target, "__func__", None)):
        if candidate is None:
            continue
        hints = safe_get_annotations(candidate)
        if hints:
            break

    sig = inspect.signature(target)
    strategies: dict[str, st.SearchStrategy[Any]] = {}

    for name, param in sig.parameters.items():
        if name == "self" or name == "cls":
            continue
        has_default = param.default is not inspect.Parameter.empty
        # 1. Explicit fixture (always wins)
        if fixtures and name in fixtures:
            strategies[name] = fixtures[name]
        # 2. Default is None → sample both None and the typed value.
        #    Previously this skipped the param entirely, which blocked
        #    mine() on any function with Optional params.
        elif has_default and param.default is None:
            if name in hints:
                # Optional[T] → sample T | None.
                # If the type hint already includes None (e.g. Optional[str],
                # str | None), strategy_for_type handles it via the Union path.
                # Only add st.none() if the hint doesn't already include None.
                hint = hints[name]
                origin = get_origin(hint)
                args = get_args(hint)
                already_optional = (origin is Union and type(None) in args) or hint is type(None)
                strat = strategy_for_type(hint)
                if not already_optional:
                    strat = st.one_of(strat, st.none())
                strategies[name] = strat
            else:
                continue
        # 3. Common name pattern
        elif (name_strat := _strategy_for_name(name)) is not None:
            strategies[name] = name_strat
        # 4. Type hint
        elif name in hints:
            strategies[name] = strategy_for_type(hints[name])
        # 5. Has non-None default → let Python use it
        elif has_default:
            continue
        # 6. Can't infer
        else:
            return None

    return strategies
def _literal_seed_value(node: ast.AST) -> Any:
    """Return a Python literal from a small AST subset."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_literal_seed_value(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal_seed_value(item) for item in node.elts)
    if isinstance(node, ast.Set):
        return {_literal_seed_value(item) for item in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _literal_seed_value(key): _literal_seed_value(value)
            for key, value in zip(node.keys, node.values, strict=False)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = _literal_seed_value(node.operand)
        if isinstance(operand, (int, float)):
            return -operand
    raise ValueError("not a literal seed value")
def _is_simple_literal_node(node: ast.AST) -> bool:
    """Return True when *node* is safe to evaluate as a literal seed."""
    try:
        _literal_seed_value(node)
    except Exception:
        return False
    return True
def _test_search_roots(module_name: str) -> list[Path]:
    """Return likely roots containing valid example seeds for *module_name*."""
    workspace_root = Path.cwd().resolve()
    roots = [workspace_root]
    try:
        module = importlib.import_module(module_name)
    except Exception:
        module = None
    module_file = getattr(module, "__file__", None)
    if module_file:
        module_dir = Path(module_file).resolve().parent
        if (
            module_dir not in roots
            and _is_project_discovery_path(module_dir, workspace_root=workspace_root)
            and not module_dir.is_relative_to(workspace_root)
        ):
            roots.append(module_dir)
    return [root for root in roots if root.exists()]
def _project_files_matching(root: Path, patterns: Sequence[str]) -> list[Path]:
    """Return matching files while pruning non-project directories before descent."""
    resolved_root = root.resolve()
    if not _is_project_discovery_path(resolved_root):
        return []
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root):
        directory = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _DISCOVERY_IGNORED_PATH_PARTS
            and not (directory == resolved_root and name in _DISCOVERY_IGNORED_ROOT_NAMES)
            and _is_project_discovery_path(directory / name)
        )
        for filename in sorted(filenames):
            if not any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                continue
            path = (directory / filename).resolve()
            if path.is_file() and _is_project_discovery_path(path):
                matches.append(path)
    return matches
def _callable_seed_files(module_name: str) -> list[Path]:
    """Return candidate Python files that may contain realistic seed examples."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in _test_search_roots(module_name):
        for resolved in _project_files_matching(
            root,
            ("test_*.py", "*_test.py", "conftest.py"),
        ):
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return sorted(candidates)
def _camel_case_tokens(text: str) -> list[str]:
    """Split one identifier into coarse searchable tokens."""
    return [token.lower() for token in re.findall(r"[A-Z]?[a-z]+|[0-9]+", text) if token]
def _searchable_tokens(text: str) -> set[str]:
    """Return coarse word tokens for lightweight harness matching."""
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(text)):
        lowered = raw.lower()
        tokens.add(lowered)
        tokens.update(part for part in lowered.split("_") if part)
        tokens.update(_camel_case_tokens(raw))
    return {token for token in tokens if token}
def _harness_doc_files(module_name: str) -> list[Path]:
    """Return markdown files that may document lifecycle harness setup."""
    roots = [Path.cwd(), Path.cwd() / "docs"]
    try:
        module = importlib.import_module(module_name)
    except Exception:
        module = None
    module_file = getattr(module, "__file__", None)
    if module_file:
        module_root = Path(module_file).resolve().parent
        roots.extend([module_root, *module_root.parents[:2]])

    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("README*.md", "*.md", "docs/**/*.md"):
            for path in root.glob(pattern):
                resolved = path.resolve()
                if (
                    resolved in seen
                    or not resolved.is_file()
                    or not _is_project_discovery_path(resolved)
                ):
                    continue
                seen.add(resolved)
                candidates.append(resolved)
    return sorted(candidates)
_SCENARIO_PACK_ATTR_ALIASES: dict[str, str] = {
    "artifact_client": "upload_download",
    "cache": "state_store",
    "classifier": "model_inference",
    "command_runner": "subprocess",
    "downloader": "upload_download",
    "embedder": "model_inference",
    "embedding_store": "feature_store",
    "encoder": "model_inference",
    "executor": "subprocess",
    "feature_client": "feature_store",
    "feature_store": "feature_store",
    "http_client": "http",
    "model": "model_inference",
    "model_client": "model_inference",
    "model_registry": "upload_download",
    "model_store": "upload_download",
    "predictor": "model_inference",
    "process_runner": "subprocess",
    "reranker": "model_inference",
    "runner": "subprocess",
    "sandbox": "sandbox_client",
    "sandbox_client": "sandbox_client",
    "scorer": "model_inference",
    "session": "http",
    "session_state": "state_store",
    "state_store": "state_store",
    "storage_client": "upload_download",
    "store": "state_store",
    "subprocess": "subprocess",
    "transport": "http",
    "upload_download": "upload_download",
    "uploader": "upload_download",
    "vector_store": "feature_store",
    "weights_store": "upload_download",
}
def _constructor_aliases(
    tree: ast.AST,
    module_name: str,
    class_name: str,
) -> tuple[set[str], set[str]]:
    """Return direct and module aliases that may construct *class_name*."""
    direct = {class_name}
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            for alias in node.names:
                if imported_module == module_name and alias.name == class_name:
                    direct.add(alias.asname or alias.name)
                elif f"{imported_module}.{alias.name}" == module_name:
                    module_aliases.add(alias.asname or alias.name)
    return direct, module_aliases
def _call_looks_like_target_constructor(
    call: ast.Call,
    *,
    direct_aliases: set[str],
    module_aliases: set[str],
    class_name: str,
) -> bool:
    """Return whether *call* looks like ``ClassName(...)`` for the target."""
    func_name = _call_name(call.func)
    if func_name is None:
        return False
    if func_name in direct_aliases or func_name == class_name:
        return True
    if "." not in func_name:
        return False
    head, tail = func_name.rsplit(".", 1)
    return tail == class_name and head in module_aliases
def _names_returning_target_instance(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    direct_aliases: set[str],
    module_aliases: set[str],
    class_name: str,
) -> set[str]:
    """Return local names within *node* that hold a target instance."""
    names: set[str] = set()
    for child in ast.walk(node):
        value: ast.AST | None = None
        targets: list[ast.expr] = []
        if isinstance(child, ast.Assign):
            value = child.value
            targets = list(child.targets)
        elif isinstance(child, ast.AnnAssign):
            value = child.value
            targets = [child.target]
        if value is None or not isinstance(value, ast.Call):
            continue
        if not _call_looks_like_target_constructor(
            value,
            direct_aliases=direct_aliases,
            module_aliases=module_aliases,
            class_name=class_name,
        ):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names
def _function_returns_target_instance(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    direct_aliases: set[str],
    module_aliases: set[str],
    class_name: str,
) -> tuple[bool, set[str]]:
    """Return whether *node* returns a target instance and the local instance names."""
    instance_names = _names_returning_target_instance(
        node,
        direct_aliases=direct_aliases,
        module_aliases=module_aliases,
        class_name=class_name,
    )
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        if isinstance(child.value, ast.Name) and child.value.id in instance_names:
            return True, instance_names
        if isinstance(child.value, ast.Call) and _call_looks_like_target_constructor(
            child.value,
            direct_aliases=direct_aliases,
            module_aliases=module_aliases,
            class_name=class_name,
        ):
            return True, instance_names
    return False, instance_names
def _instance_attr_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    instance_names: set[str],
) -> set[str]:
    """Return collaborator attr names assigned on local instance variables."""
    attrs: set[str] = set()
    if not instance_names:
        return attrs
    for child in ast.walk(node):
        targets: list[ast.expr] = []
        if isinstance(child, ast.Assign):
            targets = list(child.targets)
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in instance_names
            ):
                attrs.add(target.attr)
    return attrs
def _scenario_packs_for_attrs(attr_names: set[str]) -> list[str]:
    """Return built-in scenario packs implied by observed collaborator attrs."""
    packs: list[str] = []
    for attr_name in sorted(attr_names):
        pack = _SCENARIO_PACK_ATTR_ALIASES.get(attr_name)
        if pack is not None and pack not in packs:
            packs.append(pack)
    return packs
def _source_collaborator_attrs(
    module_name: str,
    class_name: str,
    method_name: str,
) -> tuple[set[str], str | None]:
    """Return ``self.<attr>`` names used in the target method source."""
    try:
        module = importlib.import_module(module_name)
        owner = getattr(module, class_name)
        method = getattr(owner, method_name)
        source_file = _source_file_for_callable(method)
        source_text = inspect.getsource(method)
    except Exception:
        return set(), None

    try:
        tree = ast.parse(textwrap.dedent(source_text))
    except SyntaxError:
        return set(), None

    attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"self", "instance", "env", "obj"}
    }
    if source_file is None:
        return attrs, None
    try:
        return attrs, str(source_file.relative_to(Path.cwd()))
    except ValueError:
        return attrs, str(source_file)
