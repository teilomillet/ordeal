from __future__ import annotations
# ruff: noqa
def _build_mine_agent_envelope(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    include_scan_hint: bool,
    suspicious_count: int,
    report_path: Path | None = None,
    regression_path: Path | None = None,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal mine`."""
    report = _build_mine_report(
        target=target,
        module=module,
        results=results,
        skipped=skipped,
        include_scan_hint=include_scan_hint,
        suspicious_count=suspicious_count,
    )
    details = list(report.get("details", []))
    confidence = max((_detail_confidence(detail) or 0.0 for detail in details), default=None)
    artifacts: list[Any] = []
    if report_path is not None:
        artifacts.append(_agent_artifact("report", report_path, "shareable finding report"))
    if regression_path is not None:
        artifacts.append(
            _agent_artifact("regression", regression_path, "generated pytest regressions")
        )
    return _build_agent_envelope_from_report(
        report,
        status=("blocked" if skipped and not results else ("findings" if details else "ok")),
        confidence=confidence,
        confidence_basis=(
            f"checked {len(results)} function(s)",
            f"{suspicious_count} suspicious finding(s)",
            "property confidence is derived from holds/total examples",
        ),
        blocking_reason=(
            "all candidate functions were skipped" if skipped and not results else None
        ),
        artifacts=artifacts,
        raw_details={
            "report": report,
            "results": [
                {
                    "function": name,
                    "result": result,
                }
                for name, result in results
            ],
            "checked_functions": [name for name, _ in results],
            "skipped_functions": skipped,
            "include_scan_hint": include_scan_hint,
        },
        suggested_test_file=(
            str(regression_path)
            if regression_path is not None
            else (_DEFAULT_REGRESSION_PATH if details else None)
        ),
    )
def _audit_detail_items(
    result: Any,
    *,
    include_exploratory_function_gaps: bool = False,
) -> list[dict[str, Any]]:
    """Normalize one ModuleAudit into finding-style detail items."""
    details: list[dict[str, Any]] = []
    if getattr(result, "blocking_reason", None):
        details.append(
            {
                "kind": "blocked_target",
                "category": "verification_warning",
                "summary": str(result.blocking_reason),
                "module": result.module,
                "qualname": result.module,
                "details": {
                    "fixture_completeness": getattr(result, "fixture_completeness", 0.0),
                },
            }
        )
    for hint in getattr(result, "harness_hints", []):
        details.append(
            {
                "kind": "harness_hint",
                "category": "verification_warning",
                "summary": (
                    f"{hint['function']}: suggested {hint['kind']} -> {hint['suggestion']}"
                ),
                "module": result.module,
                "function": hint["function"],
                "qualname": f"{result.module}.{hint['function']}",
                "details": dict(hint),
            }
        )
    score_fraction = result.mutation_score_fraction
    if score_fraction is not None and score_fraction < 1.0:
        details.append(
            {
                "kind": "mutation_gap",
                "category": "test_strength_gap",
                "summary": f"mutation score {result.mutation_score}",
                "confidence": score_fraction,
                "module": result.module,
                "qualname": result.module,
                "details": {"validation_mode": result.validation_mode},
            }
        )
    for gap in result.mutation_gaps:
        details.append(
            {
                "kind": "mutation",
                "category": "test_strength_gap",
                "summary": f"{gap['location']} {gap['description']}",
                "module": result.module,
                "qualname": gap["target"],
                "details": {
                    "source_line": gap.get("source_line"),
                    "remediation": gap.get("remediation"),
                },
            }
        )
    for function_name in result.gap_functions:
        details.append(
            {
                "kind": "fixture_gap",
                "category": "test_strength_gap",
                "summary": f"{function_name} needs fixtures before ordeal can verify it",
                "module": result.module,
                "function": function_name,
                "qualname": f"{result.module}.{function_name}",
            }
        )
    for finding in getattr(result, "contract_findings", []):
        function_name = str(finding.get("function", result.module))
        qualname = (
            f"{result.module}.{function_name}"
            if function_name and function_name != result.module
            else result.module
        )
        details.append(
            {
                "kind": "contract",
                "category": str(finding.get("category", "semantic_contract")),
                "summary": str(finding.get("summary", "explicit contract failed")),
                "module": result.module,
                "function": function_name,
                "qualname": qualname,
                "details": dict(finding),
            }
        )
    details.extend(
        _function_gap_detail_items(
            result,
            include_exploratory_function_gaps=include_exploratory_function_gaps,
        )
    )
    for item in result.weakest_tests:
        details.append(
            {
                "kind": "warning",
                "category": "test_strength_gap",
                "summary": f"{item['test']} only killed {item['kills']} mutant(s)",
                "module": result.module,
                "qualname": result.module,
            }
        )
    for suggestion in result.suggestions:
        details.append(
            {
                "kind": "coverage_gap",
                "category": "test_strength_gap",
                "summary": suggestion,
                "module": result.module,
                "qualname": result.module,
            }
        )
    for warning in result.warnings:
        details.append(
            {
                "kind": "warning",
                "category": "verification_warning",
                "summary": warning,
                "module": result.module,
                "qualname": result.module,
            }
        )
    return details
def _audit_summary_lines(
    result: Any,
    *,
    include_exploratory_function_gaps: bool,
) -> list[str]:
    """Render one audit summary with filtered function-gap sections."""
    lines = result.summary().splitlines()
    rendered: list[str] = []
    exploratory_count = int(getattr(result, "function_audit_counts", {}).get("exploratory", 0))
    saw_function_section = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("    functions:"):
            saw_function_section = True
            rendered.append(line)
            for status in ("exercised", "uncovered", "exploratory"):
                if status == "exploratory" and not include_exploratory_function_gaps:
                    continue
                entries = [
                    item
                    for item in getattr(result, "function_audits", [])
                    if getattr(item, "status", "") == status
                ]
                entries.sort(
                    key=lambda item: (
                        -int(getattr(item, "covered_body_lines", 0) or 0),
                        str(getattr(item, "name", "")),
                    )
                )
                if not entries:
                    continue
                names = ", ".join(item.name for item in entries[:5])
                rendered.append(f"      - {entries[0].summary_label()}: {names}")
                if entries[0].evidence:
                    first = entries[0].evidence[0]
                    rendered.append(f"        evidence: {first['kind']} — {first['detail']}")
            if exploratory_count and not include_exploratory_function_gaps:
                rendered.append(
                    "      exploratory gaps hidden by default:"
                    f" {exploratory_count} (use --include-exploratory-function-gaps)"
                )
            i += 1
            while i < len(lines) and (
                lines[i].startswith("      - ") or lines[i].startswith("        evidence:")
            ):
                i += 1
            continue
        rendered.append(line)
        i += 1
    if exploratory_count and not include_exploratory_function_gaps and not saw_function_section:
        rendered.append(
            f"  exploratory gaps hidden by default: {exploratory_count}"
            " (use --include-exploratory-function-gaps)"
        )
    return rendered
def _render_audit_report_text(
    results: Sequence[Any],
    *,
    include_exploratory_function_gaps: bool,
    config_suggestions: Sequence[Mapping[str, Any]] = (),
    surface_groups: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render a human-readable audit report from precomputed results."""
    lines = ["ordeal audit"]
    total_cur_tests = 0
    total_cur_lines = 0
    total_mig_tests = 0
    total_mig_lines = 0
    total_warnings = 0
    total_exploratory = 0

    for result in results:
        lines.extend(
            _audit_summary_lines(
                result,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
        )
        total_cur_tests += result.current_test_count
        total_cur_lines += result.current_test_lines
        total_mig_tests += result.migrated_test_count
        total_mig_lines += result.migrated_lines
        total_warnings += len(result.warnings)
        total_exploratory += result.function_audit_counts["exploratory"]

    if len(results) > 1:
        lines.append("\n  total:")
        lines.append(f"    current:  {total_cur_tests} tests | {total_cur_lines} lines")
        lines.append(f"    migrated: {total_mig_tests} tests | {total_mig_lines} lines")
        if total_cur_tests > 0:
            saved = total_cur_tests - total_mig_tests
            pct = saved / total_cur_tests * 100
            lines.append(f"    saved:    {saved} tests ({pct:.0f}%)")

    if total_exploratory and not include_exploratory_function_gaps:
        lines.append(
            f"\n  exploratory function gaps hidden by default: {total_exploratory}"
            " (use --include-exploratory-function-gaps)"
        )

    if total_warnings:
        lines.append(f"\n  warnings: {total_warnings} total")

    rendered_suggestions = _render_config_suggestions_text(config_suggestions)
    if rendered_suggestions:
        lines.append("")
        lines.extend(rendered_suggestions)
    if surface_groups:
        surface_summary = dict(_build_surface_map(surface_groups).get("summary", {}))
        provenance = dict(surface_summary.get("provenance", {}))
        lines.append("")
        lines.append(
            "  surface map: "
            f"{surface_summary.get('entry_count', 0)} entries | "
            f"{surface_summary.get('runnable_count', 0)} runnable | "
            "provenance="
            f"o{int(provenance.get('observed_count', 0) or 0)}/"
            f"d{int(provenance.get('declared_count', 0) or 0)}/"
            f"i{int(provenance.get('inferred_count', 0) or 0)}"
        )

    return "\n".join(lines)
