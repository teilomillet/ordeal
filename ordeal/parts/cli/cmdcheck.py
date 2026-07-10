from __future__ import annotations
# ruff: noqa
def _cmd_check(args: argparse.Namespace) -> int:
    """Verify a mined property or an explicit contract on one target."""
    from ordeal.auto import _evaluate_contract_checks, _resolve_explicit_target, _unwrap
    from ordeal.mine import mine

    target = args.target
    prop = args.property
    cli_contract_names = _check_contract_names(args)
    contract_runtime = _resolve_check_runtime_defaults(
        target,
        config_path=getattr(args, "config", None),
    )
    if cli_contract_names and contract_runtime.contract_checks:
        _stderr("Use either --contract or config-backed explicit contracts, not both.\n")
        return 1

    has_explicit_contracts = bool(contract_runtime.contract_checks or cli_contract_names)
    explicit_target = ":" in target

    if prop is not None and has_explicit_contracts:
        _stderr("Use either --property or explicit contracts, not both.\n")
        return 1

    if prop is None and explicit_target and not has_explicit_contracts:
        _stderr(
            f"No explicit contracts configured for {target}. "
            "Add a matching [[contracts]] entry in ordeal.toml, "
            "pass --contract, or use --property.\n"
        )
        return 1

    if prop is None and (explicit_target or has_explicit_contracts):
        module_name = _target_module_name(target)
        target_name = (
            target
            if explicit_target
            else f"{module_name}:{_scan_display_name(module_name, target)}"
        )
        try:
            resolved_name, func = _resolve_explicit_target(
                target_name,
                object_factories=contract_runtime.object_factories,
                object_setups=contract_runtime.object_setups,
                object_scenarios=contract_runtime.object_scenarios,
                object_state_factories=contract_runtime.object_state_factories,
                object_teardowns=contract_runtime.object_teardowns,
                object_harnesses=contract_runtime.object_harnesses,
            )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            _stderr(f"Cannot resolve {target_name}: {exc}\n")
            return 1

        try:
            effective_contract_checks = (
                _build_explicit_contract_checks(func, cli_contract_names)
                if cli_contract_names
                else list(contract_runtime.contract_checks)
            )
        except ValueError as exc:
            _stderr(f"Unknown contract for {target_name}: {exc}\n")
            return 1

        if contract_runtime.registry_warnings:
            for warning in contract_runtime.registry_warnings:
                _stderr(f"warning: {warning}\n")

        if getattr(func, "__ordeal_requires_factory__", False):
            skip_reason = getattr(func, "__ordeal_skip_reason__", "missing object factory")
            _stderr(f"Cannot verify {target_name}: {skip_reason}.\n")
            return 1

        target_listing: dict[str, Any] | None = None
        try:
            target_rows = _callable_listing_rows(
                module_name,
                targets=[target_name],
                selected_targets=[target_name],
                object_factories=contract_runtime.object_factories,
                object_setups=contract_runtime.object_setups,
                object_scenarios=contract_runtime.object_scenarios,
                object_state_factories=contract_runtime.object_state_factories,
                object_teardowns=contract_runtime.object_teardowns,
                object_harnesses=contract_runtime.object_harnesses,
                contract_checks={
                    _scan_display_name(module_name, target_name): effective_contract_checks
                },
            )
            target_listing = next(
                (
                    row
                    for row in target_rows
                    if row.get("name") == _scan_display_name(module_name, target_name)
                ),
                target_rows[0] if target_rows else None,
            )
        except Exception as exc:
            _stderr(f"warning: target metadata unavailable for {target_name}: {exc}\n")

        if target_listing is not None:
            _stderr(
                "Target metadata: "
                + "  ".join(_render_target_listing_parts(target_listing))
                + "\n"
            )

        _stderr(
            f"Checking {target} explicit contract(s) "
            f"({len(effective_contract_checks)} check(s))...\n"
        )
        violations, details = _evaluate_contract_checks(
            _unwrap(func),
            effective_contract_checks,
        )
        report = {
            "tool": "check",
            "target": target,
            "mode": "explicit_contract",
            "summary": [
                f"Checked {len(effective_contract_checks)} explicit contract(s)",
                f"Target: {resolved_name}",
                f"Violations: {len(details)}",
            ],
            "details": details,
            "suggested_commands": [
                (
                    " ".join(
                        [
                            "ordeal check",
                            target,
                            *[f"--contract {name}" for name in cli_contract_names],
                        ]
                    )
                    if cli_contract_names
                    else f"ordeal check {target} --config ordeal.toml"
                ),
            ],
        }
        config_suggestions = _dedupe_config_suggestions(
            [
                *(
                    _object_config_suggestions_from_rows([target_listing])
                    if target_listing is not None
                    else []
                ),
                *_contract_config_suggestions_from_checks(
                    target_name,
                    effective_contract_checks,
                    reason=(
                        "Persist this explicit contract check in ordeal.toml instead of repeating"
                        " CLI flags."
                    ),
                ),
            ]
        )
        if config_line := _config_suggestions_summary(config_suggestions):
            report["summary"].append(config_line)
        report["config_suggestions"] = config_suggestions
        if getattr(args, "json", False):
            print(
                _build_agent_envelope_from_report(
                    report,
                    status="findings" if details else "ok",
                    confidence=1.0 if details else 0.0,
                    confidence_basis=("explicit contract evaluation",),
                    blocking_reason=None if not details else None,
                    raw_details={
                        "report": report,
                        "resolved_target": resolved_name,
                        "config_path": getattr(args, "config", None),
                        "contracts": [
                            {
                                "name": check.name,
                                "summary": check.summary,
                                "kwargs": check.kwargs,
                                "metadata": check.metadata,
                            }
                            for check in effective_contract_checks
                        ],
                        "target_listing": target_listing,
                        "violations": violations,
                        "details": details,
                    },
                ).to_json()
            )
        else:
            if details:
                print(f"{len(details)} explicit contract violation(s):")
                for detail in details:
                    print(f"  FAIL {detail.get('summary', detail.get('name', 'contract'))}")
                    proof = detail.get("proof_bundle", {})
                    lifecycle = proof.get("lifecycle") if isinstance(proof, Mapping) else None
                    if lifecycle:
                        print(f"    lifecycle: {lifecycle}")
                for line in _render_config_suggestions_text(config_suggestions):
                    print(line)
                return 1
            print(f"PASS explicit contracts for {resolved_name}")
            if target_listing is not None:
                print(
                    "Target metadata: " + "  ".join(_render_target_listing_parts(target_listing))
                )
            for line in _render_config_suggestions_text(config_suggestions):
                print(line)
            return 0

        return 1 if details else 0

    # Fallback: property/mining mode for plain dotted functions.
    mod_path, _, func_name = target.rpartition(".")
    if not mod_path:
        _stderr("Target must be dotted path: module.function\n")
        return 1

    try:
        from importlib import import_module

        mod = import_module(mod_path)
        func = _unwrap(getattr(mod, func_name))
    except (ImportError, AttributeError) as e:
        _stderr(f"Cannot resolve {target}: {e}\n")
        return 1

    max_examples = args.max_examples
    if prop:
        _stderr(f"Checking {target} for '{prop}' ({max_examples} examples)...\n")
    else:
        _stderr(f"Checking {target} contracts ({max_examples} examples)...\n")

    result = mine(func, max_examples=max_examples)
    if getattr(args, "json", False):

        def _property_detail(item: Any) -> dict[str, Any]:
            return {
                "kind": "property",
                "module": mod_path,
                "function": func_name,
                "qualname": target,
                "name": item.name,
                "summary": f"{item.name} ({item.confidence:.0%})",
                "confidence": item.confidence,
                "holds": item.holds,
                "total": item.total,
                "counterexample": item.counterexample,
            }

        if prop:
            matching = [p for p in result.properties if prop.lower() in p.name.lower()]
            if not matching:
                _stderr(
                    f"\n  Property '{prop}' not found. Available: "
                    f"{', '.join(p.name for p in result.properties if p.total > 0)}\n"
                )
                return 1
            selected = matching
        else:
            contracts = [
                "never None",
                "no NaN",
                "never empty",
                "deterministic",
                "idempotent",
                "finite",
            ]
            selected = [p for p in result.properties if p.total > 0 and p.name in contracts]

        report = {
            "tool": "check",
            "target": target,
            "mode": "property" if prop else "contract",
            "summary": [result.summary()],
            "details": [_property_detail(item) for item in selected],
            "suggested_commands": [f"ordeal check {target} -n {max_examples}"],
        }
        config_suggestions = [
            _config_suggestion(
                title=f"Persist a focused scan for {target}",
                reason=(
                    "check is CLI-only for mined properties; keep the callable under versioned"
                    " scan config for repeatable verification."
                ),
                snippet_lines=[
                    "[[scan]]",
                    _toml_key_value("module", mod_path),
                    _toml_key_value("targets", [func_name]),
                ],
                section="[[scan]]",
                target=target,
                entries=[
                    {
                        "section": "[[scan]]",
                        "module": mod_path,
                        "targets": [func_name],
                    }
                ],
            )
        ]
        if config_line := _config_suggestions_summary(config_suggestions):
            report["summary"].append(config_line)
        report["config_suggestions"] = config_suggestions
        print(
            _build_agent_envelope_from_report(
                report,
                status="findings" if any(not p.universal for p in selected) else "ok",
                confidence=max((p.confidence for p in selected), default=0.0),
                confidence_basis=("mine() property mining",),
                raw_details={
                    "report": report,
                    "max_examples": max_examples,
                    "properties": [
                        {
                            "name": p.name,
                            "holds": p.holds,
                            "total": p.total,
                            "confidence": p.confidence,
                            "universal": p.universal,
                            "counterexample": p.counterexample,
                        }
                        for p in result.properties
                    ],
                },
            ).to_json()
        )
        return 1 if any(not p.universal for p in selected) else 0

    print(result.summary())

    # --contract mode: check all standard properties that catch bugs
    if not prop:
        config_suggestions = [
            _config_suggestion(
                title=f"Persist a focused scan for {target}",
                reason=(
                    "check is CLI-only for standard mined contracts; keep the callable under"
                    " versioned scan config for repeatable verification."
                ),
                snippet_lines=[
                    "[[scan]]",
                    _toml_key_value("module", mod_path),
                    _toml_key_value("targets", [func_name]),
                ],
                section="[[scan]]",
                target=target,
                entries=[
                    {
                        "section": "[[scan]]",
                        "module": mod_path,
                        "targets": [func_name],
                    }
                ],
            )
        ]
        contracts = [
            "never None",
            "no NaN",
            "never empty",
            "deterministic",
            "idempotent",
            "finite",
        ]
        violations = []
        for p in result.properties:
            if p.total > 0 and not p.universal and p.name in contracts:
                violations.append(p)
        if violations:
            print(f"\n  {len(violations)} contract violation(s):")
            for v in violations:
                print(f"    FAIL {v.name} ({v.holds}/{v.total})")
                if v.counterexample:
                    print(f"      input: {v.counterexample}")
            for line in _render_config_suggestions_text(config_suggestions):
                print(line)
            return 1
        passing = [
            p for p in result.properties if p.total > 0 and p.universal and p.name in contracts
        ]
        if passing:
            print(f"\n  {len(passing)} contract(s) verified:")
            for p in passing:
                print(f"    PASS {p.name} ({p.holds}/{p.total})")
        for line in _render_config_suggestions_text(config_suggestions):
            print(line)
        return 0

    # Single property mode
    matching = [p for p in result.properties if prop.lower() in p.name.lower()]
    if not matching:
        _stderr(
            f"\n  Property '{prop}' not found. Available: "
            f"{', '.join(p.name for p in result.properties if p.total > 0)}\n"
        )
        return 1

    violations = [p for p in matching if not p.universal]
    config_suggestions = [
        _config_suggestion(
            title=f"Persist a focused scan for {target}",
            reason=(
                "check is CLI-only for individual mined properties; keep the callable under"
                " versioned scan config for repeatable follow-up."
            ),
            snippet_lines=[
                "[[scan]]",
                _toml_key_value("module", mod_path),
                _toml_key_value("targets", [func_name]),
            ],
            section="[[scan]]",
            target=target,
            entries=[
                {
                    "section": "[[scan]]",
                    "module": mod_path,
                    "targets": [func_name],
                }
            ],
        )
    ]
    if violations:
        print(f"\n  VIOLATION: {violations[0].name} ({violations[0].holds}/{violations[0].total})")
        for line in _render_config_suggestions_text(config_suggestions):
            print(line)
        if violations[0].counterexample:
            print(f"  Counterexample: {violations[0].counterexample}")
        return 1

    holds = [p for p in matching if p.universal]
    if holds:
        print(f"\n  HOLDS: {holds[0].name} ({holds[0].holds}/{holds[0].total})")
    for line in _render_config_suggestions_text(config_suggestions):
        print(line)
    return 0
def _parse_scan_fixture_specs(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert TOML scan fixture specs into Hypothesis strategies."""
    import hypothesis.strategies as st

    if not raw:
        return None

    fixtures: dict[str, Any] = {}
    for name, value in raw.items():
        if isinstance(value, str) and "," in value:
            fixtures[name] = st.sampled_from(value.split(","))
        elif isinstance(value, str):
            fixtures[name] = st.just(value)
        else:
            fixtures[name] = st.just(value)
    return fixtures
def _audit_gap_stub_path(output_dir: str, target_name: str) -> Path:
    """Return the draft gap-stub path for one audit target."""
    safe = target_name.replace(".", "_")
    return Path(output_dir) / f"test_{safe}_gaps.py"
def _function_gap_status_rank(status: str) -> int:
    """Rank function-gap statuses from most to least actionable."""
    if status == "uncovered":
        return 0
    if status == "exploratory":
        return 1
    return 2
