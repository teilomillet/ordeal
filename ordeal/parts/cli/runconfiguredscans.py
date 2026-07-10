from __future__ import annotations
# ruff: noqa
def _run_configured_scans(
    scan_entries: Sequence[Any],
    *,
    cfg: OrdealConfig | None = None,
    shared_fixture_registries: Sequence[str] = (),
    verbose: bool = True,
) -> int:
    """Execute ``[[scan]]`` entries from config through the library scan API."""
    from ordeal.auto import scan_module

    exit_code = 0
    for warning in _load_fixture_registry_warnings(shared_modules=shared_fixture_registries):
        _stderr(f"warning: {warning}\n")
    for scan_cfg in scan_entries:
        warnings = _load_fixture_registry_warnings(extra_modules=scan_cfg.fixture_registries)
        for warning in warnings:
            _stderr(f"warning: {warning}\n")
        fixtures = _parse_scan_fixture_specs(scan_cfg.fixtures)
        if verbose:
            _stderr(f"Scanning {scan_cfg.module} from [[scan]]...\n")
        object_factories: dict[str, Any] = {}
        object_setups: dict[str, Any] = {}
        object_scenarios: dict[str, Any] = {}
        object_state_factories: dict[str, Any] = {}
        object_teardowns: dict[str, Any] = {}
        object_harnesses: dict[str, str] = {}
        contract_checks: dict[str, list[Any]] = {}
        if cfg is not None:
            try:
                (
                    object_factories,
                    object_setups,
                    object_scenarios,
                    object_state_factories,
                    object_teardowns,
                    object_harnesses,
                ) = _object_runtime_maps(_config_object_specs_for_module(cfg, scan_cfg.module))
            except Exception as exc:
                _stderr(f"warning: object factory config failed for {scan_cfg.module}: {exc}\n")
            try:
                contract_checks = _config_contract_checks_for_module(cfg, scan_cfg.module)
            except Exception as exc:
                _stderr(f"warning: contract config failed for {scan_cfg.module}: {exc}\n")
        scan_kwargs: dict[str, Any] = {
            "max_examples": scan_cfg.max_examples,
            "mode": scan_cfg.mode,
            "seed_from_tests": scan_cfg.seed_from_tests,
            "seed_from_fixtures": scan_cfg.seed_from_fixtures,
            "seed_from_docstrings": scan_cfg.seed_from_docstrings,
            "seed_from_code": scan_cfg.seed_from_code,
            "seed_from_call_sites": scan_cfg.seed_from_call_sites,
            "treat_any_as_weak": scan_cfg.treat_any_as_weak,
            "proof_bundles": scan_cfg.proof_bundles,
            "shell_injection_check": bool(getattr(scan_cfg, "shell_injection_check", False)),
            "auto_contracts": scan_cfg.auto_contracts,
            "require_replayable": scan_cfg.require_replayable,
            "min_contract_fit": scan_cfg.min_contract_fit,
            "min_reachability": scan_cfg.min_reachability,
            "min_realism": getattr(scan_cfg, "min_realism", 0.55),
            "security_focus": bool(getattr(scan_cfg, "security_focus", False)),
            "targets": scan_cfg.targets,
            "include_private": scan_cfg.include_private,
            "fixtures": fixtures,
            "object_factories": object_factories,
            "object_setups": object_setups,
            "object_scenarios": object_scenarios,
            "object_state_factories": object_state_factories,
            "object_teardowns": object_teardowns,
            "object_harnesses": object_harnesses,
            "expected_failures": scan_cfg.expected_failures,
            "expected_preconditions": scan_cfg.expected_preconditions,
            "ignore_contracts": scan_cfg.ignore_contracts,
            "ignore_properties": scan_cfg.ignore_properties,
            "ignore_relations": scan_cfg.ignore_relations,
            "contract_overrides": scan_cfg.contract_overrides,
            "expected_properties": scan_cfg.expected_properties,
            "expected_relations": scan_cfg.expected_relations,
            "property_overrides": scan_cfg.property_overrides,
            "relation_overrides": scan_cfg.relation_overrides,
            "contract_checks": contract_checks,
        }
        result = scan_module(scan_cfg.module, **scan_kwargs)
        print(result.summary())
        if not result.passed:
            exit_code = 1
    return exit_code
def _scan_prepare(scope: SimpleNamespace) -> int | None:
    try:
        scope.scan_target = _resolve_scan_target(scope.args.target)
    except ValueError as exc:
        if scope.args.json:
            print(
                _build_blocked_agent_envelope(
                    tool="scan",
                    target=str(scope.args.target or "."),
                    summary="scan target could not be detected",
                    blocking_reason=str(exc),
                    suggested_commands=("ordeal scan myapp.scoring",),
                    raw_details={"requested_target": scope.args.target},
                ).to_json()
            )
            return 2
        _stderr(f"Cannot start scan: {exc}.\n")
        return 2
    scope.module_name = _scan_base_module(scope.scan_target)
    scope.scan_started = _time.monotonic()
    scope.reliability_base_ref = (
        str(getattr(scope.args, "base_ref", None) or os.environ.get("ORDEAL_BASE_REF", "")).strip()
        or None
    )
    if scope.reliability_base_ref is not None:
        from ordeal.reliability import _base_ref_error

        base_ref_error = _base_ref_error(scope.reliability_base_ref)
        if base_ref_error is not None:
            if scope.args.json:
                print(
                    _build_blocked_agent_envelope(
                        tool="scan",
                        target=scope.scan_target,
                        summary="changed-code prioritization is blocked",
                        blocking_reason=base_ref_error,
                        suggested_commands=("git fetch origin",),
                        raw_details={"base_ref": scope.reliability_base_ref},
                    ).to_json()
                )
            else:
                _stderr(f"Cannot prioritize changed code: {base_ref_error}.\n")
            return 2
    if getattr(scope.args, "deepen", False) and scope.args.time_limit is None:
        scope.reason = "--deepen requires an explicit --time-limit safety budget"
        if scope.args.json:
            print(
                _build_blocked_agent_envelope(
                    tool="scan",
                    target=scope.scan_target,
                    summary="automatic deepening needs an explicit budget",
                    blocking_reason=scope.reason,
                    suggested_commands=(
                        f"ordeal scan {scope.scan_target} --deepen --time-limit 60",
                    ),
                ).to_json()
            )
            return 2
        _stderr(f"Cannot deepen scan: {scope.reason}.\n")
        return 2
    allow_config_override = scope.args.max_examples == 50
    scope.runtime_defaults = _resolve_scan_runtime_defaults(
        scope.scan_target,
        requested_examples=scope.args.max_examples,
        allow_config_override=allow_config_override,
        resolve_config_imports=not bool(getattr(scope.args, "list_targets", False)),
    )
    scope.scan_mode = _public_scan_mode(
        str(getattr(scope.args, "mode", None) or scope.runtime_defaults.mode)
    )
    scope.scan_seed_from_tests = (
        scope.runtime_defaults.seed_from_tests
        if getattr(scope.args, "seed_from_tests", None) is None
        else bool(scope.args.seed_from_tests)
    )
    scope.scan_min_contract_fit = float(
        getattr(scope.args, "min_contract_fit", None)
        if getattr(scope.args, "min_contract_fit", None) is not None
        else scope.runtime_defaults.min_contract_fit
    )
    scope.scan_min_reachability = float(
        getattr(scope.args, "min_reachability", None)
        if getattr(scope.args, "min_reachability", None) is not None
        else scope.runtime_defaults.min_reachability
    )
    scope.scan_min_realism = float(
        getattr(scope.args, "min_realism", None)
        if getattr(scope.args, "min_realism", None) is not None
        else scope.runtime_defaults.min_realism
    )
    scope.scan_security_focus = (
        scope.runtime_defaults.security_focus
        if getattr(scope.args, "security_focus", None) is None
        else bool(scope.args.security_focus)
    )
    explicit_target = ":" in scope.scan_target
    cli_target_selectors = _scan_target_selectors(scope.args)
    if explicit_target and cli_target_selectors:
        _stderr("Cannot combine an explicit callable target with --target selectors.\n")
        return 2
    scope.scan_targets = (
        [scope.scan_target]
        if explicit_target
        else list(cli_target_selectors or scope.runtime_defaults.targets)
    )
    scope.inc_private = bool(
        getattr(scope.args, "include_private", False) or scope.runtime_defaults.include_private
    )
    scope.scan_ignore_properties = _merge_unique_strings(
        scope.runtime_defaults.ignore_properties, getattr(scope.args, "ignore_properties", None)
    )
    scope.scan_ignore_relations = _merge_unique_strings(
        scope.runtime_defaults.ignore_relations, getattr(scope.args, "ignore_relations", None)
    )
    scope.scan_property_overrides = _merge_named_overrides(
        scope.runtime_defaults.property_overrides,
        _named_override_specs_to_map(getattr(scope.args, "cli_property_overrides", None)),
    )
    scope.scan_relation_overrides = _merge_named_overrides(
        scope.runtime_defaults.relation_overrides,
        _named_override_specs_to_map(getattr(scope.args, "cli_relation_overrides", None)),
    )
    try:
        scope.scan_target_rows = _callable_listing_rows(
            scope.module_name,
            targets=[scope.scan_target] if explicit_target else None,
            selected_targets=scope.scan_targets,
            include_private=scope.inc_private,
            object_factories=scope.runtime_defaults.object_factories,
            object_setups=scope.runtime_defaults.object_setups,
            object_scenarios=scope.runtime_defaults.object_scenarios,
            object_state_factories=scope.runtime_defaults.object_state_factories,
            object_teardowns=scope.runtime_defaults.object_teardowns,
            object_harnesses=scope.runtime_defaults.object_harnesses,
            contract_checks=scope.runtime_defaults.contract_checks,
            security_focus=scope.scan_security_focus,
        )
    except Exception as exc:
        scope.scan_target_rows = []
        if getattr(scope.args, "list_targets", False):
            if scope.args.json:
                print(
                    _build_blocked_agent_envelope(
                        tool="scan",
                        target=scope.scan_target,
                        summary="cannot resolve callable target metadata",
                        blocking_reason=f"target metadata resolution failed: {exc}",
                        raw_details={"target": scope.scan_target, "error": str(exc)},
                    ).to_json()
                )
                return 1
            _stderr(f"Target metadata resolution failed: {exc}\n")
            return 1
    scope.selected_scan_rows = [
        row for row in scope.scan_target_rows if bool(row.get("selected", True))
    ] or scope.scan_target_rows
    scope.sampling: dict[str, Any] | None = None
    if (
        not explicit_target
        and (not cli_target_selectors)
        and (not scope.runtime_defaults.targets)
        and (not getattr(scope.args, "list_targets", False))
    ):
        scope.sampling = _package_root_scan_sample(scope.module_name, scope.selected_scan_rows)
        if scope.sampling is not None:
            sampled_targets = list(scope.sampling.get("targets", ()))
            sampled_names = set(sampled_targets)
            scope.selected_scan_rows = [
                row
                for row in scope.selected_scan_rows
                if str(row.get("name", "")).strip() in sampled_names
            ]
            scope.scan_targets = sampled_targets
    scope.scan_notes: list[str] = []
    scope.scan_max_examples = int(scope.runtime_defaults.max_examples)
    scope.scan_seed_from_call_sites = scope.runtime_defaults.seed_from_call_sites
    broad_package_root_scan = scope.sampling is not None
    if scope.sampling is not None:
        scope.scan_notes.append(
            f"Package-root scan sampled {scope.sampling['sampled']}/{scope.sampling['total_runnable']} runnable exports across {scope.sampling['source_modules']} source module(s); use --list-targets or --target for exhaustive coverage."
        )
    if broad_package_root_scan and scope.scan_seed_from_call_sites:
        scope.scan_seed_from_call_sites = False
        scope.scan_notes.append(
            "Broad package-root scan disabled call-site seed mining for speed; use --target for deeper realism."
        )
    if (
        broad_package_root_scan
        and scope.args.max_examples == 50
        and (scope.runtime_defaults.max_examples == 50)
        and (scope.scan_max_examples > _BROAD_PACKAGE_SCAN_DEFAULT_MAX_EXAMPLES)
    ):
        scope.scan_max_examples = _BROAD_PACKAGE_SCAN_DEFAULT_MAX_EXAMPLES
        scope.scan_notes.append(
            f"Broad package-root scan capped max_examples to {scope.scan_max_examples} per target; pass -n or use --target for a deeper scan."
        )
    if getattr(scope.args, "list_targets", False):
        scope.config_suggestions = _scan_config_suggestions(
            scope.module_name,
            mode=scope.scan_mode,
            max_examples=scope.scan_max_examples,
            scan_targets=scope.scan_targets,
            include_private=scope.inc_private,
            seed_from_call_sites=scope.runtime_defaults.seed_from_call_sites,
            min_contract_fit=scope.scan_min_contract_fit,
            min_reachability=scope.scan_min_reachability,
            min_realism=scope.scan_min_realism,
            security_focus=scope.scan_security_focus,
            ignore_properties=scope.scan_ignore_properties,
            ignore_relations=scope.scan_ignore_relations,
            auto_contracts=scope.runtime_defaults.auto_contracts,
            sampling=None,
            rows=scope.selected_scan_rows,
            details=(),
        )
        groups = [{"module": scope.module_name, "targets": scope.scan_target_rows}]
        if getattr(scope.args, "json", False):
            print(
                _build_target_listing_envelope(
                    tool="scan",
                    target=scope.scan_target,
                    groups=groups,
                    warnings=scope.runtime_defaults.registry_warnings,
                    config_suggestions=scope.config_suggestions,
                ).to_json()
            )
        else:
            print(
                _render_target_listing_text(
                    f"Callable targets for {scope.scan_target}",
                    groups,
                    warnings=scope.runtime_defaults.registry_warnings,
                    config_suggestions=scope.config_suggestions,
                )
            )
        return 0
    if scope.selected_scan_rows and (
        blocking_reason := _blocked_callable_listing_reason(
            scope.selected_scan_rows, threshold=scope.runtime_defaults.min_fixture_completeness
        )
    ):
        if scope.args.json:
            print(
                _build_blocked_agent_envelope(
                    tool="scan",
                    target=scope.scan_target,
                    summary="scan blocked before exploration",
                    blocking_reason=blocking_reason,
                    suggested_commands=(f"ordeal scan {scope.scan_target} --list-targets",),
                    raw_details={
                        "target": scope.scan_target,
                        "module": scope.module_name,
                        "targets": scope.selected_scan_rows,
                        "warnings": list(scope.runtime_defaults.registry_warnings),
                    },
                ).to_json()
            )
            return 1
        for warning in scope.runtime_defaults.registry_warnings:
            _stderr(f"warning: {warning}\n")
        _stderr(f"Scan blocked: {blocking_reason}\n")
        _stderr(f"  Inspect targets with: ordeal scan {scope.scan_target} --list-targets\n")
        return 1
    if not scope.args.json:
        _stderr(f"Scanning {scope.scan_target} (seed={scope.args.seed})...\n")
        for warning in scope.runtime_defaults.registry_warnings:
            _stderr(f"warning: {warning}\n")
        for note in scope.scan_notes:
            _stderr(f"note: {note}\n")
    return None
def _scan_explore(scope: SimpleNamespace) -> int | None:
    from ordeal.state import explore

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        scope.state = explore(
            scope.module_name,
            seed=scope.args.seed,
            max_examples=scope.scan_max_examples,
            workers=scope.args.workers,
            time_limit=scope.args.time_limit,
            include_private=scope.inc_private,
            scan_targets=scope.scan_targets,
            scan_fixtures=scope.runtime_defaults.fixtures,
            scan_object_factories=scope.runtime_defaults.object_factories,
            scan_object_setups=scope.runtime_defaults.object_setups,
            scan_object_scenarios=scope.runtime_defaults.object_scenarios,
            scan_object_state_factories=scope.runtime_defaults.object_state_factories,
            scan_object_teardowns=scope.runtime_defaults.object_teardowns,
            scan_object_harnesses=scope.runtime_defaults.object_harnesses,
            scan_expected_failures=scope.runtime_defaults.expected_failures,
            scan_expected_preconditions=scope.runtime_defaults.expected_preconditions,
            scan_ignore_contracts=scope.runtime_defaults.ignore_contracts,
            scan_ignore_properties=scope.scan_ignore_properties,
            scan_ignore_relations=scope.scan_ignore_relations,
            scan_contract_overrides=scope.runtime_defaults.contract_overrides,
            scan_expected_properties=scope.runtime_defaults.expected_properties,
            scan_expected_relations=scope.runtime_defaults.expected_relations,
            scan_property_overrides=scope.scan_property_overrides,
            scan_relation_overrides=scope.scan_relation_overrides,
            scan_contract_checks=scope.runtime_defaults.contract_checks,
            scan_mode=scope.scan_mode,
            scan_seed_from_tests=scope.scan_seed_from_tests,
            scan_seed_from_fixtures=scope.runtime_defaults.seed_from_fixtures,
            scan_seed_from_docstrings=scope.runtime_defaults.seed_from_docstrings,
            scan_seed_from_code=scope.runtime_defaults.seed_from_code,
            scan_seed_from_call_sites=scope.scan_seed_from_call_sites,
            scan_treat_any_as_weak=scope.runtime_defaults.treat_any_as_weak,
            scan_proof_bundles=scope.runtime_defaults.proof_bundles,
            scan_require_replayable=scope.runtime_defaults.require_replayable,
            scan_auto_contracts=scope.runtime_defaults.auto_contracts,
            scan_min_contract_fit=scope.scan_min_contract_fit,
            scan_min_reachability=scope.scan_min_reachability,
            scan_min_realism=scope.scan_min_realism,
            scan_security_focus=scope.scan_security_focus,
            scan_minimize_findings=bool(getattr(scope.args, "save_artifacts", False)),
            run_mine=False,
            run_scan=not bool(getattr(scope.args, "evidence_fault", None)),
            run_mutate=False,
            run_chaos=False,
        )
    scope.config_suggestions = _scan_config_suggestions(
        scope.module_name,
        mode=scope.scan_mode,
        max_examples=scope.scan_max_examples,
        scan_targets=scope.scan_targets,
        include_private=scope.inc_private,
        seed_from_call_sites=scope.scan_seed_from_call_sites,
        min_contract_fit=scope.scan_min_contract_fit,
        min_reachability=scope.scan_min_reachability,
        min_realism=scope.scan_min_realism,
        security_focus=scope.scan_security_focus,
        ignore_properties=scope.scan_ignore_properties,
        ignore_relations=scope.scan_ignore_relations,
        auto_contracts=scope.runtime_defaults.auto_contracts,
        sampling=scope.sampling,
        rows=scope.selected_scan_rows,
        details=_scan_report_details(scope.state),
    )
    support_suggestions = _scan_support_suggestions(scope.module_name, scope.selected_scan_rows)
    scenario_libraries = _scan_scenario_library_records(
        scope.module_name, scope.selected_scan_rows
    )
    if scope.sampling is not None:
        scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
        scope.state.supervisor_info["scan_sampling"] = dict(scope.sampling)
    if scope.scan_notes:
        scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
        scope.state.supervisor_info["scan_scope_notes"] = list(scope.scan_notes)
    if scope.config_suggestions:
        scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
        scope.state.supervisor_info["config_suggestions"] = scope.config_suggestions
    if support_suggestions:
        scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
        scope.state.supervisor_info["support_suggestions"] = support_suggestions
    if scenario_libraries:
        scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
        scope.state.supervisor_info["scenario_libraries"] = scenario_libraries
    return None
