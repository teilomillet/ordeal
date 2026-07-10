from __future__ import annotations
# ruff: noqa
def _render_finding_section(detail: dict[str, Any]) -> list[str]:
    """Render one finding block for a Markdown dossier."""
    qualname = detail.get("qualname") or detail.get("function", "?")
    kind = detail.get("kind", "finding")
    category = detail.get("category")
    title = detail.get("name") or detail.get("summary") or kind
    module = detail.get("module", "")

    lines = [f"### {detail['index']}. `{qualname}`", "", f"- Type: {kind}", f"- Finding: {title}"]
    if category:
        lines.append(f"- Evidence class: {_evidence_class_for_category(str(category))}")
        lines.append(f"- Internal category: {category}")
    _append_finding_evidence(lines, detail)

    if kind == "property":
        holds = detail.get("holds")
        total = detail.get("total")
        confidence = detail.get("confidence")
        if holds is not None and total is not None and confidence is not None:
            lines.append(f"- Evidence: `{holds}/{total}` examples (`{confidence:.0%}` confidence)")
        lines.append(f"- Why this matters: {_property_impact(detail)}")
        counterexample = detail.get("counterexample")
        if counterexample:
            lines.extend(["", "Counterexample:"])
            lines.extend(_json_block(counterexample))
        stub = _render_regression_stub(module, detail, trim=True)
        if stub:
            lines.extend(["", "Regression test stub:"])
            lines.extend(_python_block(stub))
        lines.extend(
            [
                "",
                "Next steps:",
                f'- `ordeal check {qualname} -p "{detail.get("name", "")}" -n 200`',
                f"- `ordeal mutate {qualname}`",
            ]
        )
        return lines

    if kind == "crash":
        error = detail.get("error") or "unknown error"
        lines.append(f"- Evidence: `{error}`")
        if detail.get("contract_fit") is not None:
            lines.append(
                "- Ranking:"
                f" contract fit={float(detail.get('contract_fit')):.0%},"
                f" reachability={float(detail.get('reachability') or 0.0):.0%},"
                f" realism={float(detail.get('realism') or 0.0):.0%}"
            )
        if detail.get("replay_attempts"):
            lines.append(
                "- Replay:"
                f" `{detail.get('replay_matches', 0)}/{detail.get('replay_attempts', 0)}`"
                " matching replays"
            )
        lines.append(
            "- Why this matters: "
            + str(
                detail.get("proof_bundle", {}).get("likely_impact")
                or "the function crashes under generated inputs."
            )
        )
        if detail.get("failing_args"):
            lines.extend(["", "Failing input:"])
            lines.extend(_json_block(detail["failing_args"]))
        _append_proof_bundle(lines, detail)
        stub = _render_regression_stub(module, detail, trim=True)
        if stub:
            lines.extend(["", "Regression test stub:"])
            lines.extend(_python_block(stub))
        lines.extend(
            [
                "",
                "Next steps:",
                f"- `ordeal mine {qualname} -n 200`",
                (
                    f"- Reproduce the crash directly in a regression test for `{qualname}`"
                    if detail.get("replayable")
                    else f"- Re-run `{qualname}` with the recorded input to confirm the failure"
                ),
            ]
        )
        return lines

    if kind == "coverage_gap":
        lines.append(f"- Evidence: `{detail.get('error') or 'gap-triggering crash'}`")
        lines.append(
            "- Ranking:"
            f" contract fit={float(detail.get('contract_fit') or 0.0):.0%},"
            f" reachability={float(detail.get('reachability') or 0.0):.0%},"
            f" realism={float(detail.get('realism') or 0.0):.0%}"
        )
        _append_proof_bundle(lines, detail)
        lines.extend(
            [
                "",
                "Next steps:",
                f"- Add direct tests or fixtures for `{qualname}`",
                f"- Re-scan `{qualname}` in `--mode candidate` once valid inputs are seeded",
            ]
        )
        return lines

    if kind == "contract":
        lines.append(f"- Evidence: `{detail.get('summary', title)}`")
        _append_proof_bundle(lines, detail)
        category = str(detail.get("category", "semantic_contract"))
        lines.extend(
            [
                "",
                "Next steps:",
                (
                    "- Add a direct lifecycle regression for "
                    f"`{qualname}` with the recorded fault path"
                    if category == "lifecycle_contract"
                    else f"- Add a direct regression for `{qualname}` around the semantic sink"
                ),
                f"- Re-run `ordeal scan {module} --mode candidate`",
            ]
        )
        return lines

    if kind == "function_gap":
        detail_payload = detail.get("details") or {}
        status = detail_payload.get("status")
        epistemic = detail_payload.get("epistemic")
        covered = detail_payload.get("covered_body_lines")
        total = detail_payload.get("total_body_lines")
        evidence = detail_payload.get("evidence") or []

        if status:
            label = f"{status} [{epistemic}]" if epistemic else str(status)
            lines.append(f"- Function Evidence: {label}")
        if total:
            lines.append(f"- Covered Body Lines: `{covered}/{total}`")
        if evidence:
            lines.extend(["", "Evidence:"])
            for item in evidence[:5]:
                lines.append(f"- `{item.get('kind', 'evidence')}`: {item.get('detail', '')}")
        lines.extend(
            [
                "",
                "Next steps:",
                f"- Add a direct test for `{qualname}` with concrete inputs",
                f"- `ordeal audit {module} --show-generated`",
            ]
        )
        return lines

    if kind == "mutation":
        score = detail.get("mutation_score")
        survived = detail.get("survived_mutants")
        if score is not None:
            lines.append(f"- Evidence: mutation score `{score:.0%}`")
        if survived is not None:
            lines.append(f"- Surviving mutants: `{survived}`")
        lines.append(
            "- Why this matters: existing tests still miss at least one meaningful code change."
        )
        lines.extend(
            [
                "",
                "Next steps:",
                f"- `ordeal mutate {qualname}`",
                f"- Add regression tests for the surviving mutant cases in `{qualname}`",
            ]
        )
        return lines

    lines.append(f"- Evidence: {detail.get('summary', title)}")
    return lines
def _render_findings_report_markdown(report: dict[str, Any]) -> str:
    """Render a shareable Markdown report from normalized finding data."""
    lines = ["# Ordeal Finding Report", ""]
    lines.append(f"Target: `{report['target']}`")
    lines.append(f"Tool: `ordeal {report['tool']}`")
    lines.append(f"Status: {report['status']}")
    confidence = report.get("confidence")
    if confidence is not None:
        lines.append(f"Confidence: `{confidence}`")
    seed = report.get("seed")
    if seed is not None:
        lines.append(f"Seed: `{seed}`")
    lines.append("")

    lines.extend(["## Summary", ""])
    for item in report.get("summary", []):
        lines.append(f"- {item}")
    lines.append("")

    details = report.get("details", [])
    lines.extend(["## Findings", ""])
    if details:
        for idx, detail in enumerate(details, start=1):
            enriched = {"index": idx, **detail}
            lines.extend(_render_finding_section(enriched))
            lines.append("")
    else:
        lines.append("No findings yet.")
        lines.append("")

    gaps = report.get("gaps", [])
    if gaps:
        lines.extend(["## Gaps To Close", ""])
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")

    for title, items in report.get("extra_sections", []):
        if not items:
            continue
        lines.extend([f"## {title}", ""])
        for item in items:
            lines.append(f"- {item}")
        lines.append("")

    config_suggestions = list(report.get("config_suggestions", []))
    if config_suggestions:
        lines.extend(["## Suggested ordeal.toml", ""])
        for suggestion in config_suggestions:
            title = str(suggestion.get("title", "")).strip()
            reason = str(suggestion.get("reason", "")).strip()
            target = str(suggestion.get("target", "")).strip()
            evidence = [str(item) for item in suggestion.get("evidence", []) if str(item).strip()]
            if title:
                lines.append(f"### {title}")
                lines.append("")
            if reason:
                lines.append(reason)
                lines.append("")
            if target:
                lines.append(f"Target: `{target}`")
                lines.append("")
            if evidence:
                lines.append("Evidence:")
                lines.extend(f"- `{item}`" for item in evidence[:5])
                lines.append("")
            lines.append("```toml")
            lines.extend(str(suggestion.get("snippet", "")).rstrip().splitlines())
            lines.append("```")
            lines.append("")

    support_suggestions = list(report.get("support_suggestions", []))
    if support_suggestions:
        lines.extend(["## Suggested Review Scaffolds", ""])
        for suggestion in support_suggestions:
            title = str(suggestion.get("title", "")).strip() or str(
                suggestion.get("filename", "support scaffold")
            )
            reason = str(suggestion.get("reason", "")).strip()
            filename = str(suggestion.get("filename", "")).strip()
            target = str(suggestion.get("target", "")).strip()
            suggested_command = str(suggestion.get("suggested_command", "")).strip()
            evidence = [str(item) for item in suggestion.get("evidence", []) if str(item).strip()]
            lines.append(f"### {title}")
            lines.append("")
            if reason:
                lines.append(reason)
                lines.append("")
            if filename:
                lines.append(f"Intended file: `{filename}`")
            if target:
                lines.append(f"Target: `{target}`")
            if evidence:
                lines.append("")
                lines.append("Evidence:")
                lines.extend(f"- `{item}`" for item in evidence[:5])
            lines.append("")
            lines.append("```python")
            lines.extend(str(suggestion.get("snippet", "")).rstrip().splitlines())
            lines.append("```")
            if suggested_command:
                lines.append("")
                lines.append(f"Suggested command: `{suggested_command}`")
            lines.append("")

    scenario_libraries = list(report.get("scenario_libraries", []))
    if scenario_libraries:
        lines.extend(["## Scenario Libraries", ""])
        for artifact in scenario_libraries:
            title = str(artifact.get("title", "")).strip() or "Scenario libraries"
            lines.append(f"### {title}")
            lines.append("")
            inferred = list(artifact.get("inferred", []))
            if inferred:
                lines.append("Inferred for this target:")
                for item in inferred:
                    targets = ", ".join(str(name) for name in item.get("targets", []))
                    lines.append(f"- `{item.get('name')}` for {targets}")
                lines.append("")
            lines.append("Available built-ins:")
            for item in artifact.get("available", []):
                aliases = list(item.get("aliases", []))
                alias_text = f" (aliases: {', '.join(aliases)})" if aliases else ""
                lines.append(f"- `{item.get('name')}`{alias_text}: {item.get('description', '')}")
            lines.append("")

    lines.extend(["## Suggested Commands", ""])
    for command in report.get("suggested_commands", []):
        lines.append(f"- `{command}`")
    return "\n".join(lines).rstrip() + "\n"
def _render_scan_review_config_artifact(
    *,
    module: str,
    suggestions: Sequence[Mapping[str, Any]],
) -> str:
    """Render a review-only TOML artifact from config suggestions."""
    lines = [
        "# Generated by `ordeal scan --save-artifacts`.",
        f"# Target: {module}",
        "# Review these snippets before copying them into ordeal.toml.",
        "",
    ]
    for index, suggestion in enumerate(suggestions, start=1):
        title = str(suggestion.get("title", "")).strip()
        reason = str(suggestion.get("reason", "")).strip()
        target = str(suggestion.get("target", "")).strip()
        if index > 1:
            lines.append("")
        lines.append(f"# [{index}] {title}")
        if reason:
            lines.append(f"# {reason}")
        if target:
            lines.append(f"# Target: {target}")
        evidences = [str(item) for item in suggestion.get("evidence", []) if str(item).strip()]
        for evidence in evidences:
            lines.append(f"# Evidence: {evidence}")
        lines.extend(str(suggestion.get("snippet", "")).rstrip().splitlines())
    return "\n".join(lines).rstrip() + "\n"
def _render_scan_support_artifact(
    *,
    module: str,
    suggestions: Sequence[Mapping[str, Any]],
) -> str:
    """Render one review-only support scaffold artifact from saved suggestions."""
    lines = [
        '"""Generated review scaffolds from `ordeal scan --save-artifacts`.',
        "",
        f"Target: {module}",
        "",
        "Review the placeholders before copying selected pieces into your test support module.",
        '"""',
        "",
    ]
    for index, suggestion in enumerate(suggestions, start=1):
        filename = str(suggestion.get("filename", "")).strip()
        title = str(suggestion.get("title", "")).strip()
        if index > 1:
            lines.extend(["", "", "# " + "-" * 72, ""])
        lines.append(f"# [{index}] {title}")
        if filename:
            lines.append(f"# Intended file: {filename}")
        evidences = [str(item) for item in suggestion.get("evidence", []) if str(item).strip()]
        for evidence in evidences:
            lines.append(f"# Evidence: {evidence}")
        suggested_command = str(suggestion.get("suggested_command", "")).strip()
        if suggested_command:
            lines.append(f"# Suggested command: {suggested_command}")
        snippet_lines = str(suggestion.get("snippet", "")).rstrip().splitlines()
        if snippet_lines:
            lines.extend(["", *snippet_lines])
    return "\n".join(lines).rstrip() + "\n"
def _render_scan_replay_notes_artifact(
    *,
    module: str,
    findings: Sequence[Mapping[str, Any]],
) -> str:
    """Render a compact Markdown replay dossier from scan findings."""
    lines = ["# Replay Notes", "", f"Target: `{module}`", ""]
    concrete = [
        finding
        for finding in findings
        if (
            finding.get("proof_bundle")
            or finding.get("failing_args")
            or finding.get("counterexample")
        )
    ]
    if not concrete:
        lines.append("No concrete replay notes were available for this scan.")
        return "\n".join(lines).rstrip() + "\n"
    for finding in concrete:
        title = str(
            finding.get("summary") or finding.get("name") or finding.get("kind", "finding")
        )
        finding_id = str(finding.get("finding_id", "")).strip()
        lines.extend([f"## {title}", ""])
        if finding_id:
            lines.append(f"- Finding ID: `{finding_id}`")
        evidence = finding.get("evidence")
        if isinstance(evidence, Mapping):
            for label, value in _evidence_card_fields(evidence):
                lines.append(f"- {label}: {value}")
        proof = finding.get("proof_bundle")
        if isinstance(proof, Mapping):
            impact = proof.get("impact")
            if isinstance(impact, Mapping) and impact.get("summary"):
                lines.append(f"- Impact: {impact['summary']}")
            reproduction = proof.get("minimal_reproduction") or proof.get("reproduction")
            if isinstance(reproduction, Mapping):
                command = reproduction.get("command")
                snippet = reproduction.get("python_snippet")
                if command:
                    lines.append(f"- Command: `{command}`")
                if snippet:
                    lines.extend(["", "```python", str(snippet).rstrip(), "```"])
            elif reproduction:
                lines.append(f"- Reproduction: `{reproduction}`")
        failing_args = finding.get("failing_args")
        if failing_args:
            lines.extend(
                [
                    "",
                    "```json",
                    json.dumps(_trim_report_value(failing_args), indent=2),
                    "```",
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
def _scan_proof_bundle_payload(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract proof bundles into a compact machine-readable artifact."""
    entries = []
    for finding in findings:
        proof = finding.get("proof_bundle")
        if not isinstance(proof, Mapping):
            continue
        entries.append(
            {
                "finding_id": finding.get("finding_id"),
                "qualname": finding.get("qualname"),
                "summary": finding.get("summary"),
                "evidence_class": finding.get("evidence_class"),
                "evidence": _json_safe_value(finding.get("evidence")),
                "proof_bundle": _json_safe_value(dict(proof)),
            }
        )
    return {"version": 1, "entries": entries}
def _render_scan_scenario_library_artifact(
    *,
    module: str,
    records: Sequence[Mapping[str, Any]],
) -> str:
    """Render a Markdown note describing inferred and available scenario libraries."""
    lines = ["# Scenario Libraries", "", f"Target: `{module}`", ""]
    for record in records:
        inferred = list(record.get("inferred", []))
        if inferred:
            lines.append("## Inferred packs")
            lines.append("")
            for item in inferred:
                lines.append(
                    f"- `{item.get('name')}` for "
                    + ", ".join(str(name) for name in item.get("targets", []))
                )
                evidences = [
                    str(value) for value in item.get("evidence", []) if str(value).strip()
                ]
                for evidence in evidences:
                    lines.append(f"  evidence: {evidence}")
            lines.append("")
        lines.append("## Built-in libraries")
        lines.append("")
        for item in record.get("available", []):
            aliases = list(item.get("aliases", []))
            alias_text = f" (aliases: {', '.join(aliases)})" if aliases else ""
            lines.append(f"- `{item.get('name')}`{alias_text}: {item.get('description', '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
def _write_text_artifact(path_str: str, content: str, *, label: str) -> Path:
    """Write one UTF-8 text artifact and report its path."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _stderr(f"{label} saved: {path}\n")
    return path
