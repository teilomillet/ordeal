from __future__ import annotations
# ruff: noqa
@contextlib.contextmanager
def _lifecycle_fault_runtime(
    instance: Any,
    owner: type,
    *,
    method_name: str,
    setup: Any | None = None,
    teardown: Any | None = None,
    fault_names: Sequence[str] = (),
) -> Any:
    """Patch lifecycle collaborators on *instance* for contract-driven probes."""
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    applied_faults: list[str] = []
    phase_candidates: dict[str, list[str]] = {}
    restore: list[tuple[Any, str, Any]] = []
    fired: dict[str, bool] = {}

    def _record(
        *,
        phase: str,
        name: str,
        kind: str,
        injected: bool = False,
        raised: bool = False,
        error_type: str | None = None,
    ) -> None:
        events.append(
            {
                "phase": phase,
                "name": name,
                "kind": kind,
                "injected": injected,
                "raised": raised,
                "error_type": error_type,
            }
        )

    def _hook_wrapper(hook: Any, *, phase: str, fault_name: str | None) -> Any:
        if hook is None:
            return None

        @functools.wraps(hook)
        def wrapped_hook(current: Any) -> Any:
            should_inject = bool(fault_name) and not fired.get(str(fault_name), False)
            event = {
                "phase": phase,
                "name": getattr(hook, "__name__", phase),
                "kind": "hook",
                "injected": should_inject,
                "raised": False,
                "error_type": None,
            }
            events.append(event)
            if should_inject:
                fired[str(fault_name)] = True
                event["raised"] = True
                exc = _lifecycle_fault_exception(str(fault_name))
                event["error_type"] = type(exc).__name__
                raise exc
            try:
                return _call_with_optional_instance_arg(hook, current)
            except BaseException as exc:
                event["raised"] = True
                event["error_type"] = type(exc).__name__
                raise

        return wrapped_hook

    setup_hook = _hook_wrapper(
        setup,
        phase="setup",
        fault_name="raise_setup_hook" if "raise_setup_hook" in fault_names else None,
    )
    teardown_hook = _hook_wrapper(
        teardown,
        phase="teardown",
        fault_name="raise_teardown_hook" if "raise_teardown_hook" in fault_names else None,
    )
    if setup_hook is not None and "raise_setup_hook" in fault_names:
        applied_faults.append("raise_setup_hook")
    if teardown_hook is not None and "raise_teardown_hook" in fault_names:
        applied_faults.append("raise_teardown_hook")

    phase_faults = {
        "raise_cleanup_handler": "cleanup",
        "raise_teardown_handler": "teardown",
        "cancel_rollout": "rollout",
    }
    for fault_name, phase in phase_faults.items():
        if fault_name not in fault_names:
            continue
        names = _lifecycle_phase_members(owner, phase, exclude=(method_name,))
        phase_candidates[phase] = names
        if not names:
            warnings.append(f"{fault_name}: no {phase} handlers found to inject")
            continue
        injected_name = names[0]
        for name in names:
            original = getattr(instance, name)
            is_async = inspect.iscoroutinefunction(getattr(original, "__func__", original))
            if is_async:

                @functools.wraps(original)
                async def wrapper(
                    *args: Any,
                    __orig: Any = original,
                    __name: str = name,
                    __phase: str = phase,
                    __fault_name: str = fault_name,
                    __inject: bool = name == injected_name,
                    **kwargs: Any,
                ) -> Any:
                    should_inject = __inject and not fired.get(__fault_name, False)
                    event = {
                        "phase": __phase,
                        "name": __name,
                        "kind": "handler",
                        "injected": should_inject,
                        "raised": False,
                        "error_type": None,
                    }
                    events.append(event)
                    if should_inject:
                        fired[__fault_name] = True
                        event["raised"] = True
                        exc = _lifecycle_fault_exception(__fault_name)
                        event["error_type"] = type(exc).__name__
                        raise exc
                    try:
                        result = __orig(*args, **kwargs)
                        if inspect.isawaitable(result):
                            return await result
                        return result
                    except BaseException as exc:
                        event["raised"] = True
                        event["error_type"] = type(exc).__name__
                        raise
            else:

                @functools.wraps(original)
                def wrapper(
                    *args: Any,
                    __orig: Any = original,
                    __name: str = name,
                    __phase: str = phase,
                    __fault_name: str = fault_name,
                    __inject: bool = name == injected_name,
                    **kwargs: Any,
                ) -> Any:
                    should_inject = __inject and not fired.get(__fault_name, False)
                    event = {
                        "phase": __phase,
                        "name": __name,
                        "kind": "handler",
                        "injected": should_inject,
                        "raised": False,
                        "error_type": None,
                    }
                    events.append(event)
                    if should_inject:
                        fired[__fault_name] = True
                        event["raised"] = True
                        exc = _lifecycle_fault_exception(__fault_name)
                        event["error_type"] = type(exc).__name__
                        raise exc
                    try:
                        return _call_sync(__orig, *args, **kwargs)
                    except BaseException as exc:
                        event["raised"] = True
                        event["error_type"] = type(exc).__name__
                        raise

            restore.append((instance, name, original))
            setattr(instance, name, wrapper)
        applied_faults.append(fault_name)

    runtime = {
        "events": events,
        "warnings": warnings,
        "applied_faults": applied_faults,
        "phase_candidates": phase_candidates,
        "setup_hook": setup_hook or setup,
        "teardown_hook": teardown_hook or teardown,
    }
    try:
        yield runtime
    finally:
        for obj, attr_name, original in reversed(restore):
            setattr(obj, attr_name, original)
def _discover_lifecycle_handlers(
    owner: type | Any,
    phase: str,
    *,
    exclude_method: str | None = None,
) -> list[str]:
    """Return public handler names that look like they belong to one lifecycle phase."""
    cls = owner if inspect.isclass(owner) else type(owner)
    handlers: list[str] = []
    for name, raw_attr in inspect.getmembers_static(cls):
        if name.startswith("_") or name == exclude_method:
            continue
        if not (isinstance(raw_attr, (staticmethod, classmethod)) or inspect.isfunction(raw_attr)):
            continue
        if _lifecycle_phase(name, raw_attr) == phase:
            handlers.append(name)
    return sorted(dict.fromkeys(handlers))
def _contract_seed_kwargs(func: Any) -> dict[str, Any]:
    """Return one deterministic concrete input for a contract probe."""
    candidates = _candidate_inputs(
        func,
        fixtures=None,
        mutate_observed_inputs=False,
    )
    if not candidates:
        return {}
    return dict(candidates[0].kwargs)
def _instance_probe_result(
    probe: Any | None,
    *,
    instance: Any,
    owner: type | None,
    method_name: str,
) -> tuple[Callable[[], None] | None, dict[str, Any]]:
    """Apply a temporary instance probe and normalize its cleanup/context payload."""
    if probe is None:
        return None, {}
    result = probe(
        instance=instance,
        owner=owner,
        method_name=method_name,
    )
    if result is None:
        return None, {}
    if callable(result):
        return result, {}
    if isinstance(result, tuple) and len(result) == 2:
        cleanup, details = result
        if isinstance(details, Mapping):
            return cleanup, dict(details)
        return cleanup, {}
    if isinstance(result, Mapping):
        return None, dict(result)
    return None, {}
def _state_param_name_for_callable(func: Any) -> str | None:
    """Return the likely runtime state parameter name for *func*."""
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    hints = safe_get_annotations(target)
    params = [param for param in sig.parameters.values() if param.name not in {"self", "cls"}]
    for param in params:
        lowered = param.name.lower()
        hint = hints.get(param.name)
        hint_name = getattr(hint, "__name__", "")
        hint_text = str(hint_name or hint).lower()
        if lowered == "state" or lowered.endswith("_state") or "state" in hint_text:
            return param.name
    return None
def _call_with_optional_instance_arg(hook: Any, instance: Any) -> Any:
    """Call *hook* with zero or one instance argument."""
    hook = _unwrap(hook)
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        try:
            return _call_sync(hook, instance)
        except TypeError:
            return _call_sync(hook)

    params = list(signature.parameters.values())
    required = [
        param
        for param in params
        if param.default is inspect.Parameter.empty
        and param.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    accepts_varargs = any(
        param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for param in params
    )
    if accepts_varargs or required or params:
        try:
            return _call_sync(hook, instance)
        except TypeError:
            if not required:
                return _call_sync(hook)
            raise
    return _call_sync(hook)
def _is_metadata_only_hook(hook: Any) -> bool:
    """Return whether *hook* is a read-only placeholder from CLI metadata mode."""
    return bool(getattr(hook, "__ordeal_metadata_only__", False))
def _python_source_path_to_module_name(path_str: str) -> str | None:
    """Convert a project-relative Python path into an importable module name."""
    path = Path(path_str)
    if path.suffix != ".py":
        return None
    for root in (Path.cwd() / "src", Path.cwd()):
        with contextlib.suppress(ValueError):
            rel = path.resolve().relative_to(root.resolve())
            rel_parts = rel.parts[:-1] if rel.name == "__init__.py" else rel.with_suffix("").parts
            if rel_parts:
                return ".".join(rel_parts)
    resolved = path.resolve()
    module_parts = [] if resolved.name == "__init__.py" else [resolved.stem]
    parent = resolved.parent
    while (parent / "__init__.py").exists():
        module_parts.append(parent.name)
        parent = parent.parent
    if len(module_parts) > 1:
        return ".".join(reversed(module_parts))
    return None
_DISCOVERY_IGNORED_PATH_PARTS = {
    ".claude",
    ".codex",
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".nox",
    ".ordeal",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "dist-packages",
    "node_modules",
    "site-packages",
    "venv",
}
_DISCOVERY_IGNORED_ROOT_NAMES = {"site"}
def _is_project_discovery_path(path: Path, *, workspace_root: Path | None = None) -> bool:
    """Return whether *path* should contribute to learned repo evidence."""
    resolved = path.resolve()
    root = (workspace_root or Path.cwd()).resolve()
    try:
        parts = resolved.relative_to(root).parts
    except ValueError:
        parts = resolved.parts
    return not any(part in _DISCOVERY_IGNORED_PATH_PARTS for part in parts)
def _harness_hint_path_signals(evidence: str) -> tuple[str, ...]:
    """Return generic discovery signals implied by one hint evidence path."""
    path_text = str(evidence).split(":", 1)[0].replace("\\", "/").lower()
    signals: list[str] = []
    if "/tests/" in f"/{path_text}" or path_text.startswith("tests/"):
        signals.append("test_evidence")
    name = Path(path_text).name
    if any(token in name for token in ("support", "fixture", "factory", "conftest")):
        signals.append("support_file")
    if path_text.endswith(".md"):
        signals.append("doc_evidence")
    return tuple(signals)
def _harness_hint_signal_strength(signals: Sequence[str]) -> float:
    """Return the weighted evidence strength for one hint signal set."""
    unique = dict.fromkeys(str(item) for item in signals if str(item).strip())
    return round(
        sum(_HARNESS_HINT_SIGNAL_WEIGHTS.get(signal, 0.0) for signal in unique),
        3,
    )
def _score_harness_hint(confidence: float, signals: Sequence[str]) -> float:
    """Return one bounded compatibility score for a mined harness hint."""
    strength = _harness_hint_signal_strength(signals)
    score = float(confidence) + (strength * 0.04)
    return round(min(score, 1.0), 4)
def _hint_sort_key(hint: HarnessHint) -> tuple[float, float, int, str, str]:
    """Return a stable descending sort key for mined harness hints."""
    return (
        -float(hint.score),
        -float(hint.confidence),
        -_harness_hint_signal_strength(hint.signals),
        -len(hint.signals),
        hint.kind,
        hint.suggestion,
    )
def _resolve_symbol_path(path: str) -> Any:
    """Resolve ``module:attr`` or dotted import paths into Python objects."""
    import importlib.util

    module_name, sep, attr_path = path.partition(":")
    candidate_file = Path(module_name)
    if sep and (
        candidate_file.suffix == ".py"
        or candidate_file.exists()
        or module_name.startswith("./")
        or module_name.startswith("../")
    ):
        file_path = candidate_file
        if not file_path.is_absolute():
            file_path = (Path.cwd() / file_path).resolve()
        if not file_path.exists():
            raise ValueError(f"invalid symbol path: {path!r}")
        spec = importlib.util.spec_from_file_location(
            f"_ordeal_symbol_{abs(hash((str(file_path), attr_path)))}",
            file_path,
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"invalid symbol path: {path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        obj: Any = module
        for part in attr_path.split("."):
            obj = getattr(obj, part)
        return obj
    if not sep:
        module_name, _, attr_path = path.rpartition(".")
    if not module_name or not attr_path:
        raise ValueError(f"invalid symbol path: {path!r}")
    obj = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj
def _hint_symbol_path(value: object) -> str | None:
    """Normalize one mined hint value into an importable symbol path when possible."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower().startswith("docs mention "):
        return None
    match = re.match(r"^(?P<path>.+?\.py):(?P<line>\d+):(?P<name>[A-Za-z_][\w]*)$", text)
    if match is None:
        return text if ":" in text or "." in text else None
    module_name = _python_source_path_to_module_name(match.group("path"))
    if module_name is None:
        return None
    return f"{module_name}:{match.group('name')}"
def _scenario_pack_from_hint(hint: HarnessHint) -> str | None:
    """Infer one built-in scenario pack from a mined collaborator hint."""
    text = " ".join(
        [
            hint.kind,
            hint.suggestion,
            hint.evidence,
            str(hint.config.get("value", "")) if isinstance(hint.config, Mapping) else "",
        ]
    ).lower()
    if any(token in text for token in ("sandbox", "execute_command", "upload_content")):
        return "sandbox_client"
    if any(
        token in text
        for token in (
            "feature_store",
            "vector_store",
            "embedding_store",
            "feature_client",
            "fetch_features",
            "lookup_features",
        )
    ):
        return "feature_store"
    if any(
        token in text
        for token in (
            "model.predict",
            "predictor",
            "embedder",
            "encoder",
            "classifier",
            "predict_proba",
            "embedding client",
        )
    ):
        return "model_inference"
    if any(token in text for token in ("artifact", "storage", "download", "upload")):
        return "upload_download"
    if any(token in text for token in ("http", "request", "session", "transport")):
        return "http_client"
    if any(token in text for token in ("state_store", "session_state", "cache", "store")):
        return "state_store"
    if any(token in text for token in ("subprocess", "runner", "popen", "command_runner")):
        return "subprocess"
    return None
