from __future__ import annotations
# ruff: noqa
def _apply_model_inference_pack(instance: Any) -> Any:
    """Attach a stable model-inference collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "model",
            "predictor",
            "scorer",
            "embedder",
            "encoder",
            "classifier",
            "reranker",
            "model_client",
        ),
        method_behaviors={
            "predict": _model_prediction_stub,
            "predict_proba": _model_probability_stub,
            "transform": _embedding_stub,
            "embed": _embedding_stub,
            "encode": _embedding_stub,
            "score": lambda *args, **kwargs: 0.5,
            "classify": lambda *args, **kwargs: {"label": "ok", "score": 0.5},
            "infer": _model_prediction_stub,
            "run": _model_prediction_stub,
        },
    )
def _apply_feature_store_pack(instance: Any) -> Any:
    """Attach a stable feature-store collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "feature_store",
            "vector_store",
            "embedding_store",
            "retriever",
            "feature_client",
        ),
        method_behaviors={
            "get": _feature_payload_stub,
            "fetch": _feature_payload_stub,
            "lookup": _feature_payload_stub,
            "get_features": _feature_payload_stub,
            "fetch_features": _feature_payload_stub,
            "lookup_features": _feature_payload_stub,
            "put": lambda *args, **kwargs: True,
            "upsert": lambda *args, **kwargs: True,
        },
    )
_BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS: dict[str, dict[str, Any]] = {
    "subprocess": {
        "aliases": ("subprocess_runner",),
        "description": (
            "Stub subprocess-style runners and command executors with successful no-op results."
        ),
        "hook": _apply_subprocess_pack,
    },
    "sandbox": {
        "aliases": ("sandbox_client",),
        "description": (
            "Stub sandbox clients with upload/download helpers and successful command execution."
        ),
        "hook": _apply_sandbox_pack,
    },
    "upload_download": {
        "aliases": (
            "upload_download_client",
            "upload",
            "upload_client",
            "download",
            "download_client",
        ),
        "description": (
            "Stub storage, upload, and download collaborators with safe in-memory responses."
        ),
        "hook": _apply_upload_download_pack,
    },
    "model_inference": {
        "aliases": (
            "model_client",
            "predictor",
            "embedder",
            "encoder",
            "classifier",
        ),
        "description": (
            "Stub model-style collaborators with stable prediction, "
            "embedding, and scoring outputs."
        ),
        "hook": _apply_model_inference_pack,
    },
    "feature_store": {
        "aliases": (
            "vector_store",
            "embedding_store",
            "feature_client",
            "retriever",
        ),
        "description": (
            "Stub feature-store collaborators with stable row-shaped feature payloads."
        ),
        "hook": _apply_feature_store_pack,
    },
    "http": {
        "aliases": ("http_client",),
        "description": "Stub HTTP clients and transports with stable 200-style response objects.",
        "hook": _apply_http_pack,
    },
    "state_store": {
        "aliases": (),
        "description": (
            "Attach an in-memory key/value store for cache or session-style collaborators."
        ),
        "hook": _apply_state_store_pack,
    },
}
_BUILTIN_OBJECT_SCENARIO_LIBRARY_ALIASES = {
    alias: name
    for name, spec in _BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS.items()
    for alias in (name, *spec["aliases"])
}
def available_object_scenario_libraries() -> tuple[dict[str, Any], ...]:
    """Return the canonical built-in collaborator scenario library catalog."""
    return tuple(
        {
            "name": name,
            "aliases": list(spec["aliases"]),
            "description": str(spec["description"]),
        }
        for name, spec in _BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS.items()
    )
def _builtin_object_scenario_hook(name: str) -> Callable[[Any], Any] | None:
    """Return a named collaborator scenario pack hook, if known."""
    normalized = str(name).strip().lower()
    canonical = _BUILTIN_OBJECT_SCENARIO_LIBRARY_ALIASES.get(normalized)
    if canonical is None:
        return None
    spec = _BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS[canonical]
    return spec["hook"]
def _scenario_hook_from_spec(spec: Mapping[str, Any]) -> Callable[[Any], Any]:
    """Compile one TOML-friendly collaborator scenario spec into a hook."""
    kind = str(spec.get("kind") or spec.get("action") or spec.get("op") or "").strip().lower()
    if not kind:
        if spec.get("error") is not None or spec.get("exception") is not None:
            kind = "stub_raise"
        elif spec.get("path") is not None or spec.get("target") is not None:
            kind = "setattr"
    if not kind and spec.get("pack") is not None:
        kind = "pack"
    path = spec.get("path") or spec.get("target") or spec.get("attr") or spec.get("name")
    pack = spec.get("pack") or spec.get("library") or spec.get("scenario")

    def _setattr_hook(instance: Any) -> Any:
        if path is None:
            raise ValueError("scenario setattr spec needs a path")
        target, attr_name = _scenario_path_target(instance, str(path))
        setattr(target, attr_name, spec.get("value"))
        return instance

    def _stub_return_hook(instance: Any) -> Any:
        if path is None:
            raise ValueError("scenario stub_return spec needs a path")
        target, attr_name = _scenario_path_target(instance, str(path))
        original = getattr(target, attr_name)
        if not callable(original):
            raise ValueError(f"scenario path {path!r} does not resolve to a callable")
        setattr(
            target,
            attr_name,
            _scenario_stub_wrapper(original, return_value=spec.get("value")),
        )
        return instance

    def _stub_raise_hook(instance: Any) -> Any:
        if path is None:
            raise ValueError("scenario stub_raise spec needs a path")
        target, attr_name = _scenario_path_target(instance, str(path))
        original = getattr(target, attr_name)
        if not callable(original):
            raise ValueError(f"scenario path {path!r} does not resolve to a callable")
        setattr(
            target,
            attr_name,
            _scenario_stub_wrapper(
                original,
                error=_scenario_exception_from_spec(
                    spec.get("error", spec.get("exception", spec.get("value")))
                ),
            ),
        )
        return instance

    def _pack_hook(instance: Any) -> Any:
        if pack is None:
            raise ValueError("scenario pack spec needs a pack name")
        hook = _builtin_object_scenario_hook(str(pack))
        if hook is None:
            raise ValueError(f"unknown built-in scenario pack: {pack!r}")
        return hook(instance)

    match kind:
        case "setattr" | "assign" | "set":
            return _setattr_hook
        case "stub_return" | "return" | "returns":
            return _stub_return_hook
        case "stub_raise" | "raise" | "raises":
            return _stub_raise_hook
        case "pack":
            return _pack_hook
        case _:
            raise ValueError(f"unsupported scenario kind {kind!r}")
def _expand_object_scenario_hooks(hook: Any) -> tuple[Any, ...]:
    """Normalize a scenario entry into one or more executable hooks."""
    if hook is None:
        return ()
    if callable(hook):
        return (hook,)
    if isinstance(hook, (str, bytes)):
        builtin = _builtin_object_scenario_hook(hook.decode() if isinstance(hook, bytes) else hook)
        if builtin is not None:
            return (builtin,)
        raise ValueError(f"unknown built-in scenario pack: {hook!r}")
    if isinstance(hook, Mapping):
        if "pack" in hook or "library" in hook or "scenario" in hook:
            return (_scenario_hook_from_spec(hook),)
        return (_scenario_hook_from_spec(hook),)
    if isinstance(hook, Sequence):
        compiled: list[Any] = []
        for item in hook:
            compiled.extend(_expand_object_scenario_hooks(item))
        return tuple(compiled)
    return (hook,)
def _resolve_object_hooks(owner: type, hooks: dict[str, Any] | None) -> tuple[Any, ...]:
    """Resolve one or more registered hooks for *owner* from several key styles."""
    hook = _resolve_object_hook(owner, hooks)
    if hook is None:
        return ()
    return _expand_object_scenario_hooks(hook)
def _resolve_object_harness(owner: type, harnesses: dict[str, str] | None) -> str:
    """Resolve the configured harness mode for *owner*."""
    if not harnesses:
        return "fresh"
    for candidate in _object_hook_candidates(owner):
        if candidate in harnesses:
            resolved = str(harnesses[candidate]).strip().lower()
            if resolved in {"fresh", "stateful"}:
                return resolved
    return "fresh"
def _lifecycle_phase(method_name: str, method: Any | None = None) -> str | None:
    """Infer a coarse lifecycle phase from decorator attrs or method names."""
    target = _unwrap(getattr(method, "__func__", method)) if method is not None else None
    if target is not None:
        for phase in ("setup", "rollout", "stop", "cleanup", "teardown"):
            if getattr(target, phase, False) or (
                getattr(target, f"{phase}_priority", None) is not None
            ):
                return phase
    lowered = method_name.lower()
    exact = {
        "setup_state": "setup",
        "post_sandbox_setup": "setup",
        "post_rollout": "rollout",
    }
    if lowered in exact:
        return exact[lowered]
    for phase in ("setup", "cleanup", "teardown", "stop", "rollout"):
        if phase in lowered:
            return phase
    return None
def _snapshot_instance_state(instance: Any) -> Any:
    """Capture a best-effort snapshot of instance state for lifecycle predicates."""
    state = getattr(instance, "__dict__", None)
    if not isinstance(state, dict):
        return None
    try:
        return copy.deepcopy(state)
    except Exception:
        return {key: repr(value) for key, value in state.items()}
def _lifecycle_phase_members(
    owner: type,
    phase: str,
    *,
    exclude: Sequence[str] = (),
) -> list[str]:
    """Return public owner methods that look like members of one lifecycle phase."""
    excluded = set(exclude)
    members: list[str] = []
    for name, raw_attr in inspect.getmembers_static(owner):
        if name.startswith("_") or name in excluded:
            continue
        if _lifecycle_phase(name, raw_attr) != phase:
            continue
        if isinstance(raw_attr, (staticmethod, classmethod)) or inspect.isfunction(raw_attr):
            members.append(name)
    return members
def _lifecycle_fault_exception(name: str) -> BaseException:
    """Return the concrete exception raised for one lifecycle fault name."""
    if name in {"cancel", "cancel_rollout"}:
        return asyncio.CancelledError("injected rollout cancellation")
    if name == "raise_setup_hook":
        return RuntimeError("injected setup failure")
    if name == "raise_teardown_hook":
        return RuntimeError("injected teardown failure")
    if name == "raise_cleanup_handler":
        return RuntimeError("injected cleanup handler failure")
    if name == "raise_teardown_handler":
        return RuntimeError("injected teardown handler failure")
    return RuntimeError(f"injected lifecycle fault: {name}")
@contextlib.contextmanager
def _active_contract_faults(
    func: Any,
    faults: Sequence[str],
) -> Any:
    """Temporarily attach contract-scoped lifecycle fault names to *func*."""
    if not faults:
        yield
        return
    previous = getattr(func, "__ordeal_contract_faults__", None)
    setattr(func, "__ordeal_contract_faults__", tuple(faults))
    try:
        yield
    finally:
        if previous is None:
            with contextlib.suppress(AttributeError):
                delattr(func, "__ordeal_contract_faults__")
        else:
            setattr(func, "__ordeal_contract_faults__", previous)
@contextlib.contextmanager
def _active_instance_probe(
    func: Any,
    probe: Any | None,
) -> Any:
    """Temporarily attach an instance probe to one wrapped callable."""
    previous = getattr(func, "__ordeal_instance_probe__", None)
    setattr(func, "__ordeal_instance_probe__", probe)
    try:
        yield
    finally:
        setattr(func, "__ordeal_instance_probe__", previous)
