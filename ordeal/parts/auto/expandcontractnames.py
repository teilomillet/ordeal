from __future__ import annotations
# ruff: noqa
def _expand_contract_names(names: Sequence[str] | None) -> set[str]:
    """Expand contract aliases like ``transport`` into concrete built-ins."""
    expanded: set[str] = set()
    for raw in names or ():
        name = str(raw).strip()
        if not name:
            continue
        expanded.update(_CONTRACT_GROUP_ALIASES.get(name, (name,)))
    return expanded
def _expand_contract_names_ordered(names: Sequence[str] | None) -> list[str]:
    """Expand contract aliases while preserving the caller's order."""
    expanded: list[str] = []
    seen: set[str] = set()
    for raw in names or ():
        name = str(raw).strip()
        if not name:
            continue
        for concrete in _CONTRACT_GROUP_ALIASES.get(name, (name,)):
            if concrete in seen:
                continue
            seen.add(concrete)
            expanded.append(concrete)
    return expanded
def _traceback_path(exc: BaseException) -> list[str]:
    """Return a short traceback path for proof bundles."""
    frames: list[str] = []
    for frame in traceback.extract_tb(exc.__traceback__):
        frames.append(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}")
    return frames[-6:]
def _exception_replay_signature(
    exc: BaseException,
) -> tuple[type[BaseException], str, tuple[str, int, str] | None]:
    """Return the stable failure identity used by immediate scan replay."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        terminal = None
    else:
        frame = frames[-1]
        terminal = (str(Path(frame.filename).resolve()), frame.lineno, frame.name)
    return type(exc), str(exc), terminal
def _json_ready_proof(value: Any) -> Any:
    """Convert proof-bundle payloads into JSON-friendly structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, Mapping):
        return {str(key): _json_ready_proof(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_ready_proof(item) for item in value]
    return repr(value)
def _sink_likely_impact(sink_categories: Sequence[str], exc: BaseException) -> str:
    """Summarize likely impact for a failing sink-aware witness."""
    if "shell" in sink_categories or "subprocess" in sink_categories:
        return "command construction may break valid shell or subprocess execution"
    if "filesystem_write" in sink_categories:
        return "filesystem writes may escape the intended root or clobber generated files"
    if "import" in sink_categories:
        return "import resolution may load attacker-chosen modules, hooks, or classes"
    if "deserialization" in sink_categories:
        return "artifact or checkpoint parsing may trust unsafe serialized payloads"
    if "ipc" in sink_categories:
        return "shared-memory or IPC payload handling may trust forged cross-process data"
    if "symlink" in sink_categories:
        return "path resolution may follow symlinks across trust boundaries"
    if "path" in sink_categories:
        return "path quoting or normalization may corrupt filesystem operations"
    if "env" in sink_categories:
        return "environment shaping may overwrite or drop protected keys"
    if "json_tool_call" in sink_categories:
        return "JSON or tool-call normalization may reject valid payloads"
    if "http" in sink_categories:
        return "HTTP header/body shaping may break valid request construction"
    if "sql" in sink_categories:
        return "query construction may reject valid SQL-shaped inputs"
    if isinstance(exc, (TypeError, ValueError)):
        return "valid-looking inputs may still hit an unchecked contract boundary"
    return "replayable failure on a contract-fitting input"
def register_fixture(name: str, strategy: st.SearchStrategy[Any]) -> None:
    """Register a named fixture strategy for auto-scan.

    Registered strategies have highest priority after explicit fixtures.
    Call this in ``conftest.py`` to teach ordeal about project-specific
    types::

        from ordeal.auto import register_fixture
        import hypothesis.strategies as st

        register_fixture("model", st.builds(make_mock_model))
        register_fixture("direction", st.builds(make_unit_vector))

    After registration, ``scan_module`` and ``fuzz`` will auto-resolve
    parameters named ``model`` or ``direction`` without explicit fixtures.
    """
    _REGISTERED_STRATEGIES[name] = strategy
def register_object_factory(name: str, factory: Any) -> None:
    """Register an object factory for class-method targets.

    Use this for methods that need a prebuilt instance or collaborators.
    The factory can be sync or async and may return the instance directly.
    """
    _REGISTERED_OBJECT_FACTORIES[name] = factory
def register_object_setup(name: str, setup: Any) -> None:
    """Register a per-instance setup hook for class-method targets."""
    _REGISTERED_OBJECT_SETUPS[name] = setup
def register_object_scenario(name: str, scenario: Any) -> None:
    """Register one or more collaborator scenario hooks for class-method targets."""
    _REGISTERED_OBJECT_SCENARIOS[name] = scenario
def register_object_state_factory(name: str, state_factory: Any) -> None:
    """Register a per-method state factory for class-method targets."""
    _REGISTERED_OBJECT_STATE_FACTORIES[name] = state_factory
def register_object_teardown(name: str, teardown: Any) -> None:
    """Register a per-instance teardown hook for class-method targets."""
    _REGISTERED_OBJECT_TEARDOWNS[name] = teardown
def register_object_harness(name: str, harness: str) -> None:
    """Register how ordeal should exercise a class target.

    Valid values are ``"fresh"`` and ``"stateful"``.
    """
    resolved = str(harness).strip().lower() or "fresh"
    if resolved not in {"fresh", "stateful"}:
        raise ValueError("object harness must be 'fresh' or 'stateful'")
    _REGISTERED_OBJECT_HARNESSES[name] = resolved
def _strategy_for_name(name: str) -> st.SearchStrategy[Any] | None:
    """Try to infer a strategy from the parameter name alone."""
    # 1. User-registered (project-specific, highest priority)
    if name in _REGISTERED_STRATEGIES:
        return _REGISTERED_STRATEGIES[name]
    # 2. Built-in common names
    if name in COMMON_NAME_STRATEGIES:
        return COMMON_NAME_STRATEGIES[name]
    # 3. Suffix patterns
    for suffix, strategy in _SUFFIX_STRATEGIES.items():
        if name.endswith(suffix):
            return strategy
    return None
# ============================================================================
# Helpers
# ============================================================================


def _resolve_module(module: str | ModuleType) -> ModuleType:
    if isinstance(module, str):
        return importlib.import_module(module)
    return module
def _is_fatal_discovery_exception(exc: BaseException) -> bool:
    """Return whether *exc* should abort discovery immediately."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError))
def _unwrap(func: Any) -> Any:
    """Unwrap decorated functions to reach the original callable.

    Handles Ray ``@ray.remote`` (`._function``), ``functools.wraps``
    (``__wrapped__`` chains), and Celery-style patterns.
    """
    import inspect

    func = getattr(func, "_function", func)
    if getattr(func, "__ordeal_keep_wrapped__", False):
        return func
    try:
        func = inspect.unwrap(
            func,
            stop=lambda wrapped: getattr(wrapped, "__ordeal_keep_wrapped__", False),
        )
    except (ValueError, TypeError):
        pass
    return func
def _resolve_awaitable(value: Any) -> Any:
    """Resolve an awaitable value without forcing callers to use async APIs."""
    if not inspect.isawaitable(value):
        return value
    try:
        return asyncio.run(value)
    except RuntimeError as exc:
        if "asyncio.run()" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(value)
        finally:
            loop.close()
def _call_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call *func* and synchronously resolve any returned awaitable."""
    return _resolve_awaitable(func(*args, **kwargs))
def _signature_without_first_context(
    func: Any,
    *,
    omit_names: Sequence[str] = (),
) -> inspect.Signature:
    """Return a callable signature with contextual parameters removed."""
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    if params and params[0].name in {"self", "cls"}:
        params = params[1:]
    omitted = set(omit_names)
    if omitted:
        params = [param for param in params if param.name not in omitted]
    return sig.replace(parameters=params)
def _object_hook_candidates(owner: type) -> list[str]:
    """Return the registry keys that may refer to *owner*."""
    candidates = [
        f"{owner.__module__}:{owner.__qualname__}",
        f"{owner.__module__}.{owner.__qualname__}",
        f"{owner.__module__}:{owner.__name__}",
        f"{owner.__module__}.{owner.__name__}",
        owner.__qualname__,
        owner.__name__,
    ]
    return list(dict.fromkeys(candidates))
def _resolve_object_hook(owner: type, hooks: dict[str, Any] | None) -> Any | None:
    """Resolve a registered object hook for *owner* from several key styles."""
    if not hooks:
        return None
    for candidate in _object_hook_candidates(owner):
        if candidate in hooks:
            return hooks[candidate]
    return None
def _scenario_path_target(instance: Any, path: str) -> tuple[Any, str]:
    """Resolve a dotted scenario path against *instance*."""
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise ValueError("scenario path must not be empty")
    target = instance
    for part in parts[:-1]:
        target = getattr(target, part)
    return target, parts[-1]
def _scenario_exception_from_spec(spec: Any) -> BaseException:
    """Build a concrete exception object from a scenario spec."""
    if isinstance(spec, BaseException):
        return spec
    if isinstance(spec, type) and issubclass(spec, BaseException):
        return spec()
    if isinstance(spec, Mapping):
        exc_type = (
            str(
                spec.get("type") or spec.get("exception") or spec.get("name") or "RuntimeError"
            ).strip()
            or "RuntimeError"
        )
        message = spec.get("message", spec.get("value", ""))
    else:
        text = str(spec).strip()
        if not text:
            return RuntimeError("injected collaborator failure")
        if ":" in text:
            exc_type, message = text.split(":", 1)
        else:
            return RuntimeError(text)
    exc_cls = getattr(builtins, str(exc_type).strip(), RuntimeError)
    if not isinstance(exc_cls, type) or not issubclass(exc_cls, BaseException):
        exc_cls = RuntimeError
    try:
        return exc_cls(str(message).strip()) if str(message).strip() else exc_cls()
    except Exception:
        return RuntimeError(str(spec))
def _scenario_stub_wrapper(
    original: Any,
    *,
    return_value: Any = None,
    error: BaseException | None = None,
) -> Any:
    """Wrap one collaborator method so it returns or raises a fixed outcome."""
    is_async = inspect.iscoroutinefunction(original)

    if is_async:

        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if error is not None:
                raise error
            return return_value

    else:

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if error is not None:
                raise error
            return return_value

    return wrapper
def _make_pack_method_stub(original: Any, behavior: Callable[..., Any]) -> Any:
    """Wrap a collaborator method while preserving its async/sync shape."""
    is_async = inspect.iscoroutinefunction(getattr(original, "__func__", original))

    if is_async:

        @functools.wraps(original)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            return behavior(*args, **kwargs)

    else:

        @functools.wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            return behavior(*args, **kwargs)

    return wrapped
def _apply_collaborator_pack(
    instance: Any,
    *,
    attr_names: Sequence[str],
    method_behaviors: Mapping[str, Callable[..., Any]],
) -> Any:
    """Attach a built-in fake collaborator to the first matching instance attr."""
    for attr_name in attr_names:
        collaborator = getattr(instance, attr_name, None)
        if collaborator is None:
            continue
        fake = SimpleNamespace()
        for method_name, behavior in method_behaviors.items():
            original = getattr(collaborator, method_name, None)
            if callable(original):
                setattr(fake, method_name, _make_pack_method_stub(original, behavior))
        if fake.__dict__:
            setattr(instance, attr_name, fake)
    return instance
def _subprocess_response_stub(*args: Any, **kwargs: Any) -> SimpleNamespace:
    """Return a stable subprocess-like response object."""
    return SimpleNamespace(
        args=list(args),
        kwargs=dict(kwargs),
        returncode=0,
        stdout="",
        stderr="",
    )
def _http_response_stub(*args: Any, **kwargs: Any) -> SimpleNamespace:
    """Return a stable HTTP-like response object."""
    return SimpleNamespace(
        args=list(args),
        kwargs=dict(kwargs),
        status_code=200,
        headers={},
        text="",
        content=b"",
        json=lambda: {},
    )
def _sequence_arg(values: Sequence[Any]) -> Sequence[Any] | None:
    """Return the first batch-like argument from *values*."""
    for value in values:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return value
    return None
def _model_vector(width: int = 4) -> list[float]:
    """Return one stable embedding-like vector."""
    return [0.0 for _ in range(width)]
def _model_prediction_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable prediction payload shaped like the input batch."""
    batch = _sequence_arg([*args, *kwargs.values()])
    if batch is None:
        return 0.5
    return [0.5 for _ in range(max(1, len(batch)))]
def _model_probability_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable probability payload shaped like the input batch."""
    batch = _sequence_arg([*args, *kwargs.values()])
    row = [0.5, 0.5]
    if batch is None:
        return row
    return [list(row) for _ in range(max(1, len(batch)))]
def _embedding_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable embedding or batch of embeddings."""
    batch = _sequence_arg([*args, *kwargs.values()])
    if batch is None:
        return _model_vector()
    return [_model_vector() for _ in range(max(1, len(batch)))]
def _feature_payload_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable feature row or batch of feature rows."""
    batch = _sequence_arg([*args, *kwargs.values()])
    row = {"feature_0": 0.5, "feature_1": 1.0}
    if batch is None:
        return dict(row)
    return [dict(row) for _ in range(max(1, len(batch)))]
def _apply_state_store_pack(instance: Any) -> Any:
    """Attach a shared in-memory state store to matching instance collaborators."""
    store: dict[str, Any] = {}

    def _make_behavior(method_name: str) -> Callable[..., Any]:
        def behavior(*args: Any, **kwargs: Any) -> Any:
            if method_name == "get":
                key = args[0] if args else kwargs.get("key")
                default = args[1] if len(args) > 1 else kwargs.get("default")
                return store.get(key, default)
            if method_name in {"set", "put", "save"}:
                if len(args) >= 2:
                    key, value = args[0], args[1]
                else:
                    key = kwargs.get("key", kwargs.get("name", "value"))
                    value = kwargs.get("value", args[0] if args else None)
                store[str(key)] = _clone_scenario_value(value)
                return value
            if method_name == "load":
                return dict(store)
            if method_name == "delete":
                key = args[0] if args else kwargs.get("key")
                if key is not None:
                    store.pop(str(key), None)
                return None
            if method_name == "clear":
                store.clear()
                return None
            return dict(store)

        return behavior

    return _apply_collaborator_pack(
        instance,
        attr_names=("state_store", "store", "cache", "session_state"),
        method_behaviors={
            "get": _make_behavior("get"),
            "set": _make_behavior("set"),
            "put": _make_behavior("put"),
            "save": _make_behavior("save"),
            "load": _make_behavior("load"),
            "delete": _make_behavior("delete"),
            "clear": _make_behavior("clear"),
        },
    )
def _apply_subprocess_pack(instance: Any) -> Any:
    """Attach a stable subprocess/runner collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "subprocess",
            "runner",
            "command_runner",
            "process_runner",
            "executor",
        ),
        method_behaviors={
            "run": _subprocess_response_stub,
            "execute_command": _subprocess_response_stub,
            "check_output": lambda *args, **kwargs: "",
            "popen": _subprocess_response_stub,
            "call": lambda *args, **kwargs: 0,
        },
    )
def _apply_sandbox_pack(instance: Any) -> Any:
    """Attach a stable sandbox-client collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=("sandbox_client", "sandbox", "client"),
        method_behaviors={
            "execute_command": _subprocess_response_stub,
            "run": _subprocess_response_stub,
            "upload_content": lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                uploaded=True,
            ),
            "download_content": lambda *args, **kwargs: b"",
            "fetch_content": lambda *args, **kwargs: b"",
            "list_files": lambda *args, **kwargs: [],
        },
    )
def _apply_upload_download_pack(instance: Any) -> Any:
    """Attach a stable upload/download collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "upload_download",
            "storage_client",
            "artifact_client",
            "uploader",
            "downloader",
            "client",
        ),
        method_behaviors={
            "upload": lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                uploaded=True,
            ),
            "upload_content": lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                uploaded=True,
            ),
            "download": lambda *args, **kwargs: b"",
            "download_content": lambda *args, **kwargs: b"",
            "fetch_content": lambda *args, **kwargs: b"",
            "list_files": lambda *args, **kwargs: [],
        },
    )
def _apply_http_pack(instance: Any) -> Any:
    """Attach a stable HTTP client collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=("http_client", "session", "transport", "client"),
        method_behaviors={
            "request": _http_response_stub,
            "get": _http_response_stub,
            "post": _http_response_stub,
            "put": _http_response_stub,
            "patch": _http_response_stub,
            "delete": _http_response_stub,
            "send": _http_response_stub,
        },
    )
