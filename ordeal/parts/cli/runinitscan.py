from __future__ import annotations
# ruff: noqa
def _run_init_scan(modules: Sequence[str], *, max_examples: int = 10) -> dict[str, Any]:
    """Run a bounded, read-only scan over freshly bootstrapped modules."""
    from ordeal.auto import _contract_violation_promoted, scan_module

    deduped_modules = [module for module in dict.fromkeys(modules) if module]
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scanned_modules: list[str] = []
    functions_checked = 0
    skipped_functions = 0

    for module in deduped_modules:
        try:
            runtime_defaults = _resolve_scan_runtime_defaults(
                module,
                requested_examples=max_examples,
                allow_config_override=False,
                resolve_config_imports=False,
            )
            scan_kwargs: dict[str, Any] = {
                "max_examples": runtime_defaults.max_examples,
                "mode": runtime_defaults.mode,
                "seed_from_tests": runtime_defaults.seed_from_tests,
                "seed_from_fixtures": runtime_defaults.seed_from_fixtures,
                "seed_from_docstrings": runtime_defaults.seed_from_docstrings,
                "seed_from_code": runtime_defaults.seed_from_code,
                "seed_from_call_sites": runtime_defaults.seed_from_call_sites,
                "treat_any_as_weak": runtime_defaults.treat_any_as_weak,
                "proof_bundles": runtime_defaults.proof_bundles,
                "require_replayable": runtime_defaults.require_replayable,
                "shell_injection_check": runtime_defaults.shell_injection_check,
                "auto_contracts": runtime_defaults.auto_contracts,
                "min_contract_fit": runtime_defaults.min_contract_fit,
                "min_reachability": runtime_defaults.min_reachability,
                "min_realism": runtime_defaults.min_realism,
                "fixtures": runtime_defaults.fixtures,
                "expected_failures": runtime_defaults.expected_failures,
                "expected_preconditions": runtime_defaults.expected_preconditions,
            }
            if runtime_defaults.object_factories:
                scan_kwargs["object_factories"] = runtime_defaults.object_factories
            if runtime_defaults.object_setups:
                scan_kwargs["object_setups"] = runtime_defaults.object_setups
            if runtime_defaults.object_scenarios:
                scan_kwargs["object_scenarios"] = runtime_defaults.object_scenarios
            if runtime_defaults.object_state_factories:
                scan_kwargs["object_state_factories"] = runtime_defaults.object_state_factories
            if runtime_defaults.object_teardowns:
                scan_kwargs["object_teardowns"] = runtime_defaults.object_teardowns
            if runtime_defaults.object_harnesses:
                scan_kwargs["object_harnesses"] = runtime_defaults.object_harnesses
            if runtime_defaults.contract_checks:
                scan_kwargs["contract_checks"] = runtime_defaults.contract_checks
            if runtime_defaults.ignore_contracts:
                scan_kwargs["ignore_contracts"] = runtime_defaults.ignore_contracts
            if runtime_defaults.ignore_properties:
                scan_kwargs["ignore_properties"] = runtime_defaults.ignore_properties
            if runtime_defaults.ignore_relations:
                scan_kwargs["ignore_relations"] = runtime_defaults.ignore_relations
            if runtime_defaults.contract_overrides:
                scan_kwargs["contract_overrides"] = runtime_defaults.contract_overrides
            if runtime_defaults.expected_properties:
                scan_kwargs["expected_properties"] = runtime_defaults.expected_properties
            if runtime_defaults.expected_relations:
                scan_kwargs["expected_relations"] = runtime_defaults.expected_relations
            if runtime_defaults.property_overrides:
                scan_kwargs["property_overrides"] = runtime_defaults.property_overrides
            if runtime_defaults.relation_overrides:
                scan_kwargs["relation_overrides"] = runtime_defaults.relation_overrides
            result = scan_module(module, **scan_kwargs)
        except Exception as exc:
            errors.append({"module": module, "error": str(exc)})
            continue

        scanned_modules.append(module)
        functions_checked += len(result.functions)
        skipped_functions += len(result.skipped)

        from ordeal.auto import _reportable_crash_category

        for function in result.functions:
            qualname = f"{module}.{function.name}"
            raw_crash_category = getattr(function, "crash_category", None) or "speculative_crash"
            crash_category = _reportable_crash_category(
                category=raw_crash_category,
                replayable=function.replayable,
                proof_bundle=function.proof_bundle,
                sink_categories=function.sink_categories,
            )
            if function.promoted and raw_crash_category == "likely_bug":
                findings.append(
                    {
                        "kind": "crash",
                        "category": "likely_bug",
                        "evidence_class": "candidate_issue",
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": _scan_crash_summary(
                            qualname, "likely_bug", function.replayable
                        ),
                        "error": function.error,
                        "failing_args": function.failing_args,
                        "contract_fit": function.contract_fit,
                        "reachability": function.reachability,
                        "realism": function.realism,
                        "sink_signal": function.sink_signal,
                        "input_source": function.input_source,
                        "proof_bundle": function.proof_bundle,
                    }
                )
            elif not function.execution_ok:
                findings.append(
                    {
                        "kind": "crash",
                        "category": crash_category,
                        "evidence_class": _evidence_class_for_category(crash_category),
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": _scan_crash_summary(
                            qualname, crash_category, function.replayable
                        ),
                        "error": function.error,
                        "failing_args": function.failing_args,
                        "contract_fit": function.contract_fit,
                        "reachability": function.reachability,
                        "realism": function.realism,
                        "sink_signal": function.sink_signal,
                        "input_source": function.input_source,
                        "proof_bundle": function.proof_bundle,
                    }
                )
            for violation in function.property_violations:
                findings.append(
                    {
                        "kind": "property",
                        "category": "speculative_property",
                        "evidence_class": "speculative_property",
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": f"{qualname}: {violation}",
                    }
                )
            for note in function.contract_violation_details:
                findings.append(
                    {
                        **note,
                        "evidence_class": _evidence_class_for_category(
                            str(note.get("category")) if note.get("category") is not None else None
                        ),
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                    }
                )

    if any(
        item.get("category") == "likely_bug"
        or (
            item.get("category") in {"semantic_contract", "lifecycle_contract"}
            and _contract_violation_promoted(item)
        )
        for item in findings
    ):
        status = "findings found"
    elif findings:
        status = "exploratory findings"
    elif scanned_modules or not errors:
        status = "no findings yet"
    else:
        status = "scan unavailable"

    return {
        "status": status,
        "modules": scanned_modules,
        "functions_checked": functions_checked,
        "skipped_functions": skipped_functions,
        "findings": findings,
        "errors": errors,
        "max_examples": max_examples,
        "available_commands": [
            f"ordeal scan {module} --save-artifacts" for module in deduped_modules
        ],
    }
