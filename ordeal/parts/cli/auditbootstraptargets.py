from __future__ import annotations
# ruff: noqa
def _audit_bootstrap_targets(
    module_name: str,
    *,
    include_private: bool = False,
    test_dir: str = "tests",
) -> list[dict[str, Any]]:
    """Return class-level review scaffolds when callable discovery is empty."""
    from ordeal.auto import (
        _mine_object_harness_hints,
        _resolve_module,
        _state_param_name_for_callable,
    )

    mod = _resolve_module(module_name)
    support_module = _bootstrap_support_module_name(test_dir)
    targets: list[dict[str, Any]] = []
    for class_name, obj in sorted(vars(mod).items()):
        if not inspect.isclass(obj) or getattr(obj, "__module__", None) != mod.__name__:
            continue
        if class_name.startswith("_") and not include_private:
            continue
        methods: list[str] = []
        scenario_labels: set[str] = set()
        field_hints: dict[str, tuple[float, Any]] = {}
        evidence: list[str] = []
        requires_state = False
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
            methods.append(meth_name)
            scenario_labels.update(_bootstrap_review_scenarios_for_method(obj, meth_name))
            with contextlib.suppress(Exception):
                if _state_param_name_for_callable(getattr(obj, meth_name)):
                    requires_state = True
            for hint in _mine_object_harness_hints(module_name, class_name, meth_name):
                config = getattr(hint, "config", {})
                if (
                    not isinstance(config, Mapping)
                    or str(config.get("section", "")) != "[[objects]]"
                ):
                    continue
                key = str(config.get("key", "")).strip()
                if not key:
                    continue
                value = _normalize_hint_config_value(config.get("value"))
                if key == "scenarios":
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, str) and item.strip():
                            scenario_labels.add(str(item).strip())
                else:
                    current = field_hints.get(key)
                    confidence = float(getattr(hint, "confidence", 0.0))
                    if current is None or confidence > current[0]:
                        field_hints[key] = (confidence, value)
                evidence_text = str(getattr(hint, "evidence", "")).strip()
                if evidence_text and evidence_text not in evidence:
                    evidence.append(evidence_text)
        if not methods:
            continue
        class_snake = _camel_to_snake(class_name)
        support_factory = f"{support_module}:make_{class_snake}"
        support_setup = (
            f"{support_module}:prime_{class_snake}"
            if {"setup", "teardown"} & set(field_hints)
            or any(
                method.startswith(("post_", "setup_", "rollout", "cleanup", "teardown"))
                for method in methods
            )
            else None
        )
        support_state_factory = (
            f"{support_module}:make_{class_snake}_state" if requires_state else None
        )
        support_teardown = (
            f"{support_module}:cleanup_{class_snake}"
            if "teardown" in field_hints
            or any(method.startswith(("cleanup", "teardown", "post_")) for method in methods)
            else None
        )
        review_scenarios = sorted(
            {
                label
                for label in scenario_labels
                if label
                in {
                    "space_paths",
                    "quote_paths",
                    "empty_instruction",
                    "no_system_prompt",
                    "missing_log_file",
                }
            }
        )
        support_scenarios = [f"{support_module}:scenario_{label}" for label in review_scenarios]
        targets.append(
            {
                "module": module_name,
                "class_name": class_name,
                "target": f"{module_name}:{class_name}",
                "methods": methods,
                "method_count": len(methods),
                "review_scenarios": review_scenarios,
                "support_module": support_module,
                "support_path": _bootstrap_support_file_path(test_dir),
                "support_factory": support_factory,
                "support_setup": support_setup,
                "support_state_factory": support_state_factory,
                "support_teardown": support_teardown,
                "support_scenarios": support_scenarios,
                "harness": "stateful" if requires_state else "fresh",
                "evidence": evidence[:5],
            }
        )
    return targets
def _package_root_scan_sample(
    module_name: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int = _PACKAGE_ROOT_SCAN_LIMIT,
) -> dict[str, Any] | None:
    """Return a bounded representative target sample for broad package-root scans."""
    from ordeal.auto import _resolve_module

    try:
        mod = _resolve_module(module_name)
    except Exception:
        return None
    if not getattr(mod, "__path__", None):
        return None

    runnable_rows = [row for row in rows if bool(row.get("runnable", True))]
    if len(runnable_rows) <= limit:
        return None

    chosen: list[str] = []
    seen_sources: set[str] = set()
    deferred: list[str] = []

    for row in sorted(runnable_rows, key=_package_root_scan_priority):
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        source_module = str(row.get("source_module") or row.get("module") or module_name)
        if source_module not in seen_sources and len(chosen) < limit:
            chosen.append(name)
            seen_sources.add(source_module)
        else:
            deferred.append(name)

    if len(chosen) < limit:
        for name in deferred:
            if name in chosen:
                continue
            chosen.append(name)
            if len(chosen) >= limit:
                break

    if len(chosen) >= len(runnable_rows):
        return None

    return {
        "kind": "package_root_sample",
        "module": module_name,
        "limit": limit,
        "sampled": len(chosen),
        "total_runnable": len(runnable_rows),
        "source_modules": len(
            {
                str(row.get("source_module") or row.get("module") or module_name)
                for row in runnable_rows
            }
        ),
        "targets": chosen,
    }
def _resolve_symbol_path(path: str) -> Any:
    """Resolve ``module:attr`` or dotted import paths into Python objects."""
    import importlib
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
def _callable_listing_owner(module_name: str, target_name: str) -> tuple[Any | None, Any | None]:
    """Return the class owner and descriptor for ``Class.method`` targets."""
    from ordeal.auto import _resolve_module

    parts = [part for part in target_name.split(".") if part]
    if len(parts) < 2:
        return None, None

    owner: Any = _resolve_module(module_name)
    for part in parts[:-1]:
        try:
            owner = getattr(owner, part)
        except AttributeError:
            return None, None

    if not inspect.isclass(owner):
        return None, None

    try:
        descriptor = inspect.getattr_static(owner, parts[-1])
    except AttributeError:
        return None, None
    return owner, descriptor
def _callable_listing_kind(func: Any, owner: Any | None, descriptor: Any | None) -> str:
    """Return the callable kind for a discovered target."""
    kind = getattr(func, "__ordeal_kind__", None)
    if kind in {"function", "instance", "class", "static"}:
        return str(kind)
    if owner is not None:
        if isinstance(descriptor, staticmethod):
            return "static"
        if isinstance(descriptor, classmethod):
            return "class"
        if inspect.isfunction(descriptor):
            return "instance"
    if inspect.ismethod(func) and inspect.isclass(getattr(func, "__self__", None)):
        return "class"
    return "function"
def _callable_listing_async_state(func: Any) -> str:
    """Return ``async`` for coroutine targets, including wrapped callables."""
    candidate = func
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        if inspect.iscoroutinefunction(candidate):
            return "async"
        seen.add(id(candidate))
        candidate = getattr(candidate, "__wrapped__", None)
    return "sync"
def _surface_symbol_parts(name: str, kind: str) -> dict[str, Any]:
    """Return normalized symbol metadata for one callable listing name."""
    parts = [part for part in str(name).split(".") if part]
    if kind in {"instance", "class", "static"} and len(parts) >= 2:
        owner = ".".join(parts[:-1])
        member = parts[-1]
        top_level = parts[0]
        surface_kind = "method"
    else:
        owner = None
        member = parts[-1] if parts else str(name)
        top_level = member
        surface_kind = "function"
    return {
        "qualname": str(name),
        "kind": surface_kind,
        "top_level": top_level,
        "owner": owner,
        "member": member,
    }
def _surface_visibility(name: str) -> str:
    """Return the public/internal visibility label for one callable."""
    return (
        "internal"
        if any(part.startswith("_") for part in str(name).split(".") if part)
        else "public"
    )
def _hint_evidence_groups(harness_hints: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Bucket harness-hint evidence into tests, docs, and other support files."""
    buckets: dict[str, list[str]] = {"tests": [], "docs": [], "other": []}
    for hint in harness_hints:
        evidence = str(hint.get("evidence", "")).strip()
        if not evidence:
            continue
        path_part = evidence.split(":", 1)[0]
        lowered = path_part.lower().replace("\\", "/")
        bucket = "other"
        if lowered.endswith(".md"):
            bucket = "docs"
        elif lowered.endswith(".py") and (
            lowered.startswith("tests/") or "/tests/" in lowered or lowered.endswith("conftest.py")
        ):
            bucket = "tests"
        if evidence not in buckets[bucket]:
            buckets[bucket].append(evidence)
    return buckets
@functools.lru_cache(maxsize=128)
def _module_surface_support_files(
    module_name: str,
    workspace_root: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return candidate adjacent test and doc files for one module."""
    del workspace_root
    from ordeal.auto import _candidate_seed_files, _harness_doc_files

    test_files, _project_files = _candidate_seed_files(module_name)
    doc_files = _harness_doc_files(module_name)
    return (
        tuple(_display_path(path) for path in test_files[:6]),
        tuple(_display_path(path) for path in doc_files[:6]),
    )
def _add_surface_provenance_item(
    bucket: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    kind: str,
    detail: Any,
) -> None:
    """Append one normalized provenance item when it adds new information."""
    rendered = str(detail or "").strip()
    key = (str(kind), rendered)
    if not rendered or key in seen:
        return
    seen.add(key)
    bucket.append({"kind": str(kind), "detail": rendered})
def _surface_provenance_from_row(
    row: Mapping[str, Any],
    hint_groups: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Build lean provenance buckets for one surface entry."""
    observed: list[dict[str, str]] = []
    declared: list[dict[str, str]] = []
    inferred: list[dict[str, str]] = []
    observed_seen: set[tuple[str, str]] = set()
    declared_seen: set[tuple[str, str]] = set()
    inferred_seen: set[tuple[str, str]] = set()

    if source_path := row.get("source_path"):
        _add_surface_provenance_item(
            observed,
            observed_seen,
            kind="source_file",
            detail=source_path,
        )
    for path in list(row.get("adjacent_test_files", ())):
        _add_surface_provenance_item(
            observed,
            observed_seen,
            kind="test_file",
            detail=path,
        )
    for path in list(row.get("adjacent_doc_files", ())):
        _add_surface_provenance_item(
            observed,
            observed_seen,
            kind="doc_file",
            detail=path,
        )
    for evidence in hint_groups.get("tests", ()):
        _add_surface_provenance_item(
            observed,
            observed_seen,
            kind="test_support",
            detail=evidence,
        )
    for evidence in hint_groups.get("docs", ()):
        _add_surface_provenance_item(
            observed,
            observed_seen,
            kind="doc_support",
            detail=evidence,
        )
    for evidence in hint_groups.get("other", ()):
        _add_surface_provenance_item(
            observed,
            observed_seen,
            kind="support_file",
            detail=evidence,
        )

    for kind, source in (
        ("factory", row.get("factory_source")),
        ("setup", row.get("setup_source")),
        ("state_factory", row.get("state_factory_source")),
        ("teardown", row.get("teardown_source")),
        ("harness", row.get("harness_source")),
        ("scenario", row.get("scenario_source")),
    ):
        source_text = str(source or "").strip()
        if not source_text:
            continue
        bucket = declared if source_text == "configured" else observed
        seen = declared_seen if source_text == "configured" else observed_seen
        _add_surface_provenance_item(
            bucket,
            seen,
            kind=kind,
            detail=source_text,
        )

    for check_name in list(row.get("contract_checks", ())):
        _add_surface_provenance_item(
            declared,
            declared_seen,
            kind="contract_check",
            detail=check_name,
        )

    if bool(row.get("docstring_present")):
        _add_surface_provenance_item(
            inferred,
            inferred_seen,
            kind="docstring",
            detail="present",
        )
    docstring_examples = int(row.get("docstring_examples", 0) or 0)
    if docstring_examples > 0:
        _add_surface_provenance_item(
            inferred,
            inferred_seen,
            kind="doctest_examples",
            detail=str(docstring_examples),
        )
    for sink in list(row.get("sink_categories", ())):
        _add_surface_provenance_item(
            inferred,
            inferred_seen,
            kind="sink_category",
            detail=sink,
        )
    if lifecycle_phase := row.get("lifecycle_phase"):
        _add_surface_provenance_item(
            inferred,
            inferred_seen,
            kind="lifecycle_phase",
            detail=lifecycle_phase,
        )
    for handler in list(row.get("lifecycle_handlers", ())):
        _add_surface_provenance_item(
            inferred,
            inferred_seen,
            kind="lifecycle_handler",
            detail=handler,
        )

    primary = (
        "observed"
        if observed
        else ("declared" if declared else ("inferred" if inferred else "none"))
    )
    return {
        "primary_basis": primary,
        "observed": observed,
        "declared": declared,
        "inferred": inferred,
        "summary": {
            "observed_count": len(observed),
            "declared_count": len(declared),
            "inferred_count": len(inferred),
        },
    }
