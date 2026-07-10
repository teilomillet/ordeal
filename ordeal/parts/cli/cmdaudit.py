from __future__ import annotations
# ruff: noqa
def _cmd_audit(args: argparse.Namespace) -> int:
    """Run ordeal audit on specified modules."""
    from types import SimpleNamespace

    from ordeal.audit import audit

    try:
        cfg = _load_optional_config(getattr(args, "config", None))
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    audit_cfg = cfg.audit if cfg is not None else None
    modules = [_normalize_module_target(module) for module in list(args.modules or [])]
    if not modules and audit_cfg is not None:
        modules = [_normalize_module_target(module) for module in audit_cfg.modules]
    target_specs_by_module: dict[str, dict[str, Any]] = {}

    def _merge_target_spec(spec: Any) -> None:
        target = _normalize_module_target(str(getattr(spec, "target")))
        base_module = _scan_base_module(target)
        bucket = target_specs_by_module.setdefault(base_module, {})
        existing = bucket.get(target)
        if existing is None:
            bucket[target] = spec
            return
        bucket[target] = SimpleNamespace(
            target=target,
            factory=getattr(spec, "factory", None) or getattr(existing, "factory", None),
            setup=getattr(spec, "setup", None) or getattr(existing, "setup", None),
            state_factory=(
                getattr(spec, "state_factory", None) or getattr(existing, "state_factory", None)
            ),
            teardown=getattr(spec, "teardown", None) or getattr(existing, "teardown", None),
            harness=(
                str(getattr(spec, "harness", "") or "").strip()
                or str(getattr(existing, "harness", "fresh") or "fresh").strip()
            ),
            scenarios=list(
                dict.fromkeys(
                    [
                        *list(getattr(existing, "scenarios", []) or []),
                        *list(getattr(spec, "scenarios", []) or []),
                    ]
                )
            ),
            methods=list(getattr(spec, "methods", None) or getattr(existing, "methods", []) or []),
            include_private=bool(
                getattr(spec, "include_private", False)
                or getattr(existing, "include_private", False)
            ),
        )

    if cfg is not None:
        for spec in cfg.objects:
            _merge_target_spec(spec)
    if audit_cfg is not None:
        for spec in audit_cfg.targets:
            _merge_target_spec(spec)

    normalized_target_specs_by_module = {
        module: list(specs.values()) for module, specs in target_specs_by_module.items()
    }
    target_names = modules or list(normalized_target_specs_by_module)
    if not target_names and not normalized_target_specs_by_module:
        _stderr(
            "No modules or audit targets specified. Configure [audit].modules "
            "or [[audit.targets]].\n"
        )
        return 2

    test_dir = str(
        _cli_or_config(
            getattr(args, "test_dir", None),
            audit_cfg.test_dir if audit_cfg else "tests",
        )
    )
    max_examples = int(
        _cli_or_config(
            getattr(args, "max_examples", None),
            audit_cfg.max_examples if audit_cfg else 20,
        )
    )
    workers = int(
        _cli_or_config(
            getattr(args, "workers", None),
            audit_cfg.workers if audit_cfg else 1,
        )
    )
    min_fixture_completeness = float(
        _cli_or_config(
            getattr(args, "min_fixture_completeness", None),
            audit_cfg.min_fixture_completeness if audit_cfg else 0.0,
        )
    )
    validation_mode = str(
        _cli_or_config(
            getattr(args, "validation_mode", None),
            audit_cfg.validation_mode if audit_cfg else "fast",
        )
    )
    show_generated = bool(
        _cli_or_config(
            getattr(args, "show_generated", None),
            audit_cfg.show_generated if audit_cfg else False,
        )
    )
    save_generated = _cli_or_config(
        getattr(args, "save_generated", None),
        audit_cfg.save_generated if audit_cfg else None,
    )
    write_gaps = _cli_or_config(
        getattr(args, "write_gaps", None),
        audit_cfg.write_gaps_dir if audit_cfg else None,
    )
    include_exploratory_function_gaps = bool(
        _cli_or_config(
            getattr(args, "include_exploratory_function_gaps", None),
            audit_cfg.include_exploratory_function_gaps if audit_cfg else False,
        )
    )
    require_direct_tests = bool(
        _cli_or_config(
            getattr(args, "require_direct_tests", None),
            audit_cfg.require_direct_tests if audit_cfg else False,
        )
    )

    object_specs: list[Any] = []
    if cfg is not None:
        object_specs.extend(cfg.objects)
    if audit_cfg is not None:
        object_specs.extend(audit_cfg.targets)

    def _audit_target_groups() -> list[dict[str, Any]]:
        try:
            include_private_by_module = {
                _scan_base_module(target): bool(
                    getattr(args, "include_private", False)
                    or any(
                        bool(getattr(spec, "include_private", False))
                        for spec in normalized_target_specs_by_module.get(
                            _scan_base_module(target),
                            [],
                        )
                    )
                )
                for target in target_names
            }
            return _canonical_surface_groups_for_targets(
                target_names,
                cfg=cfg,
                object_specs=object_specs,
                include_private_by_module=include_private_by_module,
                bootstrap_test_dir=test_dir,
                resolve_config_imports=not bool(getattr(args, "list_targets", False)),
            )
        except Exception as exc:  # pragma: no cover - shared with list-targets/json block
            raise RuntimeError(f"target metadata resolution failed: {exc}") from exc

    if getattr(args, "list_targets", False):
        try:
            target_groups = _audit_target_groups()
        except RuntimeError as exc:
            if args.json:
                print(
                    _build_blocked_agent_envelope(
                        tool="audit",
                        target=", ".join(target_names),
                        summary="cannot resolve callable target metadata",
                        blocking_reason=str(exc),
                        raw_details={
                            "target_names": target_names,
                            "error": str(exc),
                        },
                    ).to_json()
                )
                return 1
            _stderr(f"{exc}\n")
            return 1

        listing_rows = [
            row
            for group in target_groups
            for row in list(group.get("targets", []))
            if bool(row.get("selected", True))
        ]
        config_suggestions = _dedupe_config_suggestions(
            [
                *_audit_config_suggestions(
                    modules=target_names,
                    test_dir=test_dir,
                    max_examples=max_examples,
                    workers=workers,
                    validation_mode=validation_mode,
                    min_fixture_completeness=min_fixture_completeness,
                    show_generated=show_generated,
                    save_generated=save_generated,
                    write_gaps=write_gaps,
                    include_exploratory_function_gaps=include_exploratory_function_gaps,
                    require_direct_tests=require_direct_tests,
                    target_groups=target_groups,
                    results=(),
                ),
                *_object_config_suggestions_from_rows(listing_rows),
                *_audit_target_config_suggestions_from_rows(listing_rows),
            ]
        )
        bootstrap_suggestions = _audit_bootstrap_support_suggestions(
            target_groups,
            validation_mode=validation_mode,
        )

        if args.json:
            print(
                _build_target_listing_envelope(
                    tool="audit",
                    target=", ".join(target_names),
                    groups=target_groups,
                    warnings=_safe_listing_config_warning(
                        has_object_hooks=bool(object_specs),
                        has_contracts=bool(cfg is not None and cfg.contracts),
                    ),
                    config_suggestions=config_suggestions,
                    bootstrap_suggestions=bootstrap_suggestions,
                ).to_json()
            )
        else:
            print(
                _render_target_listing_text(
                    f"Callable targets for {', '.join(target_names)}",
                    target_groups,
                    warnings=_safe_listing_config_warning(
                        has_object_hooks=bool(object_specs),
                        has_contracts=bool(cfg is not None and cfg.contracts),
                    ),
                    config_suggestions=config_suggestions,
                    bootstrap_suggestions=bootstrap_suggestions,
                )
            )
        return 0

    try:
        save_generated_path = (
            _workspace_output_path(str(save_generated), label="audit.save_generated")
            if save_generated
            else None
        )
        write_gaps_dir = (
            _workspace_output_path(str(write_gaps), label="audit.write_gaps_dir")
            if write_gaps
            else None
        )
    except ValueError as exc:
        _stderr(f"Invalid audit output path: {exc}\n")
        return 2
    if save_generated_path is not None:
        save_generated = str(save_generated_path)
    if write_gaps_dir is not None:
        write_gaps = str(write_gaps_dir)

    def _collect_results() -> list[Any]:
        collected: list[Any] = []
        for target in target_names:
            module_name = _scan_base_module(target)
            audit_kwargs: dict[str, Any] = {
                "test_dir": test_dir,
                "max_examples": max_examples,
                "workers": workers,
                "validation_mode": validation_mode,
            }
            if min_fixture_completeness > 0.0:
                audit_kwargs["min_fixture_completeness"] = min_fixture_completeness
            if target_specs := normalized_target_specs_by_module.get(module_name):
                audit_kwargs["targets"] = target_specs
            if cfg is not None:
                try:
                    module_contract_checks = _config_contract_checks_for_module(cfg, module_name)
                    if module_contract_checks:
                        audit_kwargs["contract_checks"] = module_contract_checks
                except Exception as exc:
                    _stderr(f"warning: contract config failed for {module_name}: {exc}\n")
            collected.append(audit(target, **audit_kwargs))
        return collected

    results = _collect_results()
    audit_target_groups = [
        {
            "module": result.module,
            "targets": list(getattr(result, "surface", ())),
            "bootstrap_targets": [],
        }
        for result in results
    ]
    blocked_results = [result for result in results if getattr(result, "blocking_reason", None)]
    direct_test_gate = _direct_test_gate_payload(results) if require_direct_tests else None
    config_suggestions = _audit_config_suggestions(
        modules=target_names,
        test_dir=test_dir,
        max_examples=max_examples,
        workers=workers,
        validation_mode=validation_mode,
        min_fixture_completeness=min_fixture_completeness,
        show_generated=show_generated,
        save_generated=save_generated,
        write_gaps=write_gaps,
        include_exploratory_function_gaps=include_exploratory_function_gaps,
        require_direct_tests=require_direct_tests,
        target_groups=audit_target_groups,
        results=results,
    )

    if getattr(args, "json", False):
        saved_generated_path: Path | None = None
        written_gap_files: list[dict[str, Any]] = []
        if save_generated and len(results) == 1 and results[0].generated_test:
            saved_generated_path = Path(save_generated)
            saved_generated_path.write_text(results[0].generated_test, encoding="utf-8")
        if write_gaps:
            written_gap_files = _write_audit_gap_stubs(
                results,
                output_dir=write_gaps,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
        print(
            _build_audit_agent_envelope(
                results,
                saved_generated_path=saved_generated_path,
                written_gap_files=written_gap_files,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
                require_direct_tests=require_direct_tests,
                config_suggestions=config_suggestions,
                surface_groups=audit_target_groups,
            ).to_json()
        )
        if blocked_results:
            return 1
        if direct_test_gate is not None and not bool(direct_test_gate["passed"]):
            return 1
        return 0

    if show_generated or save_generated or write_gaps:
        # Per-module mode with optional generated or gap-stub output
        for mod, result in zip(target_names, results, strict=False):
            print(
                "\n".join(
                    _audit_summary_lines(
                        result,
                        include_exploratory_function_gaps=include_exploratory_function_gaps,
                    )
                )
            )
            if show_generated and result.generated_test:
                print(f"\n  --- generated test for {mod} ---")
                print(result.generated_test)
                print("  --- end ---")
            if save_generated and result.generated_test:
                path = Path(save_generated)
                path.write_text(result.generated_test, encoding="utf-8")
                _stderr(f"Saved: {path}\n")
        if write_gaps:
            written_gap_files = _write_audit_gap_stubs(
                results,
                output_dir=write_gaps,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
            if written_gap_files:
                _stderr(f"Wrote {len(written_gap_files)} draft gap stub file(s) to {write_gaps}\n")
            else:
                _stderr(f"No draft gap stubs were written to {write_gaps}\n")
    else:
        print(
            _render_audit_report_text(
                results,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
                config_suggestions=config_suggestions,
                surface_groups=audit_target_groups,
            )
        )

    if direct_test_gate is not None:
        print(f"\n  {_direct_test_gate_summary(direct_test_gate)}")
    if blocked_results:
        return 1
    if direct_test_gate is not None and not bool(direct_test_gate["passed"]):
        gate_suffix = _direct_test_gate_summary(direct_test_gate).removeprefix(
            "Direct test gate: "
        )
        _stderr(f"  Direct tests required: {gate_suffix.lower()}\n")
        return 1
    return 0
