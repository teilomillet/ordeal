from __future__ import annotations
# ruff: noqa
@functools.lru_cache(maxsize=128)
def _mine_object_harness_hints_cached(
    module_name: str,
    class_name: str,
    method_name: str,
    workspace_root: str,
) -> tuple[HarnessHint, ...]:
    """Mine likely factory/state/teardown/client hooks from tests and docs."""
    class_tokens = {class_name.lower(), *(_camel_case_tokens(class_name))}
    method_tokens = {method_name.lower(), *(_camel_case_tokens(method_name))}
    target_tokens = class_tokens | method_tokens
    state_hint_tokens = {"state", "context", "cache"}
    state_param_name: str | None = None
    collaborator_packs: list[str] = []
    collaborator_evidence: str | None = None
    with contextlib.suppress(Exception):
        module = importlib.import_module(module_name)
        owner = getattr(module, class_name)
        if inspect.getattr_static(owner, method_name, None) is not None:
            state_param_name = _state_param_name_for_callable(getattr(owner, method_name))
        source_attrs, source_path = _source_collaborator_attrs(
            module_name,
            class_name,
            method_name,
        )
        collaborator_packs = _scenario_packs_for_attrs(source_attrs)
        collaborator_evidence = source_path
    hints: list[HarnessHint] = []
    seen: set[tuple[str, str]] = set()

    def _add_hint(
        kind: str,
        suggestion: str,
        evidence: str,
        confidence: float,
        *,
        signals: Sequence[str] = (),
        config: dict[str, Any] | None = None,
    ) -> None:
        key = (kind, suggestion)
        if key in seen:
            return
        seen.add(key)
        normalized_signals = tuple(
            dict.fromkeys(
                [
                    *(str(item) for item in signals if str(item).strip()),
                    *_harness_hint_path_signals(evidence),
                ]
            )
        )
        hints.append(
            HarnessHint(
                kind=kind,
                suggestion=suggestion,
                evidence=evidence,
                confidence=confidence,
                score=_score_harness_hint(confidence, normalized_signals),
                signals=normalized_signals,
                config=dict(config or {}),
            )
        )

    support_files = list(_callable_seed_files(module_name))
    extra_patterns = ("*factory*.py", "*fixture*.py", "*support*.py", "conftest.py")
    for root in _test_search_roots(module_name):
        for resolved in _project_files_matching(root, extra_patterns):
            if resolved not in support_files:
                support_files.append(resolved)

    fixture_catalog = _pytest_fixture_catalog(
        tuple(str(path.resolve()) for path in support_files if path.exists())
    )

    for path in sorted(dict.fromkeys(support_files)):
        tree = _parse_python_source(str(path))
        if tree is None:
            continue
        direct_aliases, module_aliases = _constructor_aliases(tree, module_name, class_name)
        try:
            display_path = path.relative_to(Path.cwd())
        except ValueError:
            display_path = path
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name_lower = node.name.lower()
            doc_lower = (ast.get_docstring(node) or "").lower()
            returns_lower = (
                ast.unparse(node.returns).lower()
                if getattr(node, "returns", None) is not None
                else ""
            )
            text_lower = " ".join([name_lower, doc_lower, returns_lower])
            text_tokens = _searchable_tokens(" ".join([node.name, doc_lower, returns_lower]))
            name_tokens = _searchable_tokens(node.name)
            is_fixture = any(
                _call_name(decorator.func) == "pytest.fixture"
                if isinstance(decorator, ast.Call)
                else _call_name(decorator) == "pytest.fixture"
                for decorator in node.decorator_list
            )
            evidence = f"{display_path}:{getattr(node, 'lineno', '?')}"
            fixture_info = fixture_catalog.get(node.name, {})
            symbol_value = str(fixture_info.get("symbol") or _symbol_hint_value(path, node.name))
            fixture_values = list(fixture_info.get("values", ()))
            returns_mapping = any(isinstance(value, Mapping) for value in fixture_values) or (
                "dict" in returns_lower or "mapping" in returns_lower
            )
            mentions_target = bool(text_tokens & target_tokens)
            mentions_state = bool(text_tokens & state_hint_tokens)
            returns_target, instance_names = _function_returns_target_instance(
                node,
                direct_aliases=direct_aliases,
                module_aliases=module_aliases,
                class_name=class_name,
            )
            returns_target = returns_target or _matches_target_fixture(
                fixture_info,
                class_tokens=class_tokens,
            )
            attr_packs = _scenario_packs_for_attrs(
                _instance_attr_names(node, instance_names=instance_names)
            )
            state_like_name = bool(name_tokens & state_hint_tokens)
            state_param_tokens = (
                _searchable_tokens(state_param_name) if state_param_name is not None else set()
            )
            matches_state_param = bool(
                state_param_name is not None
                and (name_lower == state_param_name.lower() or name_tokens & state_param_tokens)
            )
            supports_state_factory = _callable_supports_optional_instance_call(node)
            looks_like_factory = (
                name_lower.startswith(("make_", "build_", "create_", "new_"))
                or "factory" in name_lower
            )

            if returns_target or (
                mentions_target
                and looks_like_factory
                and not state_like_name
                and not returns_mapping
            ):
                _add_hint(
                    "factory",
                    f"[[objects]] factory -> {evidence}:{node.name}",
                    evidence,
                    0.95 if returns_target else 0.9,
                    signals=(
                        *(("returns_target_instance",) if returns_target else ()),
                        *(("constructor_like",) if looks_like_factory else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "factory",
                        "value": symbol_value,
                    },
                )
            if supports_state_factory and (
                (returns_mapping and (mentions_target or mentions_state or matches_state_param))
                or matches_state_param
            ):
                _add_hint(
                    "state_factory",
                    f"[[objects]] state_factory -> {evidence}:{node.name}",
                    evidence,
                    0.9 if returns_mapping else 0.82,
                    signals=(
                        *(("state_compatible",) if state_param_name is not None else ()),
                        *(("returns_mapping",) if returns_mapping else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "state_factory",
                        "value": symbol_value,
                    },
                )
            if mentions_target and any(
                token in name_lower for token in ("setup", "prepare", "prime", "initialize")
            ):
                _add_hint(
                    "setup",
                    f"[[objects]] setup -> {evidence}:{node.name}",
                    evidence,
                    0.82,
                    signals=(
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        *(("returns_target_instance",) if returns_target else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "setup",
                        "value": symbol_value,
                    },
                )
            if returns_target and attr_packs:
                _add_hint(
                    "scenario_pack",
                    f"[[objects]] scenarios -> {attr_packs!r}",
                    evidence,
                    0.9,
                    signals=(
                        "returns_target_instance",
                        "collaborator_overlap",
                        *(("mentions_target_tokens",) if mentions_target else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": attr_packs,
                    },
                )
            if (
                mentions_target
                and any(token in text_lower for token in ("teardown", "cleanup", "close", "stop"))
            ) or bool(fixture_info.get("yield_cleanup")):
                _add_hint(
                    "teardown",
                    f"[[objects]] teardown -> {evidence}:{node.name}",
                    evidence,
                    0.9 if fixture_info.get("yield_cleanup") else 0.8,
                    signals=(
                        *(("lifecycle_cleanup",) if fixture_info.get("yield_cleanup") else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "teardown",
                        "value": symbol_value,
                    },
                )
            if is_fixture and any(
                token in text_lower for token in ("client", "sandbox", "session", "transport")
            ):
                _add_hint(
                    "client_fixture",
                    f"[[objects]] scenarios -> [{evidence}:{node.name}]",
                    evidence,
                    0.75,
                    signals=(
                        *(("pytest_fixture",) if is_fixture else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        "collaborator_overlap",
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": [symbol_value],
                    },
                )

    if collaborator_packs:
        _add_hint(
            "scenario_pack",
            f"[[objects]] scenarios -> {collaborator_packs!r}",
            collaborator_evidence or f"{module_name}.{class_name}.{method_name}",
            0.7,
            signals=("collaborator_overlap",),
            config={
                "section": "[[objects]]",
                "target": f"{module_name}:{class_name}",
                "method": method_name,
                "key": "scenarios",
                "value": collaborator_packs,
            },
        )

    for path in _harness_doc_files(module_name):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            display_path = path.relative_to(Path.cwd())
        except ValueError:
            display_path = path
        lowered = content.lower()
        if not any(token in lowered for token in target_tokens):
            continue
        for idx, line in enumerate(lowered.splitlines(), 1):
            if not any(token in line for token in target_tokens):
                continue
            evidence = f"{display_path}:{idx}"
            if "state" in line:
                _add_hint(
                    "state_factory",
                    "docs mention state setup for this target",
                    evidence,
                    0.55,
                    signals=("doc_evidence",),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "state_factory",
                        "value": "docs mention state setup for this target",
                    },
                )
            if any(token in line for token in ("setup", "prepare", "initialize", "prime")):
                _add_hint(
                    "setup",
                    "docs mention setup/prepare hooks for this target",
                    evidence,
                    0.55,
                    signals=("doc_evidence",),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "setup",
                        "value": "docs mention setup/prepare hooks for this target",
                    },
                )
            if any(token in line for token in ("teardown", "cleanup", "close", "stop")):
                _add_hint(
                    "teardown",
                    "docs mention lifecycle teardown/cleanup",
                    evidence,
                    0.55,
                    signals=("lifecycle_cleanup", "doc_evidence"),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "teardown",
                        "value": "docs mention lifecycle teardown/cleanup",
                    },
                )
            if any(token in line for token in ("client", "sandbox", "session")):
                _add_hint(
                    "client_fixture",
                    "docs mention a client/session collaborator",
                    evidence,
                    0.5,
                    signals=("collaborator_overlap", "doc_evidence"),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": ["docs mention a client/session collaborator"],
                    },
                )
            doc_packs = _scenario_packs_for_attrs(
                {attr_name for attr_name in _SCENARIO_PACK_ATTR_ALIASES if attr_name in line}
            )
            if doc_packs:
                _add_hint(
                    "scenario_pack",
                    f"[[objects]] scenarios -> {doc_packs!r}",
                    evidence,
                    0.5,
                    signals=("collaborator_overlap", "doc_evidence"),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": doc_packs,
                    },
                )

    return tuple(
        sorted(
            hints,
            key=_hint_sort_key,
        )
    )
def _mine_object_harness_hints(
    module_name: str,
    class_name: str,
    method_name: str,
) -> tuple[HarnessHint, ...]:
    """Mine likely factory/state/teardown/client hooks from tests and docs."""
    return _mine_object_harness_hints_cached(
        module_name,
        class_name,
        method_name,
        str(Path.cwd().resolve()),
    )
_mine_object_harness_hints.cache_clear = _mine_object_harness_hints_cached.cache_clear  # type: ignore[attr-defined]
def _import_alias_maps(
    tree: ast.AST,
    module_name: str,
    leaf_name: str,
) -> tuple[set[str], set[str]]:
    """Return imported module aliases and direct-call aliases for a target."""
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            for alias in node.names:
                if imported_module == module_name and alias.name == leaf_name:
                    function_aliases.add(alias.asname or alias.name)
                elif f"{imported_module}.{alias.name}" == module_name:
                    module_aliases.add(alias.asname or alias.name)
    return module_aliases, function_aliases
def _call_matches_target(
    call: ast.Call,
    *,
    leaf_name: str,
    module_aliases: set[str],
    function_aliases: set[str],
) -> bool:
    """Return True when *call* looks like it invokes the target callable."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in function_aliases
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr == leaf_name and func.value.id in module_aliases
    return False
def _call_kwargs_from_ast(
    call: ast.Call,
    *,
    signature: inspect.Signature,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Convert a literal call site into concrete kwargs."""
    params = [
        param for param in signature.parameters.values() if param.name not in {"self", "cls"}
    ]
    positional_params = [
        param
        for param in params
        if param.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    ]
    if len(call.args) > len(positional_params):
        return None

    kwargs: dict[str, Any] = {}
    for param, arg in zip(positional_params, call.args, strict=False):
        value = _seed_value_from_node(arg, bindings=bindings)
        if value is _MISSING:
            return None
        kwargs[param.name] = value

    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        value = _seed_value_from_node(keyword.value, bindings=bindings)
        if value is _MISSING:
            return None
        kwargs[keyword.arg] = value

    for param in params:
        if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            return None
        if param.name not in kwargs:
            if param.default is inspect.Parameter.empty:
                return None
            kwargs[param.name] = param.default
    return kwargs
