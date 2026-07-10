from __future__ import annotations
# ruff: noqa
def _audit_state_hash(
    module: str,
    *,
    test_dir: str,
    max_examples: int,
    validation_mode: AuditValidationMode,
    target_specs: Sequence[Any] | None = None,
) -> str:
    """Hash the inputs that determine an audit result.

    The cache key includes the target module, relevant current tests,
    active coverage backend availability, the dependency lockfile, and
    the ordeal source files that affect generated tests or validation.
    """
    h = hashlib.sha256()
    h.update(module.encode("utf-8"))
    h.update(_audit_target_cache_key(module, target_specs).encode("utf-8"))
    h.update(str(Path(test_dir).resolve()).encode("utf-8"))
    h.update(str(max_examples).encode("utf-8"))
    h.update(validation_mode.encode("utf-8"))
    h.update(sys.version.encode("utf-8"))
    h.update(str(Path(sys.executable).resolve()).encode("utf-8"))
    h.update(str(importlib.util.find_spec("coverage") is not None).encode("utf-8"))
    h.update(str(importlib.util.find_spec("pytest_cov") is not None).encode("utf-8"))

    mod = _resolve_module(module)
    source_file = getattr(mod, "__file__", None)
    if source_file is None:
        raise ValueError(f"Cannot locate source for {module!r}")
    _hash_file_if_exists(h, Path(source_file))

    test_path = Path(test_dir)
    test_files = _find_test_files(module, test_path)
    for test_file in test_files:
        _hash_file_if_exists(h, test_file)

    conftests: set[Path] = set()
    root = Path.cwd().resolve()
    if (root / "conftest.py").exists():
        conftests.add(root / "conftest.py")
    if (test_path / "conftest.py").exists():
        conftests.add((test_path / "conftest.py").resolve())
    for test_file in test_files:
        resolved = test_file.resolve()
        for parent in [resolved.parent, *resolved.parents]:
            candidate = parent / "conftest.py"
            if candidate.exists():
                conftests.add(candidate.resolve())
            if parent == root:
                break
    for conftest in sorted(conftests):
        _hash_file_if_exists(h, conftest)

    for config_file in ("pyproject.toml", "ordeal.toml", "pytest.ini", "tox.ini", "setup.cfg"):
        _hash_file_if_exists(h, Path(config_file))

    for lockfile in ("uv.lock", "poetry.lock", "requirements.txt"):
        candidate = Path(lockfile)
        if candidate.exists():
            _hash_file_if_exists(h, candidate)
            break

    for spec_name in ("ordeal.audit", "ordeal.auto", "ordeal.mine", "ordeal.mutations"):
        spec = importlib.util.find_spec(spec_name)
        if spec and spec.origin:
            _hash_file_if_exists(h, Path(spec.origin))

    audit_parts = Path(__file__).with_name("parts") / "audit"
    if audit_parts.is_dir():
        for part in sorted(audit_parts.glob("*.py")):
            _hash_file_if_exists(h, part)

    return h.hexdigest()[:16]
def _load_audit_cache(cache_key: str, state_hash: str) -> ModuleAudit | None:
    """Load a cached audit result when the state hash still matches."""
    cache_path = _audit_cache_path(cache_key)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("state_hash") != state_hash:
        return None
    data = payload.get("result")
    if not isinstance(data, dict):
        return None
    try:
        return _module_audit_from_dict(data)
    except Exception:
        return None
def _save_audit_cache(cache_key: str, state_hash: str, result: ModuleAudit) -> None:
    """Persist an audit result to the local `.ordeal/audit` cache."""
    cache_path = _audit_cache_path(cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state_hash": state_hash,
        "result": _module_audit_to_dict(result),
    }
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.rename(cache_path)


def _coverage_evidence_state_hash(module: str, test_files: Sequence[Path]) -> str | None:
    """Hash source, tests, config, and runtime identity for coverage evidence."""
    try:
        mod = _resolve_module(module)
        source_file = Path(str(getattr(mod, "__file__"))).resolve()
    except Exception:
        return None
    if not source_file.is_file() or not test_files or any(not path.is_file() for path in test_files):
        return None

    from importlib import metadata

    root = Path.cwd().resolve()
    h = hashlib.sha256()
    h.update(module.encode("utf-8"))
    h.update(sys.version.encode("utf-8"))
    h.update(sys.platform.encode("utf-8"))
    for package in ("coverage", "pytest"):
        with contextlib.suppress(metadata.PackageNotFoundError):
            h.update(f"{package}={metadata.version(package)}".encode("utf-8"))
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        h.update(f"{key}={__import__('os').environ.get(key, '')}".encode("utf-8"))

    def _add(path: Path) -> None:
        if not path.is_file():
            return
        resolved = path.resolve()
        with contextlib.suppress(ValueError):
            resolved = resolved.relative_to(root)
        h.update(str(resolved).encode("utf-8"))
        h.update(path.read_bytes())

    _add(source_file)
    resolved_tests = sorted(path.resolve() for path in test_files)
    for test_file in resolved_tests:
        _add(test_file)
    conftests: set[Path] = set()
    for test_file in resolved_tests:
        for parent in [test_file.parent, *test_file.parents]:
            candidate = parent / "conftest.py"
            if candidate.is_file():
                conftests.add(candidate)
            if parent == root:
                break
    for conftest in sorted(conftests):
        _add(conftest)
    for config_file in ("pyproject.toml", "ordeal.toml", "pytest.ini", "tox.ini", "setup.cfg"):
        _add(Path(config_file))
    for lockfile in ("uv.lock", "poetry.lock", "requirements.txt"):
        candidate = Path(lockfile)
        if candidate.is_file():
            _add(candidate)
            break
    _add(Path(__file__))
    audit_parts = Path(__file__).with_name("parts") / "audit"
    if audit_parts.is_dir():
        for part in sorted(audit_parts.glob("*.py")):
            _add(part)
    return h.hexdigest()[:20]


def _coverage_evidence_cache_path(module: str, state_hash: str) -> Path:
    """Return the hash-bound audit coverage evidence file."""
    safe = module.replace(".", "_").replace(":", "_")
    return Path(".ordeal") / "audit" / "coverage" / f"{safe}-{state_hash}.json"


def _audit_coverage_evidence_state_hash(
    module: str,
    current_test_files: Sequence[Path],
    generated_test_files: Sequence[Path],
) -> str | None:
    """Extend existing-suite evidence identity with generated test contents."""
    current_hash = _coverage_evidence_state_hash(module, current_test_files)
    if current_hash is None or any(not path.is_file() for path in generated_test_files):
        return None
    h = hashlib.sha256(current_hash.encode("utf-8"))
    for path in sorted(path.resolve() for path in generated_test_files):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()[:20]


def _load_audit_coverage_evidence(
    module: str,
    current_test_files: Sequence[Path],
    generated_test_files: Sequence[Path],
) -> tuple[CoverageMeasurement, CoverageMeasurement] | None:
    """Load a complete coverage pair only when its exact evidence inputs match."""
    state_hash = _audit_coverage_evidence_state_hash(
        module,
        current_test_files,
        generated_test_files,
    )
    if state_hash is None:
        return None
    try:
        payload = json.loads(
            _coverage_evidence_cache_path(module, f"pair-{state_hash}").read_text(encoding="utf-8")
        )
        current = _coverage_measurement_from_dict(payload["current"])
        migrated = _coverage_measurement_from_dict(payload["migrated"])
    except Exception:
        return None
    if any(
        measurement.status != Status.VERIFIED or measurement.result is None
        for measurement in (current, migrated)
    ):
        return None
    return current, migrated


def _save_audit_coverage_evidence(
    module: str,
    current_test_files: Sequence[Path],
    generated_test_files: Sequence[Path],
    current: CoverageMeasurement,
    migrated: CoverageMeasurement,
) -> None:
    """Persist a complete evidence pair only after both measurements verify."""
    if any(
        measurement.status != Status.VERIFIED or measurement.result is None
        for measurement in (current, migrated)
    ):
        return
    state_hash = _audit_coverage_evidence_state_hash(
        module,
        current_test_files,
        generated_test_files,
    )
    if state_hash is None:
        return
    path = _coverage_evidence_cache_path(module, f"pair-{state_hash}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "state_hash": state_hash,
                "current": _coverage_measurement_to_dict(current),
                "migrated": _coverage_measurement_to_dict(migrated),
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(path)
# ============================================================================
# Audit planning helpers
# ============================================================================


def _split_audit_target_spec(target: str) -> tuple[str, str | None, str | None]:
    """Split ``module[:Owner[.method]]`` into module, owner, and method parts."""
    if ":" not in target:
        return target, None, None
    module_path, remainder = target.split(":", 1)
    if "." not in remainder:
        return module_path, remainder, None
    owner_path, method_name = remainder.rsplit(".", 1)
    return module_path, owner_path, method_name
def _is_fatal_symbol_resolution_exception(exc: BaseException) -> bool:
    """Return whether *exc* should abort audit symbol resolution."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError))
def _resolve_audit_symbol(path: str) -> Any:
    """Resolve a dotted import path, including lazy-exported package members."""
    if ":" in path:
        module_path, attr_path = path.split(":", 1)
        obj: Any = importlib.import_module(module_path)
        for part in attr_path.split("."):
            obj = getattr(obj, part)
        return obj

    try:
        return importlib.import_module(path)
    except BaseException as exc:
        if _is_fatal_symbol_resolution_exception(exc):
            raise
        pass

    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj: Any = importlib.import_module(".".join(parts[:i]))
        except BaseException as exc:
            if _is_fatal_symbol_resolution_exception(exc):
                raise
            continue
        try:
            for part in parts[i:]:
                obj = getattr(obj, part)
            return obj
        except BaseException as exc:
            if _is_fatal_symbol_resolution_exception(exc):
                raise
            continue
    raise ImportError(f"Cannot resolve target: {path!r}")
def _call_with_async_support(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call *func* and run any awaitable result to completion."""
    result = func(*args, **kwargs)
    if not inspect.isawaitable(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(result)
    finally:
        loop.close()
def _resolve_audit_hook(hook: str | Any | None) -> Any | None:
    """Resolve a hook path or passthrough a callable/object."""
    if hook is None or not isinstance(hook, str):
        return hook
    return _resolve_audit_symbol(hook)
def _instantiate_audit_owner(
    cls: type[Any],
    *,
    factory: str | Any | None = None,
    setup: str | Any | None = None,
    scenarios: Sequence[str | Any] | None = None,
) -> tuple[Any | None, str | None]:
    """Create an object for auditing class methods."""
    try:
        if factory:
            factory_obj = _resolve_audit_hook(factory)
            if not callable(factory_obj):
                return None, f"factory {factory} is not callable"
            instance = _call_with_async_support(factory_obj)
        else:
            instance = cls()
    except Exception as exc:
        return None, f"cannot instantiate {cls.__name__}: {type(exc).__name__}: {exc}"

    if setup:
        try:
            setup_obj = _resolve_audit_hook(setup)
            if not callable(setup_obj):
                return None, f"setup hook {setup} is not callable"
            setup_result = _call_with_async_support(setup_obj, instance)
            if setup_result is not None:
                instance = setup_result
        except Exception as exc:
            return None, f"setup failed for {cls.__name__}: {type(exc).__name__}: {exc}"

    for scenario in scenarios or ():
        try:
            scenario_obj = _resolve_audit_hook(scenario)
            if not callable(scenario_obj):
                return None, f"scenario hook {scenario} is not callable"
            scenario_result = _call_with_async_support(scenario_obj, instance)
            if scenario_result is not None:
                instance = scenario_result
        except Exception as exc:
            return None, f"scenario failed for {cls.__name__}: {type(exc).__name__}: {exc}"

    return instance, None
def _wrap_audit_callable(
    reference: Callable[..., Any],
    invoke: Callable[..., Any],
    *,
    module_name: str,
    qualname: str,
    owner: type[Any] | str | None = None,
    method_name: str | None = None,
    factory: str | None = None,
    setup: str | None = None,
    scenarios: Sequence[str | Any] | None = None,
    state_factory: str | Any | None = None,
    teardown: str | Any | None = None,
    harness: str = "fresh",
    kind: str = "function",
) -> Callable[..., Any]:
    """Wrap a callable while preserving its signature and metadata."""

    @functools.wraps(reference)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with (
            (
                _active_instance_probe(
                    invoke,
                    getattr(wrapped, "__ordeal_instance_probe__", None),
                )
                if getattr(wrapped, "__ordeal_instance_probe__", None) is not None
                else contextlib.nullcontext()
            ),
            (
                _active_contract_faults(
                    invoke,
                    tuple(getattr(wrapped, "__ordeal_contract_faults__", ())),
                )
                if getattr(wrapped, "__ordeal_contract_faults__", ())
                else contextlib.nullcontext()
            ),
        ):
            try:
                return _call_with_async_support(invoke, *args, **kwargs)
            finally:
                wrapped.__ordeal_last_call_context__ = getattr(  # type: ignore[attr-defined]
                    invoke,
                    "__ordeal_last_call_context__",
                    None,
                )

    wrapped.__ordeal_module__ = module_name  # type: ignore[attr-defined]
    wrapped.__ordeal_qualname__ = qualname  # type: ignore[attr-defined]
    wrapped.__ordeal_owner__ = owner  # type: ignore[attr-defined]
    wrapped.__ordeal_method__ = method_name  # type: ignore[attr-defined]
    wrapped.__ordeal_method_name__ = method_name  # type: ignore[attr-defined]
    wrapped.__ordeal_factory__ = factory  # type: ignore[attr-defined]
    wrapped.__ordeal_setup__ = setup  # type: ignore[attr-defined]
    wrapped.__ordeal_scenarios__ = list(scenarios or ())  # type: ignore[attr-defined]
    wrapped.__ordeal_state_factory__ = state_factory  # type: ignore[attr-defined]
    wrapped.__ordeal_teardown__ = teardown  # type: ignore[attr-defined]
    wrapped.__ordeal_harness__ = harness  # type: ignore[attr-defined]
    wrapped.__ordeal_lifecycle_phase__ = getattr(reference, "__ordeal_lifecycle_phase__", None)  # type: ignore[attr-defined]
    wrapped.__ordeal_kind__ = kind  # type: ignore[attr-defined]
    wrapped.__ordeal_instance_probe__ = None  # type: ignore[attr-defined]
    return wrapped
