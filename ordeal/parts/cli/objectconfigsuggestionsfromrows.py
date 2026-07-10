from __future__ import annotations
# ruff: noqa
def _object_config_suggestions_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    only_names: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Build [[objects]] suggestions from callable listing rows with harness hints."""
    allowed = {str(name) for name in only_names or () if str(name).strip()}
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("name", "")).strip()
        if allowed and name not in allowed:
            continue
        hints = sorted(
            list(row.get("harness_hints", [])),
            key=_hint_mapping_sort_key,
        )
        if not hints or "." not in name:
            continue
        object_target = f"{row.get('module')}:{name.rsplit('.', 1)[0]}"
        method_name = name.rsplit(".", 1)[-1]
        bucket = grouped.setdefault(
            object_target,
            {
                "target": object_target,
                "methods": set(),
                "fields": {},
                "evidence": [],
                "entries": [],
            },
        )
        bucket["methods"].add(method_name)
        if row.get("harness") == "stateful" or row.get("state_param"):
            bucket["fields"].setdefault("harness", "stateful")
        for hint in hints:
            config = hint.get("config")
            if not isinstance(config, Mapping) or str(config.get("section", "")) != "[[objects]]":
                continue
            key = str(config.get("key", "")).strip()
            if not key:
                continue
            value = _normalize_hint_config_value(config.get("value"))
            if key == "scenarios":
                existing = list(bucket["fields"].get("scenarios", []))
                for item in list(value) if isinstance(value, list) else [value]:
                    if item not in existing:
                        existing.append(item)
                bucket["fields"]["scenarios"] = existing
            elif key not in bucket["fields"]:
                bucket["fields"].setdefault(key, value)
            else:
                continue
            bucket["evidence"].append(str(hint.get("evidence", "")))
            entry = {
                "section": "[[objects]]",
                "target": object_target,
                "method": method_name,
                "key": key,
                "value": value,
            }
            if isinstance(hint.get("score"), (int, float)):
                entry["score"] = round(float(hint["score"]), 3)
            if hint.get("signals"):
                entry["signals"] = list(hint["signals"])
            bucket["entries"].append(entry)

    suggestions: list[dict[str, Any]] = []
    for object_target, bucket in grouped.items():
        fields = dict(bucket["fields"])
        useful_keys = {"factory", "setup", "state_factory", "teardown", "scenarios"}
        if not (useful_keys & fields.keys()):
            continue
        lines = ["[[objects]]", _toml_key_value("target", object_target)]
        methods = sorted(bucket["methods"])
        if methods:
            lines.append(_toml_key_value("methods", methods))
        if fields.get("harness") == "stateful":
            lines.append(_toml_key_value("harness", "stateful"))
        for key in ("factory", "setup", "state_factory", "teardown", "scenarios"):
            if key in fields:
                lines.append(_toml_key_value(key, fields[key]))
        suggestions.append(
            _config_suggestion(
                title=f"Add an object harness for {object_target}",
                reason="Persist the factory/setup/scenario hooks needed to reach bound methods.",
                snippet_lines=lines,
                section="[[objects]]",
                target=object_target,
                evidence=sorted(dict.fromkeys(bucket["evidence"]))[:5],
                entries=bucket["entries"],
            )
        )
    return suggestions
def _audit_target_config_suggestions_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    only_names: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Build `[[audit.targets]]` suggestions from discovered method surfaces."""
    allowed = {str(name) for name in only_names or () if str(name).strip()}
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name or "." not in name:
            continue
        if allowed and name not in allowed:
            continue
        module_name = str(row.get("module", "")).strip()
        if not module_name:
            continue
        owner_name, method_name = name.rsplit(".", 1)
        object_target = f"{module_name}:{owner_name}"
        bucket = grouped.setdefault(
            object_target,
            {
                "target": object_target,
                "module": module_name,
                "methods": set(),
                "fields": {},
                "evidence": [],
                "entries": [],
            },
        )
        bucket["methods"].add(method_name)
        if row.get("harness") == "stateful" or row.get("state_param"):
            bucket["fields"].setdefault("harness", "stateful")
        for hint in list(row.get("harness_hints", [])):
            config = hint.get("config")
            if not isinstance(config, Mapping):
                continue
            if str(config.get("section", "")).strip() != "[[objects]]":
                continue
            key = str(config.get("key", "")).strip()
            if not key:
                continue
            value = _normalize_hint_config_value(config.get("value"))
            if key == "scenarios":
                existing = list(bucket["fields"].get("scenarios", []))
                for item in list(value) if isinstance(value, list) else [value]:
                    if item not in existing:
                        existing.append(item)
                bucket["fields"]["scenarios"] = existing
            else:
                bucket["fields"].setdefault(key, value)
            evidence = str(hint.get("evidence", "")).strip()
            if evidence:
                bucket["evidence"].append(evidence)
            bucket["entries"].append(
                {
                    "section": "[[audit.targets]]",
                    "target": object_target,
                    "method": method_name,
                    "key": key,
                    "value": value,
                }
            )
        surface = row.get("surface")
        if isinstance(surface, Mapping):
            support = dict(surface.get("support", {}))
            evidence = dict(surface.get("evidence", {}))
            tests = dict(evidence.get("tests", {}))
            docs = dict(evidence.get("docs", {}))
            support_tests = list(tests.get("supporting_hints", ()))
            for item in support_tests[:3]:
                text = str(item).strip()
                if text:
                    bucket["evidence"].append(text)
            for path in list(docs.get("files", ()))[:2]:
                text = str(path).strip()
                if text:
                    bucket["evidence"].append(text)
            if support.get("harness", {}).get("mode") == "stateful":
                bucket["fields"].setdefault("harness", "stateful")

    suggestions: list[dict[str, Any]] = []
    for object_target, bucket in grouped.items():
        methods = sorted(bucket["methods"])
        if not methods:
            continue
        lines = ["[[audit.targets]]", _toml_key_value("target", object_target)]
        fields = dict(bucket["fields"])
        if fields.get("harness") == "stateful":
            lines.append(_toml_key_value("harness", "stateful"))
        for key in ("factory", "setup", "state_factory", "teardown", "scenarios"):
            if key in fields:
                lines.append(_toml_key_value(key, fields[key]))
        lines.append(_toml_key_value("methods", methods))
        suggestions.append(
            _config_suggestion(
                title=f"Pin audit targets for {object_target}",
                reason=(
                    "Carry the observed method surface into [audit] so follow-up reviews stay "
                    "focused on the same class methods and harness hooks."
                ),
                snippet_lines=lines,
                section="[[audit.targets]]",
                target=object_target,
                evidence=sorted(dict.fromkeys(bucket["evidence"]))[:5],
                entries=[
                    {
                        "section": "[[audit.targets]]",
                        "target": object_target,
                        "methods": methods,
                        **fields,
                    }
                ],
            )
        )
    return suggestions
def _contract_target_name(module_name: str, function_name: str) -> str:
    """Render one config target name for a function or bound method."""
    return (
        f"{module_name}:{function_name}"
        if "." in function_name and not function_name.startswith(f"{module_name}.")
        else f"{module_name}.{function_name}"
    )
def _contract_suggestion_entries_from_checks(
    target: str,
    checks: Sequence[Any],
) -> list[dict[str, Any]]:
    """Build [[contracts]] suggestions from explicit ContractCheck objects."""
    entries: list[dict[str, Any]] = []
    for check in checks:
        metadata = dict(getattr(check, "metadata", {}) or {})
        kwargs = dict(getattr(check, "kwargs", {}) or {})
        entry: dict[str, Any] = {
            "section": "[[contracts]]",
            "target": target,
            "checks": [str(getattr(check, "name", "contract"))],
        }
        if kwargs:
            entry["kwargs"] = kwargs
        if (
            entry["checks"][0]
            in {
                "shell_safe",
                "quoted_paths",
                "command_arg_stability",
                "subprocess_argv",
            }
            and kwargs
        ):
            entry["tracked_params"] = list(kwargs)
        if entry["checks"][0] == "protected_env_keys":
            env_param = next(
                (name for name, value in kwargs.items() if isinstance(value, Mapping)),
                None,
            )
            env_value = kwargs.get(env_param) if env_param is not None else None
            protected_keys = [
                key
                for key in ("PATH", "HOME", "PWD", "TMPDIR")
                if isinstance(env_value, Mapping) and key in env_value
            ]
            if env_param is not None:
                entry["env_param"] = env_param
            if protected_keys:
                entry["protected_keys"] = protected_keys
        if str(metadata.get("kind")) == "lifecycle":
            if metadata.get("phase"):
                entry["phase"] = metadata["phase"]
            if metadata.get("followup_phases"):
                entry["followup_phases"] = list(metadata["followup_phases"])
            if metadata.get("fault"):
                entry["fault"] = metadata["fault"]
            if metadata.get("handler_name"):
                entry["handler_name"] = metadata["handler_name"]
        entries.append(entry)
    return entries
def _contract_config_suggestions_from_checks(
    target: str,
    checks: Sequence[Any],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    """Build ready-to-paste [[contracts]] suggestions from explicit checks."""
    suggestions: list[dict[str, Any]] = []
    for entry in _contract_suggestion_entries_from_checks(target, checks):
        lines = ["[[contracts]]", _toml_key_value("target", entry["target"])]
        lines.append(_toml_key_value("checks", entry["checks"]))
        for key in (
            "kwargs",
            "tracked_params",
            "protected_keys",
            "env_param",
            "phase",
            "followup_phases",
            "fault",
            "handler_name",
        ):
            if key in entry:
                lines.append(_toml_key_value(key, entry[key]))
        suggestions.append(
            _config_suggestion(
                title=f"Persist explicit contract {entry['checks'][0]} for {target}",
                reason=reason,
                snippet_lines=lines,
                section="[[contracts]]",
                target=target,
                entries=[entry],
            )
        )
    return suggestions
def _contract_config_suggestions_from_scan_details(
    module_name: str,
    details: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build [[contracts]] suggestions from scan contract findings."""
    suggestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for detail in details:
        if str(detail.get("kind")) != "contract":
            continue
        function_name = str(detail.get("function", "")).strip()
        check_name = str(detail.get("name", "")).strip()
        if not function_name or not check_name:
            continue
        kwargs = detail.get("failing_args")
        target = _contract_target_name(module_name, function_name)
        entry = {
            "section": "[[contracts]]",
            "target": target,
            "checks": [check_name],
        }
        if isinstance(kwargs, Mapping) and kwargs:
            entry["kwargs"] = dict(kwargs)
        key = (target, check_name, pformat(entry.get("kwargs", {}), compact=True))
        if key in seen:
            continue
        seen.add(key)
        lines = ["[[contracts]]", _toml_key_value("target", target)]
        lines.append(_toml_key_value("checks", [check_name]))
        if "kwargs" in entry:
            lines.append(_toml_key_value("kwargs", entry["kwargs"]))
        suggestions.append(
            _config_suggestion(
                title=f"Persist scan contract {check_name} for {target}",
                reason=(
                    "Keep the failing semantic contract under versioned config for "
                    "repeatable checks."
                ),
                snippet_lines=lines,
                section="[[contracts]]",
                target=target,
                entries=[entry],
            )
        )
    return suggestions
def _scan_config_suggestions(
    module_name: str,
    *,
    mode: str,
    max_examples: int,
    scan_targets: Sequence[str],
    include_private: bool,
    seed_from_call_sites: bool,
    min_contract_fit: float,
    min_reachability: float,
    min_realism: float,
    security_focus: bool,
    ignore_properties: Sequence[str],
    ignore_relations: Sequence[str],
    auto_contracts: Sequence[str],
    sampling: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build config suggestions for a scan invocation."""
    lines = ["[[scan]]", _toml_key_value("module", module_name)]
    if sampling is not None and sampling.get("targets"):
        lines.append(_toml_key_value("targets", list(sampling.get("targets", ()))))
    elif scan_targets:
        lines.append(_toml_key_value("targets", list(scan_targets)))
    public_mode = _public_scan_mode(mode)
    if public_mode != "evidence":
        lines.append(_toml_key_value("mode", public_mode))
    if max_examples != 50:
        lines.append(_toml_key_value("max_examples", max_examples))
    if include_private:
        lines.append(_toml_key_value("include_private", True))
    if not seed_from_call_sites:
        lines.append(_toml_key_value("seed_from_call_sites", False))
    if min_contract_fit != 0.55:
        lines.append(_toml_key_value("min_contract_fit", min_contract_fit))
    if min_reachability != 0.45:
        lines.append(_toml_key_value("min_reachability", min_reachability))
    if min_realism != 0.55:
        lines.append(_toml_key_value("min_realism", min_realism))
    if security_focus:
        lines.append(_toml_key_value("security_focus", True))
    if ignore_properties:
        lines.append(_toml_key_value("ignore_properties", list(ignore_properties)))
    if ignore_relations:
        lines.append(_toml_key_value("ignore_relations", list(ignore_relations)))
    if auto_contracts:
        lines.append(_toml_key_value("auto_contracts", list(auto_contracts)))
    suggestions = [
        _config_suggestion(
            title=(
                f"Persist the sampled scan surface for {module_name}"
                if sampling is not None
                else f"Persist scan defaults for {module_name}"
            ),
            reason=(
                "Freeze the current sampled package-root surface for repeatable follow-up scans."
                if sampling is not None
                else "Keep this scan target selection and runtime policy in ordeal.toml."
            ),
            snippet_lines=lines,
            section="[[scan]]",
            target=module_name,
            entries=[
                {
                    "section": "[[scan]]",
                    "module": module_name,
                    "targets": (
                        list(sampling.get("targets", ()))
                        if sampling is not None
                        else list(scan_targets)
                    ),
                    "mode": public_mode,
                    "max_examples": max_examples,
                    "security_focus": security_focus,
                }
            ],
        )
    ]
    suggestions.extend(_object_config_suggestions_from_rows(rows))
    suggestions.extend(_audit_target_config_suggestions_from_rows(rows))
    suggestions.extend(_contract_config_suggestions_from_scan_details(module_name, details))
    return _dedupe_config_suggestions(suggestions)
