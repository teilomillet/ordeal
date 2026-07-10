from __future__ import annotations
# ruff: noqa
# ============================================================================
# Common parameter name → strategy inference
# ============================================================================

# Maps parameter names (and patterns) to sensible Hypothesis strategies.
# This eliminates the most common fixture boilerplate — users only need
# to provide strategies for truly domain-specific types.
COMMON_NAME_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    # Text
    "text": st.text(min_size=0, max_size=200),
    "prompt": st.text(min_size=1, max_size=200),
    "response": st.text(min_size=0, max_size=500),
    "message": st.text(min_size=1, max_size=200),
    "content": st.text(min_size=0, max_size=500),
    "query": st.text(min_size=1, max_size=200),
    "input": st.text(min_size=0, max_size=200),
    "output": st.text(min_size=0, max_size=500),
    "label": st.text(min_size=1, max_size=50),
    "name": st.text(min_size=1, max_size=50),
    "key": st.text(min_size=1, max_size=50),
    "description": st.text(min_size=0, max_size=200),
    # Numeric
    "seed": st.integers(min_value=0, max_value=2**31 - 1),
    "random_seed": st.integers(min_value=0, max_value=2**31 - 1),
    "threshold": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "alpha": st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    "probability": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "weight": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "tolerance": st.floats(min_value=1e-10, max_value=0.1, allow_nan=False),
    "tol": st.floats(min_value=1e-10, max_value=0.1, allow_nan=False),
    "count": st.integers(min_value=0, max_value=100),
    "n": st.integers(min_value=1, max_value=100),
    "size": st.integers(min_value=1, max_value=100),
    "max_tokens": st.integers(min_value=1, max_value=500),
    "max_iterations": st.integers(min_value=1, max_value=20),
    "num_prompts": st.integers(min_value=1, max_value=50),
    "top_k": st.integers(min_value=1, max_value=10),
    "batch_size": st.integers(min_value=1, max_value=32),
    # Boolean
    "verbose": st.booleans(),
    "strict": st.booleans(),
    "normalize": st.booleans(),
}
# Suffix patterns: if param name ends with these, use this strategy
_SUFFIX_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    "_text": st.text(min_size=0, max_size=200),
    "_path": st.text(min_size=1, max_size=50),
    "_count": st.integers(min_value=0, max_value=100),
    "_size": st.integers(min_value=1, max_value=100),
    "_rate": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_prob": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_threshold": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_weight": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_flag": st.booleans(),
    "_enabled": st.booleans(),
}
# User-registered strategies (project-specific, added at runtime)
_REGISTERED_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {}
_REGISTERED_OBJECT_FACTORIES: dict[str, Any] = {}
_REGISTERED_OBJECT_SETUPS: dict[str, Any] = {}
_REGISTERED_OBJECT_SCENARIOS: dict[str, Any] = {}
_REGISTERED_OBJECT_STATE_FACTORIES: dict[str, Any] = {}
_REGISTERED_OBJECT_TEARDOWNS: dict[str, Any] = {}
_REGISTERED_OBJECT_HARNESSES: dict[str, str] = {}
_BOUNDARY_SMOKE_VALUES: dict[object, tuple[object, ...]] = {
    bool: (False, True),
    int: (0, 1, -1),
    float: (0.0, 1.0, -1.0),
    str: ("", "a"),
    bytes: (b"", b"x"),
}
_VALID_SCAN_MODES: set[str] = set(_SCAN_MODE_ALIASES)
_WEAK_CONTRACT_FIT = 0.35
_SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS = 0.55
_SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS = 0.55
_SEMANTIC_CONTRACT_STRONG_REALISM = 0.75
@dataclass(frozen=True)
class SeedExample:
    """One concrete input shape derived from existing code or tests."""

    kwargs: dict[str, Any]
    source: str
    evidence: str
@dataclass(frozen=True)
class CandidateInput:
    """One deterministic input candidate with provenance metadata."""

    kwargs: dict[str, Any]
    origin: str
    rationale: tuple[str, ...] = ()
@dataclass(frozen=True)
class HarnessHint:
    """One mined suggestion for configuring a stateful object target."""

    kind: str
    suggestion: str
    evidence: str
    confidence: float = 0.5
    score: float = 0.5
    signals: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
@dataclass(frozen=True)
class AutoObjectRuntime:
    """One conservative runtime assembled from mined harness hints."""

    factory: Any | None = None
    factory_source: str | None = None
    setup: Any | None = None
    setup_source: str | None = None
    state_factory: Any | None = None
    state_factory_source: str | None = None
    teardown: Any | None = None
    teardown_source: str | None = None
    scenarios: tuple[Any, ...] = ()
    scenario_source: str | None = None
    harness: str | None = None
    harness_source: str | None = None
    hints: tuple[HarnessHint, ...] = ()
_DEFAULT_AUTO_CONTRACTS = (
    "shell_safe",
    "quoted_paths",
    "command_arg_stability",
    "protected_env_keys",
    "json_roundtrip",
    "http_shape",
    "subprocess_argv",
    "lifecycle_attempts_all",
    "lifecycle_followup",
)
_HARNESS_HINT_SIGNAL_WEIGHTS: dict[str, float] = {
    "returns_target_instance": 0.28,
    "constructor_like": 0.1,
    "mentions_target_tokens": 0.08,
    "pytest_fixture": 0.06,
    "test_evidence": 0.05,
    "support_file": 0.06,
    "state_compatible": 0.14,
    "returns_mapping": 0.08,
    "lifecycle_cleanup": 0.08,
    "collaborator_overlap": 0.08,
    "doc_evidence": 0.03,
}
@functools.lru_cache(maxsize=512)
def _read_python_source(path_str: str) -> str:
    """Read one Python file with a tiny cache for repeated scan lookups."""
    return Path(path_str).read_text(encoding="utf-8")
@functools.lru_cache(maxsize=512)
def _parse_python_source(path_str: str) -> ast.AST | None:
    """Parse one Python file, returning ``None`` on syntax or I/O failure."""
    try:
        return ast.parse(_read_python_source(path_str))
    except Exception:
        return None
def _literal_ast_value(node: ast.AST) -> Any:
    """Return a Python literal from *node*, or ``None`` when unsupported."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        values = [_literal_ast_value(item) for item in node.elts]
        if any(value is _MISSING for value in values):
            return _MISSING
        return values
    if isinstance(node, ast.Tuple):
        values = [_literal_ast_value(item) for item in node.elts]
        if any(value is _MISSING for value in values):
            return _MISSING
        return tuple(values)
    if isinstance(node, ast.Set):
        values = [_literal_ast_value(item) for item in node.elts]
        if any(value is _MISSING for value in values):
            return _MISSING
        return set(values)
    if isinstance(node, ast.Dict):
        keys = [_literal_ast_value(item) for item in node.keys]
        values = [_literal_ast_value(item) for item in node.values]
        if any(item is _MISSING for item in (*keys, *values)):
            return _MISSING
        return dict(zip(keys, values, strict=False))
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        operand = node.operand.value
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Call):
        func_name = _call_name(node.func)
        if (
            func_name
            in {
                "Path",
                "PurePath",
                "PosixPath",
                "WindowsPath",
                "pathlib.Path",
                "pathlib.PurePath",
                "pathlib.PosixPath",
                "pathlib.WindowsPath",
            }
            and len(node.args) == 1
        ):
            value = _literal_ast_value(node.args[0])
            if isinstance(value, str):
                return Path(value)
    return _MISSING
def _literal_ast_value_with_env(
    node: ast.AST,
    bindings: Mapping[str, Any] | None = None,
) -> Any:
    """Return a literal value, resolving simple local-name bindings when available."""
    if isinstance(node, ast.Name) and bindings is not None:
        if node.id in bindings:
            return bindings[node.id]
    return _literal_ast_value(node)
_MISSING = object()
def _call_name(node: ast.AST) -> str | None:
    """Return the dotted name for *node* when it is name-like."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        root = _call_name(node.value)
        if root is None:
            return None
        return f"{root}.{node.attr}"
    return None
def _symbol_hint_value(path: Path, symbol_name: str) -> str:
    """Return a TOML-ready file-path symbol reference for one local helper."""
    try:
        display = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        display = path.resolve()
    return f"{display.as_posix()}:{symbol_name}"
def _simple_assigned_names(target: ast.AST) -> list[str]:
    """Return simple local names assigned by *target*."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in target.elts:
            names.extend(_simple_assigned_names(item))
        return names
    return []
def _iter_scope_statements(scope: ast.AST) -> Sequence[ast.stmt]:
    """Return the direct statements for a module or function scope."""
    if isinstance(scope, ast.Module):
        return scope.body
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return scope.body
    return ()
def _yield_cleanup_mentions(node: ast.AST) -> bool:
    """Return whether the post-yield body mentions teardown-like cleanup."""
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr.lower() in {"cleanup", "close", "stop", "teardown", "reset"}
        for child in ast.walk(node)
    )
def _callable_supports_optional_instance_call(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Return whether *node* can be called with zero args or one instance arg."""
    positional = [*node.args.posonlyargs, *node.args.args]
    if positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    positional_defaults = [
        None,
    ] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    required_positional = [
        arg
        for arg, default in zip(positional, positional_defaults, strict=True)
        if default is None
    ]
    required_kwonly = [
        arg
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
        if default is None
    ]
    return not required_kwonly and len(required_positional) <= 1
@functools.lru_cache(maxsize=64)
def _pytest_fixture_catalog(path_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Return simple metadata for pytest fixtures across the given files."""
    catalog: dict[str, dict[str, Any]] = {}
    for path_str in path_keys:
        tree = _parse_python_source(path_str)
        if tree is None:
            continue
        path = Path(path_str).resolve()
        try:
            display_path = path.relative_to(Path.cwd())
        except ValueError:
            display_path = path
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
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
            annotation_text = ""
            with contextlib.suppress(Exception):
                if getattr(node, "returns", None) is not None:
                    annotation_text = ast.unparse(node.returns)
            body_text = ""
            with contextlib.suppress(Exception):
                body_text = " ".join(ast.unparse(stmt) for stmt in node.body[:8])
            text_parts = [node.name, annotation_text, ast.get_docstring(node) or "", body_text]
            info["text"] = " ".join(str(part).lower() for part in text_parts if part).strip()
            if annotation_text:
                lowered = annotation_text.lower()
                info["return_names"].add(lowered)
                info["return_names"].update(
                    token.lower()
                    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", annotation_text)
                )
            saw_yield = False
            yield_index: int | None = None
            for index, stmt in enumerate(node.body):
                value_node: ast.AST | None = None
                if isinstance(stmt, ast.Return):
                    value_node = stmt.value
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
                    value_node = stmt.value.value
                    saw_yield = True
                    yield_index = index
                if value_node is None:
                    if saw_yield and _yield_cleanup_mentions(stmt):
                        info["yield_cleanup"] = True
                    continue
                value = _literal_ast_value(value_node)
                if value is not _MISSING and value not in info["values"]:
                    info["values"].append(value)
                if isinstance(value_node, ast.Call):
                    callee = _call_name(value_node.func)
                    if callee:
                        info["return_names"].add(callee.lower())
                        info["return_names"].add(callee.rsplit(".", 1)[-1].lower())
            if saw_yield and yield_index is not None:
                trailing = node.body[yield_index + 1 :]
                if any(_yield_cleanup_mentions(item) for item in trailing):
                    info["yield_cleanup"] = True
    return catalog
def _scope_literal_bindings(
    scope: ast.AST,
    fixture_catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return simple literal bindings visible inside one module or function scope."""
    bindings: dict[str, Any] = {}
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]:
            values = list(fixture_catalog.get(arg.arg, {}).get("values", ()))
            if values:
                bindings[arg.arg] = values[0]
    for stmt in _iter_scope_statements(scope):
        if isinstance(stmt, ast.Assign):
            value = _literal_ast_value_with_env(stmt.value, bindings)
            if value is _MISSING:
                continue
            for target in stmt.targets:
                for name in _simple_assigned_names(target):
                    bindings[name] = value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            value = _literal_ast_value_with_env(stmt.value, bindings)
            if value is _MISSING:
                continue
            for name in _simple_assigned_names(stmt.target):
                bindings[name] = value
    return bindings
def _class_import_aliases(
    tree: ast.AST,
    *,
    module_name: str,
    class_name: str | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Return imported module, callable, and class aliases for a target module."""
    module_aliases: set[str] = set()
    callable_aliases: set[str] = set()
    class_aliases: set[str] = {class_name} if class_name else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            for alias in node.names:
                if imported_module == module_name:
                    imported_name = alias.asname or alias.name
                    callable_aliases.add(imported_name)
                    if class_name and alias.name == class_name:
                        class_aliases.add(imported_name)
                elif f"{imported_module}.{alias.name}" == module_name:
                    module_aliases.add(alias.asname or alias.name)
    return module_aliases, callable_aliases, class_aliases
def _factory_like_helper_names(
    tree: ast.AST,
    *,
    class_tokens: set[str],
    fixture_catalog: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Return helper names that likely build the target object."""
    helpers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        info = fixture_catalog.get(node.name, {})
        lowered = str(info.get("text", node.name)).lower()
        return_names = {str(item).lower() for item in info.get("return_names", set())}
        if (
            node.name.startswith(("make_", "build_", "create_", "new_"))
            or any(token in node.name.lower() for token in class_tokens)
        ) and (return_names & class_tokens or any(token in lowered for token in class_tokens)):
            helpers.add(node.name)
    return helpers
def _matches_target_fixture(
    info: Mapping[str, Any] | None,
    *,
    class_tokens: set[str],
) -> bool:
    """Return whether one fixture metadata record looks like the target object."""
    if not info:
        return False
    return_names = {str(item).lower() for item in info.get("return_names", set())}
    lowered = str(info.get("text", "")).lower()
    return bool(return_names & class_tokens) or any(token in lowered for token in class_tokens)
def _scope_instance_names(
    scope: ast.AST,
    *,
    class_tokens: set[str],
    class_aliases: set[str],
    factory_names: set[str],
    fixture_catalog: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Return local names in *scope* that likely hold the target instance."""
    instance_names: set[str] = set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]:
            info = fixture_catalog.get(arg.arg)
            if _matches_target_fixture(info, class_tokens=class_tokens) or any(
                token in arg.arg.lower() for token in class_tokens
            ):
                instance_names.add(arg.arg)
    for stmt in _iter_scope_statements(scope):
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        callee = _call_name(value.func)
        if callee is None:
            continue
        leaf = callee.rsplit(".", 1)[-1]
        if leaf not in class_aliases and leaf not in factory_names:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for target in targets:
            for name in _simple_assigned_names(target):
                instance_names.add(name)
    return instance_names
def _call_matches_bound_method(
    call: ast.Call,
    *,
    method_name: str,
    instance_names: set[str],
    class_aliases: set[str],
    factory_names: set[str],
) -> bool:
    """Return whether *call* looks like a target instance-method invocation."""
    if not isinstance(call.func, ast.Attribute) or call.func.attr != method_name:
        return False
    receiver = call.func.value
    if isinstance(receiver, ast.Name):
        return receiver.id in instance_names
    if isinstance(receiver, ast.Call):
        callee = _call_name(receiver.func)
        if callee is None:
            return False
        leaf = callee.rsplit(".", 1)[-1]
        return leaf in class_aliases or leaf in factory_names
    return False
