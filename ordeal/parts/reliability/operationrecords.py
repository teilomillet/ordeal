from __future__ import annotations
# ruff: noqa
def _operation_records(
    module: str,
    state: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    base_ref: str | None,
    allow_service_faults: bool,
) -> list[dict[str, Any]]:
    """Build source-backed operations, seams, hypotheses, and cells."""
    from ordeal.auto import _source_bound_subprocess_match

    changed_files = _changed_files(base_ref)
    sources = _module_sources(module)
    test_roots = sorted({root for _, _, root in sources})
    operations: list[dict[str, Any]] = []
    for source_module, path, _ in sources:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path.as_posix())
        except (OSError, UnicodeError, SyntaxError):
            continue
        schema_definitions = _schema_definitions(tree, path)
        parents: list[str] = []

        def visit(body: Sequence[ast.stmt]) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    if node.name.startswith("_"):
                        continue
                    parents.append(node.name)
                    visit(node.body)
                    parents.pop()
                    continue
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                qualname = ".".join((*parents, node.name))
                selector = qualname
                target = f"{source_module}.{qualname}"
                function_source = _function_source(source, node)
                lowered = function_source.lower()
                calls = {_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)}
                source_bound_subprocesses = sum(
                    _call_name(call) == "subprocess.run"
                    and _source_bound_subprocess_match(call) is not None
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                )
                writes_file = any(
                    _write_mode_open(call) for call in ast.walk(node) if isinstance(call, ast.Call)
                )
                searchable = " ".join((lowered, *sorted(calls)))
                seams = [
                    {
                        "kind": kind,
                        "evidence": f"{_relative_path(path)}:{node.lineno}",
                        "faults": list(faults),
                    }
                    for kind, (tokens, faults) in _SEAM_RULES.items()
                    if any(token in searchable for token in tokens)
                ]
                annotation = _annotation_text(node)
                profiles = [
                    {
                        "kind": kind,
                        "evidence": f"{_relative_path(path)}:{node.lineno}",
                        "fault": fault,
                        "property": property_name,
                    }
                    for kind, (tokens, fault, property_name) in _ML_PROFILE_RULES.items()
                    if any(token in f"{searchable} {annotation}" for token in tokens)
                ]
                if not seams and not profiles:
                    continue
                docstring = ast.get_docstring(node) or ""
                test_evidence = _test_provenance(node.name, test_roots)
                provenance: list[dict[str, Any]] = [
                    {
                        "kind": "source",
                        "evidence": f"{_relative_path(path)}:{node.lineno}",
                    }
                ]
                if annotation:
                    provenance.append({"kind": "types", "evidence": annotation[:300]})
                    for schema_name, location in schema_definitions.items():
                        if re.search(rf"\b{re.escape(schema_name)}\b", annotation):
                            provenance.append({"kind": "schema", "evidence": location})
                if docstring:
                    provenance.append(
                        {
                            "kind": "documentation",
                            "evidence": f"{_relative_path(path)}:{node.lineno}",
                        }
                    )
                if any(isinstance(item, ast.Assert) for item in ast.walk(node)):
                    provenance.append(
                        {
                            "kind": "assertion",
                            "evidence": f"{_relative_path(path)}:{node.lineno}",
                        }
                    )
                provenance.extend(test_evidence)
                function_state = _matching_state(state, qualname, node.name)
                blocker = _surface_blocker(rows, qualname, node.name)
                if function_state is not None and getattr(
                    function_state, "scan_limitation_kind", None
                ):
                    blocker = str(
                        getattr(function_state, "scan_blocking_reason", None)
                        or getattr(function_state, "scan_limitation_kind")
                    )
                relative = _relative_path(path)
                changed = relative in changed_files
                hypotheses: list[dict[str, Any]] = []
                cells: list[dict[str, Any]] = []

                def add_runtime_fault_cell(kind: str, fault: str) -> None:
                    if not _source_fault_probe_supported(
                        fault,
                        calls,
                        writes_file=writes_file,
                        source_bound_subprocesses=source_bound_subprocesses,
                    ):
                        return
                    property_name = _runtime_fault_property(fault)
                    hypotheses.append(
                        {
                            "name": property_name,
                            "epistemic_status": "hypothesis",
                            "profile": "runtime_fault_probe",
                            "provenance": provenance,
                        }
                    )
                    observation = _runtime_cell_status(
                        state,
                        target=target,
                        fault=fault,
                        property_name=property_name,
                    )
                    status, observed_blocker = observation or ("NOT EXERCISED", None)
                    effective_blocker = blocker or observed_blocker
                    experiment = _next_experiment(
                        module=source_module,
                        target=target,
                        selector=selector,
                        seam=kind,
                        status=status,
                        blocker=effective_blocker,
                        base_ref=base_ref,
                        changed=changed,
                        has_tests=bool(test_evidence),
                        allow_service_faults=allow_service_faults,
                        fault=fault,
                        automatable=True,
                    )
                    cells.append(
                        {
                            "id": hashlib.sha256(
                                f"{target}|{kind}|{fault}|{property_name}".encode()
                            ).hexdigest()[:16],
                            "operation": target,
                            "seam": kind,
                            "fault": fault,
                            "property": property_name,
                            "status": status,
                            "blocking_reason": effective_blocker,
                            "next_experiment": experiment,
                        }
                    )

                for seam in seams:
                    property_name = _SEAM_PROPERTIES[seam["kind"]]
                    hypotheses.append(
                        {
                            "name": property_name,
                            "epistemic_status": "hypothesis",
                            "provenance": provenance,
                        }
                    )
                    for fault in seam["faults"]:
                        status, measured_blocker = _cell_status(function_state, fault)
                        effective_blocker = blocker or measured_blocker
                        experiment = _next_experiment(
                            module=source_module,
                            target=target,
                            selector=selector,
                            seam=seam["kind"],
                            status=status,
                            blocker=effective_blocker,
                            base_ref=base_ref,
                            changed=changed,
                            has_tests=bool(test_evidence),
                            allow_service_faults=allow_service_faults,
                        )
                        cells.append(
                            {
                                "id": hashlib.sha256(
                                    f"{target}|{seam['kind']}|{fault}|{property_name}".encode()
                                ).hexdigest()[:16],
                                "operation": target,
                                "seam": seam["kind"],
                                "fault": fault,
                                "property": property_name,
                                "status": status,
                                "blocking_reason": effective_blocker,
                                "next_experiment": experiment,
                            }
                        )
                        add_runtime_fault_cell(str(seam["kind"]), str(fault))
                for profile in profiles:
                    property_name = str(profile["property"])
                    hypotheses.append(
                        {
                            "name": property_name,
                            "epistemic_status": "hypothesis",
                            "profile": profile["kind"],
                            "provenance": provenance,
                        }
                    )
                    status, measured_blocker = _cell_status(function_state, str(profile["fault"]))
                    effective_blocker = blocker or measured_blocker
                    experiment = _next_experiment(
                        module=source_module,
                        target=target,
                        selector=selector,
                        seam=str(profile["kind"]),
                        status=status,
                        blocker=effective_blocker,
                        base_ref=base_ref,
                        changed=changed,
                        has_tests=bool(test_evidence),
                        allow_service_faults=allow_service_faults,
                    )
                    cells.append(
                        {
                            "id": hashlib.sha256(
                                f"{target}|{profile['kind']}|{profile['fault']}|{property_name}".encode()
                            ).hexdigest()[:16],
                            "operation": target,
                            "seam": profile["kind"],
                            "fault": profile["fault"],
                            "property": property_name,
                            "status": status,
                            "blocking_reason": effective_blocker,
                            "next_experiment": experiment,
                        }
                    )
                    add_runtime_fault_cell(str(profile["kind"]), str(profile["fault"]))
                unique_hypotheses = {
                    (item["name"], item.get("profile")): item for item in hypotheses
                }
                operations.append(
                    {
                        "target": target,
                        "selector": selector,
                        "source": f"{relative}:{node.lineno}",
                        "source_sha256": hashlib.sha256(function_source.encode()).hexdigest(),
                        "changed_since_base": changed,
                        "priority": len(cells) + (5 if changed else 0) + (2 if blocker else 0),
                        "seams": seams,
                        "ml_data_profiles": profiles,
                        "candidate_properties": list(unique_hypotheses.values()),
                        "cells": cells,
                    }
                )

        visit(tree.body)
    return sorted(
        operations,
        key=lambda item: (-int(item["priority"]), str(item["target"])),
    )
def _plan_diff(current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare cell identity and status with a previous persisted plan."""
    current_cells = {cell["id"]: cell for cell in current.get("cells", ())}
    previous_cells = {
        cell["id"]: cell for cell in (previous or {}).get("cells", ()) if cell.get("id")
    }
    shared = set(current_cells) & set(previous_cells)
    new_cells = sorted(set(current_cells) - set(previous_cells))
    removed_cells = sorted(set(previous_cells) - set(current_cells))
    status_changes = [
        {
            "id": cell_id,
            "before": previous_cells[cell_id].get("status"),
            "after": current_cells[cell_id].get("status"),
        }
        for cell_id in sorted(shared)
        if previous_cells[cell_id].get("status") != current_cells[cell_id].get("status")
    ]
    current_operations = {
        operation["id"]: operation
        for operation in current.get("operations", ())
        if operation.get("id")
    }
    previous_operations = {
        operation["id"]: operation
        for operation in (previous or {}).get("operations", ())
        if operation.get("id")
    }
    shared_operations = set(current_operations) & set(previous_operations)
    new_operations = sorted(set(current_operations) - set(previous_operations))
    removed_operations = sorted(set(previous_operations) - set(current_operations))
    source_changes = [
        {
            "id": operation_id,
            "target": current_operations[operation_id].get("target"),
            "before_sha256": previous_operations[operation_id].get("source_sha256"),
            "after_sha256": current_operations[operation_id].get("source_sha256"),
        }
        for operation_id in sorted(shared_operations)
        if previous_operations[operation_id].get("source_sha256")
        != current_operations[operation_id].get("source_sha256")
    ]
    bounded_lists = (
        new_cells,
        removed_cells,
        status_changes,
        new_operations,
        removed_operations,
        source_changes,
    )
    return {
        "new_cell_count": len(new_cells),
        "new_cells": new_cells[:50],
        "removed_cell_count": len(removed_cells),
        "removed_cells": removed_cells[:50],
        "status_change_count": len(status_changes),
        "status_changes": status_changes[:50],
        "new_operation_count": len(new_operations),
        "new_operations": new_operations[:50],
        "removed_operation_count": len(removed_operations),
        "removed_operations": removed_operations[:50],
        "source_change_count": len(source_changes),
        "source_changes": source_changes[:50],
        "truncated": any(len(items) > 50 for items in bounded_lists),
        "retained_cells": len(shared),
    }
def _merge_productive_hints(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> dict[str, list[Any]]:
    """Carry prior seed/config hints into the current plan without duplicates."""
    prior = (previous or {}).get("productive_hints", {})
    merged: dict[str, list[Any]] = {}
    for key in ("input_sources", "config_suggestions"):
        values: list[Any] = []
        seen: set[str] = set()
        for value in (*prior.get(key, ()), *current.get(key, ())):
            identity = json.dumps(value, sort_keys=True, default=str)
            if identity in seen:
                continue
            seen.add(identity)
            values.append(value)
        merged[key] = values
    return merged
def _default_reliability_map_path(module: str) -> Path:
    """Return the default persisted plan path for one module."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", module).strip("._") or "project"
    return Path(".ordeal") / "evidence-plans" / f"{safe}.json"
def _load_reliability_map(path: Path) -> dict[str, Any] | None:
    """Read one prior map if it has the supported schema."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if payload.get("schema") == RELIABILITY_MAP_SCHEMA else None
def _write_reliability_map(path: Path, payload: Mapping[str, Any]) -> Path:
    """Persist a deterministic reliability map and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
