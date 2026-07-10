from __future__ import annotations
# ruff: noqa
def _build_audit_agent_envelope(
    results: Sequence[Any],
    *,
    saved_generated_path: Path | None = None,
    written_gap_files: Sequence[Mapping[str, Any]] = (),
    include_exploratory_function_gaps: bool = False,
    require_direct_tests: bool = False,
    config_suggestions: Sequence[Mapping[str, Any]] = (),
    surface_groups: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal audit`."""
    from ordeal.audit import _generated_test_path, _module_audit_to_dict

    details = [
        detail
        for result in results
        for detail in _audit_detail_items(
            result,
            include_exploratory_function_gaps=include_exploratory_function_gaps,
        )
    ]
    modules = [result.module for result in results]
    suggested_commands = []
    for module in modules:
        suggested_commands.extend(
            [
                f"ordeal audit {module} --show-generated",
                f"ordeal mine {module} -n 200",
                f"ordeal mutate {module}",
            ]
        )
    seen: set[str] = set()
    deduped_commands = [cmd for cmd in suggested_commands if not (cmd in seen or seen.add(cmd))]
    blocked_count = sum(1 for result in results if getattr(result, "blocking_reason", None))
    surface_map = (
        _build_surface_map(surface_groups)
        if surface_groups
        else {"summary": {}, "groups": [], "entries": []}
    )
    surface_summary = dict(surface_map.get("summary", {}))
    surface_provenance = dict(surface_summary.get("provenance", {}))
    report = {
        "target": ", ".join(modules),
        "tool": "audit",
        "status": "findings found" if details else "no major gaps found",
        "summary": [
            f"Audited: {len(results)} module(s)",
            f"Findings: {len(details)}",
            f"Blocked modules: {blocked_count}",
            (
                "Function evidence: "
                f"{sum(result.function_audit_counts['exercised'] for result in results)}"
                " exercised, "
                f"{sum(result.function_audit_counts['exploratory'] for result in results)}"
                " exploratory, "
                f"{sum(result.function_audit_counts['uncovered'] for result in results)} uncovered"
            ),
            (
                "Coverage preserved:"
                f" {sum(1 for result in results if result.coverage_preserved)}"
                f"/{len(results)}"
            ),
        ],
        "details": details,
        "suggested_commands": deduped_commands,
    }
    if config_line := _config_suggestions_summary(config_suggestions):
        report["summary"].append(config_line)
    verified_measurements = sum(
        int(result.current_coverage.status.value == "verified")
        + int(result.migrated_coverage.status.value == "verified")
        for result in results
    )
    total_measurements = max(len(results) * 2, 1)
    mutation_fractions = [
        result.mutation_score_fraction
        for result in results
        if result.mutation_score_fraction is not None
    ]
    total_functions = sum(max(result.total_functions, 0) for result in results)
    covered_functions = sum(
        max(int(round(getattr(result, "fixture_completeness", 0.0) * result.total_functions)), 0)
        for result in results
    )
    evidence = {
        "search_depth": {"modules": len(results), "coverage_measurements": total_measurements},
        "replayability": verified_measurements / total_measurements,
        "mutation_strength": (
            sum(mutation_fractions) / len(mutation_fractions) if mutation_fractions else None
        ),
        "fixture_completeness": (
            covered_functions / total_functions if total_functions > 0 else 1.0
        ),
    }
    mutation_strength_text = (
        f"{evidence['mutation_strength']:.0%}"
        if evidence["mutation_strength"] is not None
        else "n/a"
    )
    exploratory_count = sum(result.function_audit_counts["exploratory"] for result in results)
    direct_test_gate = _direct_test_gate_payload(results) if require_direct_tests else None
    report["summary"].append(
        "Evidence:"
        f" search depth={evidence['search_depth']['modules']} modules/"
        f"{evidence['search_depth']['coverage_measurements']} measurements,"
        f" replayability={evidence['replayability']:.0%},"
        f" mutation strength={mutation_strength_text},"
        f" fixture completeness={evidence['fixture_completeness']:.0%}"
    )
    if direct_test_gate is not None:
        report["summary"].append(_direct_test_gate_summary(direct_test_gate))
    if surface_summary.get("entry_count"):
        report["summary"].append(
            "Surface map: "
            f"{surface_summary.get('entry_count', 0)} entries, "
            f"{surface_summary.get('runnable_count', 0)} runnable, "
            "provenance="
            f"o{int(surface_provenance.get('observed_count', 0) or 0)}/"
            f"d{int(surface_provenance.get('declared_count', 0) or 0)}/"
            f"i{int(surface_provenance.get('inferred_count', 0) or 0)}"
        )
    if exploratory_count and not include_exploratory_function_gaps:
        report["summary"].append(
            "Exploratory function gaps hidden by default: "
            f"{exploratory_count} (use --include-exploratory-function-gaps)"
        )
    if written_gap_files:
        report["summary"].append(f"Gap stubs written: {len(written_gap_files)}")
    report["extra_sections"] = [
        (
            "Function-Level Evidence",
            [
                (
                    f"{result.module}: "
                    f"{result.function_audit_counts['exercised']} exercised [verified], "
                    f"{result.function_audit_counts['exploratory']} exploratory [inferred], "
                    f"{result.function_audit_counts['uncovered']} no effective tests [none]"
                )
                for result in results
            ],
        ),
        (
            "Evidence Dimensions",
            [
                (
                    "search depth: "
                    f"{evidence['search_depth']['modules']} modules, "
                    f"{evidence['search_depth']['coverage_measurements']} "
                    "verified-or-attempted measurements"
                ),
                f"replayability: {evidence['replayability']:.0%}",
                (
                    "mutation strength: "
                    + (
                        mutation_strength_text
                        if mutation_strength_text != "n/a"
                        else "not measured yet"
                    )
                ),
                f"fixture completeness: {evidence['fixture_completeness']:.0%}",
            ],
        ),
        (
            "Mutation Alignment",
            [
                (f"{result.module}: {result.mutation_validation_view()['summary']}")
                for result in results
            ],
        ),
        (
            "Test Protection",
            [
                (f"{result.module}: {result.test_protection_view()['summary']}")
                for result in results
            ],
        ),
    ]
    if surface_summary.get("entry_count"):
        report["extra_sections"].append(
            (
                "Surface Map",
                [
                    (
                        f"{surface_summary.get('entry_count', 0)} entries, "
                        f"{surface_summary.get('runnable_count', 0)} runnable, "
                        f"{surface_summary.get('source_module_count', 0)} source modules, "
                        "provenance="
                        f"o{int(surface_provenance.get('observed_count', 0) or 0)}/"
                        f"d{int(surface_provenance.get('declared_count', 0) or 0)}/"
                        f"i{int(surface_provenance.get('inferred_count', 0) or 0)}"
                    )
                ],
            )
        )
    if written_gap_files:
        report["extra_sections"].append(
            (
                "Draft Gap Stubs",
                [
                    f"{item.get('target', 'unknown')} -> {item.get('path', '')}"
                    for item in written_gap_files
                ],
            )
        )
    report["config_suggestions"] = _dedupe_config_suggestions(config_suggestions)
    artifacts: list[Any] = []
    if saved_generated_path is not None:
        artifacts.append(
            _agent_artifact("generated-test", saved_generated_path, "saved ordeal-generated test")
        )
    else:
        for result in results:
            generated_path = _generated_test_path(result.module)
            if generated_path.exists():
                artifacts.append(
                    _agent_artifact(
                        "generated-test",
                        generated_path,
                        "ordeal-generated migrated test",
                        module=result.module,
                    )
                )
    for item in written_gap_files:
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        metadata = {key: value for key, value in item.items() if key != "path"}
        artifacts.append(_agent_artifact("gap-stub", path, "draft audit gap stub", **metadata))
    blocking_reason = None
    blocked_reasons = list(
        dict.fromkeys(
            str(result.blocking_reason)
            for result in results
            if getattr(result, "blocking_reason", None)
        )
    )
    if blocked_reasons:
        blocking_reason = "; ".join(blocked_reasons)
    if direct_test_gate is not None and not bool(direct_test_gate["passed"]):
        blocking_reason = (
            "direct tests required for "
            f"{len(direct_test_gate['exploratory']) + len(direct_test_gate['uncovered'])}"
            " function(s)"
        )
    return _build_agent_envelope_from_report(
        report,
        status=(
            "blocked"
            if blocking_reason is not None
            else ("findings" if details else ("exploratory" if exploratory_count else "ok"))
        ),
        confidence=verified_measurements / total_measurements,
        confidence_basis=(
            (
                "search depth: "
                f"{evidence['search_depth']['modules']} modules, "
                f"{evidence['search_depth']['coverage_measurements']} measurements"
            ),
            f"replayability: {evidence['replayability']:.0%}",
            (
                "mutation strength: "
                + (mutation_strength_text if mutation_strength_text != "n/a" else "not measured")
            ),
            f"fixture completeness: {evidence['fixture_completeness']:.0%}",
        ),
        blocking_reason=blocking_reason,
        artifacts=artifacts,
        raw_details={
            "report": report,
            "modules": [_module_audit_to_dict(result) for result in results],
            "target_groups": [dict(group) for group in surface_groups],
            "targets": [row for group in surface_groups for row in list(group.get("targets", ()))],
            "surface_map": surface_map,
            "mutation_views": [
                {
                    "module": result.module,
                    **result.mutation_validation_view(),
                }
                for result in results
            ],
            "protection_views": [
                {
                    "module": result.module,
                    **result.test_protection_view(),
                }
                for result in results
            ],
            "function_audits": [
                {"module": result.module, **item}
                for result in results
                for item in _module_audit_to_dict(result).get("function_audits", [])
            ],
            "gap_stub_files": [dict(item) for item in written_gap_files],
            "direct_test_gate": (
                {"required": True, **direct_test_gate} if direct_test_gate is not None else None
            ),
            "evidence_dimensions": evidence,
        },
        suggested_test_file=(
            str(saved_generated_path)
            if saved_generated_path is not None
            else (
                str(written_gap_files[0].get("path", ""))
                if len(written_gap_files) == 1 and written_gap_files[0].get("path")
                else None
            )
        ),
    )
def _mutant_to_detail(target: str, mutant: Any) -> dict[str, Any]:
    """Normalize a surviving mutant into a finding-style detail item."""
    return {
        "kind": "mutation",
        "category": "test_strength_gap",
        "summary": f"{mutant.location} {mutant.description}",
        "module": target.rsplit(".", 1)[0] if "." in target else target,
        "qualname": target,
        "location": mutant.location,
        "details": {
            "operator": mutant.operator,
            "source_line": mutant.source_line,
            "remediation": mutant.remediation,
        },
    }
def _build_mutate_agent_envelope(
    *,
    targets: Sequence[str],
    results: Sequence[tuple[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
    threshold: float,
    stubs_path: Path | None = None,
    surface_groups: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal mutate`."""
    details = []
    for target, result in results:
        details.extend(_mutant_to_detail(target, mutant) for mutant in result.survived)
    for blocker in blockers:
        details.append(
            {
                "kind": "no_tests",
                "summary": str(blocker["summary"]),
                "qualname": str(blocker["target"]),
                "details": {
                    "suggested_test_file": blocker.get("suggested_test_file"),
                    "starter_tests": blocker.get("starter_tests"),
                },
            }
        )
    suggested_commands: list[str] = []
    for blocker in blockers:
        suggested_commands.append(f"ordeal init {blocker['target']}")
    for target, _ in results:
        suggested_commands.append(f"ordeal mutate {target}")
    if results:
        cmd = f"ordeal mutate {results[0][0]} --generate-stubs {_DEFAULT_REGRESSION_PATH}"
        suggested_commands.append(cmd)
    seen: set[str] = set()
    deduped_commands = [cmd for cmd in suggested_commands if not (cmd in seen or seen.add(cmd))]
    total_mutants = sum(result.total for _, result in results)
    total_killed = sum(result.killed for _, result in results)
    overall = total_killed / total_mutants if total_mutants > 0 else (None if blockers else 1.0)
    status = "ok"
    if blockers and not results:
        status = "blocked"
    elif details:
        status = "findings"
    surface_map = (
        _build_surface_map(surface_groups)
        if surface_groups
        else {"summary": {}, "groups": [], "entries": []}
    )
    surface_summary = dict(surface_map.get("summary", {}))
    surface_provenance = dict(surface_summary.get("provenance", {}))
    report = {
        "target": ", ".join(targets),
        "tool": "mutate",
        "status": "findings found" if details else "all mutants killed",
        "summary": [
            f"Targets: {len(targets)}",
            f"Mutants tested: {total_mutants}",
            f"Survivors: {sum(len(result.survived) for _, result in results)}",
        ],
        "details": details,
        "suggested_commands": deduped_commands,
    }
    if surface_summary.get("entry_count"):
        report["summary"].append(
            "Surface map: "
            f"{surface_summary.get('entry_count', 0)} entries, "
            f"{surface_summary.get('runnable_count', 0)} runnable, "
            "provenance="
            f"o{int(surface_provenance.get('observed_count', 0) or 0)}/"
            f"d{int(surface_provenance.get('declared_count', 0) or 0)}/"
            f"i{int(surface_provenance.get('inferred_count', 0) or 0)}"
        )
    blocking_reason = str(blockers[0]["summary"]) if blockers and not results else None
    artifacts = (
        [_agent_artifact("regression", stubs_path, "generated mutation test stubs")]
        if stubs_path is not None and stubs_path.exists()
        else []
    )
    recommended = _recommended_action_for_report(report)
    if blockers and not results:
        target = blockers[0]["target"]
        recommended = (
            f"Bootstrap tests with `ordeal init {target}` or save the provided starter scaffold."
        )
    return _build_agent_envelope_from_report(
        {**report, "recommended_action": recommended},
        status=status,
        confidence=overall,
        confidence_basis=(
            f"{total_mutants} mutant(s) tested" if total_mutants else "no mutants tested",
            f"threshold={threshold:.0%}" if threshold > 0 else "no threshold configured",
        ),
        blocking_reason=blocking_reason,
        artifacts=artifacts,
        raw_details={
            "targets": [
                {
                    "target": target,
                    "score": result.score,
                    "killed": result.killed,
                    "total": result.total,
                    "diagnostics": result.diagnostics,
                    "survived_mutants": result.survived,
                    "timings": result.timings,
                }
                for target, result in results
            ],
            "blockers": list(blockers),
            "overall_score": overall,
            "threshold": threshold,
            "surface_groups": [dict(group) for group in surface_groups],
            "surface_targets": [
                row for group in surface_groups for row in list(group.get("targets", ()))
            ],
            "surface_map": surface_map,
        },
        suggested_test_file=(
            str(stubs_path)
            if stubs_path is not None
            else (
                str(blockers[0].get("suggested_test_file"))
                if blockers
                else (_DEFAULT_REGRESSION_PATH if details else None)
            )
        ),
    )
