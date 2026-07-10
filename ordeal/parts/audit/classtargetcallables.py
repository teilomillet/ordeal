from __future__ import annotations
# ruff: noqa
def _class_target_callables(
    module_name: str,
    cls_name: str,
    cls: type[Any],
    *,
    factory: str | Any | None = None,
    setup: str | Any | None = None,
    scenarios: Sequence[str | Any] | None = None,
    state_factory: str | Any | None = None,
    teardown: str | Any | None = None,
    harness: str = "fresh",
    method_names: Sequence[str] | None = None,
    include_private: bool = False,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Collect callable methods from a class target."""
    discovered: list[tuple[str, object]] = []
    skipped: list[str] = []
    candidate_names = list(method_names or [])
    if not candidate_names:
        candidate_names = [
            name
            for name in sorted(dir(cls))
            if not name.startswith("__") and (include_private or not name.startswith("_"))
        ]

    for method_name in candidate_names:
        if not include_private and method_name.startswith("_"):
            continue
        try:
            descriptor = inspect.getattr_static(cls, method_name)
        except AttributeError:
            skipped.append(f"{cls_name}.{method_name} (missing attribute)")
            continue

        qualname = f"{cls_name}.{method_name}"

        if isinstance(descriptor, staticmethod):
            reference = getattr(cls, method_name)
            discovered.append(
                (
                    qualname,
                    _wrap_audit_callable(
                        reference,
                        reference,
                        module_name=module_name,
                        qualname=qualname,
                        owner=cls,
                        method_name=method_name,
                        factory=factory,
                        setup=setup,
                        scenarios=scenarios,
                        state_factory=state_factory,
                        teardown=teardown,
                        harness=harness,
                        kind="static",
                    ),
                )
            )
            continue
        if isinstance(descriptor, classmethod):
            reference = getattr(cls, method_name)
            discovered.append(
                (
                    qualname,
                    _wrap_audit_callable(
                        reference,
                        reference,
                        module_name=module_name,
                        qualname=qualname,
                        owner=cls,
                        method_name=method_name,
                        factory=factory,
                        setup=setup,
                        scenarios=scenarios,
                        state_factory=state_factory,
                        teardown=teardown,
                        harness=harness,
                        kind="class",
                    ),
                )
            )
            continue
        if callable(descriptor):
            prototype, reason = _instantiate_audit_owner(
                cls,
                factory=factory,
                setup=setup,
                scenarios=scenarios,
            )
            if prototype is None:
                skipped.append(f"{qualname} ({reason})")
                continue

            reference = getattr(prototype, method_name)
            state_param = _state_param_name_for_callable(reference)

            def _invoke(
                *args: Any,
                __cls: type[Any] = cls,
                __method_name: str = method_name,
                __factory: str | Any | None = factory,
                __setup: str | Any | None = setup,
                __scenarios: tuple[str | Any, ...] = tuple(scenarios or ()),
                __state_factory: str | Any | None = state_factory,
                __state_param: str | None = state_param,
                __teardown: str | Any | None = teardown,
                __reference: Any = reference,
                **kwargs: Any,
            ) -> Any:
                instance, inst_reason = _instantiate_audit_owner(
                    __cls,
                    factory=__factory,
                    setup=None,
                    scenarios=None,
                )
                if instance is None:
                    raise RuntimeError(inst_reason or f"cannot instantiate {__cls.__name__}")
                fault_names = tuple(getattr(_invoke, "__ordeal_contract_faults__", ()))
                call_args = tuple(args)
                call_kwargs = dict(kwargs)
                before_state = _snapshot_instance_state(instance)
                result: Any = None
                error: BaseException | None = None
                teardown_called = False
                teardown_error: str | None = None
                probe_cleanup: Callable[[], None] | None = None
                probe_context: dict[str, Any] = {}
                with _lifecycle_fault_runtime(
                    instance,
                    __cls,
                    method_name=__method_name,
                    setup=_resolve_audit_hook(__setup),
                    teardown=_resolve_audit_hook(__teardown),
                    fault_names=fault_names,
                ) as lifecycle_runtime:
                    runtime_setup = lifecycle_runtime.get(
                        "setup_hook",
                        _resolve_audit_hook(__setup),
                    )
                    runtime_teardown = lifecycle_runtime.get(
                        "teardown_hook",
                        _resolve_audit_hook(__teardown),
                    )
                    try:
                        probe_cleanup, probe_context = _instance_probe_result(
                            getattr(_invoke, "__ordeal_instance_probe__", None),
                            instance=instance,
                            owner=__cls,
                            method_name=__method_name,
                        )
                        if __setup:
                            setup_result = _call_with_async_support(runtime_setup, instance)
                            if setup_result is not None:
                                instance = setup_result
                        for scenario in __scenarios:
                            scenario_obj = _resolve_audit_hook(scenario)
                            if scenario_obj is None:
                                continue
                            scenario_result = _call_with_async_support(scenario_obj, instance)
                            if scenario_result is not None:
                                instance = scenario_result
                        bound = getattr(instance, __method_name)
                        call_args, call_kwargs = _prepare_bound_method_call(
                            __reference,
                            args,
                            kwargs,
                            instance=instance,
                            state_factory=_resolve_audit_hook(__state_factory),
                            state_param=__state_param,
                        )
                        before_state = _snapshot_instance_state(instance)
                        result = _call_with_async_support(bound, *call_args, **call_kwargs)
                        return result
                    except BaseException as exc:
                        error = exc
                        raise
                    finally:
                        if runtime_teardown is not None:
                            teardown_called = True
                            try:
                                _call_with_async_support(runtime_teardown, instance)
                            except BaseException as exc:
                                teardown_error = f"{type(exc).__name__}: {exc}"
                                if error is None:
                                    error = exc
                                    raise
                        _invoke.__ordeal_last_call_context__ = {
                            "instance": instance,
                            "before_state": before_state,
                            "after_state": _snapshot_instance_state(instance),
                            "kwargs": dict(call_kwargs),
                            "args": tuple(call_args),
                            "method_name": __method_name,
                            "owner": __cls,
                            "harness": harness,
                            "result": result,
                            "error": error,
                            "teardown_called": teardown_called,
                            "teardown_error": teardown_error,
                            "lifecycle_phase": getattr(
                                __reference,
                                "__ordeal_lifecycle_phase__",
                                None,
                            ),
                            "lifecycle_runtime": lifecycle_runtime,
                            **probe_context,
                        }
                        if probe_cleanup is not None:
                            probe_cleanup()

            discovered.append(
                (
                    qualname,
                    _wrap_audit_callable(
                        reference,
                        _invoke,
                        module_name=module_name,
                        qualname=qualname,
                        owner=cls,
                        method_name=method_name,
                        factory=factory,
                        setup=setup,
                        scenarios=scenarios,
                        state_factory=state_factory,
                        teardown=teardown,
                        harness=harness,
                        kind="instance",
                    ),
                )
            )
            continue

        skipped.append(f"{qualname} (not callable)")

    return discovered, skipped
def _module_target_callables(
    mod: ModuleType,
    *,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Collect public module callables plus class methods when available."""
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
    return discovered, []
def _collect_target_callables(
    target: str,
    *,
    factory: str | Any | None = None,
    setup: str | Any | None = None,
    scenarios: Sequence[str | Any] | None = None,
    state_factory: str | Any | None = None,
    teardown: str | Any | None = None,
    harness: str = "fresh",
    methods: Sequence[str] | None = None,
    include_private: bool = False,
) -> tuple[list[tuple[str, object]], list[str]]:
    """Collect callables for a configured audit target."""
    module_path, owner_path, method_name = _split_audit_target_spec(target)
    mod = _resolve_audit_symbol(module_path)
    if not isinstance(mod, ModuleType):
        raise TypeError(f"{module_path!r} is not a module")

    if owner_path is None:
        return _module_target_callables(mod, include_private=include_private)

    owner: Any = mod
    for part in owner_path.split("."):
        owner = getattr(owner, part)

    if not inspect.isclass(owner):
        raise TypeError(f"{target!r} does not resolve to a class target")

    if method_name is not None:
        return _class_target_callables(
            mod.__name__,
            owner_path,
            owner,
            factory=factory,
            setup=setup,
            scenarios=scenarios,
            state_factory=state_factory,
            teardown=teardown,
            harness=harness,
            method_names=[method_name],
            include_private=include_private,
        )

    return _class_target_callables(
        mod.__name__,
        owner_path,
        owner,
        factory=factory,
        setup=setup,
        scenarios=scenarios,
        state_factory=state_factory,
        teardown=teardown,
        harness=harness,
        method_names=methods,
        include_private=include_private,
    )
def _audit_object_hook_maps(
    target_specs: Sequence[Any] | None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, str],
]:
    """Resolve configured object hooks for class targets."""
    factories: dict[str, Any] = {}
    setups: dict[str, Any] = {}
    scenarios: dict[str, Any] = {}
    state_factories: dict[str, Any] = {}
    teardowns: dict[str, Any] = {}
    harnesses: dict[str, str] = {}
    for spec in target_specs or []:
        if isinstance(spec, str):
            target = spec
            factory = None
            setup = None
            state_factory = None
            teardown = None
            harness = "fresh"
            scenario_paths: list[str] = []
        else:
            target = str(getattr(spec, "target"))
            factory = getattr(spec, "factory", None)
            setup = getattr(spec, "setup", None)
            state_factory = getattr(spec, "state_factory", None)
            teardown = getattr(spec, "teardown", None)
            harness = str(getattr(spec, "harness", "fresh") or "fresh").strip().lower()
            scenario_paths = list(getattr(spec, "scenarios", []) or [])
        module_path, owner_path, _method_name = _split_audit_target_spec(target)
        if owner_path is None:
            continue
        owner_keys = {
            owner_path,
            f"{module_path}:{owner_path}",
            f"{module_path}.{owner_path}",
            f"{module_path}:{owner_path.split('.')[-1]}",
            f"{module_path}.{owner_path.split('.')[-1]}",
        }
        if factory:
            factory_obj = _resolve_audit_symbol(factory)
            for key in owner_keys:
                factories[key] = factory_obj
        if setup:
            setup_obj = _resolve_audit_symbol(setup)
            for key in owner_keys:
                setups[key] = setup_obj
        if state_factory:
            state_factory_obj = _resolve_audit_symbol(state_factory)
            for key in owner_keys:
                state_factories[key] = state_factory_obj
        if teardown:
            teardown_obj = _resolve_audit_symbol(teardown)
            for key in owner_keys:
                teardowns[key] = teardown_obj
        for key in owner_keys:
            harnesses[key] = harness if harness in {"fresh", "stateful"} else "fresh"
        if scenario_paths:
            resolved_hooks = [_resolve_audit_symbol(path) for path in scenario_paths]

            def _scenario_hook(
                instance: Any,
                *,
                __hooks: Sequence[Any] = tuple(resolved_hooks),
            ) -> Any:
                current = instance
                for hook in __hooks:
                    result = _call_with_async_support(hook, current)
                    if result is not None:
                        current = result
                return current

            setattr(_scenario_hook, "__ordeal_scenario_count__", len(resolved_hooks))
            for key in owner_keys:
                scenarios[key] = _scenario_hook
    return factories, setups, scenarios, state_factories, teardowns, harnesses
def _collect_audit_functions(
    module: str | ModuleType,
    *,
    target_specs: Sequence[Any] | None = None,
) -> tuple[list[tuple[str, object]], list[str], list[tuple[str, object]]]:
    """Split public module and object-target callables into scannable and skipped groups.

    ``scannable`` means ordeal can infer Hypothesis strategies for the
    function, so audit can fuzz it, mine properties, and generate tests.
    ``skipped`` functions still appear in the summary as fixture gaps.
    """
    mod = _resolve_module(module)
    (
        object_factories,
        object_setups,
        object_scenarios,
        object_state_factories,
        object_teardowns,
        object_harnesses,
    ) = _audit_object_hook_maps(target_specs)
    scannable: dict[str, object] = {}
    discovered_callables: dict[str, object] = {}
    skipped: dict[str, None] = {}

    def _add_scannable(name: str, func: object) -> None:
        scannable[name] = func
        discovered_callables[name] = func
        skipped.pop(name, None)

    def _add_skipped(name: str, func: object | None = None) -> None:
        if func is not None:
            discovered_callables[name] = func
        skipped.setdefault(name, None)

    discovered, module_skipped = _module_target_callables(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )
    for name, func in discovered:
        _add_scannable(name, func)
    for name in module_skipped:
        _add_skipped(name)

    for spec in target_specs or []:
        if isinstance(spec, str):
            target = spec
            factory = None
            setup = None
            scenarios = None
            methods = None
            include_private = False
        else:
            target = str(getattr(spec, "target"))
            factory = getattr(spec, "factory", None)
            setup = getattr(spec, "setup", None)
            scenarios = list(getattr(spec, "scenarios", []) or [])
            state_factory = getattr(spec, "state_factory", None)
            teardown = getattr(spec, "teardown", None)
            harness = str(getattr(spec, "harness", "fresh") or "fresh")
            methods = list(getattr(spec, "methods", []))
            include_private = bool(getattr(spec, "include_private", False))
        discovered, target_skipped = _collect_target_callables(
            target,
            factory=factory,
            setup=setup,
            scenarios=scenarios,
            state_factory=state_factory,
            teardown=teardown,
            harness=harness,
            methods=methods,
            include_private=include_private,
        )
        for name, func in discovered:
            _add_scannable(name, func)
        for name in target_skipped:
            _add_skipped(name)

    for name, func in list(scannable.items()):
        if _infer_strategies(func) is None:
            scannable.pop(name, None)
            _add_skipped(name, func)

    return list(scannable.items()), list(skipped.keys()), list(discovered_callables.items())
