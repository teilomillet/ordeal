from __future__ import annotations
# ruff: noqa
def chaos_for(
    module: str | ModuleType,
    *,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    invariants: list[Invariant] | dict[str, Invariant] | None = None,
    faults: list[Fault] | None = None,
    max_examples: int = 50,
    stateful_step_count: int = 30,
    rule_timeout: float = 30.0,
) -> type:
    """Auto-generate a ChaosTest from a module's public API.

    Each public function becomes a ``@rule``.  The nemesis toggles
    *faults*.  After each step, *invariants* are checked on every
    return value.

    Zero-config — discovers everything automatically::

        TestScoring = chaos_for("myapp.scoring")
        # Scans code for subprocess/file/network calls → generates faults
        # Mines each function with random inputs → generates invariants

    With explicit overrides::

        TestScoring = chaos_for(
            "myapp.scoring",
            faults=[timing.timeout("myapp.db.query")],
            invariants={"compute": bounded(0, 1)},
        )

    Pass ``faults=[]`` or ``invariants=[]`` to disable auto-discovery.

    Returns a pytest-discoverable ``TestCase`` class.

    Args:
        module: Module path or object.
        fixtures: Strategy overrides for parameter names.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_state_factories: Optional per-class state factories for methods that take
            a runtime ``state`` parameter.
        object_teardowns: Optional per-class teardown hooks run during ChaosTest teardown.
        object_harnesses: Per-class harness mode (``fresh`` or ``stateful``).
        invariants: ``None`` = auto-mine, list = global, dict = per-function.
        faults: ``None`` = auto-infer from code, list = explicit.
        max_examples: Hypothesis examples.
        stateful_step_count: Max rules per test case.
        rule_timeout: Per-rule timeout in seconds (default 30, 0 to disable).
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__

    # Auto-discover faults from code analysis when not provided
    if faults is None:
        fault_list = _infer_faults(
            mod,
            mod_name,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
    else:
        fault_list = list(faults)

    # Auto-discover invariants from mine() when not provided
    if invariants is None:
        invariant_map, global_invs = _infer_invariants(
            mod,
            fixtures,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
    elif isinstance(invariants, dict):
        invariant_map: dict[str, list[Invariant]] = {
            k: [v] if isinstance(v, Invariant) else list(v) for k, v in invariants.items()
        }
        global_invs: list[Invariant] = []
    else:
        invariant_map = {}
        global_invs = list(invariants)

    # Collect rule methods
    rules_dict: dict[str, Any] = {}
    initialize_dict: dict[str, Any] = {}
    teardown_hooks: list[tuple[str, Any]] = []
    seen_stateful_owners: set[str] = set()
    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            continue
        # Per-function invariants override global; if neither, empty list
        func_invs = invariant_map.get(name, global_invs)
        if (
            getattr(func, "__ordeal_kind__", None) == "instance"
            and getattr(func, "__ordeal_harness__", "fresh") == "stateful"
            and getattr(func, "__ordeal_factory__", None) is not None
        ):
            owner = getattr(func, "__ordeal_owner__", None)
            method_name = str(getattr(func, "__ordeal_method_name__", name.rsplit(".", 1)[-1]))
            owner_key = re.sub(
                r"[^0-9a-zA-Z_]",
                "_",
                getattr(owner, "__qualname__", getattr(owner, "__name__", "owner")),
            ).lower()
            owner_attr = f"_ordeal_owner_{owner_key}"
            if owner_attr not in seen_stateful_owners:
                init_method = _make_stateful_initialize_method(
                    owner_attr,
                    factory=getattr(func, "__ordeal_factory__"),
                    setup=getattr(func, "__ordeal_setup__", None),
                    scenarios=tuple(getattr(func, "__ordeal_scenarios__", ()) or ()),
                )
                initialize_dict[init_method.__name__] = init_method
                teardown_hook = getattr(func, "__ordeal_teardown__", None)
                if teardown_hook is not None:
                    teardown_hooks.append((owner_attr, teardown_hook))
                seen_stateful_owners.add(owner_attr)
            method = _make_stateful_rule_method(
                name,
                func,
                strategies,
                func_invs,
                owner_attr=owner_attr,
                method_name=method_name,
            )
        else:
            method = _make_rule_method(name, func, strategies, func_invs)
        rules_dict[method.__name__] = method

    if not rules_dict:
        raise ValueError(
            f"No testable functions found in {mod_name}. "
            f"Ensure functions have type hints or provide fixtures."
        )

    # Build class
    namespace: dict[str, Any] = {
        "faults": fault_list,
        "rule_timeout": rule_timeout,
        **initialize_dict,
        **rules_dict,
    }
    if teardown_hooks:
        namespace["teardown"] = _make_stateful_teardown_method(teardown_hooks)

    class_name = f"AutoChaos_{mod_name.replace('.', '_')}"
    AutoChaos = type(class_name, (ChaosTest,), namespace)

    TestCase = AutoChaos.TestCase
    TestCase.settings = settings(
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )
    return TestCase
def _make_rule_method(
    func_name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    invariants: list[Invariant],
) -> Any:
    """Create a @rule method that calls func and checks invariants on the result."""
    safe_name = func_name.replace(".", "_")

    @rule(**strategies)
    def method(self: Any, **kwargs: Any) -> None:
        result = _call_sync(func, **kwargs)
        if result is not None:
            for inv in invariants:
                try:
                    inv(result)
                except TypeError:
                    pass  # invariant doesn't apply to this return type

    method.__name__ = f"call_{safe_name}"
    method.__qualname__ = f"AutoChaos.call_{safe_name}"
    return method
def _make_stateful_initialize_method(
    owner_attr: str,
    *,
    factory: Any,
    setup: Any | None = None,
    scenarios: Sequence[Any] | None = None,
) -> Any:
    """Create an ``@initialize`` hook that persists one owner instance."""

    @initialize()
    def method(self: Any) -> None:
        instance = _call_sync(factory)
        instance = _apply_instance_hook(instance, setup)
        instance = _apply_instance_hooks(instance, scenarios)
        setattr(self, owner_attr, instance)

    method.__name__ = f"setup_{owner_attr}"
    method.__qualname__ = f"AutoChaos.setup_{owner_attr}"
    return method
def _make_stateful_rule_method(
    func_name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    invariants: list[Invariant],
    *,
    owner_attr: str,
    method_name: str,
) -> Any:
    """Create a rule that reuses one persistent object instance."""
    safe_name = func_name.replace(".", "_")

    @rule(**strategies)
    def method(self: Any, **kwargs: Any) -> None:
        instance = getattr(self, owner_attr, None)
        if instance is None:
            raise RuntimeError(f"stateful harness did not initialize {owner_attr}")
        probe_cleanup, probe_context = _instance_probe_result(
            getattr(func, "__ordeal_instance_probe__", None),
            instance=instance,
            owner=getattr(func, "__ordeal_owner__", None),
            method_name=method_name,
        )
        before_state = _snapshot_instance_state(instance)
        target = _unwrap(func)
        call_args, call_kwargs = _prepare_bound_method_call(
            target,
            (),
            kwargs,
            instance=instance,
            state_factory=getattr(func, "__ordeal_state_factory__", None),
            state_param=getattr(func, "__ordeal_state_param__", None),
        )
        result = _call_sync(getattr(instance, method_name), *call_args, **call_kwargs)
        func.__ordeal_last_call_context__ = {
            "instance": instance,
            "before_state": before_state,
            "after_state": _snapshot_instance_state(instance),
            "kwargs": dict(call_kwargs),
            "args": tuple(call_args),
            "method_name": method_name,
            "owner": getattr(func, "__ordeal_owner__", None),
            "harness": "stateful",
            "lifecycle_phase": getattr(func, "__ordeal_lifecycle_phase__", None),
            **probe_context,
        }
        if probe_cleanup is not None:
            probe_cleanup()
        if result is not None:
            for inv in invariants:
                try:
                    inv(result)
                except TypeError:
                    pass

    method.__name__ = f"call_{safe_name}"
    method.__qualname__ = f"AutoChaos.call_{safe_name}"
    return method
def _make_stateful_teardown_method(
    owner_hooks: Sequence[tuple[str, Any]],
) -> Any:
    """Create a teardown that attempts every configured owner cleanup hook."""

    def teardown(self: Any) -> None:
        errors: list[str] = []
        for owner_attr, hook in owner_hooks:
            instance = getattr(self, owner_attr, None)
            if instance is None or hook is None:
                continue
            try:
                _call_sync(hook, instance)
            except Exception as exc:
                errors.append(f"{owner_attr}: {type(exc).__name__}: {exc}")
        ChaosTest.teardown(self)
        if errors:
            raise AssertionError("; ".join(errors))

    teardown.__name__ = "teardown"
    teardown.__qualname__ = "AutoChaos.teardown"
    return teardown
