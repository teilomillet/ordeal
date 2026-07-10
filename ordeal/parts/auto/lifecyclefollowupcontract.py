from __future__ import annotations
# ruff: noqa
def lifecycle_followup_contract(
    *,
    kwargs: dict[str, Any],
    phase: str,
    followup_phases: Sequence[str],
    fault: str = "raise_setup_hook",
    handler_name: str | None = None,
    contract_name: str = "lifecycle_followup",
) -> ContractCheck:
    """Build a lifecycle probe that requires follow-up phases after a fault."""

    def predicate(
        value: Any,
        *,
        lifecycle_probe: Mapping[str, Any] | None = None,
        teardown_called: bool | None = None,
        **_extra: Any,
    ) -> bool:
        del value
        probe = dict(lifecycle_probe or {})
        attempts = list(probe.get("attempts", []))
        followup_handlers = dict(probe.get("followup_handlers", {}))
        if not followup_handlers and teardown_called:
            return True
        saw_followup = False
        for followup_phase, names in followup_handlers.items():
            if followup_phase == "teardown" and teardown_called:
                saw_followup = True
                continue
            if not names:
                continue
            if any(name in attempts for name in names):
                saw_followup = True
                continue
            return False
        return saw_followup

    phases = [str(item) for item in followup_phases if str(item).strip()]
    summary = (
        f"{', '.join(phases)} handlers should still be attempted after {phase} fails"
        if phases
        else f"follow-up lifecycle handlers should still be attempted after {phase} fails"
    )
    return ContractCheck(
        name=contract_name,
        kwargs=dict(kwargs),
        predicate=predicate,
        summary=summary,
        metadata={
            "kind": "lifecycle",
            "phase": phase,
            "fault": fault,
            "handler_name": handler_name,
            "followup_phases": phases,
            "runtime_faults": (
                [fault]
                if fault in {"raise_setup_hook", "raise_teardown_hook", "cancel_rollout"}
                else []
            ),
        },
    )
def builtin_contract_check(
    name: str,
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
    protected_keys: Sequence[str] | None = None,
    env_param: str | None = None,
    phase: str | None = None,
    followup_phases: Sequence[str] | None = None,
    fault: str = "raise",
    handler_name: str | None = None,
) -> ContractCheck:
    """Build one built-in semantic contract probe by *name*."""
    match name:
        case "cleanup_attempts_all":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="cleanup",
                fault="raise",
                handler_name=handler_name,
            )
        case "teardown_attempts_all":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="teardown",
                fault="raise",
                handler_name=handler_name,
            )
        case "setup_failure_triggers_teardown":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="setup",
                followup_phases=["teardown"],
                fault="raise",
                handler_name=handler_name,
            )
        case "rollout_cancellation_triggers_cleanup":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="rollout",
                followup_phases=["cleanup", "teardown"],
                fault="cancel",
                handler_name=handler_name,
            )
        case "shell_safe":
            return shell_safe_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "shell_injection":
            return shell_injection_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "quoted_paths":
            return quoted_paths_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "command_arg_stability":
            return command_arg_stability_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "protected_env_keys":
            return protected_env_keys_contract(
                kwargs=kwargs,
                protected_keys=list(protected_keys or []),
                env_param=env_param,
            )
        case "json_roundtrip":
            return json_roundtrip_contract(kwargs=kwargs)
        case "http_shape":
            return http_shape_contract(kwargs=kwargs)
        case "subprocess_argv":
            return subprocess_argv_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "all_cleanup_handlers_attempted":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="cleanup",
                fault="raise_cleanup_handler",
                handler_name=handler_name,
                contract_name="all_cleanup_handlers_attempted",
            )
        case "all_teardown_handlers_attempted":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="teardown",
                fault="raise_teardown_handler",
                handler_name=handler_name,
                contract_name="all_teardown_handlers_attempted",
            )
        case "cleanup_after_setup_failure":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="setup",
                followup_phases=list(followup_phases or ("cleanup", "teardown")),
                fault="raise_setup_hook",
                handler_name=handler_name,
                contract_name="cleanup_after_setup_failure",
            )
        case "cleanup_after_cancellation":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="rollout",
                followup_phases=list(followup_phases or ("cleanup", "teardown")),
                fault="cancel_rollout",
                handler_name=handler_name,
                contract_name="cleanup_after_cancellation",
            )
        case "lifecycle_attempts_all":
            resolved_phase = str(phase or "cleanup")
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase=resolved_phase,
                fault=fault,
                handler_name=handler_name,
            )
        case "lifecycle_followup":
            resolved_phase = str(phase or "rollout")
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase=resolved_phase,
                followup_phases=list(followup_phases or ("cleanup", "teardown")),
                fault=fault,
                handler_name=handler_name,
            )
        case _:
            raise ValueError(f"unknown built-in contract check: {name}")
def _auto_contract_checks(
    func: Any,
    seed_examples: Sequence[SeedExample],
    *,
    auto_contracts: Sequence[str] | None,
    ignore_contracts: Sequence[str] | None = None,
    shell_injection_check: bool = False,
    security_focus: bool = False,
) -> tuple[list[ContractCheck], list[str]]:
    """Infer built-in sink-aware contract probes for *func* from source and seeds."""
    sink_categories = _infer_sink_categories(func, security_focus=security_focus)

    enabled = set(_expand_contract_names_ordered(auto_contracts or _DEFAULT_AUTO_CONTRACTS))
    if shell_injection_check:
        enabled.add("shell_injection")
    ignored = _expand_contract_names(ignore_contracts)
    probe_kwargs = dict(seed_examples[0].kwargs) if seed_examples else _contract_seed_kwargs(func)
    tracked_params = list(probe_kwargs)
    env_param = next(
        (name for name, value in probe_kwargs.items() if isinstance(value, Mapping)),
        None,
    )
    protected_keys = [
        key
        for key in ("PATH", "HOME", "PWD", "TMPDIR")
        if env_param is not None
        and isinstance(probe_kwargs.get(env_param), Mapping)
        and key in probe_kwargs.get(env_param, {})
    ]

    contract_names: list[str] = []
    if {"shell", "subprocess"} & set(sink_categories):
        contract_names.extend(["shell_safe", "command_arg_stability", "subprocess_argv"])
        if shell_injection_check:
            contract_names.append("shell_injection")
    if "path" in sink_categories:
        contract_names.append("quoted_paths")
    if "env" in sink_categories and protected_keys:
        contract_names.append("protected_env_keys")
    if "json_tool_call" in sink_categories:
        contract_names.append("json_roundtrip")
    if "http" in sink_categories:
        contract_names.append("http_shape")

    lifecycle_phase = getattr(func, "__ordeal_lifecycle_phase__", None)
    if (
        lifecycle_phase
        and getattr(func, "__ordeal_kind__", None) == "instance"
        and getattr(func, "__ordeal_factory__", None) is not None
    ):
        owner = getattr(func, "__ordeal_owner__", None)
        method_name = str(getattr(func, "__ordeal_method_name__", ""))
        handlers = _discover_lifecycle_handlers(owner, lifecycle_phase)
        if method_name in handlers and len(handlers) > 1:
            handlers = [name for name in handlers if name != method_name]
        if lifecycle_phase == "cleanup" and len(handlers) >= 1:
            contract_names.append("cleanup_attempts_all")
        if lifecycle_phase == "stop" and len(handlers) >= 1:
            contract_names.append("lifecycle_attempts_all")
        if lifecycle_phase == "teardown" and len(handlers) >= 1:
            contract_names.append("teardown_attempts_all")
        if lifecycle_phase in {"setup", "rollout"}:
            followup = [
                phase
                for phase in ("cleanup", "teardown", "stop")
                if _discover_lifecycle_handlers(owner, phase)
                or (phase == "teardown" and getattr(func, "__ordeal_teardown__", None) is not None)
            ]
            if followup:
                if lifecycle_phase == "setup":
                    contract_names.append("setup_failure_triggers_teardown")
                else:
                    contract_names.append("rollout_cancellation_triggers_cleanup")

    checks: list[ContractCheck] = []
    for name in dict.fromkeys(contract_names):
        if name not in enabled or name in ignored or not probe_kwargs:
            continue
        followup_phases: list[str] | None = None
        phase = None
        if name in {"lifecycle_attempts_all", "cleanup_attempts_all", "teardown_attempts_all"}:
            phase = str(lifecycle_phase or "cleanup")
            if name == "cleanup_attempts_all":
                phase = "cleanup"
            elif name == "teardown_attempts_all":
                phase = "teardown"
        elif name in {
            "lifecycle_followup",
            "setup_failure_triggers_teardown",
            "rollout_cancellation_triggers_cleanup",
        }:
            phase = str(lifecycle_phase or "rollout")
            if name == "setup_failure_triggers_teardown":
                phase = "setup"
                followup_phases = ["teardown"]
            elif name == "rollout_cancellation_triggers_cleanup":
                phase = "rollout"
                followup_phases = ["cleanup", "teardown"]
            else:
                followup_phases = [
                    phase_name
                    for phase_name in ("cleanup", "teardown", "stop")
                    if _discover_lifecycle_handlers(
                        getattr(func, "__ordeal_owner__", None), phase_name
                    )
                ]
        checks.append(
            builtin_contract_check(
                name,
                kwargs=probe_kwargs,
                tracked_params=tracked_params,
                protected_keys=protected_keys,
                env_param=env_param,
                phase=phase,
                followup_phases=followup_phases,
            )
        )
    return checks, sink_categories
def _get_public_functions(
    mod: ModuleType,
    *,
    include_private: bool = False,
    preserve_wrappers: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> list[tuple[str, Any]]:
    """Return (name, callable) pairs for testable callables.

    By default this includes public module functions and public class
    methods. Instance methods are wrapped only when a registered or
    explicit object factory is available; otherwise they are returned as
    placeholder callables that report a missing object factory.

    Discovery is based on the module's own ``__dict__`` and class
    ``__dict__`` entries for normal modules. Package targets also include
    public callable exports visible via ``dir()`` when they resolve back
    into the same package namespace, so lazy-exported APIs such as
    ``ordeal.scan_module`` remain discoverable from the package root.

    Decorated functions (``@ray.remote``, ``@functools.wraps``, etc.)
    are auto-unwrapped so that ``mine()``, ``fuzz()``, and ``scan_module()``
    can inspect signatures and call the real function. Set
    ``preserve_wrappers=True`` when the wrapper itself is the behavior under
    evaluation, as in a migration comparison.
    """
    merged_factories = dict(_REGISTERED_OBJECT_FACTORIES)
    if object_factories:
        merged_factories.update(object_factories)
    merged_setups = dict(_REGISTERED_OBJECT_SETUPS)
    if object_setups:
        merged_setups.update(object_setups)
    merged_scenarios = dict(_REGISTERED_OBJECT_SCENARIOS)
    if object_scenarios:
        merged_scenarios.update(object_scenarios)
    merged_state_factories = dict(_REGISTERED_OBJECT_STATE_FACTORIES)
    if object_state_factories:
        merged_state_factories.update(object_state_factories)
    merged_teardowns = dict(_REGISTERED_OBJECT_TEARDOWNS)
    if object_teardowns:
        merged_teardowns.update(object_teardowns)
    merged_harnesses = dict(_REGISTERED_OBJECT_HARNESSES)
    if object_harnesses:
        merged_harnesses.update(object_harnesses)

    results: list[tuple[str, Any]] = []
    package_prefix = f"{mod.__name__}."
    is_package = bool(getattr(mod, "__path__", None))
    items: dict[str, Any] = dict(sorted(vars(mod).items()))
    if is_package:
        for name in sorted(dir(mod)):
            if name.startswith("__"):
                continue
            if name.startswith("_") and not include_private:
                continue
            if name in items:
                continue
            try:
                obj = getattr(mod, name)
            except BaseException as exc:
                if _is_fatal_discovery_exception(exc):
                    raise
                continue
            obj_mod = getattr(obj, "__module__", None)
            if obj_mod == mod.__name__ or (
                isinstance(obj_mod, str) and obj_mod.startswith(package_prefix)
            ):
                items[name] = obj

    for name, obj in items.items():
        if name.startswith("__"):
            continue
        if name.startswith("_") and not include_private:
            continue
        if callable(obj) and not isinstance(obj, type):
            obj_mod = getattr(obj, "__module__", None)
            if (
                obj_mod
                and obj_mod != mod.__name__
                and not (
                    is_package and isinstance(obj_mod, str) and obj_mod.startswith(package_prefix)
                )
            ):
                continue
            target = obj if preserve_wrappers else _unwrap(obj)
            if inspect.iscoroutinefunction(target):
                results.append(
                    (
                        name,
                        _make_sync_callable(target, qualname=name, keep_wrapped=True),
                    )
                )
            else:
                results.append((name, target))
            continue
        obj_mod = getattr(obj, "__module__", None)
        if not inspect.isclass(obj) or obj_mod != mod.__name__:
            continue

        for meth_name, static_attr in sorted(vars(obj).items()):
            if meth_name.startswith("__"):
                continue
            if meth_name.startswith("_") and not include_private:
                continue
            if isinstance(static_attr, property):
                continue
            if not (
                isinstance(static_attr, (staticmethod, classmethod))
                or inspect.isfunction(static_attr)
            ):
                continue
            results.append(
                _resolve_method_callable(
                    obj,
                    meth_name,
                    static_attr,
                    object_factories=merged_factories,
                    object_setups=merged_setups,
                    object_scenarios=merged_scenarios,
                    object_state_factories=merged_state_factories,
                    object_teardowns=merged_teardowns,
                    object_harnesses=merged_harnesses,
                )
            )
    return results
_FIXTURE_REGISTRY_MODULES: set[str] = set()
def _load_fixture_registry_path(path: Path) -> str | None:
    """Import one fixture registry file and return a warning on failure."""
    resolved = path.resolve()
    key = str(resolved)
    if key in _FIXTURE_REGISTRY_MODULES:
        return None
    spec = importlib.util.spec_from_file_location(
        f"_ordeal_fixture_registry_{len(_FIXTURE_REGISTRY_MODULES)}",
        resolved,
    )
    if spec is None or spec.loader is None:
        return f"could not load fixture registry: {resolved}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _FIXTURE_REGISTRY_MODULES.add(key)
    return None
def load_fixture_registry_modules(modules: list[str]) -> list[str]:
    """Import explicit registry modules so ``register_fixture()`` takes effect."""
    warnings: list[str] = []
    for module_name in modules:
        if module_name in _FIXTURE_REGISTRY_MODULES:
            continue
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            warnings.append(f"fixture registry load failed for {module_name}: {exc}")
            continue
        _FIXTURE_REGISTRY_MODULES.add(module_name)
    return warnings
def load_project_fixture_registries(
    *,
    root: Path | None = None,
    extra_modules: list[str] | None = None,
) -> list[str]:
    """Import local registries so ``register_fixture()`` takes effect."""
    base = (root or Path.cwd()).resolve()
    warnings: list[str] = []
    candidates = [
        base / "conftest.py",
        base / "tests" / "conftest.py",
        base / "test" / "conftest.py",
        base / "src" / "tests" / "conftest.py",
    ]

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        try:
            warning = _load_fixture_registry_path(resolved)
            if warning:
                warnings.append(warning)
        except Exception as exc:
            warnings.append(f"fixture registry load failed for {resolved}: {exc}")
    if extra_modules:
        warnings.extend(load_fixture_registry_modules(list(extra_modules)))
    return warnings
