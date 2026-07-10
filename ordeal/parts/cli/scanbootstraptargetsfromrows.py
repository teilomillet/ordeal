from __future__ import annotations
# ruff: noqa
def _scan_bootstrap_targets_from_rows(
    module_name: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    test_dir: str = "tests",
) -> list[dict[str, Any]]:
    """Build review-first support-target scaffolds from scan callable rows."""
    support_module = _bootstrap_support_module_name(test_dir)
    support_path = _bootstrap_support_file_path(test_dir)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not bool(row.get("selected", True)):
            continue
        if str(row.get("kind", "")) != "instance":
            continue
        name = str(row.get("name", "")).strip()
        if "." not in name:
            continue
        owner_name, method_name = name.rsplit(".", 1)
        class_name = owner_name.rsplit(".", 1)[-1]
        key = f"{module_name}:{owner_name}"
        bucket = grouped.setdefault(
            key,
            {
                "module": module_name,
                "target": key,
                "class_name": class_name,
                "methods": set(),
                "review_scenarios": set(),
                "support_module": support_module,
                "support_path": support_path,
                "support_factory": f"{support_module}:make_{_camel_to_snake(class_name)}",
                "support_setup": None,
                "support_state_factory": None,
                "support_teardown": None,
                "support_scenarios": [],
                "scenario_libraries": [],
                "evidence": [],
                "harness": "fresh",
            },
        )
        bucket["methods"].add(method_name)
        owner = _resolve_qualname_attr(module_name, owner_name)
        if isinstance(owner, type):
            for label in _bootstrap_review_scenarios_for_method(owner, method_name):
                bucket["review_scenarios"].add(label)
        hints = list(row.get("harness_hints", []))
        for hint in hints:
            evidence = str(hint.get("evidence", "")).strip()
            if evidence:
                bucket["evidence"].append(evidence)
            config = hint.get("config")
            if not isinstance(config, Mapping):
                continue
            key_name = str(config.get("key", "")).strip()
            value = _normalize_hint_config_value(config.get("value"))
            if key_name == "scenarios":
                values = value if isinstance(value, list) else [value]
                libraries = [
                    str(item)
                    for item in values
                    if isinstance(item, str) and ":" not in item and "." not in item
                ]
                for library in libraries:
                    if library not in bucket["scenario_libraries"]:
                        bucket["scenario_libraries"].append(library)
            elif key_name == "setup":
                bucket["support_setup"] = f"{support_module}:prime_{_camel_to_snake(class_name)}"
            elif key_name == "state_factory":
                bucket["support_state_factory"] = (
                    f"{support_module}:make_{_camel_to_snake(class_name)}_state"
                )
            elif key_name == "teardown":
                bucket["support_teardown"] = (
                    f"{support_module}:cleanup_{_camel_to_snake(class_name)}"
                )
            elif key_name == "harness" and str(value) == "stateful":
                bucket["harness"] = "stateful"
        if row.get("setup_configured") and bucket["support_setup"] is None:
            bucket["support_setup"] = f"{support_module}:prime_{_camel_to_snake(class_name)}"
        if row.get("state_param") and bucket["support_state_factory"] is None:
            bucket["support_state_factory"] = (
                f"{support_module}:make_{_camel_to_snake(class_name)}_state"
            )
        if row.get("teardown_configured") and bucket["support_teardown"] is None:
            bucket["support_teardown"] = f"{support_module}:cleanup_{_camel_to_snake(class_name)}"
        if str(row.get("harness", "fresh")) == "stateful":
            bucket["harness"] = "stateful"
        if bucket["support_setup"] is None and (
            hints or row.get("scenario_count") or row.get("setup_configured")
        ):
            bucket["support_setup"] = f"{support_module}:prime_{_camel_to_snake(class_name)}"

    results: list[dict[str, Any]] = []
    for bucket in grouped.values():
        results.append(
            {
                **bucket,
                "methods": sorted(bucket["methods"]),
                "review_scenarios": sorted(bucket["review_scenarios"]),
                "support_scenarios": list(bucket["support_scenarios"]),
                "scenario_libraries": list(bucket["scenario_libraries"]),
                "evidence": list(dict.fromkeys(bucket["evidence"]))[:5],
            }
        )
    return sorted(results, key=lambda item: str(item["target"]))
def _scan_support_suggestions(
    module_name: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build review scaffolds for scan-discovered bound methods."""
    bootstrap_targets = _scan_bootstrap_targets_from_rows(module_name, rows)
    if not bootstrap_targets:
        return []
    return _audit_bootstrap_support_suggestions(
        [{"module": module_name, "bootstrap_targets": bootstrap_targets}],
        validation_mode="fast",
    )
def _scan_scenario_library_records(
    module_name: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build one scenario-library review payload from selected callable rows."""
    from ordeal.auto import available_object_scenario_libraries

    relevant_rows = [
        row
        for row in rows
        if bool(row.get("selected", True))
        and (
            str(row.get("kind", "")) == "instance"
            or list(row.get("harness_hints", ()))
            or int(row.get("scenario_count", 0)) > 0
        )
    ]
    if not relevant_rows:
        return []

    available = list(available_object_scenario_libraries())
    inferred: dict[str, dict[str, Any]] = {}
    for row in relevant_rows:
        name = str(row.get("name", "")).strip()
        for hint in list(row.get("harness_hints", ())):
            config = hint.get("config")
            if not isinstance(config, Mapping) or str(config.get("key", "")) != "scenarios":
                continue
            value = _normalize_hint_config_value(config.get("value"))
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str) or ":" in item or "." in item:
                    continue
                pack = str(item).strip()
                if not pack:
                    continue
                bucket = inferred.setdefault(
                    pack,
                    {
                        "name": pack,
                        "targets": [],
                        "evidence": [],
                    },
                )
                bucket["targets"].append(name)
                evidence = str(hint.get("evidence", "")).strip()
                if evidence:
                    bucket["evidence"].append(evidence)

    return [
        {
            "title": f"Reusable scenario libraries for {module_name}",
            "filename": _default_scan_scenario_library_path(module_name),
            "target": module_name,
            "available": available,
            "inferred": [
                {
                    "name": name,
                    "targets": list(dict.fromkeys(item["targets"])),
                    "evidence": list(dict.fromkeys(item["evidence"]))[:5],
                }
                for name, item in sorted(inferred.items())
            ],
        }
    ]
def _finding_identity(detail: dict[str, Any]) -> dict[str, Any]:
    """Return the stable identity fields for one finding."""
    return {
        "module": detail.get("module"),
        "function": detail.get("function"),
        "kind": detail.get("kind"),
        "name": detail.get("name"),
    }
def _finding_fingerprint(detail: dict[str, Any]) -> str:
    """Return a stable fingerprint for correlating the same finding across runs."""
    payload = json.dumps(_finding_identity(detail), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
def _annotate_finding(
    detail: dict[str, Any],
    *,
    regression_path: Path | None = None,
    guard_command: str | None = None,
) -> dict[str, Any]:
    """Attach stable IDs to a normalized finding detail record."""
    fingerprint = _finding_fingerprint(detail)
    generated_test, generated_binding = _regression_metadata(detail)
    regression_test = detail.get("regression_test") or generated_test
    regression_binding = detail.get("regression_binding") or generated_binding
    evidence = detail.get("evidence")
    if isinstance(evidence, Mapping):
        evidence = _evidence_with_regression_binding(
            evidence,
            regression_binding,
            regression_path=regression_path,
            guard_command=guard_command,
        )
    category = detail.get("category")
    return {
        **detail,
        "evidence": evidence,
        "evidence_class": _evidence_class_for_category(str(category)) if category else None,
        "finding_id": f"fnd_{fingerprint[:12]}",
        "fingerprint": fingerprint,
        "status": "open",
        "regression_test": regression_test,
        "regression_binding": regression_binding,
    }
def _read_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON artifact from disk."""
    return json.loads(path.read_text(encoding="utf-8"))
def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON artifact with stable formatting."""
    path.write_text(json.dumps(_json_safe_value(payload), indent=2) + "\n", encoding="utf-8")
def _resolve_artifact_path(path_str: str | None, *, workspace: str | None = None) -> Path | None:
    """Resolve an artifact path against the recorded workspace when needed."""
    if not path_str:
        return None
    path = Path(path_str)
    if path.is_absolute():
        return path
    if workspace:
        return Path(workspace) / path
    return path
def _python_literal(value: Any, *, trim: bool = True) -> str:
    """Render a stable Python literal for regression stubs."""
    rendered = (
        _trim_report_value(value, max_depth=4, max_items=6, max_string=80) if trim else value
    )
    return pformat(rendered, width=88, sort_dicts=False)
def _property_impact(detail: dict[str, Any]) -> str:
    """Explain why a mined property violation matters."""
    name = detail.get("name", "")
    messages = {
        "deterministic": "the same input produced different outputs across repeated calls.",
        "idempotent": "calling the function again changed a value that should have stabilized.",
        "involution": "running the function twice failed to recover the original value.",
        "never None": "a generated input returned None where callers likely expect a real value.",
        "no NaN": "a generated input produced NaN, which can silently poison downstream math.",
        "commutative": (
            "swapping the operands changed the result, so behavior depends on argument order."
        ),
        "associative": (
            "grouping equivalent operations changed the result,"
            " which hints at an algebraic edge case."
        ),
        "bijective": "distinct inputs collapsed to the same output, so information is being lost.",
    }
    return messages.get(
        name,
        "this property held for most examples but not all,"
        " which suggests a boundary or consistency bug.",
    )
def _render_regression_stub(
    module: str,
    detail: dict[str, Any],
    *,
    trim: bool = True,
) -> str | None:
    """Generate a compact pytest stub for concrete findings when possible."""
    function = detail.get("function")
    if not function:
        return None
    function = str(function)

    slug = _slugify_report_name(detail.get("name") or detail.get("kind", "finding"))
    function_slug = _slugify_report_name(function)
    test_name = f"test_{function_slug}_{slug}_regression"
    function_parts = [part for part in function.split(".") if part]
    proof = detail.get("proof_bundle")
    reproduction: Mapping[str, Any] = {}
    if isinstance(proof, Mapping):
        candidate = proof.get("minimal_reproduction") or proof.get("reproduction")
        if isinstance(candidate, Mapping):
            reproduction = candidate

    if len(function_parts) > 1:
        import_name = function_parts[0]
        call_expr = ".".join(function_parts)
    else:
        import_name = function
        call_expr = function
    lines = [f"from {module} import {import_name}", "", "", f"def {test_name}() -> None:"]

    kind = detail.get("kind")
    counterexample = detail.get("counterexample") or {}
    failing_args = detail.get("failing_args")
    raw_input = counterexample.get("input")
    input_args = raw_input if isinstance(raw_input, dict) else None
    name = detail.get("name")

    if kind in {"crash", "coverage_gap", "contract"} and isinstance(failing_args, dict):
        if len(function_parts) > 1 and not bool(reproduction.get("direct_call_supported")):
            snippet = reproduction.get("python_snippet")
            if not bool(reproduction.get("harness_replay_supported")) or not isinstance(
                snippet, str
            ):
                return None
            lines = [f"def {test_name}() -> None:"]
            lines.extend(f"    {line}" if line else "" for line in snippet.splitlines())
            return "\n".join(lines)
        lines.append(f"    args = {_python_literal(failing_args, trim=trim)}")
        lines.append(f"    {call_expr}(**args)")
        return "\n".join(lines)

    if len(function_parts) > 1 and not bool(reproduction.get("direct_call_supported")):
        return None

    if kind == "property" and name == "bijective":
        colliding = counterexample.get("colliding_inputs")
        if not isinstance(colliding, (list, tuple)) or len(colliding) < 2:
            return None
        try:
            first_args = dict(colliding[0])
            second_args = dict(colliding[1])
        except (TypeError, ValueError):
            return None
        lines.append(f"    first_args = {_python_literal(first_args, trim=trim)}")
        lines.append(f"    second_args = {_python_literal(second_args, trim=trim)}")
        lines.append(
            f"    assert first_args == second_args or "
            f"{call_expr}(**first_args) != {call_expr}(**second_args)"
        )
        return "\n".join(lines)

    if kind != "property" or not isinstance(input_args, dict) or not input_args:
        return None

    first_param = next(iter(input_args))
    lines.append(f"    args = {_python_literal(input_args, trim=trim)}")

    if name == "deterministic":
        lines.append(f"    first = {call_expr}(**args)")
        lines.append(f"    second = {call_expr}(**args)")
        lines.append("    assert second == first")
        return "\n".join(lines)

    if name == "idempotent":
        lines.append(f"    first = {call_expr}(**args)")
        lines.append("    replay_args = dict(args)")
        lines.append(f"    replay_args[{first_param!r}] = first")
        lines.append(f"    second = {call_expr}(**replay_args)")
        lines.append("    assert second == first")
        return "\n".join(lines)

    if name == "involution":
        lines.append(f"    first = {call_expr}(**args)")
        lines.append("    replay_args = dict(args)")
        lines.append(f"    replay_args[{first_param!r}] = first")
        lines.append(f"    second = {call_expr}(**replay_args)")
        lines.append(f"    assert second == args[{first_param!r}]")
        return "\n".join(lines)

    if name == "never None":
        lines.append(f"    assert {call_expr}(**args) is not None")
        return "\n".join(lines)

    if name == "no NaN":
        lines.append("    from ordeal.invariants import no_nan")
        lines.append(f"    no_nan({call_expr}(**args))")
        return "\n".join(lines)

    if name == "commutative" and isinstance(counterexample.get("swapped_input"), dict):
        swapped_lit = _python_literal(counterexample["swapped_input"], trim=trim)
        lines.append(f"    swapped = {swapped_lit}")
        lines.append(f"    left = {call_expr}(**args)")
        lines.append(f"    right = {call_expr}(**swapped)")
        lines.append("    assert right == left")
        return "\n".join(lines)

    if name == "associative" and "third" in input_args and len(input_args) == 3:
        param_names = [param for param in input_args if param != "third"]
        if len(param_names) != 2:
            return None
        left_name, right_name = param_names
        lines.append(f"    a = args[{left_name!r}]")
        lines.append(f"    b = args[{right_name!r}]")
        lines.append("    c = args['third']")
        lines.append(
            f"    left = {call_expr}(**{{{left_name!r}: a, {right_name!r}: "
            f"{call_expr}(**{{{left_name!r}: b, {right_name!r}: c}})}})"
        )
        lines.append(
            f"    right = {call_expr}(**{{{left_name!r}: "
            f"{call_expr}(**{{{left_name!r}: a, {right_name!r}: b}}), "
            f"{right_name!r}: c}})"
        )
        lines.append("    assert right == left")
        return "\n".join(lines)

    return None
def _append_proof_bundle(lines: list[str], detail: dict[str, Any]) -> None:
    """Render proof-bundle details when present."""
    proof = detail.get("proof_bundle")
    if not isinstance(proof, Mapping):
        return
    witness = proof.get("witness") or proof.get("valid_input_witness")
    contract_basis = proof.get("contract_basis")
    confidence_breakdown = proof.get("confidence_breakdown")
    reproduction = proof.get("minimal_reproduction") or proof.get("reproduction")
    failure_path = proof.get("failure_path") or proof.get("failing_path")
    impact = proof.get("impact") or proof.get("likely_impact")
    verdict = proof.get("verdict")
    if witness:
        lines.extend(["", "Proof bundle:"])
        lines.extend(_json_block(witness))
    if contract_basis:
        lines.extend(["", "Contract basis:"])
        lines.extend(_json_block(contract_basis))
    if confidence_breakdown:
        lines.extend(["", "Confidence breakdown:"])
        lines.extend(_json_block(confidence_breakdown))
    if reproduction:
        lines.extend(["", "Minimal reproduction:"])
        if isinstance(reproduction, Mapping):
            rendered = dict(reproduction)
            snippet = rendered.pop("python_snippet", None)
            if rendered:
                lines.extend(_json_block(rendered))
            if snippet:
                lines.extend(["", "Python snippet:"])
                lines.extend(_python_block(str(snippet)))
        else:
            lines.extend(_json_block(reproduction))
    if failure_path:
        lines.extend(["", "Failure path:"])
        lines.extend(_json_block(failure_path))
    if impact:
        if isinstance(impact, Mapping):
            summary = impact.get("summary")
            if summary:
                lines.append(f"- Likely impact: {summary}")
            extra = {key: value for key, value in impact.items() if key != "summary"}
            if extra:
                lines.extend(["", "Impact details:"])
                lines.extend(_json_block(extra))
        else:
            lines.append(f"- Likely impact: {impact}")
    if isinstance(verdict, Mapping) and verdict.get("demotion_reason"):
        lines.append(f"- Demotion reason: {verdict['demotion_reason']}")
def _append_finding_evidence(lines: list[str], detail: Mapping[str, Any]) -> None:
    """Render the compact user-facing evidence card when present."""
    card = detail.get("evidence")
    if not isinstance(card, Mapping):
        return
    lines.extend(["", "Evidence card:"])
    lines.extend(f"- {label}: {value}" for label, value in _evidence_card_fields(card))
