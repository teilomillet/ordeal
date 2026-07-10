from __future__ import annotations
# ruff: noqa
def _analyze_shell_statements(
    statements: Sequence[ast.stmt],
    *,
    env: dict[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    start_line: int,
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[_ShellInjectionFlow | None, dict[str, int], int]:
    """Walk one function body, propagating taint and surfacing the first sink flow."""
    return_state = _SHELL_TAINT_CLEAN
    for statement in statements:
        if isinstance(statement, ast.Assign):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            for target in statement.targets:
                _shell_bind_targets(target, state, env)
            continue
        if isinstance(statement, ast.AnnAssign):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            _shell_bind_targets(statement.target, state, env)
            continue
        if isinstance(statement, ast.AugAssign):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            if isinstance(statement.target, ast.Name):
                env[statement.target.id] = _merge_shell_taint(
                    env.get(statement.target.id, _SHELL_TAINT_CLEAN),
                    state,
                )
            continue
        if isinstance(statement, ast.Return):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            return_state = _merge_shell_taint(return_state, state)
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call_name = _resolve_shell_call_name(statement.value, module=module, owner=owner)
            flow = _shell_sink_uses_tainted_input(
                statement.value,
                call_name=call_name or "",
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                start_line=start_line,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            return_state = _merge_shell_taint(return_state, state)
            continue
        branch_lists: list[Sequence[ast.stmt]] = []
        if isinstance(statement, ast.If):
            branch_lists = [statement.body, statement.orelse]
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            branch_lists = [statement.body, statement.orelse]
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            branch_lists = [statement.body]
        elif isinstance(statement, ast.Try):
            branch_lists = [
                statement.body,
                statement.orelse,
                statement.finalbody,
                *(handler.body for handler in statement.handlers),
            ]
        if branch_lists:
            branch_envs: list[dict[str, int]] = [dict(env)]
            branch_returns: list[int] = [return_state]
            for branch in branch_lists:
                flow, branch_env, branch_return = _analyze_shell_statements(
                    branch,
                    env=dict(env),
                    module=module,
                    owner=owner,
                    depth=depth,
                    call_path=call_path,
                    start_line=start_line,
                    seen=seen,
                )
                if flow is not None:
                    return flow, env, return_state
                branch_envs.append(branch_env)
                branch_returns.append(branch_return)
            env = _merge_shell_envs(*branch_envs)
            return_state = _merge_shell_taint(*branch_returns)
    return None, env, return_state
def _analyze_shell_flow(
    func: Any,
    param_states: Mapping[str, int],
    *,
    depth: int,
    call_path: tuple[str, ...],
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[_ShellInjectionFlow | None, int]:
    """Analyze one callable for shell-injection flow and tainted return construction."""
    key = (
        _callable_display_name(func),
        tuple(sorted((str(name), int(state)) for name, state in param_states.items())),
        depth,
    )
    if key in seen:
        return None, _SHELL_TAINT_CLEAN
    seen.add(key)
    bundle = _function_ast_bundle(func)
    if bundle is None:
        return None, _SHELL_TAINT_CLEAN
    node, start_line, module, owner = bundle
    flow, _env, return_state = _analyze_shell_statements(
        node.body,
        env={str(name): int(state) for name, state in param_states.items()},
        module=module,
        owner=owner,
        depth=depth,
        call_path=call_path,
        start_line=start_line,
        seen=seen,
    )
    return flow, return_state
def _record_static_contract_context(func: Any, context: Mapping[str, Any] | None) -> None:
    """Store one static-analysis context payload on *func* for later reporting."""
    setattr(func, "__ordeal_last_static_contract_context__", dict(context or {}))
def _static_shell_injection_flow(
    func: Any,
    kwargs: Mapping[str, Any],
) -> _ShellInjectionFlow | None:
    """Return a static shell-injection flow from *kwargs* into a known shell sink."""
    param_states = {
        str(name): _shell_taint_from_value(value)
        for name, value in kwargs.items()
        if _shell_taint_from_value(value) != _SHELL_TAINT_CLEAN
    }
    if not param_states:
        return None
    flow, _return_state = _analyze_shell_flow(
        func,
        param_states,
        depth=3,
        call_path=(_callable_display_name(func),),
        seen=set(),
    )
    return flow
def shell_safe_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a shell-safety probe for command construction helpers."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable("shell_safe only applies to command-builder outputs")
        for raw in _tracked_string_args(kwargs, tracked_params):
            if any(ch in raw for ch in " \t;&|`$><()[]{}*?"):
                if _tracked_token_count(tokens, raw) != 1:
                    return False
        return True

    return ContractCheck(
        name="shell_safe",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="shell-unsafe string interpolation",
    )
def shell_injection_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a static shell-injection oracle for command-executing helpers."""

    def predicate(
        value: Any,
        *,
        func: Any,
        kwargs: Mapping[str, Any],
        **_extra: Any,
    ) -> bool:
        del value
        if not any(_shell_value_has_metacharacters(item) for item in kwargs.values()):
            raise ContractNotApplicable(
                "shell_injection only applies to string inputs containing shell metacharacters"
            )
        flow = _static_shell_injection_flow(func, kwargs)
        _record_static_contract_context(
            func,
            (
                {
                    "kind": "shell_injection",
                    "sink": flow.sink,
                    "line": flow.line,
                    "parameter": flow.parameter,
                    "call_path": list(flow.call_path),
                    "source_params": list(flow.source_params),
                }
                if flow is not None
                else None
            ),
        )
        return flow is None

    return ContractCheck(
        name="shell_injection",
        kwargs=_shell_injection_probe_kwargs(kwargs, tracked_params),
        predicate=predicate,
        summary="shell metacharacters can reach a shell sink without quoting",
        metadata={"static_only": True},
    )
def quoted_paths_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a path-quoting probe for command builders."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable("quoted_paths only applies to command-builder outputs")
        for raw in _tracked_string_args(kwargs, tracked_params):
            if "/" in raw or "\\" in raw or " " in raw:
                if _tracked_token_count(tokens, raw) != 1:
                    return False
        return True

    return ContractCheck(
        name="quoted_paths",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="path quoting or escaping regression",
    )
def command_arg_stability_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a probe that ensures tracked args survive command construction."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable(
                "command_arg_stability only applies to command-builder outputs"
            )
        for raw in _tracked_string_args(kwargs, tracked_params):
            if _tracked_token_count(tokens, raw) != 1:
                return False
        return True

    return ContractCheck(
        name="command_arg_stability",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="command construction invariant failed",
    )
def protected_env_keys_contract(
    *,
    kwargs: dict[str, Any],
    protected_keys: Sequence[str],
    env_param: str | None = None,
) -> ContractCheck:
    """Build a probe that checks protected env keys survive updates."""
    resolved_env_param = env_param or next(
        (name for name, value in kwargs.items() if isinstance(value, Mapping)),
        None,
    )

    def predicate(value: Any) -> bool:
        if resolved_env_param is None:
            return False
        original = kwargs.get(resolved_env_param)
        if not isinstance(original, Mapping) or not isinstance(value, Mapping):
            return False
        return all(value.get(key) == original.get(key) for key in protected_keys)

    return ContractCheck(
        name="protected_env_keys",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="protected env-var contract violated",
    )
def json_roundtrip_contract(
    *,
    kwargs: dict[str, Any],
) -> ContractCheck:
    """Build a probe that checks returned values survive JSON normalization."""
    import json

    def predicate(value: Any) -> bool:
        try:
            json.dumps(value)
        except Exception:
            return False
        return True

    return ContractCheck(
        name="json_roundtrip",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="JSON/tool-call normalization regression",
    )
def http_shape_contract(
    *,
    kwargs: dict[str, Any],
) -> ContractCheck:
    """Build a probe that checks HTTP-like payloads keep string-shaped headers and body."""

    def _mapping_is_httpish(value: Mapping[Any, Any]) -> bool:
        for key, item in value.items():
            if not isinstance(key, str):
                return False
            if isinstance(item, Mapping):
                if any(not isinstance(nested_key, str) for nested_key in item):
                    return False
        return True

    def _body_is_httpish(value: Any) -> bool:
        return value is None or isinstance(
            value,
            (str, bytes, bytearray, memoryview, Mapping, list, tuple),
        )

    def predicate(value: Any) -> bool:
        if isinstance(value, Mapping):
            return _mapping_is_httpish(value)
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) == 2 and isinstance(items[0], Mapping):
                return _mapping_is_httpish(items[0]) and _body_is_httpish(items[1])
            if len(items) == 3 and isinstance(items[0], int) and isinstance(items[1], Mapping):
                return _mapping_is_httpish(items[1]) and _body_is_httpish(items[2])
        raise ContractNotApplicable(
            "http_shape only applies to HTTP-like mapping or response outputs"
        )

    return ContractCheck(
        name="http_shape",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="HTTP header/body shaping regression",
    )
def subprocess_argv_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a probe that checks subprocess argv tokens stay intact."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable("subprocess_argv only applies to command-builder outputs")
        if not tokens or not isinstance(tokens[0], str) or not tokens[0]:
            return False
        for raw in _tracked_string_args(kwargs, tracked_params):
            if _tracked_token_count(tokens, raw) != 1:
                return False
        return True

    return ContractCheck(
        name="subprocess_argv",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="subprocess argv construction regression",
    )
def lifecycle_attempts_all_contract(
    *,
    kwargs: dict[str, Any],
    phase: str,
    fault: str = "raise_cleanup_handler",
    handler_name: str | None = None,
    contract_name: str = "lifecycle_attempts_all",
) -> ContractCheck:
    """Build a lifecycle probe that requires best-effort handler attempts."""

    def predicate(
        value: Any,
        *,
        lifecycle_probe: Mapping[str, Any] | None = None,
        **_extra: Any,
    ) -> bool:
        del value
        probe = dict(lifecycle_probe or {})
        attempts = list(probe.get("attempts", []))
        target_handlers = list(probe.get("target_handlers", []))
        if not target_handlers:
            return False
        return all(name in attempts for name in target_handlers)

    return ContractCheck(
        name=contract_name,
        kwargs=dict(kwargs),
        predicate=predicate,
        summary=f"all {phase} handlers should be attempted even if one fails",
        metadata={
            "kind": "lifecycle",
            "phase": phase,
            "fault": fault,
            "handler_name": handler_name,
        },
    )
