from __future__ import annotations
# ruff: noqa
def _audit_config_suggestions(
    *,
    modules: Sequence[str],
    test_dir: str,
    max_examples: int,
    workers: int,
    validation_mode: str,
    min_fixture_completeness: float,
    show_generated: bool,
    save_generated: str | None,
    write_gaps: str | None,
    include_exploratory_function_gaps: bool,
    require_direct_tests: bool,
    target_groups: Sequence[Mapping[str, Any]],
    results: Sequence[Any],
) -> list[dict[str, Any]]:
    """Build config suggestions for an audit invocation."""
    lines = ["[audit]", _toml_key_value("modules", list(modules))]
    if test_dir != "tests":
        lines.append(_toml_key_value("test_dir", test_dir))
    if max_examples != 20:
        lines.append(_toml_key_value("max_examples", max_examples))
    if workers != 1:
        lines.append(_toml_key_value("workers", workers))
    if validation_mode != "fast":
        lines.append(_toml_key_value("validation_mode", validation_mode))
    if min_fixture_completeness > 0.0:
        lines.append(_toml_key_value("min_fixture_completeness", min_fixture_completeness))
    if show_generated:
        lines.append(_toml_key_value("show_generated", True))
    if save_generated:
        lines.append(_toml_key_value("save_generated", _display_path(Path(save_generated))))
    if write_gaps:
        lines.append(_toml_key_value("write_gaps_dir", _display_path(Path(write_gaps))))
    if include_exploratory_function_gaps:
        lines.append(_toml_key_value("include_exploratory_function_gaps", True))
    if require_direct_tests:
        lines.append(_toml_key_value("require_direct_tests", True))
    names_to_hint = {
        str(getattr(item, "name", ""))
        for result in results
        for item in getattr(result, "function_audits", [])
        if str(getattr(item, "status", "")) != "exercised"
    }
    rows = [row for group in target_groups for row in list(group.get("targets", []))]
    suggestions = [
        _config_suggestion(
            title="Persist audit defaults for this verification pass",
            reason=(
                "Keep audit modules, gates, and output paths in ordeal.toml for "
                "repeatable reviews."
            ),
            snippet_lines=lines,
            section="[audit]",
            target=", ".join(modules),
            entries=[
                {
                    "section": "[audit]",
                    "modules": list(modules),
                    "test_dir": test_dir,
                    "max_examples": max_examples,
                    "workers": workers,
                    "validation_mode": validation_mode,
                    "min_fixture_completeness": min_fixture_completeness,
                    "show_generated": show_generated,
                    "save_generated": save_generated,
                    "write_gaps_dir": write_gaps,
                    "include_exploratory_function_gaps": include_exploratory_function_gaps,
                    "require_direct_tests": require_direct_tests,
                }
            ],
        )
    ]
    suggestions.extend(
        _object_config_suggestions_from_rows(rows, only_names=sorted(names_to_hint))
    )
    suggestions.extend(_audit_bootstrap_config_suggestions(target_groups))
    return _dedupe_config_suggestions(suggestions)
def _audit_bootstrap_config_suggestions(
    target_groups: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build `[[audit.targets]]` suggestions from class-bootstrap fallback rows."""
    suggestions: list[dict[str, Any]] = []
    for group in target_groups:
        for target in list(group.get("bootstrap_targets", [])):
            lines = ["[[audit.targets]]", _toml_key_value("target", target["target"])]
            if target.get("support_factory"):
                lines.append(_toml_key_value("factory", target["support_factory"]))
            if target.get("support_setup"):
                lines.append(_toml_key_value("setup", target["support_setup"]))
            if target.get("support_state_factory"):
                lines.append(_toml_key_value("state_factory", target["support_state_factory"]))
            if target.get("support_teardown"):
                lines.append(_toml_key_value("teardown", target["support_teardown"]))
            if target.get("support_scenarios"):
                lines.append(_toml_key_value("scenarios", target["support_scenarios"]))
            if target.get("harness") == "stateful":
                lines.append(_toml_key_value("harness", "stateful"))
            lines.append(_toml_key_value("methods", list(target.get("methods", ()))))
            suggestions.append(
                _config_suggestion(
                    title=f"Bootstrap audit target for {target['class_name']}",
                    reason=(
                        "Audit could not reach callable methods directly, so this review scaffold "
                        "pins the class target, method subset, and support hooks explicitly."
                    ),
                    snippet_lines=lines,
                    section="[[audit.targets]]",
                    target=str(target["target"]),
                    evidence=list(target.get("evidence", ())),
                    entries=[
                        {
                            "section": "[[audit.targets]]",
                            "target": target["target"],
                            "factory": target.get("support_factory"),
                            "setup": target.get("support_setup"),
                            "state_factory": target.get("support_state_factory"),
                            "teardown": target.get("support_teardown"),
                            "scenarios": list(target.get("support_scenarios", ())),
                            "harness": target.get("harness", "fresh"),
                            "methods": list(target.get("methods", ())),
                        }
                    ],
                )
            )
    return suggestions
def _bootstrap_placeholder_expression(param_name: str) -> str:
    """Return a conservative placeholder expression for one factory parameter."""
    lower = param_name.lower()
    if "state" in lower:
        return "{}"
    if "upload" in lower:
        return "AsyncMock(return_value=None)"
    if any(token in lower for token in ("client", "session", "transport", "sandbox")):
        return (
            "SimpleNamespace("
            "execute_command=AsyncMock("
            "return_value=SimpleNamespace(returncode=0, stdout='', stderr='')"
            "), "
            "upload_content=AsyncMock(return_value=None)"
            ")"
        )
    return "SimpleNamespace()"
def _bootstrap_constructor_lines(cls: type) -> tuple[list[str], list[str]]:
    """Return setup lines and constructor arguments for one scaffolded factory."""
    setup_lines: list[str] = []
    arg_names: list[str] = []
    with contextlib.suppress(Exception):
        params = list(inspect.signature(cls).parameters.values())
        for param in params:
            if param.name == "self" or param.default is not inspect.Signature.empty:
                continue
            arg_names.append(param.name)
            setup_lines.append(
                f"    {param.name} = {_bootstrap_placeholder_expression(param.name)}"
            )
    return setup_lines, arg_names
def _audit_bootstrap_support_suggestions(
    target_groups: Sequence[Mapping[str, Any]],
    *,
    validation_mode: str,
) -> list[dict[str, Any]]:
    """Build review scaffolds for `tests/ordeal_support.py` from bootstrap rows."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for group in target_groups:
        for target in list(group.get("bootstrap_targets", [])):
            grouped.setdefault(str(target.get("support_path", "")), []).append(target)

    suggestions: list[dict[str, Any]] = []
    for support_path, targets in grouped.items():
        if not support_path:
            continue
        imports = [
            "from __future__ import annotations",
            "",
            "from types import SimpleNamespace",
            "from unittest.mock import AsyncMock",
        ]
        class_imports: dict[str, list[str]] = {}
        scenario_labels: list[str] = []
        body: list[str] = []
        seen_scenarios: set[str] = set()
        for target in targets:
            module_name = str(target["module"])
            class_name = str(target["class_name"])
            class_imports.setdefault(module_name, []).append(class_name)
            try:
                cls = getattr(importlib.import_module(module_name), class_name)
            except Exception:
                cls = None
            class_snake = _camel_to_snake(class_name)
            if cls is not None:
                setup_lines, arg_names = _bootstrap_constructor_lines(cls)
            else:
                setup_lines, arg_names = [], []
            body.extend(
                [
                    "",
                    f"def make_{class_snake}() -> {class_name}:",
                    f'    """Review scaffold for `{class_name}` audit/bootstrap."""',
                    "    # TODO: replace placeholder collaborators with real project fixtures.",
                ]
            )
            if setup_lines:
                body.extend(setup_lines)
                call_args = ", ".join(f"{name}={name}" for name in arg_names)
                body.append(f"    return {class_name}({call_args})")
            else:
                body.append(f"    return {class_name}()")
            if target.get("support_setup"):
                body.extend(
                    [
                        "",
                        f"def prime_{class_snake}(instance: {class_name}) -> {class_name}:",
                        '    """Review scaffold for one post-construction setup hook."""',
                        "    return instance",
                    ]
                )
            if target.get("support_state_factory"):
                body.extend(
                    [
                        "",
                        f"def make_{class_snake}_state() -> dict[str, object]:",
                        '    """Review scaffold for one method state payload."""',
                        "    return {}",
                    ]
                )
            if target.get("support_teardown"):
                body.extend(
                    [
                        "",
                        f"def cleanup_{class_snake}(instance: {class_name}) -> None:",
                        '    """Review scaffold for cleanup after audit runs."""',
                        "    return None",
                    ]
                )
            for label in list(target.get("review_scenarios", ())):
                if label in seen_scenarios:
                    continue
                seen_scenarios.add(label)
                scenario_labels.append(label)
                body.extend(
                    [
                        "",
                        f"def scenario_{label}(instance: object) -> object:",
                        f'    """Review scaffold for `{label}` on bound-method targets."""',
                        "    return instance",
                    ]
                )

        import_lines = imports + [
            f"from {module_name} import {', '.join(sorted(dict.fromkeys(names)))}"
            for module_name, names in sorted(class_imports.items())
        ]
        command_mode = "deep" if validation_mode == "fast" else validation_mode
        command = (
            f"ordeal audit {targets[0]['module']} --validation-mode {command_mode} "
            "--write-gaps .ordeal/audit-gaps"
        )
        suggestions.append(
            {
                "title": f"Scaffold {support_path} for review-first audit hooks",
                "reason": (
                    "Bootstrap the object factory and scenario hooks automatically, then review "
                    "the placeholders before trusting the audit results."
                ),
                "filename": support_path,
                "snippet": "\n".join(import_lines + body).rstrip() + "\n",
                "target": ", ".join(str(target["target"]) for target in targets),
                "evidence": [
                    evidence for target in targets for evidence in list(target.get("evidence", ()))
                ][:5],
                "suggested_command": command,
                "review_scenarios": scenario_labels,
            }
        )
    return suggestions
def _config_suggestions_summary(suggestions: Sequence[Mapping[str, Any]]) -> str | None:
    """Render one summary line for config suggestions."""
    if not suggestions:
        return None
    count = len(suggestions)
    noun = "block" if count == 1 else "blocks"
    return f"Config suggestions: {count} ready-to-paste ordeal.toml {noun}"
def _render_config_suggestions_text(
    suggestions: Sequence[Mapping[str, Any]],
    *,
    indent: str = "  ",
) -> list[str]:
    """Render human-readable ready-to-paste config suggestions."""
    if not suggestions:
        return []
    lines = [f"{indent}Suggested ordeal.toml:"]
    for index, suggestion in enumerate(suggestions, start=1):
        title = str(suggestion.get("title", "")).strip()
        reason = str(suggestion.get("reason", "")).strip()
        lines.append(f"{indent}  {index}. {title}")
        if reason:
            lines.append(f"{indent}     {reason}")
        for snippet_line in str(suggestion.get("snippet", "")).rstrip().splitlines():
            lines.append(f"{indent}     {snippet_line}")
    return lines
def _render_bootstrap_suggestions_text(
    suggestions: Sequence[Mapping[str, Any]],
    *,
    indent: str = "  ",
) -> list[str]:
    """Render review-first support-file scaffolds for target discovery fallbacks."""
    if not suggestions:
        return []
    lines = [f"{indent}Suggested review scaffolds:"]
    for index, suggestion in enumerate(suggestions, start=1):
        title = str(suggestion.get("title", "")).strip()
        reason = str(suggestion.get("reason", "")).strip()
        filename = str(suggestion.get("filename", "")).strip()
        lines.append(f"{indent}  {index}. {title}")
        if filename:
            lines.append(f"{indent}     file: {filename}")
        if reason:
            lines.append(f"{indent}     {reason}")
        command = str(suggestion.get("suggested_command", "")).strip()
        if command:
            lines.append(f"{indent}     command: {command}")
        for snippet_line in str(suggestion.get("snippet", "")).rstrip().splitlines():
            lines.append(f"{indent}     {snippet_line}")
    return lines
def _render_target_listing_text(
    title: str,
    groups: Sequence[Mapping[str, Any]],
    *,
    warnings: Sequence[str] = (),
    config_suggestions: Sequence[Mapping[str, Any]] = (),
    bootstrap_suggestions: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render callable discovery rows for human-readable CLI output."""
    lines = [title]
    surface_map = _build_surface_map(groups)
    for warning in warnings:
        lines.append(f"warning: {warning}")
    summary = dict(surface_map.get("summary", {}))
    if summary.get("entry_count"):
        provenance = dict(summary.get("provenance", {}))
        lines.append(
            "surface map: "
            f"{summary.get('entry_count', 0)} entries, "
            f"{summary.get('runnable_count', 0)} runnable, "
            f"{summary.get('source_module_count', 0)} source modules, "
            "provenance="
            f"o{int(provenance.get('observed_count', 0) or 0)}/"
            f"d{int(provenance.get('declared_count', 0) or 0)}/"
            f"i{int(provenance.get('inferred_count', 0) or 0)}"
        )
    for group in groups:
        module = str(group.get("module", ""))
        targets = list(group.get("targets", []))
        bootstrap_targets = list(group.get("bootstrap_targets", []))
        lines.append(f"\n{module}")
        if not targets:
            lines.append("  (no callable targets found)")
            if bootstrap_targets:
                lines.append("  review bootstrap targets:")
                for target in bootstrap_targets:
                    methods = ", ".join(list(target.get("methods", ()))[:5])
                    scenarios = ", ".join(list(target.get("review_scenarios", ()))[:4]) or "none"
                    lines.append(
                        "    "
                        f"{target.get('class_name', ''):<30} "
                        f"methods={methods}  "
                        f"factory={target.get('support_factory')}  "
                        f"scenarios={scenarios}"
                    )
            continue
        for row in targets:
            parts = _render_target_listing_parts(row)
            lines.append(f"  {row.get('name', ''):<38} " + "  ".join(parts))
            surface = row.get("surface") or _surface_entry_from_listing_row(row)
            lines.append(f"    surface: {_render_surface_entry_summary(surface)}")
    lines.extend(_render_config_suggestions_text(config_suggestions))
    lines.extend(_render_bootstrap_suggestions_text(bootstrap_suggestions))
    return "\n".join(lines)
def _build_target_listing_envelope(
    *,
    tool: str,
    target: str,
    groups: Sequence[Mapping[str, Any]],
    warnings: Sequence[str] = (),
    config_suggestions: Sequence[Mapping[str, Any]] = (),
    bootstrap_suggestions: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Build the agent envelope for callable discovery output."""
    from ordeal.agent_schema import build_agent_envelope

    surface_map = _build_surface_map(groups)
    flat_targets = [row for group in groups for row in list(group.get("targets", []))]
    if config_suggestions:
        normalized_config_suggestions = [dict(item) for item in config_suggestions]
    else:
        payload = _config_suggestion_payload(
            [
                block
                for group in groups
                for block in (
                    _config_suggestions_from_rows(
                        str(group.get("module", target)),
                        list(group.get("targets", [])),
                    )
                    or {}
                ).get("blocks", [])
            ]
        )
        normalized_config_suggestions = []
        if payload is not None:
            normalized_config_suggestions = [
                _config_suggestion(
                    title=f"Persist callable surface for {target}",
                    reason=(
                        "Carry the observed callable surface into ordeal.toml for repeatable "
                        "follow-up scans and audits."
                    ),
                    snippet_lines=str(payload.get("toml", "")).rstrip().splitlines(),
                    section=str(payload.get("blocks", [{}])[0].get("section", "")),
                    target=target,
                    entries=list(payload.get("blocks", [])),
                )
            ]
    runnable_count = sum(1 for row in flat_targets if row.get("runnable"))
    skip_count = len(flat_targets) - runnable_count
    bootstrap_target_count = sum(len(list(group.get("bootstrap_targets", ()))) for group in groups)
    status = "exploratory" if skip_count or bootstrap_target_count else "ok"
    summary = [
        f"Listed {len(flat_targets)} callable target(s) across {len(groups)} module(s)",
        f"Runnable: {runnable_count}",
        f"Skipped: {skip_count}",
    ]
    if bootstrap_target_count:
        summary.append(f"Bootstrap classes: {bootstrap_target_count}")
    if warnings:
        summary.append(f"Warnings: {len(warnings)}")
    return build_agent_envelope(
        tool=tool,
        target=target,
        status=status,
        summary=" | ".join(summary),
        recommended_action=(
            "Use these callable names directly, or review the bootstrap scaffolds before "
            "rerunning `audit` when callable discovery is empty."
            if bootstrap_target_count
            else "Use these callable names and metadata directly in `scan`, `audit`, or `mutate`."
        ),
        confidence=None,
        confidence_basis=("target discovery only",),
        findings=(),
        artifacts=(),
        raw_details={
            "target_groups": [dict(group) for group in groups],
            "targets": flat_targets,
            "bootstrap_targets": [
                dict(item) for group in groups for item in list(group.get("bootstrap_targets", ()))
            ],
            "warnings": list(warnings),
            "runnable_count": runnable_count,
            "skip_count": skip_count,
            "surface_map": surface_map,
            "config_suggestions": normalized_config_suggestions,
            "bootstrap_suggestions": [dict(item) for item in bootstrap_suggestions],
        },
    )
def _callable_fixture_completeness(rows: Sequence[Mapping[str, Any]]) -> float:
    """Return runnable-target completeness for a callable listing."""
    if not rows:
        return 0.0
    runnable = sum(1 for row in rows if row.get("runnable"))
    return runnable / len(rows)
def _blocked_callable_listing_reason(
    rows: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.0,
) -> str | None:
    """Return a blocking reason when discovery lacks enough runnable targets."""
    if not rows:
        return "no callable targets were discovered"
    completeness = _callable_fixture_completeness(rows)
    if completeness <= 0.0:
        skip_reasons = {str(row.get("skip_reason", "")) for row in rows}
        if {
            "missing object factory",
            "missing state factory",
        } & skip_reasons:
            return "need instance/state harness or object/state factory for discovered methods"
        return "no discovered targets had inferable fixtures or strategies"
    if threshold > 0.0 and completeness < threshold:
        return (
            "fixture completeness is too low for meaningful exploration "
            f"({completeness:.0%} < {threshold:.0%})"
        )
    return None
