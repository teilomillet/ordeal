from __future__ import annotations
# ruff: noqa
def _make_bound_method_callable(
    owner: type,
    method_name: str,
    method: Any,
    *,
    factory: Any,
    setup: Any | None = None,
    scenarios: Sequence[Any] | None = None,
    state_factory: Any | None = None,
    state_param: str | None = None,
    teardown: Any | None = None,
    harness: str = "fresh",
    harness_hints: Sequence[HarnessHint] | None = None,
    factory_source: str | None = None,
    setup_source: str | None = None,
    scenario_source: str | None = None,
    state_factory_source: str | None = None,
    teardown_source: str | None = None,
    harness_source: str | None = None,
    harness_verified: bool = True,
    harness_dry_run_error: str | None = None,
) -> Any:
    """Build a sync wrapper that creates a fresh object per invocation."""
    target = _unwrap(method)
    lifecycle_phase = _lifecycle_phase(method_name, target)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        instance: Any = None
        result: Any = None
        error: BaseException | None = None
        before_state = None
        call_args = tuple(args)
        call_kwargs = dict(kwargs)
        teardown_called = False
        teardown_error: str | None = None
        probe_cleanup: Callable[[], None] | None = None
        probe_context: dict[str, Any] = {}
        lifecycle_runtime: dict[str, Any] = {}
        call_stage = "factory"
        failure_stage: str | None = None
        fault_names = tuple(getattr(wrapped, "__ordeal_contract_faults__", ()))
        try:
            instance = _call_sync(factory)
            before_state = _snapshot_instance_state(instance)
            with _lifecycle_fault_runtime(
                instance,
                owner,
                method_name=method_name,
                setup=setup,
                teardown=teardown,
                fault_names=fault_names,
            ) as lifecycle_runtime:
                runtime_setup = lifecycle_runtime.get("setup_hook", setup)
                runtime_teardown = lifecycle_runtime.get("teardown_hook", teardown)
                try:
                    call_stage = "probe"
                    probe_cleanup, probe_context = _instance_probe_result(
                        getattr(wrapped, "__ordeal_instance_probe__", None),
                        instance=instance,
                        owner=owner,
                        method_name=method_name,
                    )
                    call_stage = "setup"
                    instance = _apply_instance_hook(instance, runtime_setup)
                    call_stage = "scenario"
                    instance = _apply_instance_hooks(instance, scenarios)
                    before_state = _snapshot_instance_state(instance)
                    bound = getattr(instance, method_name)
                    call_stage = "prepare"
                    call_args, call_kwargs = _prepare_bound_method_call(
                        target,
                        args,
                        kwargs,
                        instance=instance,
                        state_factory=state_factory,
                        state_param=state_param,
                    )
                    call_stage = "invoke"
                    result = _call_sync(bound, *call_args, **call_kwargs)
                    return result
                except BaseException as exc:
                    error = exc
                    failure_stage = call_stage
                    raise
                finally:
                    if runtime_teardown is not None:
                        call_stage = "teardown"
                        teardown_called = True
                        try:
                            _call_with_optional_instance_arg(runtime_teardown, instance)
                        except BaseException as exc:
                            teardown_error = f"{type(exc).__name__}: {exc}"
                            if error is None:
                                error = exc
                                raise
        except BaseException as exc:
            error = exc
            if failure_stage is None:
                failure_stage = call_stage
            raise
        finally:
            wrapped.__ordeal_last_call_context__ = {
                "instance": instance,
                "before_state": before_state,
                "after_state": (
                    _snapshot_instance_state(instance) if instance is not None else None
                ),
                "kwargs": dict(call_kwargs),
                "args": tuple(call_args),
                "method_name": method_name,
                "owner": owner,
                "harness": harness,
                "result": result,
                "error": error,
                "teardown_called": teardown_called,
                "teardown_error": teardown_error,
                "lifecycle_phase": lifecycle_phase,
                "lifecycle_runtime": lifecycle_runtime,
                "call_stage": call_stage,
                "failure_stage": failure_stage,
                **probe_context,
            }
            if probe_cleanup is not None:
                probe_cleanup()

    try:
        wrapped.__signature__ = _signature_without_first_context(
            target,
            omit_names=((state_param,) if state_factory is not None and state_param else ()),
        )
    except (TypeError, ValueError):
        pass
    wrapped.__qualname__ = f"{owner.__qualname__}.{method_name}"
    wrapped.__ordeal_requires_factory__ = False
    wrapped.__ordeal_owner__ = owner
    wrapped.__ordeal_method_name__ = method_name
    wrapped.__ordeal_factory__ = factory
    wrapped.__ordeal_factory_source__ = factory_source
    wrapped.__ordeal_setup__ = setup
    wrapped.__ordeal_setup_source__ = setup_source
    wrapped.__ordeal_scenario__ = (scenarios or (None,))[0]
    wrapped.__ordeal_scenarios__ = tuple(scenarios or ())
    wrapped.__ordeal_scenario_source__ = scenario_source
    wrapped.__ordeal_state_factory__ = state_factory
    wrapped.__ordeal_state_factory_source__ = state_factory_source
    wrapped.__ordeal_state_param__ = state_param
    wrapped.__ordeal_teardown__ = teardown
    wrapped.__ordeal_teardown_source__ = teardown_source
    wrapped.__ordeal_harness__ = harness
    wrapped.__ordeal_harness_source__ = harness_source
    wrapped.__ordeal_kind__ = "instance"
    wrapped.__ordeal_lifecycle_phase__ = lifecycle_phase
    wrapped.__ordeal_keep_wrapped__ = True
    wrapped.__ordeal_instance_probe__ = None
    wrapped.__ordeal_auto_harness__ = any(
        source == "mined"
        for source in (
            factory_source,
            setup_source,
            scenario_source,
            state_factory_source,
            teardown_source,
            harness_source,
        )
    )
    wrapped.__ordeal_harness_verified__ = harness_verified
    wrapped.__ordeal_harness_dry_run_error__ = harness_dry_run_error
    wrapped.__ordeal_harness_hints__ = tuple(
        harness_hints or _mine_object_harness_hints(owner.__module__, owner.__name__, method_name)
    )
    return wrapped
def _make_unbound_method_placeholder(
    owner: type,
    method_name: str,
    method: Any,
    *,
    state_param: str | None = None,
    state_factory: Any | None = None,
    state_factory_source: str | None = None,
    harness_hints: Sequence[HarnessHint] | None = None,
) -> Any:
    """Build a placeholder callable for a method that still needs a factory."""
    target = _unwrap(method)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(f"{owner.__qualname__}.{method_name} needs an object factory")

    try:
        wrapped.__signature__ = _signature_without_first_context(target)
    except (TypeError, ValueError):
        pass
    wrapped.__qualname__ = f"{owner.__qualname__}.{method_name}"
    wrapped.__ordeal_requires_factory__ = True
    wrapped.__ordeal_owner__ = owner
    wrapped.__ordeal_method_name__ = method_name
    wrapped.__ordeal_kind__ = "instance"
    wrapped.__ordeal_harness__ = "fresh"
    wrapped.__ordeal_harness_source__ = None
    wrapped.__ordeal_state_factory__ = state_factory
    wrapped.__ordeal_state_factory_source__ = state_factory_source
    wrapped.__ordeal_state_param__ = state_param
    wrapped.__ordeal_lifecycle_phase__ = _lifecycle_phase(method_name, target)
    wrapped.__ordeal_skip_reason__ = "missing object factory"
    wrapped.__ordeal_keep_wrapped__ = True
    wrapped.__ordeal_instance_probe__ = None
    wrapped.__ordeal_auto_harness__ = False
    wrapped.__ordeal_factory_source__ = None
    wrapped.__ordeal_setup_source__ = None
    wrapped.__ordeal_scenario_source__ = None
    wrapped.__ordeal_teardown_source__ = None
    wrapped.__ordeal_harness_hints__ = tuple(
        harness_hints or _mine_object_harness_hints(owner.__module__, owner.__name__, method_name)
    )
    wrapped.__ordeal_scenarios__ = ()
    return wrapped
def _callable_skip_reason(func: Any) -> str | None:
    """Return a human-readable reason a generated callable is not runnable."""
    if getattr(func, "__ordeal_requires_factory__", False):
        return getattr(func, "__ordeal_skip_reason__", "missing object factory")
    return None
def _resolve_explicit_target(
    target: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Resolve ``module:callable`` or ``module:Class.method`` targets."""
    module_name, sep, attr_path = target.partition(":")
    if not sep or not attr_path:
        raise ValueError("Explicit targets must use 'module:callable' syntax")

    obj = _resolve_module(module_name)
    parts = [part for part in attr_path.split(".") if part]
    if not parts:
        raise ValueError("Explicit targets must name a callable")

    for part in parts[:-1]:
        obj = getattr(obj, part)

    final_name = parts[-1]
    if inspect.isclass(obj):
        static_attr = inspect.getattr_static(obj, final_name, None)
        if static_attr is None:
            raise AttributeError(f"{target} does not exist")
        return _resolve_method_callable(
            obj,
            final_name,
            static_attr,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )

    resolved = getattr(obj, final_name)
    if inspect.isclass(resolved):
        raise TypeError(f"{target} resolves to a class, not a callable")
    if not callable(resolved):
        raise TypeError(f"{target} does not resolve to a callable")

    qualname = (
        final_name
        if obj.__class__.__module__ == "builtins"
        else f"{getattr(obj, '__qualname__', obj.__class__.__name__)}.{final_name}"
    )
    return qualname, resolved
def _is_exact_target_selector(selector: str) -> bool:
    """Return whether *selector* is an exact callable selector, not a glob."""
    text = str(selector).strip()
    return bool(text) and not any(char in text for char in "*?[]")
def _resolve_local_target(
    mod: ModuleType,
    target: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Resolve one local callable selector like ``foo`` or ``Env.render``."""
    selector = str(target).strip()
    module_variants = [f"{mod.__name__}."]
    module_parts = [part for part in str(mod.__name__).split(".") if part]
    for index in range(1, len(module_parts)):
        module_variants.append(f"{'.'.join(module_parts[index:])}.")
    for prefix in module_variants:
        if selector.startswith(prefix):
            selector = selector[len(prefix) :]
            break
    try:
        return _resolve_explicit_target(
            f"{mod.__name__}:{selector}",
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"target selector {target!r} matched no callables in module {mod.__name__!r}"
        ) from exc
def _selected_public_functions(
    mod: ModuleType,
    *,
    targets: Sequence[str] | None = None,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> list[tuple[str, Any]]:
    """Return discovered callables filtered to *targets* when provided."""
    normalized_targets = [str(raw).strip() for raw in targets or () if str(raw).strip()]
    if normalized_targets and all(
        _is_exact_target_selector(target) for target in normalized_targets
    ):
        selected: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for target in normalized_targets:
            if ":" in target:
                base_module = target.split(":", 1)[0]
                if base_module != mod.__name__:
                    raise ValueError(
                        f"target {target!r} does not belong to module {mod.__name__!r}"
                    )
                name, func = _resolve_explicit_target(
                    target,
                    object_factories=object_factories,
                    object_setups=object_setups,
                    object_scenarios=object_scenarios,
                    object_state_factories=object_state_factories,
                    object_teardowns=object_teardowns,
                    object_harnesses=object_harnesses,
                )
            else:
                name, func = _resolve_local_target(
                    mod,
                    target,
                    object_factories=object_factories,
                    object_setups=object_setups,
                    object_scenarios=object_scenarios,
                    object_state_factories=object_state_factories,
                    object_teardowns=object_teardowns,
                    object_harnesses=object_harnesses,
                )
            if name in seen:
                continue
            seen.add(name)
            selected.append((name, func))
        return selected

    discovered = _get_public_functions(
        mod,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )
    if not normalized_targets:
        return discovered

    discovered_map = {name: func for name, func in discovered}
    selected: list[tuple[str, Any]] = []
    seen: set[str] = set()

    for target in normalized_targets:
        if ":" in target:
            base_module = target.split(":", 1)[0]
            if base_module != mod.__name__:
                raise ValueError(f"target {target!r} does not belong to module {mod.__name__!r}")

        matched_names = [
            name
            for name in discovered_map
            if _callable_matches_target_selector(mod.__name__, name, target)
        ]
        if matched_names:
            for name in matched_names:
                if name in seen:
                    continue
                seen.add(name)
                selected.append((name, discovered_map[name]))
            continue

        if target in discovered_map:
            name = target
            func = discovered_map[target]
        elif ":" in target:
            name, func = _resolve_explicit_target(
                target,
                object_factories=object_factories,
                object_setups=object_setups,
                object_scenarios=object_scenarios,
                object_state_factories=object_state_factories,
                object_teardowns=object_teardowns,
                object_harnesses=object_harnesses,
            )
        else:
            raise ValueError(
                f"target selector {target!r} matched no callables in module {mod.__name__!r}"
            )

        if name in seen:
            continue
        seen.add(name)
        selected.append((name, func))
    return selected
def _callable_matches_target_selector(module_name: str, name: str, selector: str) -> bool:
    """Return whether *selector* matches discovered callable *name*."""
    raw_selector = str(selector).strip()
    if not raw_selector:
        return False
    variants: list[str] = [
        name,
        f"{module_name}.{name}",
        f"{module_name}:{name}",
    ]
    module_parts = [part for part in str(module_name).split(".") if part]
    for index in range(1, len(module_parts)):
        suffix = ".".join(module_parts[index:])
        variants.extend((f"{suffix}.{name}", f"{suffix}:{name}"))
    return any(fnmatch.fnmatchcase(variant, raw_selector) for variant in variants)
def _command_tokens(value: Any) -> list[str] | None:
    """Return command tokens for shell-like return values."""
    if isinstance(value, os.PathLike):
        return [os.fspath(value)]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, (str, os.PathLike)) for item in value
    ):
        return [os.fspath(item) for item in value]
    return None
def _tracked_string_args(
    kwargs: Mapping[str, Any],
    tracked_params: Sequence[str] | None,
) -> list[str]:
    """Return the string argument values tracked by a semantic contract."""
    names = list(
        tracked_params
        or [name for name, value in kwargs.items() if isinstance(value, (str, os.PathLike))]
    )
    tracked: list[str] = []
    for name in names:
        value = kwargs.get(name)
        if isinstance(value, str):
            tracked.append(value)
        elif isinstance(value, os.PathLike):
            tracked.append(os.fspath(value))
    return tracked
def _tracked_token_count(tokens: Sequence[str], raw: str) -> int:
    """Count occurrences of a tracked argument, allowing slash normalization."""
    variants = {raw}
    if "/" in raw or "\\" in raw:
        variants.add(raw.replace("\\", "/"))
        variants.add(raw.replace("/", "\\"))
    return sum(1 for token in tokens if token in variants)
def _contract_check_is_static(check: ContractCheck) -> bool:
    """Return whether *check* is static-only and should avoid execution."""
    return bool(check.metadata.get("static_only"))
