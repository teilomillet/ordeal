from __future__ import annotations
# ruff: noqa
def _format_swarm_telemetry(result: Any) -> str | None:
    """Render joint rule+fault swarm telemetry."""
    section = _resolve_telemetry_section(
        result,
        "swarm",
        "rule_swarm",
        "swarm_stats",
        "swarm_summary",
    )
    if isinstance(section, str):
        return section.strip() or None
    if isinstance(section, Mapping):
        summary = _result_value(section, "summary", "description", "text")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()

    parts: list[str] = []
    runs = _lookup_telemetry_value(result, "rule_swarm_runs", "swarm_runs")
    total_runs = _result_value(result, "total_runs")
    if isinstance(runs, int):
        if isinstance(total_runs, int) and total_runs > 0:
            parts.append(f"{runs}/{total_runs} runs used joint rule+fault configs")
        else:
            parts.append(f"{runs} swarm run(s)")
    elif runs is not None:
        parts.append(f"{runs} swarm run(s)")

    config_value = None
    if isinstance(section, Mapping):
        config_value = _result_value(section, "configs", "swarm_configs", "configurations")
    if config_value is None:
        config_value = _lookup_telemetry_value(
            result,
            "swarm_configs",
            "rule_swarm_configs",
            "configurations",
        )
    config_count = _count_entries(config_value)
    if config_count:
        parts.append(f"{config_count} configs")

    if isinstance(section, Mapping) and any(
        key in section
        for key in (
            "top_config",
            "best_config",
            "dead_configs",
            "edge_gain",
            "failure_count",
        )
    ):
        extra = _format_mapping_counts(
            section,
            preferred_keys=(
                "top_config",
                "best_config",
                "dead_configs",
                "edge_gain",
                "failure_count",
            ),
            max_items=2,
        )
        if extra:
            parts.append(extra)

    return "; ".join(parts) if parts else None
def _format_behavior_telemetry(result: Any) -> str | None:
    """Render behavior-coverage telemetry."""
    section = _resolve_telemetry_section(
        result,
        "behavior",
        "behavior_coverage",
        "coverage",
        "coverage_summary",
    )
    if isinstance(section, str):
        return section.strip() or None
    if isinstance(section, Mapping):
        summary = _result_value(section, "summary", "description", "text")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()

    parts: list[str] = []
    if isinstance(section, Mapping):
        properties = _result_value(
            section,
            "properties_satisfied",
            "sometimes_properties",
            "properties",
        )
        if properties is not None:
            if isinstance(properties, int):
                if properties > 0:
                    parts.append(f"{properties} sometimes-properties satisfied")
            else:
                count = _count_entries(properties)
                if count:
                    parts.append(f"{count} sometimes-properties satisfied")

        gaps = _result_value(section, "coverage_gaps", "gaps", "branch_gaps")
        if gaps is None:
            gaps = _lookup_telemetry_value(result, "coverage_gaps")
        gap_count = _count_entries(gaps)
        if gap_count is not None:
            parts.append(f"{gap_count} coverage gaps")

        lines_covered = _result_value(section, "lines_covered")
        if lines_covered is None:
            lines_covered = _lookup_telemetry_value(result, "lines_covered")
        lines_total = _result_value(section, "lines_total")
        if lines_total is None:
            lines_total = _lookup_telemetry_value(result, "lines_total")
        if isinstance(lines_covered, int) and isinstance(lines_total, int) and lines_total > 0:
            parts.append(f"{lines_covered}/{lines_total} lines covered")

        if any(key in section for key in ("retry_paths", "fallback_paths", "recovery_paths")):
            extra = _format_mapping_counts(
                section,
                preferred_keys=("retry_paths", "fallback_paths", "recovery_paths"),
                max_items=2,
            )
            if extra:
                parts.append(extra)
    else:
        properties = _lookup_telemetry_value(
            result,
            "properties_satisfied",
            "sometimes_properties",
        )
        if isinstance(properties, int):
            if properties > 0:
                parts.append(f"{properties} sometimes-properties satisfied")
        elif properties is not None:
            count = _count_entries(properties)
            if count:
                parts.append(f"{count} sometimes-properties satisfied")

        gaps = _lookup_telemetry_value(result, "coverage_gaps")
        gap_count = _count_entries(gaps)
        if gap_count is not None:
            parts.append(f"{gap_count} coverage gaps")

        lines_covered = _lookup_telemetry_value(result, "lines_covered")
        lines_total = _lookup_telemetry_value(result, "lines_total")
        if isinstance(lines_covered, int) and isinstance(lines_total, int) and lines_total > 0:
            parts.append(f"{lines_covered}/{lines_total} lines covered")

    return ", ".join(parts) if parts else None
def _format_boundary_telemetry(result: Any) -> str | None:
    """Render native-boundary telemetry."""
    section = _resolve_telemetry_section(
        result,
        "native_boundary",
        "boundary",
        "boundary_findings",
        "native_boundary_findings",
        "subprocess_failures",
    )
    if isinstance(section, str):
        return section.strip() or None

    findings = section
    if isinstance(section, Mapping):
        summary = _result_value(section, "summary", "description", "text")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        findings = _result_value(section, "findings", "events", "crashes", "items")
        if findings is None:
            findings = section

    count, labels = _summarize_telemetry_items(findings)
    if count is None and not labels:
        return None

    parts: list[str] = []
    if count is not None:
        noun = "finding" if count == 1 else "findings"
        parts.append(f"{count} {noun}")
    label_text = _format_counter(labels)
    if label_text:
        parts.append(f"({label_text})")
    return " ".join(parts) if parts else None
def _exploration_telemetry_payload(result: Any) -> dict[str, Any]:
    """Return a compact JSON-safe telemetry payload for one exploration result."""
    payload: dict[str, Any] = {}

    swarm = _resolve_telemetry_section(
        result,
        "swarm",
        "rule_swarm",
        "swarm_stats",
        "swarm_summary",
    )
    swarm_summary = _format_swarm_telemetry(result)
    if swarm is not None or swarm_summary is not None:
        swarm_configs = None
        if isinstance(swarm, Mapping):
            swarm_configs = _result_value(swarm, "configs", "swarm_configs", "configurations")
        if swarm_configs is None:
            swarm_configs = _lookup_telemetry_value(
                result,
                "swarm_configs",
                "rule_swarm_configs",
                "configurations",
            )
        payload["swarm"] = {
            "summary": swarm_summary,
            "rule_swarm_runs": _lookup_telemetry_value(result, "rule_swarm_runs", "swarm_runs"),
            "total_runs": _result_value(result, "total_runs"),
            "config_count": _count_entries(swarm_configs),
        }

    behavior = _resolve_telemetry_section(
        result,
        "behavior",
        "behavior_coverage",
        "coverage",
        "coverage_summary",
    )
    behavior_summary = _format_behavior_telemetry(result)
    if behavior is not None or behavior_summary is not None:
        behavior_gaps = None
        behavior_properties = None
        behavior_lines_covered = None
        behavior_lines_total = None
        if isinstance(behavior, Mapping):
            behavior_gaps = _result_value(behavior, "coverage_gaps", "gaps", "branch_gaps")
            behavior_properties = _result_value(
                behavior,
                "properties_satisfied",
                "sometimes_properties",
                "properties",
            )
            behavior_lines_covered = _result_value(behavior, "lines_covered")
            behavior_lines_total = _result_value(behavior, "lines_total")
        if behavior_gaps is None:
            behavior_gaps = _lookup_telemetry_value(result, "coverage_gaps")
        if behavior_properties is None:
            behavior_properties = _lookup_telemetry_value(
                result,
                "properties_satisfied",
                "sometimes_properties",
            )
        if behavior_lines_covered is None:
            behavior_lines_covered = _lookup_telemetry_value(result, "lines_covered")
        if behavior_lines_total is None:
            behavior_lines_total = _lookup_telemetry_value(result, "lines_total")
        payload["behavior"] = {
            "summary": behavior_summary,
            "properties_satisfied": behavior_properties,
            "coverage_gaps": _count_entries(behavior_gaps),
            "lines_covered": behavior_lines_covered,
            "lines_total": behavior_lines_total,
        }

    boundary = _resolve_telemetry_section(
        result,
        "native_boundary",
        "boundary",
        "boundary_findings",
        "native_boundary_findings",
        "subprocess_failures",
    )
    boundary_summary = _format_boundary_telemetry(result)
    if boundary is not None or boundary_summary is not None:
        count, labels = _summarize_telemetry_items(
            _result_value(boundary, "findings", "events", "crashes", "items")
            if isinstance(boundary, Mapping)
            else boundary
        )
        payload["native_boundary"] = {
            "summary": boundary_summary,
            "finding_count": count,
            "categories": dict(labels),
        }

    return payload
def _result_telemetry_lines(result: Any) -> list[str]:
    """Return compact human-readable telemetry lines for one result."""
    lines: list[str] = []
    swarm = _format_swarm_telemetry(result)
    if swarm:
        lines.append(f"    Swarm: {swarm}")
    behavior = _format_behavior_telemetry(result)
    if behavior:
        lines.append(f"    Behavior: {behavior}")
    boundary = _format_boundary_telemetry(result)
    if boundary:
        lines.append(f"    Native boundary: {boundary}")
    return lines
def _print_report(
    results: list[tuple[str, ExplorationResult]],
    cfg: OrdealConfig,
) -> None:
    """Print text report to stdout."""
    if cfg.report.format not in ("text", "both"):
        return

    print("\n--- Ordeal Exploration Report ---\n")
    for class_path, result in results:
        print(f"  {class_path}")
        print(
            f"    {result.total_runs} runs, {result.total_steps} steps, "
            f"{result.duration_seconds:.1f}s"
        )
        print(f"    {result.unique_edges} edges, {result.checkpoints_saved} checkpoints")
        for line in _result_telemetry_lines(result):
            print(line)
        if result.failures:
            print(f"    {len(result.failures)} FAILURES:")
            for f in result.failures[:10]:
                steps = f" ({len(f.trace.steps)} steps)" if f.trace else ""
                print(f"      {type(f.error).__name__}: {f.error}{steps}")
        else:
            print("    No failures.")
        print()
def _format_scan_summary(state: Any) -> str:
    """Render a concise, action-oriented summary for ``ordeal scan``."""
    lines = [f"ordeal scan: {state.module}"]
    details = _scan_report_details(state)
    config_suggestions = list(getattr(state, "supervisor_info", {}).get("config_suggestions", ()))
    coverage_gaps = [detail for detail in details if detail.get("category") == "coverage_gap"]
    invalid_inputs = [
        detail for detail in details if detail.get("category") == "invalid_input_crash"
    ]
    robustness = [
        detail
        for detail in details
        if detail.get("category") == "beyond_declared_contract_robustness"
    ]
    exploratory_crashes = [
        detail for detail in details if detail.get("category") == "speculative_crash"
    ]
    exploratory_properties = [
        detail for detail in details if detail.get("category") == "speculative_property"
    ]
    expected = [
        detail for detail in details if detail.get("category") == "expected_precondition_failure"
    ]
    blocked = [detail for detail in details if detail.get("kind") == "blocked"]
    if state.findings:
        status = "findings found"
    elif coverage_gaps:
        status = "coverage gaps found"
    elif exploratory_crashes or exploratory_properties:
        status = "exploratory findings"
    elif robustness:
        status = "robustness findings observed"
    elif invalid_inputs:
        status = "invalid-input crashes observed"
    elif blocked:
        status = "blocked targets observed"
    elif expected:
        status = "expected preconditions observed"
    else:
        status = "no findings yet"
    lines.append(f"  status: {status}")
    lines.append(f"  confidence: {_calibrated_scan_confidence(state):.0%}")

    lines.append(f"  checked: {', '.join(_scan_checked_items(state))}")
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    if isinstance(sampling, Mapping):
        lines.append(
            "  surface sample: "
            f"{sampling.get('sampled', 0)}/{sampling.get('total_runnable', 0)} runnable exports "
            f"across {sampling.get('source_modules', 0)} source module(s); "
            "use --list-targets or --target for exhaustive coverage"
        )

    if state.findings:
        lines.append("  findings:")
        for finding in state.findings[:5]:
            lines.append(f"    - {finding}")
    else:
        lines.append("  findings: none promoted")
        if coverage_gaps:
            lines.append("  coverage gaps:")
            for detail in coverage_gaps[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if invalid_inputs:
            lines.append("  invalid-input crashes:")
            for detail in invalid_inputs[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if robustness:
            lines.append("  beyond-contract robustness:")
            for detail in robustness[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if exploratory_crashes:
            lines.append("  exploratory crashes:")
            for detail in exploratory_crashes[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if exploratory_properties:
            lines.append("  exploratory properties:")
            for detail in exploratory_properties[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if expected:
            lines.append("  expected preconditions:")
            for detail in expected[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if blocked:
            lines.append("  blocked targets:")
            for detail in blocked[:5]:
                lines.append(
                    f"    - {detail.get('function', '?')}: "
                    f"{detail.get('blocking_reason') or detail.get('error')}"
                )

    if details:
        lines.append("  evidence cards:")
        for raw_detail in details[:5]:
            detail = _scan_detail_with_evidence(state.module, raw_detail)
            card = detail["evidence"]
            lines.append(f"    - {detail['qualname']} [{card['status']}]")
            for label, value in _evidence_card_fields(card):
                if label == "Status":
                    continue
                lines.append(f"      {label.lower()}: {value}")

    frontier = state.frontier
    if frontier:
        lines.append("  gaps to close:")
        shown = 0
        for name, gaps in frontier.items():
            if shown >= 5:
                break
            lines.append(f"    - {name}: {', '.join(gaps)}")
            shown += 1

    reliability_map = getattr(state, "supervisor_info", {}).get("reliability_map")
    if isinstance(reliability_map, Mapping) and reliability_map.get("summary"):
        summary = reliability_map["summary"]
        lines.append(
            "  reliability map: "
            f"{summary.get('operations', 0)} operations | "
            f"PASS {summary.get('pass', 0)} | "
            f"NOT EXERCISED {summary.get('not_exercised', 0)} | "
            f"FAIL {summary.get('fail', 0)} | "
            f"blocked {summary.get('blocked', 0)}"
        )
        next_experiment = reliability_map.get("next_experiment")
        if isinstance(next_experiment, Mapping) and next_experiment.get("command"):
            lines.append(f"  next evidence: {next_experiment['command']}")

    from ordeal.suggest import format_suggestions

    avail = format_suggestions(state)
    if avail:
        lines.append(avail)
    lines.extend(_render_config_suggestions_text(config_suggestions))
    return "\n".join(lines)
def _scan_report_details(state: Any) -> list[dict[str, Any]]:
    """Return structured finding details for scan report generation."""
    details = getattr(state, "finding_details", None)
    if details is not None:
        return list(details)
    return []
