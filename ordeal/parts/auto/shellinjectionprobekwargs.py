from __future__ import annotations
# ruff: noqa
def _shell_injection_probe_kwargs(
    kwargs: Mapping[str, Any],
    tracked_params: Sequence[str] | None,
) -> dict[str, Any]:
    """Return probe kwargs with shell-metacharacter payloads in tracked string fields."""
    probe_kwargs = dict(kwargs)
    candidate_names = list(tracked_params or probe_kwargs)
    mutated = False
    for name in candidate_names:
        value = probe_kwargs.get(name)
        if isinstance(value, (str, os.PathLike)):
            probe_kwargs[name] = _SHELL_INJECTION_PROBE_VALUE
            mutated = True
    if not mutated:
        for name, value in list(probe_kwargs.items()):
            if isinstance(value, (str, os.PathLike)):
                probe_kwargs[name] = _SHELL_INJECTION_PROBE_VALUE
                mutated = True
    return probe_kwargs
def _shell_value_has_metacharacters(value: Any) -> bool:
    """Return whether *value* contains shell-significant metacharacters."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    return isinstance(value, str) and any(ch in value for ch in _SHELL_INJECTION_META_CHARS)
def _shell_taint_from_value(value: Any) -> int:
    """Return shell-taint severity for one concrete value."""
    if _shell_value_has_metacharacters(value):
        return _SHELL_TAINT_UNSAFE
    return _SHELL_TAINT_CLEAN
def _merge_shell_taint(*states: int) -> int:
    """Return the most dangerous shell-taint state."""
    return max(states, default=_SHELL_TAINT_CLEAN)
def _merge_shell_envs(*envs: Mapping[str, int]) -> dict[str, int]:
    """Merge branch-local shell taint environments conservatively."""
    merged: dict[str, int] = {}
    keys = {key for env in envs for key in env}
    for key in keys:
        merged[key] = _merge_shell_taint(
            *(int(env.get(key, _SHELL_TAINT_CLEAN)) for env in envs),
        )
    return merged
def _callable_display_name(func: Any) -> str:
    """Return a stable display name for *func* in diagnostics."""
    module_name, qual_parts, leaf_name = _call_target_parts(func)
    qualname = ".".join([*qual_parts, leaf_name]) if qual_parts else leaf_name
    return f"{module_name}.{qualname}" if module_name else qualname
def _function_ast_bundle(
    func: Any,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, int, ModuleType | None, type | None] | None:
    """Return parsed AST plus source metadata for *func* when available."""
    target = _unwrap(func)
    try:
        source_lines, start_line = inspect.getsourcelines(target)
        tree = ast.parse(textwrap.dedent("".join(source_lines)))
    except (OSError, TypeError, SyntaxError):
        return None
    node = next(
        (item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    if node is None:
        return None
    module_name = getattr(target, "__module__", "")
    module: ModuleType | None = None
    with contextlib.suppress(Exception):
        module = importlib.import_module(module_name)
    owner = getattr(func, "__ordeal_owner__", None)
    return node, start_line, module, owner
def _resolve_shell_call_name(
    node: ast.Call,
    *,
    module: ModuleType | None,
    owner: type | None,
) -> str | None:
    """Resolve one AST call target into a dotted name when possible."""
    raw_name = _call_to_string(node)
    func_expr = node.func
    if isinstance(func_expr, ast.Name):
        if module is not None:
            obj = getattr(module, func_expr.id, None)
            if callable(obj) and not inspect.isclass(obj):
                obj = _unwrap(obj)
                obj_name = getattr(obj, "__name__", func_expr.id)
                obj_module = getattr(obj, "__module__", "")
                if obj_module and obj_name:
                    return f"{obj_module}.{obj_name}"
        return raw_name
    if (
        isinstance(func_expr, ast.Attribute)
        and isinstance(func_expr.value, ast.Name)
        and func_expr.value.id in {"self", "cls"}
        and owner is not None
    ):
        method = inspect.getattr_static(owner, func_expr.attr, None)
        if callable(method) and not inspect.isclass(method):
            method = _unwrap(method)
            return ".".join(
                part
                for part in (
                    getattr(owner, "__module__", ""),
                    getattr(owner, "__name__", ""),
                    getattr(method, "__name__", func_expr.attr),
                )
                if part
            )
    return raw_name
def _shell_sink_specs() -> tuple[dict[str, Any], ...]:
    """Return built-in sink specs relevant to shell-injection analysis."""
    return _DEFAULT_SHELL_INJECTION_SINK_SPECS
def _matching_shell_sink_specs(call_name: str | None) -> list[dict[str, Any]]:
    """Return shell sink specs whose pattern matches *call_name*."""
    if not call_name:
        return []
    matches: list[dict[str, Any]] = []
    for spec in _shell_sink_specs():
        pattern = str(spec.get("pattern", "")).strip()
        if not pattern:
            continue
        with contextlib.suppress(re.error):
            if re.search(pattern, call_name):
                matches.append(spec)
    return matches
def _shell_call_arg(
    node: ast.Call,
    parameter: str | int,
) -> ast.AST | None:
    """Return the AST expression bound to one sink parameter selector."""
    if isinstance(parameter, int):
        return node.args[parameter] if 0 <= parameter < len(node.args) else None
    for keyword in node.keywords:
        if keyword.arg == parameter:
            return keyword.value
    return None
def _subprocess_shell_enabled(node: ast.Call) -> bool:
    """Return whether a subprocess-like call explicitly enables shell parsing."""
    for keyword in node.keywords:
        if keyword.arg != "shell":
            continue
        if isinstance(keyword.value, ast.Constant):
            return bool(keyword.value.value)
        return True
    return False
def _shell_expr_taint(
    expr: ast.AST | None,
    *,
    env: Mapping[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[int, _ShellInjectionFlow | None]:
    """Return shell taint for one expression plus any nested sink flow."""
    if expr is None:
        return _SHELL_TAINT_CLEAN, None
    if isinstance(expr, ast.Name):
        return int(env.get(expr.id, _SHELL_TAINT_CLEAN)), None
    if isinstance(expr, ast.Constant):
        return _shell_taint_from_value(expr.value), None
    if isinstance(expr, ast.JoinedStr):
        states = []
        for value in expr.values:
            if isinstance(value, ast.FormattedValue):
                state, flow = _shell_expr_taint(
                    value.value,
                    env=env,
                    module=module,
                    owner=owner,
                    depth=depth,
                    call_path=call_path,
                    seen=seen,
                )
                if flow is not None:
                    return state, flow
                states.append(state)
        if any(state == _SHELL_TAINT_UNSAFE for state in states):
            return _SHELL_TAINT_UNSAFE, None
        if any(state == _SHELL_TAINT_SAFE for state in states):
            return _SHELL_TAINT_SAFE, None
        return _SHELL_TAINT_CLEAN, None
    if isinstance(expr, ast.BinOp):
        left_state, flow = _shell_expr_taint(
            expr.left,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return left_state, flow
        right_state, flow = _shell_expr_taint(
            expr.right,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return right_state, flow
        return _merge_shell_taint(left_state, right_state), None
    if isinstance(expr, (ast.List, ast.Tuple)):
        states: list[int] = []
        for item in expr.elts:
            state, flow = _shell_expr_taint(
                item,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return state, flow
            states.append(state)
        if any(state != _SHELL_TAINT_CLEAN for state in states):
            return _SHELL_TAINT_SAFE, None
        return _SHELL_TAINT_CLEAN, None
    if isinstance(expr, ast.Call):
        call_name = _resolve_shell_call_name(expr, module=module, owner=owner)
        if call_name:
            flow = _shell_sink_uses_tainted_input(
                expr,
                call_name=call_name,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                start_line=0,
                seen=seen,
            )
            if flow is not None:
                return _SHELL_TAINT_UNSAFE, flow
        if call_name in {"shlex.quote", "quote"} and expr.args:
            arg_state, flow = _shell_expr_taint(
                expr.args[0],
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return arg_state, flow
            if arg_state != _SHELL_TAINT_CLEAN:
                return _SHELL_TAINT_SAFE, None
            return _SHELL_TAINT_CLEAN, None
        if (
            isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "format"
            and isinstance(expr.func.value, ast.Constant)
            and isinstance(expr.func.value.value, str)
        ):
            states: list[int] = []
            for item in (*expr.args, *(keyword.value for keyword in expr.keywords)):
                state, flow = _shell_expr_taint(
                    item,
                    env=env,
                    module=module,
                    owner=owner,
                    depth=depth,
                    call_path=call_path,
                    seen=seen,
                )
                if flow is not None:
                    return state, flow
                states.append(state)
            return _merge_shell_taint(*states), None
        if call_name in {"str", "repr", "os.fspath", "pathlib.Path"} and expr.args:
            return _shell_expr_taint(
                expr.args[0],
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
        helper = _resolve_local_shell_helper(call_name, module=module, owner=owner)
        if helper is not None and depth > 0:
            arg_states, flow = _shell_call_arg_states(
                expr,
                helper,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return _SHELL_TAINT_UNSAFE, flow
            helper_flow, return_state = _analyze_shell_flow(
                helper,
                arg_states,
                depth=depth - 1,
                call_path=(*call_path, _callable_display_name(helper)),
                seen=seen,
            )
            if helper_flow is not None:
                return _SHELL_TAINT_UNSAFE, helper_flow
            return return_state, None
    return _SHELL_TAINT_CLEAN, None
def _resolve_local_shell_helper(
    call_name: str | None,
    *,
    module: ModuleType | None,
    owner: type | None,
) -> Any | None:
    """Resolve same-module or same-class helpers for interprocedural shell analysis."""
    if not call_name:
        return None
    leaf = call_name.rsplit(".", 1)[-1]
    if module is not None:
        helper = getattr(module, leaf, None)
        if callable(helper) and not inspect.isclass(helper):
            helper = _unwrap(helper)
            if getattr(helper, "__module__", None) == module.__name__:
                return helper
    if owner is not None and "." in call_name and call_name.split(".")[0] in {"self", "cls"}:
        helper = inspect.getattr_static(owner, leaf, None)
        if callable(helper) and not inspect.isclass(helper):
            return helper
    if owner is not None and call_name.endswith(f".{leaf}"):
        helper = inspect.getattr_static(owner, leaf, None)
        if callable(helper) and not inspect.isclass(helper):
            return helper
    return None
def _shell_call_arg_states(
    node: ast.Call,
    helper: Any,
    *,
    env: Mapping[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[dict[str, int], _ShellInjectionFlow | None]:
    """Map one helper call's tainted arguments onto the callee's parameters."""
    try:
        params = [
            param
            for param in inspect.signature(_unwrap(helper)).parameters.values()
            if param.name not in {"self", "cls"}
        ]
    except Exception:
        return {}, None
    states: dict[str, int] = {}
    for index, arg in enumerate(node.args):
        if index >= len(params):
            break
        state, flow = _shell_expr_taint(
            arg,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return states, flow
        states[params[index].name] = state
    param_names = {param.name for param in params}
    for keyword in node.keywords:
        if keyword.arg is None or keyword.arg not in param_names:
            continue
        state, flow = _shell_expr_taint(
            keyword.value,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return states, flow
        states[keyword.arg] = state
    return states, None
def _shell_sink_uses_tainted_input(
    node: ast.Call,
    *,
    call_name: str,
    env: Mapping[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    start_line: int,
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> _ShellInjectionFlow | None:
    """Return a flow when tainted input reaches one shell sink unsafely."""
    for spec in _matching_shell_sink_specs(call_name):
        for parameter in tuple(spec.get("taint_args", ())):
            expr = _shell_call_arg(node, parameter)
            if expr is None:
                continue
            state, nested_flow = _shell_expr_taint(
                expr,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if nested_flow is not None:
                return nested_flow
            if state != _SHELL_TAINT_UNSAFE:
                continue
            if "subprocess." in call_name and not _subprocess_shell_enabled(node):
                if isinstance(expr, (ast.List, ast.Tuple, ast.Name)):
                    continue
            line = start_line + getattr(node, "lineno", 1) - 1 if start_line > 0 else None
            source_params = tuple(
                sorted(name for name, value in env.items() if value == _SHELL_TAINT_UNSAFE)
            )
            return _ShellInjectionFlow(
                sink=call_name,
                line=line,
                parameter=parameter,
                call_path=call_path,
                source_params=source_params,
            )
    return None
def _shell_bind_targets(target: ast.AST, state: int, env: dict[str, int]) -> None:
    """Bind one assignment target in the local shell-taint environment."""
    if isinstance(target, ast.Name):
        env[target.id] = state
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _shell_bind_targets(item, state, env)
