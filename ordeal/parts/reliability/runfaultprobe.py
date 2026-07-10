from __future__ import annotations
# ruff: noqa
def _run_fault_probe(
    module: str,
    selector: str,
    fault: str,
    *,
    max_examples: int,
    scan_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one exact fault/no-uncaught-exception observation."""
    from ordeal.auto import (
        _infer_faults,
        _resolve_module,
        _selected_public_functions,
        _source_bound_subprocess_match,
        _unwrap,
        scan_module,
    )

    property_name = _runtime_fault_property(fault)
    target = f"{module}.{selector}"
    observation: dict[str, Any] = {
        "target": target,
        "fault": fault,
        "property": property_name,
        "status": "NOT EXERCISED",
        "blocking_reason": None,
    }
    if not _fault_probe_supported(fault):
        observation["blocking_reason"] = f"no safe Python fault probe is registered for {fault}"
        return observation

    kwargs = dict(scan_kwargs or {})
    try:
        mod = _resolve_module(module)
        selected = _selected_public_functions(
            mod,
            targets=[selector],
            object_factories=kwargs.get("object_factories"),
            object_setups=kwargs.get("object_setups"),
            object_scenarios=kwargs.get("object_scenarios"),
            object_state_factories=kwargs.get("object_state_factories"),
            object_teardowns=kwargs.get("object_teardowns"),
            object_harnesses=kwargs.get("object_harnesses"),
        )
        resolved = _unwrap(selected[0][1]) if len(selected) == 1 else None
        if resolved is not None:
            target = f"{resolved.__module__}.{resolved.__qualname__}"
            observation["target"] = target
        if fault == "timeout" and resolved is not None:
            from ordeal.faults.io import subprocess_timeout

            source = textwrap.dedent(inspect.getsource(inspect.unwrap(resolved)))
            tree = ast.parse(source)
            inferred = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _call_name(node) != "subprocess.run":
                    continue
                command_match = _source_bound_subprocess_match(node)
                if command_match is None:
                    continue
                inferred_fault = subprocess_timeout(command_match)
                setattr(inferred_fault, "__ordeal_operation__", selector)
                setattr(inferred_fault, "__ordeal_fault_kind__", "subprocess_timeout")
                setattr(inferred_fault, "__ordeal_source_match__", command_match)
                setattr(
                    inferred_fault,
                    "__ordeal_source_location__",
                    (node.lineno, node.col_offset),
                )
                inferred.append(inferred_fault)
        else:
            inferred = _infer_faults(
                mod,
                module,
                object_factories=kwargs.get("object_factories"),
                object_setups=kwargs.get("object_setups"),
                object_scenarios=kwargs.get("object_scenarios"),
                object_state_factories=kwargs.get("object_state_factories"),
                object_teardowns=kwargs.get("object_teardowns"),
                object_harnesses=kwargs.get("object_harnesses"),
            )
    except Exception as exc:
        observation["blocking_reason"] = (f"fault discovery failed: {type(exc).__name__}: {exc}")[
            :500
        ]
        return observation

    aliases = _FAULT_PROBE_ALIASES[fault]
    candidates = [
        item
        for item in inferred
        if getattr(item, "__ordeal_operation__", None) == selector
        and getattr(item, "__ordeal_fault_kind__", None) in aliases
    ]
    if not candidates:
        observation["blocking_reason"] = (
            f"source analysis found no injectable {fault} boundary for {target}"
        )
        return observation
    if len(candidates) != 1:
        observation["blocking_reason"] = (
            f"source analysis found {len(candidates)} injectable {fault} boundaries for "
            f"{target}; automatic closure requires exactly one"
        )
        return observation

    injected = candidates[0]
    try:
        injected.reset()
        injected.activate()
        if not injected.active:
            observation["blocking_reason"] = f"the {fault} injection boundary could not activate"
            return observation
        result = scan_module(
            mod,
            targets=[selector],
            max_examples=max_examples,
            **kwargs,
        )
        hits = int(getattr(injected, "observation_hits", 0))
    except Exception as exc:
        observation["blocking_reason"] = (
            f"fault probe failed inside Ordeal: {type(exc).__name__}: {exc}"
        )[:500]
        return observation
    finally:
        injected.deactivate()

    observation["injection"] = {
        "kind": str(getattr(injected, "__ordeal_fault_kind__", fault)),
        "name": str(getattr(injected, "name", fault)),
        "hits": hits,
    }
    if hits == 0:
        observation["blocking_reason"] = (
            "the fault activated but the target did not reach its injection boundary"
        )
        return observation
    if len(result.functions) != 1:
        observation["blocking_reason"] = (
            f"targeted probe returned {len(result.functions)} function results instead of one"
        )
        return observation

    function = result.functions[0]
    observation["evidence"] = {
        "verdict": function.verdict,
        "error_type": function.error_type,
        "error": function.error,
        "replay_attempts": function.replay_attempts,
        "replay_matches": function.replay_matches,
    }
    if function.limitation_kind is not None:
        observation["blocking_reason"] = function.blocking_reason or function.limitation_kind
    elif function.execution_ok and function.verdict != "expected_precondition_failure":
        observation["status"] = "PASS"
    elif function.verdict == "expected_precondition_failure":
        observation["blocking_reason"] = (
            "the operation raised an expected precondition exception; it did not return cleanly"
        )
    elif function.replayable:
        observation["status"] = "FAIL"
    else:
        observation["blocking_reason"] = (
            "the injected fault produced an unreplayed outcome; no cell verdict was promoted"
        )
    return observation
def _build_reliability_map(
    module: str,
    state: Any,
    surface_rows: Sequence[Mapping[str, Any]],
    *,
    base_ref: str | None = None,
    allow_service_faults: bool = False,
    previous_path: Path | None = None,
) -> dict[str, Any]:
    """Build the source-backed reliability map consumed by scan reports."""
    operations = _operation_records(
        module,
        state,
        surface_rows,
        base_ref=base_ref,
        allow_service_faults=allow_service_faults,
    )
    cells = [cell for operation in operations for cell in operation["cells"]]
    counts = {
        status.lower().replace(" ", "_"): sum(1 for cell in cells if cell["status"] == status)
        for status in ("PASS", "NOT EXERCISED", "FAIL")
    }
    counts["blocked"] = sum(1 for cell in cells if cell.get("blocking_reason"))
    safe_experiments = [
        cell["next_experiment"]
        for cell in cells
        if cell["status"] == "NOT EXERCISED"
        and cell["next_experiment"].get("safety") == "safe"
        and cell["next_experiment"].get("auto_runnable")
    ]
    experiment_catalog: dict[str, dict[str, Any]] = {}
    property_catalog: dict[str, dict[str, str]] = {}
    for operation in operations:
        operation_id = hashlib.sha256(str(operation["target"]).encode()).hexdigest()[:16]
        operation["id"] = operation_id
        operation_provenance: set[str] = set()
        test_evidence: set[str] = set()
        property_ids: set[str] = set()
        for candidate in operation["candidate_properties"]:
            for record in candidate.pop("provenance", []):
                kind = str(record.get("kind") or "source")
                operation_provenance.add(kind)
                if kind == "test" and record.get("evidence"):
                    test_evidence.add(str(record["evidence"]))
            property_name = str(candidate["name"])
            property_id = hashlib.sha256(property_name.encode()).hexdigest()[:16]
            property_catalog[property_id] = {
                "id": property_id,
                "name": property_name,
                "epistemic_status": "hypothesis",
            }
            property_ids.add(property_id)
        operation["property_ids"] = sorted(property_ids)
        operation["provenance"] = sorted(operation_provenance)
        operation["test_evidence"] = sorted(test_evidence)
        operation.pop("candidate_properties", None)
        operation["seams"] = sorted({str(item["kind"]) for item in operation["seams"]})
        operation["ml_data_profiles"] = sorted(
            {str(item["kind"]) for item in operation["ml_data_profiles"]}
        )
        for cell in operation["cells"]:
            property_name = str(cell.pop("property"))
            property_id = hashlib.sha256(property_name.encode()).hexdigest()[:16]
            property_catalog[property_id] = {
                "id": property_id,
                "name": property_name,
                "epistemic_status": "hypothesis",
            }
            experiment = dict(cell.pop("next_experiment"))
            encoded = json.dumps(experiment, sort_keys=True)
            experiment_id = hashlib.sha256(encoded.encode()).hexdigest()[:16]
            experiment_catalog[experiment_id] = {
                "id": experiment_id,
                **{
                    key: value
                    for key, value in experiment.items()
                    if key not in {"module", "target", "selector"}
                },
            }
            cell.pop("operation", None)
            cell["operation_id"] = operation_id
            cell["property_id"] = property_id
            cell["next_experiment_id"] = experiment_id
    compact_operations = [
        {key: value for key, value in operation.items() if key != "cells"}
        for operation in operations
    ]
    payload: dict[str, Any] = {
        "schema": RELIABILITY_MAP_SCHEMA,
        "module": module,
        "base_ref": base_ref,
        "service_faults_enabled": allow_service_faults,
        "summary": {
            "operations": len(operations),
            "cells": len(cells),
            **counts,
        },
        "operations": compact_operations,
        "cells": cells,
        "properties": sorted(property_catalog.values(), key=lambda item: item["id"]),
        "experiments": sorted(experiment_catalog.values(), key=lambda item: item["id"]),
        "next_experiment": safe_experiments[0] if safe_experiments else None,
        "reliability_observations": list(
            getattr(state, "supervisor_info", {}).get("reliability_observations", ())
        ),
        "productive_hints": {
            "input_sources": sorted(
                {
                    str(item.get("source"))
                    for function in (getattr(state, "functions", {}) or {}).values()
                    for item in getattr(function, "scan_input_sources", ())
                    if item.get("source")
                }
            ),
            "config_suggestions": list(
                getattr(state, "supervisor_info", {}).get("config_suggestions", ())
            ),
        },
    }
    previous = _load_reliability_map(previous_path) if previous_path is not None else None
    payload["continuity"] = _plan_diff(payload, previous)
    if previous is not None:
        payload["continuity"]["carried_forward_hints"] = previous.get("productive_hints", {})
        payload["productive_hints"] = _merge_productive_hints(
            payload["productive_hints"], previous
        )
    return payload
