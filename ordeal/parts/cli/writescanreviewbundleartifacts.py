from __future__ import annotations
# ruff: noqa
def _write_scan_review_bundle_artifacts(
    state: Any,
    *,
    report: Mapping[str, Any],
    regression_path: Path | None = None,
) -> dict[str, Path]:
    """Write extra review artifacts for `scan --save-artifacts`."""
    guard_command = _shell_command("uv", "run", "ordeal", "verify", "--ci")
    findings = [
        _annotate_finding(
            detail,
            regression_path=regression_path,
            guard_command=guard_command,
        )
        for detail in report.get("details", [])
    ]
    config_suggestions = list(report.get("config_suggestions", []))
    support_suggestions = list(report.get("support_suggestions", []))
    scenario_records = list(report.get("scenario_libraries", []))
    written: dict[str, Path] = {}
    reliability_map = report.get("reliability_map")
    if isinstance(reliability_map, Mapping) and reliability_map.get("cells"):
        from ordeal.reliability import _default_reliability_map_path, _write_reliability_map

        written["reliability_map"] = _write_reliability_map(
            _default_reliability_map_path(state.module), reliability_map
        )
        _stderr(f"Reliability map saved: {written['reliability_map']}\n")
    if config_suggestions:
        written["config"] = _write_text_artifact(
            _default_scan_review_config_path(state.module),
            _render_scan_review_config_artifact(
                module=state.module,
                suggestions=config_suggestions,
            ),
            label="Scan config suggestions",
        )
    if support_suggestions:
        written["support"] = _write_text_artifact(
            _default_scan_support_bundle_path(state.module),
            _render_scan_support_artifact(
                module=state.module,
                suggestions=support_suggestions,
            ),
            label="Scan support scaffold",
        )
    proof_payload = _scan_proof_bundle_payload(findings)
    if proof_payload["entries"]:
        path = Path(_default_scan_proofs_path(state.module))
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(path, proof_payload)
        _stderr(f"Scan proof bundles saved: {path}\n")
        written["proofs"] = path
        written["replay"] = _write_text_artifact(
            _default_scan_replay_notes_path(state.module),
            _render_scan_replay_notes_artifact(module=state.module, findings=findings),
            label="Scan replay notes",
        )
    if scenario_records:
        written["scenarios"] = _write_text_artifact(
            _default_scan_scenario_library_path(state.module),
            _render_scan_scenario_library_artifact(
                module=state.module,
                records=scenario_records,
            ),
            label="Scan scenario libraries",
        )
    return written
def _build_scan_report(
    state: Any,
    *,
    regression_path: Path | None = None,
) -> dict[str, Any]:
    """Normalize scan output into the shared finding report shape."""
    evidence = _scan_evidence_dimensions(state)
    search_depth = evidence["search_depth"]
    replayability = evidence["replayability"]
    mutation_strength = evidence["mutation_strength"]
    fixture_completeness = evidence["fixture_completeness"]
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    scope_notes = [
        str(note)
        for note in getattr(state, "supervisor_info", {}).get("scan_scope_notes", ())
        if note
    ]
    config_suggestions = _dedupe_config_suggestions(
        getattr(state, "supervisor_info", {}).get("config_suggestions", ())
    )
    support_suggestions = list(
        getattr(state, "supervisor_info", {}).get("support_suggestions", ())
    )
    scenario_libraries = list(getattr(state, "supervisor_info", {}).get("scenario_libraries", ()))
    reliability_map = getattr(state, "supervisor_info", {}).get("reliability_map", {})
    reliability_summary = (
        dict(reliability_map.get("summary", {})) if isinstance(reliability_map, Mapping) else {}
    )
    guard_command = (
        _shell_command("uv", "run", "ordeal", "verify", "--ci")
        if regression_path is not None
        else None
    )
    details = [
        _scan_detail_with_evidence(
            state.module,
            detail,
            regression_path=regression_path,
            guard_command=guard_command,
        )
        for detail in _scan_report_details(state)
    ]
    promoted_count = len(getattr(state, "findings", []))
    lifecycle_contract_count = sum(
        1 for detail in details if detail.get("category") == "lifecycle_contract"
    )
    semantic_contract_count = sum(
        1 for detail in details if detail.get("category") == "semantic_contract"
    )
    coverage_gap_count = sum(1 for detail in details if detail.get("category") == "coverage_gap")
    invalid_input_count = sum(
        1 for detail in details if detail.get("category") == "invalid_input_crash"
    )
    robustness_count = sum(
        1 for detail in details if detail.get("category") == "beyond_declared_contract_robustness"
    )
    exploratory_crash_count = sum(
        1 for detail in details if detail.get("category") == "speculative_crash"
    )
    exploratory_property_count = sum(
        1 for detail in details if detail.get("category") == "speculative_property"
    )
    expected_count = sum(
        1 for detail in details if detail.get("category") == "expected_precondition_failure"
    )
    blocked_count = sum(1 for detail in details if detail.get("kind") == "blocked")
    if promoted_count:
        status = "findings found"
    elif exploratory_crash_count or exploratory_property_count or robustness_count:
        status = "exploratory findings"
    elif blocked_count:
        status = "blocked"
    elif expected_count:
        status = "expected preconditions observed"
    else:
        status = "no findings yet"
    summary = [
        f"Checked: {', '.join(_scan_checked_items(state))}",
        f"Promoted findings: {promoted_count}",
        f"Lifecycle contracts: {lifecycle_contract_count}",
        f"Semantic contracts: {semantic_contract_count}",
        f"Coverage gaps: {coverage_gap_count}",
        f"Invalid-input crashes: {invalid_input_count}",
        f"Beyond-contract robustness: {robustness_count}",
        f"Exploratory crashes: {exploratory_crash_count}",
        f"Exploratory properties: {exploratory_property_count}",
        f"Expected precondition failures: {expected_count}",
        f"Blocked targets: {blocked_count}",
        f"Gaps: {sum(len(v) for v in state.frontier.values()) if state.frontier else 0}",
        (
            "Evidence:"
            f" search depth={search_depth['functions']} functions/"
            f"{search_depth['transitions']} transitions/"
            f"{search_depth['checkpoints']} checkpoints,"
            f" replayability={replayability['replayable_findings']}/"
            f"{replayability['total_findings']},"
            f" mutation strength="
            f"{(f'{mutation_strength:.0%}' if mutation_strength is not None else 'n/a')},"
            f" fixture completeness={fixture_completeness:.0%}"
        ),
    ]
    if isinstance(sampling, Mapping):
        summary.insert(
            1,
            "Surface sampling: "
            f"{sampling.get('sampled', 0)}/{sampling.get('total_runnable', 0)} "
            "runnable exports checked",
        )
    if config_line := _config_suggestions_summary(config_suggestions):
        summary.append(config_line)
    if support_suggestions:
        count = len(support_suggestions)
        noun = "scaffold" if count == 1 else "scaffolds"
        summary.append(f"Review scaffolds: {count} support-file {noun}")
    if scenario_libraries:
        inferred = sum(len(list(item.get("inferred", ()))) for item in scenario_libraries)
        summary.append(
            f"Scenario libraries: {inferred} inferred pack(s), reusable library notes available"
        )
    if reliability_summary:
        summary.append(
            "Reliability map: "
            f"{reliability_summary.get('operations', 0)} operations, "
            f"{reliability_summary.get('pass', 0)} PASS, "
            f"{reliability_summary.get('not_exercised', 0)} NOT EXERCISED, "
            f"{reliability_summary.get('fail', 0)} FAIL, "
            f"{reliability_summary.get('blocked', 0)} blocked"
        )
        deepening = reliability_map.get("deepening")
        if isinstance(deepening, Mapping):
            summary.append(
                "Automatic deepening: "
                f"{deepening.get('status')}"
                + (f" via {deepening.get('engine')}" if deepening.get("engine") else "")
                + f" in {deepening.get('elapsed_seconds', 0)}s"
            )
    suggested_commands = [
        f"ordeal scan {state.module}",
        f"ordeal mine {state.module} -n 200",
        f"ordeal mutate {state.module}",
    ]
    extra_sections: list[tuple[str, list[str]]] = []
    if isinstance(sampling, Mapping):
        sampling_notes = [
            note for note in scope_notes if not note.startswith("Package-root scan sampled ")
        ]
        extra_sections.append(
            (
                "Surface Sampling",
                [
                    "Package-root scan sampled "
                    f"{sampling.get('sampled', 0)} of "
                    f"{sampling.get('total_runnable', 0)} runnable exports across "
                    f"{sampling.get('source_modules', 0)} source module(s).",
                    "Use `--list-targets` to inspect the full exported surface.",
                    "Use `--target` to run an exhaustive check on a specific callable or glob.",
                    *sampling_notes,
                ],
            )
        )
        sampled_targets = [str(item) for item in sampling.get("targets", ()) if str(item)]
        suggested_commands = [f"ordeal scan {state.module} --list-targets"]
        if sampled_targets:
            suggested_commands.append(
                f"ordeal scan {state.module} --target {sampled_targets[0]} -n 50"
            )
    elif scope_notes:
        extra_sections.append(("Scope Notes", scope_notes))
    extra_sections.append(
        (
            "Evidence Dimensions",
            [
                (
                    "search depth: "
                    f"{search_depth['functions']} functions, "
                    f"{search_depth['transitions']} transitions, "
                    f"{search_depth['checkpoints']} checkpoints"
                ),
                (
                    "replayability: "
                    f"{replayability['replayable_findings']}/"
                    f"{replayability['total_findings']} findings have concrete inputs"
                ),
                (
                    "mutation strength: "
                    + (
                        f"{mutation_strength:.0%}"
                        if mutation_strength is not None
                        else "not measured yet"
                    )
                ),
                f"fixture completeness: {fixture_completeness:.0%}",
            ],
        )
    )
    if isinstance(reliability_map, Mapping) and reliability_map.get("cells"):
        operation_names = {
            item.get("id"): item.get("target") for item in reliability_map.get("operations", ())
        }
        property_names = {
            item.get("id"): item.get("name") for item in reliability_map.get("properties", ())
        }
        top_cells = [
            cell for cell in reliability_map.get("cells", ()) if cell.get("status") != "PASS"
        ][:5]
        extra_sections.append(
            (
                "Reliability Map",
                [
                    f"{operation_names.get(cell.get('operation_id'), '?')} | "
                    f"{cell.get('seam')} × {cell.get('fault')} × "
                    f"{property_names.get(cell.get('property_id'), '?')}: "
                    f"{cell.get('status')}"
                    + (
                        f" (blocked: {cell.get('blocking_reason')})"
                        if cell.get("blocking_reason")
                        else ""
                    )
                    for cell in top_cells
                ],
            )
        )
        next_experiment = reliability_map.get("next_experiment")
        if isinstance(next_experiment, Mapping) and next_experiment.get("command"):
            command = str(next_experiment["command"])
            suggested_commands = [
                *suggested_commands,
                *([] if command in suggested_commands else [command]),
            ]
    return {
        "target": state.module,
        "tool": "scan",
        "status": status,
        "confidence": f"{_calibrated_scan_confidence(state):.0%}",
        "seed": getattr(state, "supervisor_info", {}).get("seed"),
        "summary": summary,
        "details": details,
        "gaps": [
            f"`{state.module}.{name}`: {', '.join(gaps)}" for name, gaps in state.frontier.items()
        ],
        "suggested_commands": suggested_commands,
        "extra_sections": extra_sections,
        "config_suggestions": config_suggestions,
        "support_suggestions": support_suggestions,
        "scenario_libraries": scenario_libraries,
        "reliability_map": reliability_map,
    }
def _render_scan_report_markdown(
    state: Any,
    *,
    regression_path: Path | None = None,
) -> str:
    """Render a shareable Markdown finding report for `ordeal scan`."""
    return _render_findings_report_markdown(
        _build_scan_report(state, regression_path=regression_path)
    )
def _build_scan_bundle(
    state: Any,
    *,
    report_path: Path,
    regression_path: Path | None,
    config_path: Path | None = None,
    support_path: Path | None = None,
    proofs_path: Path | None = None,
    replay_path: Path | None = None,
    scenario_library_path: Path | None = None,
) -> dict[str, Any]:
    """Build the machine-readable scan artifact bundle."""
    report = _build_scan_report(state, regression_path=regression_path)
    guard_command = _shell_command("uv", "run", "ordeal", "verify", "--ci")
    findings = [
        _annotate_finding(
            detail,
            regression_path=regression_path,
            guard_command=guard_command,
        )
        for detail in report["details"]
    ]
    saved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "version": 1,
        "saved_at": saved_at,
        "tool": "scan",
        "target": report["target"],
        "workspace": os.getcwd(),
        "status": report["status"],
        "confidence": round(state.confidence, 4),
        "seed": report.get("seed"),
        "summary": report["summary"],
        "gaps": report["gaps"],
        "finding_count": len(findings),
        "findings": findings,
        "config_suggestions": list(report.get("config_suggestions", [])),
        "support_suggestions": list(report.get("support_suggestions", [])),
        "scenario_libraries": list(report.get("scenario_libraries", [])),
        "artifacts": {
            "report": _display_path(report_path),
            "bundle": None,
            "regression": _display_path(regression_path) if regression_path else None,
            "regression_manifest": (_DEFAULT_REGRESSION_MANIFEST if regression_path else None),
            "config": _display_path(config_path) if config_path else None,
            "support": _display_path(support_path) if support_path else None,
            "proofs": _display_path(proofs_path) if proofs_path else None,
            "replay": _display_path(replay_path) if replay_path else None,
            "scenario_libraries": (
                _display_path(scenario_library_path) if scenario_library_path else None
            ),
            "index": _display_path(Path(_default_artifact_index_path())),
        },
        "commands": {
            "pytest": (
                _shell_command("uv", "run", "pytest", _display_path(regression_path), "-q")
                if regression_path
                else None
            ),
            "rescan": _shell_command(
                "uv",
                "run",
                "ordeal",
                "scan",
                state.module,
                "--save-artifacts",
            ),
            "ci": guard_command if regression_path else None,
        },
    }
def _split_regression_stub(stub: str) -> tuple[str | None, str, str | None]:
    """Split a stub into import line, function body, and test name."""
    lines = stub.rstrip().splitlines()
    import_line = lines[0] if lines and lines[0].startswith("from ") else None
    body_start = next((idx for idx, line in enumerate(lines) if line.startswith("def ")), None)
    body = "\n".join(lines[body_start:]).rstrip() if body_start is not None else stub.rstrip()
    return import_line, body, _regression_test_name(stub)
def _render_regression_file(header: list[str], stubs: list[str]) -> str:
    """Render a fresh regression module from generated stubs."""
    imports: list[str] = []
    seen_imports: set[str] = set()
    bodies: list[str] = []
    for stub in stubs:
        import_line, body, _ = _split_regression_stub(stub)
        if import_line and import_line not in seen_imports:
            imports.append(import_line)
            seen_imports.add(import_line)
        bodies.append(body)

    lines = header[:]
    if imports:
        lines.extend(imports)
        lines.extend(["", ""])
    for idx, body in enumerate(bodies):
        if idx:
            lines.extend(["", ""])
        lines.append(body)
    lines.append("")
    return "\n".join(lines)
def _merge_regression_file(existing: str, stubs: list[str]) -> tuple[str, int, int]:
    """Append stubs into an existing regression file, deduping by test name."""
    source = existing.rstrip()
    existing_imports = set(re.findall(r"^from .+$", existing, re.MULTILINE))
    existing_tests = set(re.findall(r"^def (test_[0-9A-Za-z_]+)\(", existing, re.MULTILINE))
    added = 0
    skipped = 0

    for stub in stubs:
        import_line, body, test_name = _split_regression_stub(stub)
        if test_name and test_name in existing_tests:
            skipped += 1
            continue

        chunk: list[str] = []
        if import_line and import_line not in existing_imports:
            chunk.append(import_line)
            existing_imports.add(import_line)
        if chunk:
            chunk.extend(["", ""])
        chunk.append(body)

        if source:
            source += "\n\n\n" + "\n".join(chunk)
        else:
            source = "\n".join(chunk)
        added += 1
        if test_name:
            existing_tests.add(test_name)

    return source.rstrip() + "\n", added, skipped
def _write_regression_file(
    *,
    path_str: str,
    header: list[str],
    stubs: list[str],
) -> tuple[Path, int, int]:
    """Create or extend a regression file from generated stubs."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        merged, added, skipped = _merge_regression_file(path.read_text(encoding="utf-8"), stubs)
        path.write_text(merged, encoding="utf-8")
        return path, added, skipped
    path.write_text(_render_regression_file(header, stubs), encoding="utf-8")
    return path, len(stubs), 0
def _regression_stubs_from_details(
    *,
    module: str,
    details: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Build runnable regression stubs from normalized finding details."""
    stubs: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for detail in details:
        stub = _render_regression_stub(module, detail, trim=False)
        if stub is None:
            skipped.append(detail.get("qualname") or detail.get("function", "?"))
            continue
        if stub in seen:
            continue
        seen.add(stub)
        stubs.append(stub)
    return stubs, skipped
def _scan_regression_stubs(state: Any) -> tuple[list[str], list[str]]:
    """Build runnable regression stubs from replayable scan findings."""
    details = _build_scan_report(state)["details"]
    return _regression_stubs_from_details(module=state.module, details=details)
