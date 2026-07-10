from __future__ import annotations
# ruff: noqa
def _canonical_surface_groups_for_targets(
    targets: Sequence[str],
    *,
    cfg: OrdealConfig | None = None,
    object_specs: Sequence[Any] = (),
    include_private_by_module: Mapping[str, bool] | None = None,
    bootstrap_test_dir: str | None = None,
    resolve_config_imports: bool = True,
) -> list[dict[str, Any]]:
    """Return grouped callable rows for explicit target inputs using one shared path."""
    (
        object_factories,
        object_setups,
        object_scenarios,
        object_state_factories,
        object_teardowns,
        object_harnesses,
    ) = _object_runtime_maps(object_specs, resolve_imports=resolve_config_imports)
    module_requests: dict[str, dict[str, Any]] = {}
    for target in targets:
        normalized_target = _normalize_module_target(str(target))
        module_name = _scan_base_module(normalized_target)
        if ":" not in normalized_target:
            imported_module = False
            with contextlib.suppress(Exception):
                importlib.import_module(module_name)
                imported_module = True
            if not imported_module:
                module_name = _target_module_name(normalized_target)
        request = module_requests.setdefault(
            module_name,
            {"select_all": False, "selectors": []},
        )
        if normalized_target == module_name:
            request["select_all"] = True
            continue
        request["selectors"].append(normalized_target)

    groups: list[dict[str, Any]] = []
    for module_name, request in module_requests.items():
        include_private = bool((include_private_by_module or {}).get(module_name, False))
        try:
            module_contract_checks = (
                _config_contract_checks_for_module(
                    cfg,
                    module_name,
                    resolve_imports=resolve_config_imports,
                )
                if cfg is not None
                else {}
            )
        except Exception:
            module_contract_checks = {}
        try:
            rows = _callable_listing_rows(
                module_name,
                targets=(
                    None
                    if bool(request.get("select_all"))
                    else list(request.get("selectors", ())) or None
                ),
                include_private=include_private,
                object_factories=object_factories,
                object_setups=object_setups,
                object_scenarios=object_scenarios,
                object_state_factories=object_state_factories,
                object_teardowns=object_teardowns,
                object_harnesses=object_harnesses,
                contract_checks=module_contract_checks,
            )
        except Exception:
            rows = []
        bootstrap_targets: list[dict[str, Any]] = []
        if not rows and bootstrap_test_dir is not None:
            with contextlib.suppress(Exception):
                bootstrap_targets = _audit_bootstrap_targets(
                    module_name,
                    include_private=include_private,
                    test_dir=bootstrap_test_dir,
                )
        groups.append(
            {
                "module": module_name,
                "targets": rows,
                "bootstrap_targets": bootstrap_targets,
            }
        )
    return groups
def _hint_mapping_sort_key(hint: Mapping[str, Any]) -> tuple[float, float, int, str, str]:
    """Return a stable descending sort key for serialized harness hints."""
    signals = hint.get("signals")
    signal_count = (
        len([item for item in signals if str(item).strip()])
        if isinstance(signals, Sequence) and not isinstance(signals, (str, bytes, bytearray))
        else 0
    )
    return (
        -float(hint.get("score", 0.0) or 0.0),
        -float(hint.get("confidence", 0.0) or 0.0),
        -signal_count,
        str(hint.get("kind", "")),
        str(hint.get("suggestion", "")),
    )
def _harness_hint_summary(hints: Sequence[Mapping[str, Any]]) -> str | None:
    """Render a compact one-line summary of mined harness hints."""
    best_by_kind: dict[str, Mapping[str, Any]] = {}
    for hint in hints:
        kind = str(hint.get("kind", "")).strip()
        suggestion = str(hint.get("suggestion", "")).strip()
        if not (kind and suggestion):
            continue
        current = best_by_kind.get(kind)
        if current is None or _hint_mapping_sort_key(hint) < _hint_mapping_sort_key(current):
            best_by_kind[kind] = hint
    if not best_by_kind:
        return None
    parts = []
    for kind, hint in best_by_kind.items():
        score = hint.get("score")
        suffix = f" [{float(score):.3f}]" if isinstance(score, (int, float)) else ""
        parts.append(f"{kind}={str(hint.get('suggestion', '')).strip()}{suffix}")
    return "; ".join(parts[:3])
def _harness_hint_config_summary(hints: Sequence[Mapping[str, Any]]) -> str | None:
    """Render a compact one-line summary of mined harness config hints."""
    configs: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        config = hint.get("config")
        if not isinstance(config, Mapping):
            continue
        section = str(config.get("section", "")).strip()
        target = str(config.get("target", "")).strip()
        method = str(config.get("method", "")).strip()
        key = str(config.get("key", "")).strip()
        value = config.get("value")
        value_text = value if isinstance(value, str) else pformat(value, compact=True)
        parts = [part for part in (section, target, method) if part]
        if key:
            parts.append(f"{key}={value_text}")
        elif value is not None:
            parts.append(str(value_text))
        summary = " ".join(parts).strip()
        if summary and summary not in seen:
            seen.add(summary)
            configs.append(summary)
    if not configs:
        return None
    return "; ".join(configs[:3])
def _config_target_for_row(module_name: str, row: Mapping[str, Any]) -> str:
    """Return the config-friendly target string for one callable row."""
    name = str(row.get("name", "")).strip()
    kind = str(row.get("kind", "")).strip()
    if not name:
        return module_name
    if kind in {"instance", "classmethod", "staticmethod"} and "." in name:
        return f"{module_name}:{name}"
    return f"{module_name}.{name}"
def _toml_value(value: Any) -> str:
    """Render a small TOML-compatible value literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if value is None:
        return '""'
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Mapping):
        items = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + items + " }"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))
def _merge_config_suggestion_blocks(
    blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge suggestion fragments into stable TOML block payloads."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for raw in blocks:
        section = str(raw.get("section", "")).strip()
        if not section:
            continue
        target = str(raw.get("target", "")).strip()
        key = (section, target)
        block = merged.get(key)
        if block is None:
            block = {"section": section}
            if target:
                block["target"] = target
            if "module" in raw and str(raw.get("module", "")).strip():
                block["module"] = str(raw.get("module"))
            merged[key] = block
            order.append(key)
        for field_name, field_value in raw.items():
            if field_name in {"section", "target"}:
                continue
            if field_value in (None, "", [], {}):
                continue
            if field_name == "methods":
                items = [str(item) for item in field_value if str(item).strip()]
                existing = list(block.get("methods", []))
                for item in items:
                    if item not in existing:
                        existing.append(item)
                if existing:
                    block["methods"] = existing
                continue
            if field_name in {"checks", "scenarios", "tracked_params", "protected_keys"}:
                items = [item for item in field_value if str(item).strip()]
                existing = list(block.get(field_name, []))
                for item in items:
                    if item not in existing:
                        existing.append(item)
                if existing:
                    block[field_name] = existing
                continue
            block.setdefault(field_name, field_value)
    return [merged[key] for key in order]
def _render_config_suggestion_blocks(blocks: Sequence[Mapping[str, Any]]) -> str:
    """Render merged config suggestion blocks as ready-to-paste TOML."""
    lines: list[str] = []
    key_order = (
        "module",
        "target",
        "methods",
        "factory",
        "state_factory",
        "setup",
        "teardown",
        "harness",
        "scenarios",
        "checks",
        "kwargs",
        "tracked_params",
        "protected_keys",
        "env_param",
        "phase",
        "followup_phases",
        "fault",
        "handler_name",
        "auto_contracts",
        "targets",
        "mode",
        "min_contract_fit",
    )
    for idx, block in enumerate(blocks):
        section = str(block.get("section", "")).strip()
        if not section:
            continue
        if idx:
            lines.append("")
        lines.append(section)
        remaining = [key for key in block.keys() if key != "section"]
        ordered_keys = [key for key in key_order if key in remaining] + [
            key for key in remaining if key not in key_order
        ]
        for key in ordered_keys:
            lines.append(f"{key} = {_toml_value(block[key])}")
    return "\n".join(lines)
def _config_suggestion_payload(
    blocks: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Normalize config suggestion fragments into TOML and structured blocks."""
    merged = _merge_config_suggestion_blocks(blocks)
    if not merged:
        return None
    return {
        "count": len(merged),
        "blocks": merged,
        "toml": _render_config_suggestion_blocks(merged),
    }
def _config_suggestions_from_rows(
    module_name: str,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Build ready-to-paste TOML suggestions from callable discovery rows."""
    blocks: list[dict[str, Any]] = []
    selected = [row for row in rows if bool(row.get("selected", True))]
    if selected:
        names = [
            str(row.get("name", "")).strip()
            for row in selected
            if str(row.get("name", "")).strip()
        ]
        if names:
            blocks.append(
                {
                    "section": "[[scan]]",
                    "module": module_name,
                    "targets": names,
                }
            )
    for row in selected or rows:
        for hint in row.get("harness_hints", []):
            config = hint.get("config")
            if isinstance(config, Mapping):
                blocks.append(dict(config))
        contract_checks = [
            str(item).strip() for item in row.get("contract_checks", []) if str(item).strip()
        ]
        if contract_checks:
            blocks.append(
                {
                    "section": "[[contracts]]",
                    "target": _config_target_for_row(module_name, row),
                    "checks": contract_checks,
                }
            )
    return _config_suggestion_payload(blocks)
def _config_suggestions_from_harness_hints(
    hints: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Build ready-to-paste TOML suggestions from audit/check harness hints."""
    blocks = [
        dict(config)
        for hint in hints
        for config in [hint.get("config")]
        if isinstance(config, Mapping)
    ]
    return _config_suggestion_payload(blocks)
def _config_suggestions_for_contract_checks(
    target: str,
    checks: Sequence[Any],
) -> dict[str, Any] | None:
    """Build a `[[contracts]]` suggestion for explicit check invocations."""
    names = [
        str(getattr(check, "name", "")).strip()
        for check in checks
        if str(getattr(check, "name", "")).strip()
    ]
    if not names:
        return None
    kwargs = dict(getattr(checks[0], "kwargs", {}) or {})
    block: dict[str, Any] = {
        "section": "[[contracts]]",
        "target": target,
        "checks": names,
    }
    if kwargs:
        block["kwargs"] = kwargs
    return _config_suggestion_payload([block])
def _render_target_listing_parts(row: Mapping[str, Any]) -> list[str]:
    """Return the normalized text fragments for one callable discovery row."""

    def _configured_text(enabled: object, source: object) -> str:
        if not enabled:
            return "no"
        return f"yes[{source}]" if source else "yes"

    state_text = (
        "not-needed"
        if not row.get("state_param")
        else _configured_text(
            row.get("state_factory_configured"),
            row.get("state_factory_source"),
        )
    )
    factory_text = (
        "not-needed"
        if not row.get("factory_required")
        else "required, "
        f"configured={_configured_text(row.get('factory_configured'), row.get('factory_source'))}"
    )
    parts = [
        f"kind={row.get('kind')}",
        f"async={row.get('async')}",
        f"selected={'yes' if row.get('selected', True) else 'no'}",
        f"factory={factory_text}",
        (
            f"harness={row.get('harness', 'fresh')}[{row.get('harness_source')}]"
            if row.get("harness_source")
            else f"harness={row.get('harness', 'fresh')}"
        ),
        f"setup={_configured_text(row.get('setup_configured'), row.get('setup_source'))}",
        f"state={state_text}",
        f"teardown={_configured_text(row.get('teardown_configured'), row.get('teardown_source'))}",
        (
            f"scenarios={row.get('scenario_count', 0)}[{row.get('scenario_source')}]"
            if row.get("scenario_source")
            else f"scenarios={row.get('scenario_count', 0)}"
        ),
        f"runnable={'yes' if row.get('runnable') else 'no'}",
    ]
    if row.get("auto_harness"):
        parts.append("auto_harness=yes")
    contract_checks = list(row.get("contract_checks", []))
    if contract_checks:
        parts.append(f"contracts={','.join(contract_checks)}")
    lifecycle_phase = row.get("lifecycle_phase")
    if lifecycle_phase:
        parts.append(f"phase={lifecycle_phase}")
    skip_reason = row.get("skip_reason")
    if skip_reason:
        parts.append(f"skip={skip_reason}")
    hint_summary = _harness_hint_summary(list(row.get("harness_hints", [])))
    if hint_summary:
        parts.append(f"hints={hint_summary}")
    config_summary = _harness_hint_config_summary(list(row.get("harness_hints", [])))
    if config_summary:
        parts.append(f"configs={config_summary}")
    return parts
def _python_path_to_module_name(path_str: str) -> str | None:
    """Convert a project-relative Python file path into an importable module name."""
    path = Path(path_str)
    if path.suffix != ".py":
        return None
    for root in (Path.cwd() / "src", Path.cwd()):
        with contextlib.suppress(ValueError):
            rel = path.resolve().relative_to(root.resolve())
            rel_parts = rel.parts[:-1] if rel.name == "__init__.py" else rel.with_suffix("").parts
            if rel_parts:
                return ".".join(rel_parts)
    return None
def _normalize_hint_config_value(value: Any) -> Any:
    """Normalize harness-hint config values into TOML-ready symbol paths when possible."""
    if isinstance(value, list):
        return [_normalize_hint_config_value(item) for item in value]
    if not isinstance(value, str):
        return value
    match = re.match(r"^(?P<path>.+?\.py):(?P<line>\d+):(?P<name>[A-Za-z_][\w]*)$", value)
    if not match:
        return value
    module_name = _python_path_to_module_name(match.group("path"))
    if module_name is None:
        return value
    return f"{module_name}:{match.group('name')}"
def _toml_key_value(key: str, value: Any) -> str:
    """Render one TOML key/value line."""
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = str(value)
    elif isinstance(value, str):
        rendered = json.dumps(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rendered_items = [_toml_key_value("", item).split(" = ", 1)[-1] for item in value]
        rendered = "[" + ", ".join(rendered_items) + "]"
    elif isinstance(value, Mapping):
        rendered_items = [
            f"{name} = {_toml_key_value('', item).split(' = ', 1)[-1]}"
            for name, item in value.items()
        ]
        rendered = "{ " + ", ".join(rendered_items) + " }"
    else:
        rendered = json.dumps(str(value))
    return f"{key} = {rendered}" if key else rendered
def _config_suggestion(
    *,
    title: str,
    reason: str,
    snippet_lines: Sequence[str],
    section: str,
    target: str | None = None,
    evidence: Sequence[str] = (),
    entries: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build one ready-to-paste ordeal.toml suggestion payload."""
    return {
        "title": title,
        "reason": reason,
        "filename": "ordeal.toml",
        "section": section,
        "target": target,
        "snippet": "\n".join(snippet_lines).rstrip() + "\n",
        "evidence": [str(item) for item in evidence if str(item).strip()],
        "entries": [dict(item) for item in entries],
    }
def _dedupe_config_suggestions(
    suggestions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate config suggestions while preserving order."""
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for suggestion in suggestions:
        snippet = str(suggestion.get("snippet", "")).strip()
        title = str(suggestion.get("title", "")).strip()
        key = (title, snippet)
        if not snippet or key in seen:
            continue
        seen.add(key)
        deduped.append(dict(suggestion))
    return deduped
