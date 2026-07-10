from __future__ import annotations
# ruff: noqa
def _single_resolved_hint(
    hints: Sequence[HarnessHint],
    *,
    kind: str,
    min_confidence: float,
) -> tuple[Any | None, HarnessHint | None]:
    """Resolve the strongest mined hook for *kind* when evidence is decisive."""
    resolved: dict[str, tuple[Any, HarnessHint]] = {}
    for hint in hints:
        if hint.kind != kind or float(hint.score) < min_confidence:
            continue
        symbol_path = _hint_symbol_path(getattr(hint, "config", {}).get("value"))
        if symbol_path is None:
            continue
        try:
            obj = _resolve_symbol_path(symbol_path)
        except BaseException as exc:
            if _is_fatal_discovery_exception(exc):
                raise
            continue
        current = resolved.get(symbol_path)
        if current is None or _hint_sort_key(hint) < _hint_sort_key(current[1]):
            resolved[symbol_path] = (obj, hint)
    if not resolved:
        return None, None
    ranked = sorted(resolved.values(), key=lambda item: _hint_sort_key(item[1]))
    top_obj, top_hint = ranked[0]
    if len(ranked) == 1:
        return top_obj, top_hint
    second_hint = ranked[1][1]
    top_score = float(top_hint.score)
    second_score = float(second_hint.score)
    decisive_signals = {"returns_target_instance", "lifecycle_cleanup"}
    top_strength = _harness_hint_signal_strength(top_hint.signals)
    second_strength = _harness_hint_signal_strength(second_hint.signals)
    if (
        top_score - second_score >= 0.01
        or top_strength - second_strength >= 0.04
        or decisive_signals & set(top_hint.signals)
    ):
        return top_obj, top_hint
    return None, None
def _single_scenario_pack_hint(
    hints: Sequence[HarnessHint],
    *,
    min_confidence: float,
) -> tuple[tuple[Any, ...], HarnessHint | None]:
    """Resolve one high-confidence built-in scenario pack from mined hints."""
    packs: dict[str, HarnessHint] = {}
    for hint in hints:
        if float(hint.score) < min_confidence:
            continue
        if hint.kind == "scenario_pack":
            raw_value = getattr(hint, "config", {}).get("value")
            values = (
                list(raw_value)
                if isinstance(raw_value, Sequence)
                and not isinstance(raw_value, (str, bytes, bytearray))
                else [raw_value]
            )
            for value in values:
                if not isinstance(value, str):
                    continue
                pack = value.strip()
                if _builtin_object_scenario_hook(pack) is not None:
                    current = packs.get(pack)
                    if current is None or _hint_sort_key(hint) < _hint_sort_key(current):
                        packs[pack] = hint
        elif hint.kind == "client_fixture":
            pack = _scenario_pack_from_hint(hint)
            if pack:
                current = packs.get(pack)
                if current is None or _hint_sort_key(hint) < _hint_sort_key(current):
                    packs[pack] = hint
    if not packs:
        return (), None
    ranked = sorted(packs.items(), key=lambda item: _hint_sort_key(item[1]))
    pack_name, hint = ranked[0]
    if len(ranked) > 1:
        second_hint = ranked[1][1]
        if (
            float(hint.score) - float(second_hint.score) < 0.01
            and _harness_hint_signal_strength(hint.signals)
            - _harness_hint_signal_strength(second_hint.signals)
            < 0.04
        ):
            return (), None
    hook = _builtin_object_scenario_hook(pack_name)
    if hook is None:
        return (), None
    return (hook,), hint
def _mined_object_runtime(owner: type, method_name: str) -> AutoObjectRuntime:
    """Resolve one conservative object runtime from mined harness hints."""
    hints = tuple(_mine_object_harness_hints(owner.__module__, owner.__name__, method_name))
    factory, factory_hint = _single_resolved_hint(hints, kind="factory", min_confidence=0.85)
    setup, setup_hint = _single_resolved_hint(hints, kind="setup", min_confidence=0.8)
    state_factory, state_hint = _single_resolved_hint(
        hints,
        kind="state_factory",
        min_confidence=0.8,
    )
    teardown, teardown_hint = _single_resolved_hint(hints, kind="teardown", min_confidence=0.75)
    scenarios, scenario_hint = _single_scenario_pack_hint(hints, min_confidence=0.75)
    harness = (
        "stateful"
        if state_factory is not None or setup is not None or teardown is not None or scenarios
        else None
    )
    harness_source = "mined" if harness is not None else None
    return AutoObjectRuntime(
        factory=factory,
        factory_source="mined" if factory_hint is not None else None,
        setup=setup,
        setup_source="mined" if setup_hint is not None else None,
        state_factory=state_factory,
        state_factory_source="mined" if state_hint is not None else None,
        teardown=teardown,
        teardown_source="mined" if teardown_hint is not None else None,
        scenarios=scenarios,
        scenario_source="mined" if scenario_hint is not None else None,
        harness=harness,
        harness_source=harness_source,
        hints=hints,
    )
def _verify_auto_object_runtime(
    owner: type,
    *,
    factory: Any | None,
    setup: Any | None = None,
    scenarios: Sequence[Any] | None = None,
    state_factory: Any | None = None,
    state_param: str | None = None,
    factory_source: str | None = None,
    setup_source: str | None = None,
    scenario_source: str | None = None,
    state_factory_source: str | None = None,
) -> tuple[bool, str | None]:
    """Dry-run mined object harness pieces before treating a method as runnable."""
    mined_sources = {
        "factory": factory_source,
        "setup": setup_source,
        "scenario": scenario_source,
        "state_factory": state_factory_source,
    }
    if not any(source == "mined" for source in mined_sources.values()):
        return True, None
    if (
        (factory_source == "configured" and _is_metadata_only_hook(factory))
        or (setup_source == "configured" and _is_metadata_only_hook(setup))
        or (
            scenario_source == "configured"
            and any(_is_metadata_only_hook(hook) for hook in scenarios or ())
        )
        or (state_factory_source == "configured" and _is_metadata_only_hook(state_factory))
    ):
        return True, None
    if factory is None:
        return False, "auto-harness dry-run could not find an object factory"
    try:
        instance = _call_sync(_unwrap(factory))
    except Exception as exc:
        return (
            False,
            f"auto-harness dry-run failed during factory invocation: {type(exc).__name__}: {exc}",
        )
    if not isinstance(instance, owner):
        return (
            False,
            "auto-harness dry-run returned "
            f"{type(instance).__name__}, expected {owner.__qualname__}",
        )
    try:
        if setup is not None and setup_source == "mined":
            instance = _apply_instance_hook(instance, setup)
        if scenarios and scenario_source == "mined":
            instance = _apply_instance_hooks(instance, scenarios)
        if state_factory is not None and state_factory_source == "mined" and state_param:
            _build_state_value(state_factory, instance=instance)
    except Exception as exc:
        return (
            False,
            "auto-harness dry-run failed while preparing the instance: "
            f"{type(exc).__name__}: {exc}",
        )
    return True, None
def _build_state_value(
    state_factory: Any | None,
    *,
    instance: Any,
) -> Any:
    """Build one state object for a bound method invocation."""
    if state_factory is None:
        raise ValueError("state factory is not configured")
    return _call_with_optional_instance_arg(state_factory, instance)
def _prepare_bound_method_call(
    target: Any,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    *,
    instance: Any,
    state_factory: Any | None,
    state_param: str | None,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Normalize wrapper args into kwargs and inject configured state when needed."""
    if not state_param or state_factory is None:
        return tuple(args), dict(kwargs)

    wrapper_sig = _signature_without_first_context(target, omit_names=(state_param,))
    bound = wrapper_sig.bind_partial(*args, **kwargs)
    call_kwargs = dict(bound.arguments)
    call_kwargs.setdefault(state_param, _build_state_value(state_factory, instance=instance))
    return (), call_kwargs
def _apply_instance_hook(instance: Any, hook: Any | None) -> Any:
    """Apply a setup or scenario hook and keep any replacement instance."""
    if hook is None:
        return instance
    if isinstance(hook, Mapping):
        return _apply_instance_scenario_spec(instance, hook)
    result = _call_sync(_unwrap(hook), instance)
    return instance if result is None else result
def _apply_instance_hooks(instance: Any, hooks: Sequence[Any] | None) -> Any:
    """Apply a sequence of setup/scenario hooks in order."""
    current = instance
    for hook in hooks or ():
        current = _apply_instance_hook(current, hook)
    return current
def _normalize_scenario_path(path: str) -> str:
    """Normalize a scenario target path relative to one configured instance."""
    cleaned = path.strip()
    for prefix in ("self.", "instance.", "obj."):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned
def _resolve_scenario_target(instance: Any, path: str) -> tuple[Any, str]:
    """Resolve ``foo.bar.baz`` into ``(foo.bar, "baz")`` on *instance*."""
    cleaned = _normalize_scenario_path(path)
    parts = [part for part in cleaned.split(".") if part]
    if not parts:
        raise ValueError("scenario path is empty")
    current = instance
    for part in parts[:-1]:
        current = getattr(current, part)
    return current, parts[-1]
def _clone_scenario_value(value: object) -> object:
    """Clone configured scenario values when possible to avoid cross-call sharing."""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value
def _scenario_exception(error: object) -> BaseException:
    """Coerce one TOML-friendly exception description into an exception instance."""
    if isinstance(error, BaseException):
        return error
    if inspect.isclass(error) and issubclass(error, BaseException):
        return error()
    if isinstance(error, Mapping):
        name = str(error.get("type") or error.get("name") or "RuntimeError").strip()
        message = str(error.get("message") or error.get("detail") or "").strip()
        exc_type = getattr(builtins, name, RuntimeError)
        if inspect.isclass(exc_type) and issubclass(exc_type, BaseException):
            return exc_type(message)
        return RuntimeError(f"{name}: {message}" if message else name)
    if isinstance(error, str):
        name, sep, message = error.partition(":")
        exc_name = name.strip() or "RuntimeError"
        exc_type = getattr(builtins, exc_name, RuntimeError)
        detail = message.strip() if sep else error.strip()
        if inspect.isclass(exc_type) and issubclass(exc_type, BaseException):
            return exc_type(detail)
        return RuntimeError(detail or exc_name)
    return RuntimeError(repr(error))
def _scenario_stub(original: Any, *, value: object = None, error: object | None = None) -> Any:
    """Build one stub wrapper that preserves async behavior for collaborators."""
    is_async = inspect.iscoroutinefunction(getattr(original, "__func__", original))

    if is_async:

        async def wrapped(*_args: Any, **_kwargs: Any) -> Any:
            if error is not None:
                raise _scenario_exception(error)
            return _clone_scenario_value(value)

    else:

        def wrapped(*_args: Any, **_kwargs: Any) -> Any:
            if error is not None:
                raise _scenario_exception(error)
            return _clone_scenario_value(value)

    return functools.wraps(original)(wrapped)
def _apply_instance_scenario_spec(
    instance: Any,
    spec: Mapping[str, object],
) -> Any:
    """Apply one declarative collaborator scenario spec to *instance*."""
    kind = str(spec.get("kind") or spec.get("action") or "").strip().lower()
    path = str(spec.get("path") or spec.get("attr") or spec.get("target") or "").strip()
    if not kind:
        raise ValueError("scenario spec is missing 'kind'")
    if not path:
        raise ValueError("scenario spec is missing 'path'")

    target, attr_name = _resolve_scenario_target(instance, path)
    match kind:
        case "setattr":
            setattr(target, attr_name, _clone_scenario_value(spec.get("value")))
        case "stub_return":
            original = getattr(target, attr_name)
            setattr(
                target,
                attr_name,
                _scenario_stub(original, value=spec.get("value")),
            )
        case "stub_raise":
            original = getattr(target, attr_name)
            setattr(
                target,
                attr_name,
                _scenario_stub(
                    original,
                    error=spec.get("error") or spec.get("exception") or "RuntimeError",
                ),
            )
        case _:
            raise ValueError(f"unsupported scenario kind: {kind!r}")
    return instance
def _make_sync_callable(
    func: Any,
    *,
    qualname: str | None = None,
    keep_wrapped: bool = False,
) -> Any:
    """Wrap *func* so callers can invoke sync or async callables uniformly."""

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return _call_sync(func, *args, **kwargs)

    try:
        wrapped.__signature__ = inspect.signature(func)
    except (TypeError, ValueError):
        pass
    if qualname is not None:
        wrapped.__qualname__ = qualname
    if keep_wrapped:
        wrapped.__ordeal_keep_wrapped__ = True
    return wrapped
def _resolve_method_callable(
    owner: type,
    method_name: str,
    raw_attr: Any,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Resolve a class attribute into a sync-capable callable."""
    qualname = f"{owner.__qualname__}.{method_name}"
    if isinstance(raw_attr, staticmethod) or isinstance(raw_attr, classmethod):
        bound = getattr(owner, method_name)
        if inspect.iscoroutinefunction(bound):
            return qualname, _make_sync_callable(
                bound,
                qualname=qualname,
                keep_wrapped=True,
            )
        return qualname, bound

    factory = _resolve_object_hook(owner, object_factories)
    setup = _resolve_object_hook(owner, object_setups)
    scenarios = _resolve_object_hooks(owner, object_scenarios)
    state_factory = _resolve_object_hook(owner, object_state_factories)
    teardown = _resolve_object_hook(owner, object_teardowns)
    harness = _resolve_object_harness(owner, object_harnesses)
    factory_source = "configured" if factory is not None else None
    setup_source = "configured" if setup is not None else None
    scenario_source = "configured" if scenarios else None
    state_factory_source = "configured" if state_factory is not None else None
    teardown_source = "configured" if teardown is not None else None
    harness_source = "configured" if harness != "fresh" else None
    mined_runtime = _mined_object_runtime(owner, method_name)
    if factory is None and mined_runtime.factory is not None:
        factory = mined_runtime.factory
        factory_source = mined_runtime.factory_source
    if setup is None and mined_runtime.setup is not None:
        setup = mined_runtime.setup
        setup_source = mined_runtime.setup_source
    if not scenarios and mined_runtime.scenarios:
        scenarios = mined_runtime.scenarios
        scenario_source = mined_runtime.scenario_source
    if state_factory is None and mined_runtime.state_factory is not None:
        state_factory = mined_runtime.state_factory
        state_factory_source = mined_runtime.state_factory_source
    if teardown is None and mined_runtime.teardown is not None:
        teardown = mined_runtime.teardown
        teardown_source = mined_runtime.teardown_source
    if harness == "fresh" and mined_runtime.harness is not None:
        harness = mined_runtime.harness
        harness_source = mined_runtime.harness_source
    state_param = _state_param_name_for_callable(raw_attr)
    if inspect.isfunction(raw_attr):
        if factory is None:
            return (
                qualname,
                _make_unbound_method_placeholder(
                    owner,
                    method_name,
                    raw_attr,
                    state_param=state_param,
                    state_factory=state_factory,
                    state_factory_source=state_factory_source,
                    harness_hints=mined_runtime.hints,
                ),
            )
        harness_verified, harness_dry_run_error = _verify_auto_object_runtime(
            owner,
            factory=factory,
            setup=setup,
            scenarios=scenarios,
            state_factory=state_factory,
            state_param=state_param,
            factory_source=factory_source,
            setup_source=setup_source,
            scenario_source=scenario_source,
            state_factory_source=state_factory_source,
        )
        return (
            qualname,
            _make_bound_method_callable(
                owner,
                method_name,
                raw_attr,
                factory=factory,
                setup=setup,
                scenarios=scenarios,
                state_factory=state_factory,
                state_param=state_param,
                teardown=teardown,
                harness=harness,
                harness_hints=mined_runtime.hints,
                factory_source=factory_source,
                setup_source=setup_source,
                scenario_source=scenario_source,
                state_factory_source=state_factory_source,
                teardown_source=teardown_source,
                harness_source=harness_source,
                harness_verified=harness_verified,
                harness_dry_run_error=harness_dry_run_error,
            ),
        )

    return qualname, _make_sync_callable(getattr(owner, method_name), qualname=qualname)
