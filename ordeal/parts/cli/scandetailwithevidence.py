from __future__ import annotations
# ruff: noqa
def _scan_detail_with_evidence(
    module: str,
    detail: Mapping[str, Any],
    *,
    regression_path: Path | None = None,
    guard_command: str | None = None,
) -> dict[str, Any]:
    """Normalize one scan detail and attach its compact evidence card."""
    from ordeal.finding_evidence import _build_finding_evidence

    normalized = {
        **dict(detail),
        "module": module,
        "qualname": detail.get("qualname") or f"{module}.{detail.get('function', '?')}",
    }
    if normalized.get("kind") == "blocked":
        normalized["evidence"] = {
            "schema": "ordeal.scan-limitation/v1",
            "status": "blocked",
            "subject": {
                "target": normalized["qualname"],
                "source_sha256": normalized.get("source_sha256"),
            },
            "limitation": {
                "kind": normalized.get("limitation_kind"),
                "reason": normalized.get("blocking_reason") or normalized.get("error"),
            },
            "boundaries": {
                "establishes": "Ordeal did not reach a measured execution of the target.",
                "does_not_establish": [
                    "that the target crashed",
                    "that the target is correct",
                    "that the blocked path is unreachable",
                ],
            },
        }
        normalized["regression_test"] = None
        normalized["regression_binding"] = None
        return normalized
    regression_test, regression_binding = _regression_metadata(normalized)
    evidence = _build_finding_evidence(normalized, module=module)
    normalized["evidence"] = _evidence_with_regression_binding(
        evidence,
        regression_binding,
        regression_path=regression_path,
        guard_command=guard_command,
    )
    normalized["regression_test"] = regression_test
    normalized["regression_binding"] = regression_binding
    return normalized
def _regression_metadata(
    detail: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the generated regression name and semantic binding for one finding."""
    from ordeal.regression_evidence import _regression_binding

    module = str(detail.get("module") or "").strip()
    if not module:
        return None, None
    stub = _render_regression_stub(module, dict(detail), trim=False)
    test_name = _regression_test_name(stub) if stub else None
    binding = _regression_binding(stub, test_name) if stub and test_name else None
    return test_name, binding
def _evidence_with_regression_binding(
    evidence: Mapping[str, Any],
    binding: Mapping[str, Any] | None,
    *,
    regression_path: Path | None = None,
    guard_command: str | None = None,
) -> dict[str, Any]:
    """Attach one generated regression binding to a copied evidence card."""
    copied = dict(evidence)
    control = copied.get("post_fix_control")
    if isinstance(control, Mapping) and binding is not None:
        copied["post_fix_control"] = {
            **dict(control),
            "regression_binding": dict(binding),
        }
        saved = regression_path is not None
        copied["regression"] = {
            "status": "saved" if saved else "generated",
            "path": _display_path(regression_path) if regression_path is not None else None,
            "test_name": binding.get("test_name"),
            "binding": dict(binding),
        }
        if saved:
            copied["ci_guard"] = {
                "status": "ready",
                "command": guard_command,
                "acceptance": "The bound regression must remain unchanged and pass in CI.",
            }
        workflow = copied.get("workflow")
        if isinstance(workflow, Mapping):
            copied["workflow"] = {
                **dict(workflow),
                "save_regression": "saved" if saved else "generated",
                "guard_ci": "ready" if saved else workflow.get("guard_ci", "not_ready"),
            }
    return copied
def _evidence_card_fields(card: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return compact label/value pairs for text and Markdown renderers."""
    fields = [
        ("Status", str(card.get("status") or "unknown")),
        ("Claim", str(card.get("claim") or "No bounded claim available.")),
    ]
    subject = card.get("subject")
    if isinstance(subject, Mapping):
        source_sha256 = subject.get("source_sha256")
        trace_sha256 = subject.get("trace_sha256")
        if source_sha256:
            binding = f"callable source sha256={source_sha256}"
        elif trace_sha256:
            binding = f"compose trace sha256={trace_sha256}"
        else:
            binding = "callable source hash unavailable"
        fields.append(("Code binding", binding))
    witness = card.get("witness")
    if isinstance(witness, Mapping) and witness.get("available"):
        rendered_input = json.dumps(
            _trim_report_value(witness.get("input")),
            sort_keys=True,
            default=str,
        )
        fields.append(
            (
                "Witness",
                f"sha256={witness.get('sha256')} input={rendered_input}",
            )
        )
    replay = card.get("replay")
    if isinstance(replay, Mapping):
        fields.append(
            (
                "Replay",
                f"{replay.get('status')} "
                f"({replay.get('exact_matches', 0)}/{replay.get('attempts', 0)} exact matches)",
            )
        )
    minimization = card.get("minimization")
    if isinstance(minimization, Mapping):
        size = ""
        if minimization.get("original_complexity") is not None:
            size = (
                f" ({minimization.get('original_complexity')}→"
                f"{minimization.get('minimized_complexity')} complexity units)"
            )
        fields.append(
            (
                "Minimization",
                f"{minimization.get('status')}"
                f" via {minimization.get('method') or 'no method'}{size}",
            )
        )
    regression = card.get("regression")
    if isinstance(regression, Mapping):
        location = regression.get("path") or regression.get("test_name") or "not saved"
        fields.append(("Regression", f"{regression.get('status')}: {location}"))
    control = card.get("post_fix_control")
    if isinstance(control, Mapping):
        fields.append(
            (
                "Post-fix control",
                f"{control.get('status')}: {control.get('acceptance')}",
            )
        )
        binding = control.get("regression_binding")
        if isinstance(binding, Mapping):
            import_hashes = ",".join(str(item) for item in binding.get("import_ast_sha256", ()))
            fields.append(
                (
                    "Regression binding",
                    f"test AST sha256={binding.get('test_ast_sha256')} "
                    f"import AST sha256=[{import_hashes}]",
                )
            )
    guard = card.get("ci_guard")
    if isinstance(guard, Mapping):
        command = f": {guard.get('command')}" if guard.get("command") else ""
        fields.append(("CI guard", f"{guard.get('status')}{command}"))
    coverage = card.get("reliability_coverage")
    if isinstance(coverage, Mapping):
        summary = coverage.get("summary")
        if isinstance(summary, Mapping):
            fields.append(
                (
                    "Reliability coverage",
                    f"{summary.get('pass', 0)} pass, "
                    f"{summary.get('not_exercised', 0)} not exercised, "
                    f"{summary.get('fail', 0)} fail",
                )
            )
    protection = card.get("test_protection")
    if isinstance(protection, Mapping):
        fields.append(
            (
                "Test protection",
                f"{protection.get('status', 'inconclusive')}: "
                f"{protection.get('summary', 'not measured')}",
            )
        )
    boundaries = card.get("boundaries")
    if isinstance(boundaries, Mapping):
        limits = ", ".join(str(item) for item in boundaries.get("does_not_establish", ()))
        fields.append(("Boundary", f"{boundaries.get('establishes')} Not: {limits}."))
    return fields
_SPECULATIVE_SCAN_CATEGORIES = {
    "speculative_crash",
    "speculative_property",
    "invalid_input_crash",
    "beyond_declared_contract_robustness",
    "coverage_gap",
}
def _is_speculative_scan_detail(detail: Mapping[str, Any]) -> bool:
    """Return whether a scan detail is exploratory rather than promoted."""
    return detail.get("category") in _SPECULATIVE_SCAN_CATEGORIES
def _scan_checked_items(state: Any) -> list[str]:
    """Return the coarse coverage summary for a scan report."""
    checked = [f"{len(state.functions)} functions"]
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    if isinstance(sampling, Mapping):
        checked.append(
            "sampled "
            f"{sampling.get('sampled', 0)}/{sampling.get('total_runnable', 0)} runnable exports"
        )
    if getattr(state, "supervisor_info", None):
        checked.append(f"{state.supervisor_info.get('trajectory_steps', 0)} transitions")
    tree = getattr(state, "tree", None)
    if tree is not None and getattr(tree, "size", 0) > 0:
        checked.append(f"{tree.size} checkpoints")
    return checked
def _scan_evidence_dimensions(state: Any) -> dict[str, Any]:
    """Expose scan evidence as interpretable dimensions, not one score."""
    functions = getattr(state, "functions", {}) or {}
    skipped = list(getattr(state, "skipped", []))
    details = _scan_report_details(state)
    actionable_details = [
        detail
        for detail in details
        if detail.get("kind") not in {"blocked", "precondition"}
        and detail.get("category") != "expected_precondition_failure"
    ]
    replayable = sum(
        1
        for detail in actionable_details
        if detail.get("replayable")
        or detail.get("counterexample") is not None
        or detail.get("failing_args") is not None
    )
    mutation_scores = [
        float(getattr(func_state, "mutation_score"))
        for func_state in functions.values()
        if getattr(func_state, "mutation_score", None) is not None
    ]
    total_functions = len(functions) + len(skipped)
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    if isinstance(sampling, Mapping) and int(sampling.get("total_runnable", 0)) > 0:
        surface_coverage = int(sampling.get("sampled", 0)) / int(sampling.get("total_runnable", 0))
    else:
        surface_coverage = len(functions) / total_functions if total_functions > 0 else 1.0
    blocked_functions = sum(
        1 for function in functions.values() if getattr(function, "scan_limitation_kind", None)
    )
    return {
        "search_depth": {
            "functions": len(functions),
            "transitions": getattr(state, "supervisor_info", {}).get("trajectory_steps", 0),
            "checkpoints": getattr(getattr(state, "tree", None), "size", 0),
        },
        "replayability": {
            "replayable_findings": replayable,
            "total_findings": len(actionable_details),
        },
        "mutation_strength": (
            sum(mutation_scores) / len(mutation_scores) if mutation_scores else None
        ),
        "fixture_completeness": (len(functions) / total_functions if total_functions > 0 else 1.0),
        "surface_coverage": round(surface_coverage, 4),
        "blocked_functions": blocked_functions,
    }
def _calibrated_scan_confidence(state: Any) -> float:
    """Bound aggregate confidence by observed surface, replay, and blockers."""
    evidence = _scan_evidence_dimensions(state)
    replay = evidence["replayability"]
    replay_score = (
        replay["replayable_findings"] / replay["total_findings"]
        if replay["total_findings"]
        else 1.0
    )
    functions = getattr(state, "functions", {}) or {}
    blocked_ratio = evidence["blocked_functions"] / max(len(functions), 1)
    observed = min(float(getattr(state, "confidence", 0.0)), evidence["surface_coverage"])
    return max(0.0, min(1.0, observed * (0.75 + 0.25 * replay_score) * (1 - blocked_ratio / 2)))
def _trim_report_value(
    value: Any,
    *,
    max_depth: int = 3,
    max_items: int = 6,
    max_string: int = 120,
) -> Any:
    """Trim large nested values so reports stay readable."""
    if max_depth <= 0:
        text = repr(value)
        return text if len(text) <= max_string else text[: max_string - 3] + "..."
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max_string - 3] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        items = list(value.items())
        trimmed = {
            str(key): _trim_report_value(
                item,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string=max_string,
            )
            for key, item in items[:max_items]
        }
        if len(items) > max_items:
            trimmed["..."] = f"+{len(items) - max_items} more field(s)"
        return trimmed
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        trimmed = [
            _trim_report_value(
                item,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string=max_string,
            )
            for item in seq[:max_items]
        ]
        if len(seq) > max_items:
            trimmed.append(f"... +{len(seq) - max_items} more item(s)")
        return trimmed
    text = repr(value)
    return text if len(text) <= max_string else text[: max_string - 3] + "..."
def _json_safe_value(value: Any) -> Any:
    """Recursively normalize values into JSON-native structures."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        try:
            items = sorted(value, key=repr)
        except TypeError:
            items = list(value)
        return [_json_safe_value(item) for item in items]
    return repr(value)
def _json_block(value: Any) -> list[str]:
    """Render a fenced JSON block for Markdown reports."""
    trimmed = _trim_report_value(value)
    return ["```json", json.dumps(trimmed, indent=2, default=str), "```"]
def _python_block(code: str) -> list[str]:
    """Render a fenced Python block for Markdown reports."""
    return ["```python", code.rstrip(), "```"]
def _slugify_report_name(text: str) -> str:
    """Collapse free-form finding names into test-friendly identifiers."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    return slug or "finding"
def _regression_test_name(stub: str) -> str | None:
    """Extract the pytest test name from a generated regression stub."""
    match = re.search(r"^def (test_[0-9A-Za-z_]+)\(", stub, re.MULTILINE)
    return match.group(1) if match else None
def _default_scan_report_path(module: str) -> str:
    """Return the default Markdown artifact path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".md"])
def _default_scan_bundle_path(module: str) -> str:
    """Return the default JSON artifact path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".json"])
def _default_scan_review_config_path(module: str) -> str:
    """Return the default review-only TOML suggestion path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".ordeal.toml"])
def _default_scan_support_bundle_path(module: str) -> str:
    """Return the default review-only support scaffold path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".ordeal_support.py"])
def _default_scan_replay_notes_path(module: str) -> str:
    """Return the default replay-note artifact path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".replay.md"])
def _default_scan_proofs_path(module: str) -> str:
    """Return the default proof-bundle JSON path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".proofs.json"])
def _default_scan_scenario_library_path(module: str) -> str:
    """Return the default scenario-library note path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".scenarios.md"])
def _default_artifact_index_path() -> str:
    """Return the default artifact index path for saved scan findings."""
    return f"{_DEFAULT_FINDINGS_DIR}/index.json"
def _display_path(path: Path) -> str:
    """Render a path in a stable, shell-friendly form for CLI output."""
    return path.as_posix()
def _shell_command(*parts: str) -> str:
    """Join shell arguments into a displayable command string."""
    return shlex.join(parts)
def _artifact_bundle_path(report_path: str) -> str:
    """Derive the JSON bundle path from a Markdown report path."""
    return str(Path(report_path).with_suffix(".json"))
def _resolve_qualname_attr(module_name: str, qualname: str) -> Any | None:
    """Resolve one dotted qualname from an importable module."""
    try:
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        return obj
    except Exception:
        return None
