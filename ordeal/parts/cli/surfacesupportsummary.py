from __future__ import annotations
# ruff: noqa
def _surface_support_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build support/factory readiness details for one callable row."""
    hints = list(row.get("harness_hints", []))
    needs: list[str] = []
    if row.get("factory_required") and not row.get("factory_configured"):
        needs.append("object_factory")
    if row.get("state_param") and not row.get("state_factory_configured"):
        needs.append("state_factory")
    return {
        "needs": needs,
        "factory": {
            "required": bool(row.get("factory_required")),
            "configured": bool(row.get("factory_configured")),
            "source": row.get("factory_source"),
        },
        "setup": {
            "configured": bool(row.get("setup_configured")),
            "source": row.get("setup_source"),
        },
        "state": {
            "param": row.get("state_param"),
            "required": bool(row.get("state_param")),
            "configured": bool(row.get("state_factory_configured")),
            "source": row.get("state_factory_source"),
        },
        "teardown": {
            "configured": bool(row.get("teardown_configured")),
            "source": row.get("teardown_source"),
        },
        "scenarios": {
            "count": int(row.get("scenario_count", 0) or 0),
            "source": row.get("scenario_source"),
        },
        "harness": {
            "mode": row.get("harness", "fresh"),
            "source": row.get("harness_source"),
            "auto": bool(row.get("auto_harness")),
        },
        "hints": [dict(item) for item in hints],
    }
def _surface_entry_from_listing_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build one first-class surface entry from a callable listing row."""
    hints = list(row.get("harness_hints", []))
    hint_groups = _hint_evidence_groups(hints)
    tests = list(row.get("adjacent_test_files", ()))
    docs = list(row.get("adjacent_doc_files", ()))
    if not tests:
        tests = [item.split(":", 1)[0] for item in hint_groups["tests"]]
    if not docs:
        docs = [item.split(":", 1)[0] for item in hint_groups["docs"]]
    symbol = dict(
        row.get("surface_symbol")
        or _surface_symbol_parts(str(row.get("name", "")), str(row.get("kind", "function")))
    )
    lifecycle_handlers = list(row.get("lifecycle_handlers", ()))
    sink_categories = list(row.get("sink_categories", ()))
    docstring_examples = int(row.get("docstring_examples", 0) or 0)
    docstring_present = bool(row.get("docstring_present"))
    provenance = _surface_provenance_from_row(row, hint_groups)
    return {
        "name": row.get("name"),
        "target": row.get("target"),
        "module": row.get("module"),
        "source_module": row.get("source_module"),
        "source_path": row.get("source_path"),
        "symbol": symbol,
        "visibility": row.get("visibility") or _surface_visibility(str(row.get("name", ""))),
        "async": row.get("async"),
        "selected": bool(row.get("selected", True)),
        "execution": {
            "can_execute_now": bool(row.get("runnable")),
            "blocking_reason": row.get("skip_reason"),
        },
        "support": _surface_support_summary(row),
        "contracts": {
            "checks": list(row.get("contract_checks", [])),
            "sink_categories": sink_categories,
            "lifecycle_phase": row.get("lifecycle_phase"),
            "lifecycle_handlers": lifecycle_handlers,
        },
        "provenance": provenance,
        "evidence": {
            "tests": {
                "files": tests,
                "count": len(tests),
                "supporting_hints": hint_groups["tests"],
            },
            "docs": {
                "files": docs,
                "count": len(docs) + (1 if docstring_present else 0),
                "docstring_present": docstring_present,
                "doctest_examples": docstring_examples,
                "supporting_hints": hint_groups["docs"],
            },
            "support_files": {
                "count": len(hint_groups["other"]),
                "items": hint_groups["other"],
            },
        },
    }
def _build_surface_map(groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a reusable surface map from grouped callable listings."""
    group_maps: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    source_modules: set[str] = set()
    for group in groups:
        module = str(group.get("module", ""))
        rows = list(group.get("targets", []))
        bootstrap_targets = [dict(item) for item in list(group.get("bootstrap_targets", ()))]
        group_entries = [
            dict(row.get("surface") or _surface_entry_from_listing_row(row)) for row in rows
        ]
        entries.extend(group_entries)
        source_modules.update(
            str(entry.get("source_module") or module)
            for entry in group_entries
            if entry.get("name")
        )
        group_provenance = {
            "observed_count": sum(
                int(entry.get("provenance", {}).get("summary", {}).get("observed_count", 0) or 0)
                for entry in group_entries
            ),
            "declared_count": sum(
                int(entry.get("provenance", {}).get("summary", {}).get("declared_count", 0) or 0)
                for entry in group_entries
            ),
            "inferred_count": sum(
                int(entry.get("provenance", {}).get("summary", {}).get("inferred_count", 0) or 0)
                for entry in group_entries
            ),
        }
        group_maps.append(
            {
                "module": module,
                "entry_count": len(group_entries),
                "runnable_count": sum(
                    1
                    for entry in group_entries
                    if entry.get("execution", {}).get("can_execute_now")
                ),
                "blocked_reason": _blocked_callable_listing_reason(rows),
                "bootstrap_targets": bootstrap_targets,
                "provenance": group_provenance,
                "entries": group_entries,
            }
        )

    provenance_summary = {
        "observed_count": sum(
            int(entry.get("provenance", {}).get("summary", {}).get("observed_count", 0) or 0)
            for entry in entries
        ),
        "declared_count": sum(
            int(entry.get("provenance", {}).get("summary", {}).get("declared_count", 0) or 0)
            for entry in entries
        ),
        "inferred_count": sum(
            int(entry.get("provenance", {}).get("summary", {}).get("inferred_count", 0) or 0)
            for entry in entries
        ),
    }
    return {
        "summary": {
            "group_count": len(group_maps),
            "entry_count": len(entries),
            "runnable_count": sum(
                1 for entry in entries if entry.get("execution", {}).get("can_execute_now")
            ),
            "public_count": sum(1 for entry in entries if entry.get("visibility") == "public"),
            "internal_count": sum(1 for entry in entries if entry.get("visibility") == "internal"),
            "async_count": sum(1 for entry in entries if entry.get("async") == "async"),
            "source_module_count": len(source_modules),
            "bootstrap_class_count": sum(
                len(group.get("bootstrap_targets", ())) for group in group_maps
            ),
            "provenance": provenance_summary,
        },
        "groups": group_maps,
        "entries": entries,
    }
def _render_surface_entry_summary(entry: Mapping[str, Any]) -> str:
    """Return a compact human-readable summary for one surface entry."""
    symbol = dict(entry.get("symbol", {}))
    support = dict(entry.get("support", {}))
    contracts = dict(entry.get("contracts", {}))
    evidence = dict(entry.get("evidence", {}))
    provenance = dict(entry.get("provenance", {}))
    provenance_summary = dict(provenance.get("summary", {}))
    tests = dict(evidence.get("tests", {}))
    docs = dict(evidence.get("docs", {}))
    parts = [
        f"{entry.get('visibility', 'public')} {symbol.get('kind', 'function')}",
        (
            "ready=yes"
            if entry.get("execution", {}).get("can_execute_now")
            else f"ready=no[{entry.get('execution', {}).get('blocking_reason')}]"
        ),
    ]
    if support.get("needs"):
        parts.append(f"needs={','.join(support['needs'])}")
    if int(tests.get("count", 0) or 0) > 0:
        parts.append(f"tests={tests.get('count')}")
    if int(docs.get("count", 0) or 0) > 0:
        parts.append(f"docs={docs.get('count')}")
    sink_categories = list(contracts.get("sink_categories", ()))
    if sink_categories:
        parts.append(f"sinks={','.join(sink_categories)}")
    lifecycle_phase = contracts.get("lifecycle_phase")
    if lifecycle_phase:
        parts.append(f"lifecycle={lifecycle_phase}")
    observed_count = int(provenance_summary.get("observed_count", 0) or 0)
    declared_count = int(provenance_summary.get("declared_count", 0) or 0)
    inferred_count = int(provenance_summary.get("inferred_count", 0) or 0)
    if observed_count or declared_count or inferred_count:
        parts.append(f"prov=o{observed_count}/d{declared_count}/i{inferred_count}")
    return "  ".join(parts)
def _callable_listing_rows(
    module_name: str,
    *,
    targets: Sequence[str] | None = None,
    selected_targets: Sequence[str] | None = None,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    contract_checks: Mapping[str, Sequence[Any]] | None = None,
    security_focus: bool = False,
) -> list[dict[str, Any]]:
    """Return stable discovery rows for callable targets in *module_name*."""
    from ordeal.auto import (
        _REGISTERED_OBJECT_FACTORIES,
        _REGISTERED_OBJECT_HARNESSES,
        _REGISTERED_OBJECT_SCENARIOS,
        _REGISTERED_OBJECT_SETUPS,
        _REGISTERED_OBJECT_STATE_FACTORIES,
        _REGISTERED_OBJECT_TEARDOWNS,
        _callable_matches_target_selector,
        _discover_lifecycle_handlers,
        _doctest_seed_examples,
        _infer_sink_categories,
        _infer_strategies,
        _mine_object_harness_hints,
        _resolve_module,
        _resolve_object_harness,
        _resolve_object_hook,
        _selected_public_functions,
        _source_file_for_callable,
        _state_param_name_for_callable,
        _unwrap,
    )

    mod = _resolve_module(module_name)
    candidate_test_files, candidate_doc_files = _module_surface_support_files(
        module_name,
        str(Path.cwd().resolve()),
    )
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
    discovered = _selected_public_functions(
        mod,
        targets=targets,
        include_private=include_private,
        object_factories=merged_factories,
        object_setups=merged_setups,
        object_scenarios=merged_scenarios,
        object_state_factories=merged_state_factories,
        object_teardowns=merged_teardowns,
        object_harnesses=merged_harnesses,
    )
    selectors = [str(raw).strip() for raw in selected_targets or () if str(raw).strip()]

    rows: list[dict[str, Any]] = []
    for name, func in discovered:
        owner, descriptor = _callable_listing_owner(module_name, name)
        kind = _callable_listing_kind(func, owner, descriptor)
        factory_required = kind == "instance"
        factory_source = getattr(func, "__ordeal_factory_source__", None)
        if factory_source is None and factory_required and owner is not None:
            factory_source = (
                "configured" if _resolve_object_hook(owner, merged_factories) else None
            )
        factory_configured = bool(factory_required and factory_source is not None)
        setup_source = getattr(func, "__ordeal_setup_source__", None)
        if setup_source is None and owner is not None:
            setup_source = "configured" if _resolve_object_hook(owner, merged_setups) else None
        setup_configured = bool(setup_source is not None)
        teardown_source = getattr(func, "__ordeal_teardown_source__", None)
        if teardown_source is None and owner is not None:
            teardown_source = (
                "configured" if _resolve_object_hook(owner, merged_teardowns) else None
            )
        teardown_configured = bool(teardown_source is not None)
        resolved_scenario = (
            _resolve_object_hook(owner, merged_scenarios) if owner is not None else None
        )
        if getattr(func, "__ordeal_scenarios__", None):
            resolved_scenario = getattr(func, "__ordeal_scenarios__")
        state_param = (
            str(
                getattr(func, "__ordeal_state_param__", None)
                or (_state_param_name_for_callable(_unwrap(func)) if owner is not None else "")
                or ""
            ).strip()
            or None
        )
        state_factory_source = getattr(func, "__ordeal_state_factory_source__", None)
        if state_factory_source is None and state_param and owner is not None:
            state_factory_source = (
                "configured" if _resolve_object_hook(owner, merged_state_factories) else None
            )
        state_factory_configured = bool(state_param and state_factory_source is not None)
        harness_mode = str(
            getattr(func, "__ordeal_harness__", None)
            or (_resolve_object_harness(owner, merged_harnesses) if owner is not None else "fresh")
        )
        harness_source = getattr(func, "__ordeal_harness_source__", None)
        if harness_source is None and harness_mode != "fresh":
            harness_source = "configured"
        scenario_source = getattr(func, "__ordeal_scenario_source__", None)
        if scenario_source is None and resolved_scenario is not None:
            scenario_source = "configured"
        scenario_count = int(getattr(resolved_scenario, "__ordeal_scenario_count__", 0))
        if (
            scenario_count == 0
            and isinstance(resolved_scenario, Sequence)
            and not isinstance(resolved_scenario, (str, bytes, bytearray))
        ):
            scenario_count = len([item for item in resolved_scenario if item is not None])
        if resolved_scenario is not None and scenario_count == 0:
            scenario_count = 1
        skip_reason = getattr(func, "__ordeal_skip_reason__", None)
        harness_verified = bool(getattr(func, "__ordeal_harness_verified__", True))
        harness_dry_run_error = getattr(func, "__ordeal_harness_dry_run_error__", None)
        inferred_strategies = _infer_strategies(_unwrap(func))
        if factory_required and not factory_configured and not skip_reason:
            skip_reason = "missing object factory"
        if bool(getattr(func, "__ordeal_auto_harness__", False)) and not harness_verified:
            skip_reason = str(harness_dry_run_error or "auto-harness dry-run failed")
        if state_param and not state_factory_configured and inferred_strategies is None:
            skip_reason = skip_reason or "missing state factory"
        if inferred_strategies is None and not skip_reason:
            skip_reason = "missing inferable strategies"
        checks = list(contract_checks.get(name, [])) if contract_checks is not None else []
        harness_hints: list[dict[str, Any]] = []
        if owner is not None and kind == "instance":
            for hint in _mine_object_harness_hints(
                getattr(owner, "__module__", module_name),
                getattr(owner, "__name__", "Owner"),
                name.rsplit(".", 1)[-1],
            )[:5]:
                harness_hints.append(
                    {
                        "kind": hint.kind,
                        "suggestion": hint.suggestion,
                        "evidence": hint.evidence,
                        "confidence": round(float(hint.confidence), 2),
                        "score": round(float(hint.score), 3),
                        "signals": list(hint.signals),
                        "config": hint.config,
                    }
                )
        try:
            source_path = _source_file_for_callable(func)
            display_source_path = _display_path(source_path) if source_path is not None else None
        except Exception:
            display_source_path = None
        try:
            docstring_present = bool(inspect.getdoc(_unwrap(func)) or "")
        except Exception:
            docstring_present = False
        try:
            docstring_examples = len(_doctest_seed_examples(func))
        except Exception:
            docstring_examples = 0
        try:
            sink_categories = _infer_sink_categories(func, security_focus=security_focus)
        except Exception:
            sink_categories = []
        try:
            lifecycle_handlers = (
                _discover_lifecycle_handlers(
                    owner,
                    str(getattr(func, "__ordeal_lifecycle_phase__", "")),
                )
                if owner is not None and getattr(func, "__ordeal_lifecycle_phase__", None)
                else []
            )
        except Exception:
            lifecycle_handlers = []

        row = {
            "module": module_name,
            "source_module": getattr(_unwrap(func), "__module__", module_name),
            "name": name,
            "target": f"{module_name}.{name}",
            "kind": kind,
            "async": _callable_listing_async_state(func),
            "selected": (
                True
                if not selectors
                else any(
                    _callable_matches_target_selector(module_name, name, selector)
                    for selector in selectors
                )
            ),
            "factory_required": factory_required,
            "factory_configured": factory_configured,
            "factory_source": factory_source,
            "setup_configured": setup_configured,
            "setup_source": setup_source,
            "state_param": state_param,
            "state_factory_configured": state_factory_configured,
            "state_factory_source": state_factory_source,
            "teardown_configured": teardown_configured,
            "teardown_source": teardown_source,
            "harness": harness_mode,
            "harness_source": harness_source,
            "scenario_count": scenario_count,
            "scenario_source": scenario_source,
            "auto_harness": bool(getattr(func, "__ordeal_auto_harness__", False)),
            "harness_verified": harness_verified,
            "harness_dry_run_error": harness_dry_run_error,
            "contract_checks": [str(getattr(check, "name", check)) for check in checks],
            "lifecycle_phase": getattr(func, "__ordeal_lifecycle_phase__", None),
            "harness_hints": harness_hints,
            "runnable": skip_reason is None,
            "skip_reason": skip_reason,
            "surface_symbol": _surface_symbol_parts(name, kind),
            "visibility": _surface_visibility(name),
            "source_path": display_source_path,
            "adjacent_test_files": list(candidate_test_files),
            "adjacent_doc_files": list(candidate_doc_files),
            "docstring_present": docstring_present,
            "docstring_examples": docstring_examples,
            "sink_categories": sink_categories,
            "lifecycle_handlers": lifecycle_handlers,
        }
        row["surface"] = _surface_entry_from_listing_row(row)
        rows.append(row)

    unmatched_selectors = [
        selector
        for selector in selectors
        if not any(
            _callable_matches_target_selector(module_name, str(row.get("name", "")), selector)
            for row in rows
        )
    ]
    if unmatched_selectors:
        missing = ", ".join(repr(selector) for selector in unmatched_selectors)
        raise ValueError(
            f"target selector(s) {missing} matched no callables in module {module_name!r}"
        )

    return rows
