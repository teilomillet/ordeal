from __future__ import annotations
# ruff: noqa
def _reachability_score(
    origin: str | None,
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> float:
    """Score whether the failing input came from a reachable, realistic source."""
    base = {
        "test": 1.0,
        "fixture": 0.95,
        "call_site": 0.85,
        "docstring": 0.75,
        "source_boundary": 0.75,
        "pytest_seed": 1.0,
        "seed_mutation": 0.8,
        "artifact_mutation": 0.8,
        "security_probe": 0.7,
        "boundary": 0.7,
        "random_fuzz": 0.45,
    }.get(origin or "", 0.4)
    params = profile.get("params", {})
    if any(
        value in list(params.get(name, {}).get("comparison_values", []))
        for name, value in kwargs.items()
    ):
        base = max(base, 0.75)
    return min(base, 1.0)
def _classify_crash(
    *,
    mode: ScanMode,
    replayable: bool,
    contract_fit: float,
    reachability: float,
    realism: float,
    robustness_case: bool,
    min_contract_fit: float,
    min_reachability: float,
    min_realism: float,
    require_replayable: bool = True,
) -> str:
    """Classify a crash for reporting and promotion."""
    if require_replayable and not replayable:
        return "speculative_crash"
    if not replayable:
        return "speculative_crash"
    if (
        contract_fit >= min_contract_fit
        and reachability >= min_reachability
        and realism >= min_realism
    ):
        return "likely_bug"
    if robustness_case:
        return "beyond_declared_contract_robustness"
    if contract_fit <= _WEAK_CONTRACT_FIT or realism < 0.35:
        return "invalid_input_crash"
    return "coverage_gap" if mode == "coverage_gap" else "speculative_crash"
def _verdict_for_crash(category: str) -> str:
    """Map one crash category to the coarse scan verdict bucket."""
    return {
        "likely_bug": "promoted_real_bug",
        "coverage_gap": "coverage_gap",
        "speculative_crash": "exploratory_crash",
        "invalid_input_crash": "invalid_input_crash",
        "beyond_declared_contract_robustness": "beyond_declared_contract_robustness",
    }.get(category, "exploratory_crash")
def _likely_impact(category: str, sink_signal: float) -> str:
    """Describe likely impact for a crash report."""
    if sink_signal >= 1.0:
        return "reaches a path/shell/json/env shaping sink with a contract-valid input."
    if category == "coverage_gap":
        return "the input looks partially valid, but current evidence points to missing coverage."
    if category == "beyond_declared_contract_robustness":
        return "the failure sits just beyond the declared contract and is best read as robustness."
    if category == "invalid_input_crash":
        return "the crash currently looks driven by out-of-contract input rather than a bug."
    return "the function crashes on an input that matches the inferred contract."
def _proof_target_qualname(qualname: str, profile: Mapping[str, Any]) -> str:
    """Return the fully qualified target name for a proof bundle."""
    module_name = str(profile.get("module") or "").strip()
    if not module_name or qualname.startswith(f"{module_name}."):
        return qualname
    return f"{module_name}.{qualname}"
def _proof_supporting_evidence(
    failing_args: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return structured contract evidence for the failing witness."""
    params = profile.get("params", {})
    evidence: list[dict[str, Any]] = []
    for name, value in failing_args.items():
        meta = params.get(name, {})
        parameter_evidence: list[dict[str, Any]] = []
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        observed_types = list(meta.get("observed_types", []))
        comparison_values = list(meta.get("comparison_values", []))
        semantic = str(meta.get("semantic", "generic"))
        if hint is not None and not weak_hint:
            parameter_evidence.append(
                {
                    "kind": "type_hint",
                    "detail": _json_ready_proof(hint),
                }
            )
        elif weak_hint:
            parameter_evidence.append(
                {
                    "kind": "weak_type_hint",
                    "detail": _json_ready_proof(hint),
                }
            )
        if observed_types:
            parameter_evidence.append(
                {
                    "kind": "observed_types",
                    "detail": list(observed_types),
                    "matched": type(value).__name__ in observed_types,
                }
            )
        if comparison_values and value in comparison_values:
            parameter_evidence.append(
                {
                    "kind": "boundary_value",
                    "detail": _json_ready_proof(value),
                }
            )
        if meta.get("doc_mentions"):
            parameter_evidence.append(
                {
                    "kind": "docstring",
                    "detail": f"{name} is mentioned in the callable docstring",
                }
            )
        if semantic != "generic":
            parameter_evidence.append(
                {
                    "kind": "semantic_shape",
                    "detail": semantic,
                }
            )
        evidence.append(
            {
                "parameter": name,
                "value": _json_ready_proof(value),
                "value_type": type(value).__name__,
                "checks": parameter_evidence,
            }
        )
    return evidence
def _profile_fixture_completeness(profile: Mapping[str, Any]) -> float:
    """Estimate how well the inferred profile covers the callable inputs."""
    params = profile.get("params", {})
    if not params:
        return 1.0
    scores: list[float] = []
    for meta in params.values():
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        has_runtime_evidence = bool(
            meta.get("observed_types") or meta.get("comparison_values") or meta.get("doc_mentions")
        )
        has_strong_hint = hint is not None and hint is not Any and not weak_hint
        if has_runtime_evidence:
            scores.append(1.0)
        elif has_strong_hint:
            scores.append(0.6)
        elif weak_hint:
            scores.append(0.2)
        else:
            scores.append(0.0)
    completeness = sum(scores) / len(scores)
    if any(
        getattr(example, "source", None) in {"test", "fixture", "pytest_seed", "call_site"}
        for example in profile.get("seed_examples", [])
    ):
        completeness = min(completeness + 0.1, 1.0)
    return completeness
def _bootstrap_failure_demotion(
    func: Any,
    *,
    category: str,
    call_context: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    """Demote mined harness bootstrap failures before they become promoted findings."""
    if category in {"invalid_input_crash", "beyond_declared_contract_robustness"}:
        return category, None
    if not getattr(func, "__ordeal_auto_harness__", False):
        return category, None
    if getattr(func, "__ordeal_harness_verified__", True):
        return category, None
    call_stage = str(
        (call_context or {}).get("failure_stage") or (call_context or {}).get("call_stage") or ""
    ).strip()
    if call_stage in {"", "invoke", "teardown"}:
        return category, None
    dry_run_error = str(getattr(func, "__ordeal_harness_dry_run_error__", "") or "").strip()
    reason = dry_run_error or (
        "auto-mined object harness needs a successful dry-run factory invocation before "
        "bound-method crashes can promote"
    )
    return "coverage_gap", reason
def _security_focus_demotion(
    *,
    category: str,
    mode: ScanMode,
    security_focus: bool,
    input_source: str | None,
    replayable: bool,
    fixture_completeness: float,
    aligned_sink_categories: Sequence[str],
) -> tuple[str, str | None]:
    """Apply stricter candidate-mode security-focus promotion gates."""
    if category != "likely_bug":
        return category, None
    if input_source == "artifact_mutation" and (not replayable or not aligned_sink_categories):
        return (
            "coverage_gap",
            "artifact mutation probes stay exploratory until they produce a "
            "replayable, sink-aligned failure",
        )
    if security_focus and mode == "real_bug":
        if fixture_completeness < _SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS:
            return (
                "coverage_gap",
                "fixture completeness stayed below the security-focus promotion bar "
                f"({fixture_completeness:.0%} < "
                f"{_SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS:.0%})",
            )
    return category, None
def _proof_demotion_reason(
    *,
    category: str,
    replayable: bool,
    contract_fit: float,
    reachability: float,
    realism: float,
    fixture_completeness: float | None = None,
    min_contract_fit: float,
    min_reachability: float,
    min_realism: float,
    min_fixture_completeness: float | None = None,
    forced_reason: str | None = None,
) -> str | None:
    """Return the concrete reason a finding was not promoted as a candidate issue."""
    if forced_reason:
        return forced_reason
    if category in {"likely_bug", "lifecycle_contract"}:
        return None
    if category == "expected_precondition_failure":
        return "the raised exception matches a documented precondition instead of a bug."
    reasons: list[str] = []
    if not replayable:
        reasons.append("replay did not confirm the failure")
    if contract_fit < min_contract_fit:
        reasons.append(
            "contract fit stayed below the promotion bar "
            f"({contract_fit:.0%} < {min_contract_fit:.0%})"
        )
    if reachability < min_reachability:
        reasons.append(
            "the witness did not come from a strong reachable seed "
            f"({reachability:.0%} < {min_reachability:.0%})"
        )
    if realism < min_realism:
        reasons.append(
            f"the input realism stayed below the promotion bar ({realism:.0%} < {min_realism:.0%})"
        )
    if (
        min_fixture_completeness is not None
        and fixture_completeness is not None
        and fixture_completeness < min_fixture_completeness
    ):
        reasons.append(
            "fixture completeness stayed below the promotion bar "
            f"({fixture_completeness:.0%} < {min_fixture_completeness:.0%})"
        )
    if category == "invalid_input_crash":
        reasons.append("the crash still looks driven by out-of-contract input")
    elif category == "beyond_declared_contract_robustness":
        reasons.append(
            "the witness sits beyond the declared contract, so treat this as robustness"
        )
    elif category == "coverage_gap":
        reasons.append("current evidence points to missing coverage more than a defect")
    elif category == "speculative_crash" and not replayable:
        reasons.append("the crash remains exploratory because it is not replayable")
    return "; ".join(dict.fromkeys(reasons)) or None
def _replay_symbol_path(value: Any) -> str | None:
    """Return a resolvable symbol path for one harness replay dependency."""
    module_name = str(getattr(value, "__module__", "") or "").strip()
    qualname = str(getattr(value, "__qualname__", "") or "").strip()
    if not module_name or not qualname or "<locals>" in qualname or "<lambda>" in qualname:
        return None

    try:
        resolved: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            resolved = getattr(resolved, part)
    except (AttributeError, ImportError, ModuleNotFoundError):
        resolved = None
    if resolved is value:
        return f"{module_name}:{qualname}"

    try:
        source_path = Path(inspect.getsourcefile(value) or inspect.getfile(value)).resolve()
    except (OSError, TypeError):
        return None
    if not source_path.is_file():
        return None
    try:
        display_path = source_path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        display_path = str(source_path)
    return f"{display_path}:{qualname}"
def _bound_method_replay_payload(
    func: Any,
    failing_args: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Build an exact replay snippet for one resolved instance-method harness."""
    owner = getattr(func, "__ordeal_owner__", None)
    method_name = str(getattr(func, "__ordeal_method_name__", "") or "").strip()
    factory = getattr(func, "__ordeal_factory__", None)
    if not inspect.isclass(owner) or not method_name or factory is None:
        return None

    dependencies = {
        "owner": owner,
        "factory": factory,
        "setup": getattr(func, "__ordeal_setup__", None),
        "state_factory": getattr(func, "__ordeal_state_factory__", None),
        "teardown": getattr(func, "__ordeal_teardown__", None),
    }
    scenarios = tuple(getattr(func, "__ordeal_scenarios__", ()))
    refs: dict[str, str | None] = {
        name: _replay_symbol_path(value) if value is not None else None
        for name, value in dependencies.items()
    }
    scenario_refs = [_replay_symbol_path(scenario) for scenario in scenarios]
    if refs["owner"] is None or refs["factory"] is None:
        return None
    if any(value is not None and refs[name] is None for name, value in dependencies.items()):
        return None
    if any(reference is None for reference in scenario_refs):
        return None

    owner_key = f"{owner.__module__}:{owner.__qualname__}"
    lines = [
        "from inspect import getattr_static",
        "from ordeal.auto import _resolve_method_callable",
        "from ordeal.cli import _resolve_symbol_path",
        "",
        f"owner = _resolve_symbol_path({refs['owner']!r})",
        f"factory = _resolve_symbol_path({refs['factory']!r})",
    ]
    for name in ("setup", "state_factory", "teardown"):
        reference = refs[name]
        if reference is not None:
            lines.append(f"{name} = _resolve_symbol_path({reference!r})")
    for index, reference in enumerate(scenario_refs):
        lines.append(f"scenario_{index} = _resolve_symbol_path({reference!r})")

    resolve_kwargs = [f"object_factories={{{owner_key!r}: factory}}"]
    if refs["setup"] is not None:
        resolve_kwargs.append(f"object_setups={{{owner_key!r}: setup}}")
    if scenario_refs:
        scenario_names = ", ".join(f"scenario_{index}" for index in range(len(scenario_refs)))
        resolve_kwargs.append(f"object_scenarios={{{owner_key!r}: [{scenario_names}]}}")
    if refs["state_factory"] is not None:
        resolve_kwargs.append(f"object_state_factories={{{owner_key!r}: state_factory}}")
    if refs["teardown"] is not None:
        resolve_kwargs.append(f"object_teardowns={{{owner_key!r}: teardown}}")
    harness = str(getattr(func, "__ordeal_harness__", "fresh") or "fresh")
    resolve_kwargs.append(f"object_harnesses={{{owner_key!r}: {harness!r}}}")

    lines.extend(
        [
            "_, target = _resolve_method_callable(",
            "    owner,",
            f"    {method_name!r},",
            f"    getattr_static(owner, {method_name!r}),",
            *(f"    {item}," for item in resolve_kwargs),
            ")",
            f"args = {pformat(_json_ready_proof(dict(failing_args)), width=88, sort_dicts=False)}",
            "target(**args)",
        ]
    )
    metadata = {
        "mode": harness,
        "owner": refs["owner"],
        "method": method_name,
        "factory": refs["factory"],
        "setup": refs["setup"],
        "scenarios": scenario_refs,
        "state_factory": refs["state_factory"],
        "state_param": getattr(func, "__ordeal_state_param__", None),
        "teardown": refs["teardown"],
    }
    return "\n".join(lines), metadata
def _proof_minimal_reproduction(
    *,
    qualname: str,
    failing_args: Mapping[str, Any],
    profile: Mapping[str, Any],
    harness_mode: str | None,
    callable_kind: str | None,
    callable_obj: Any | None = None,
    contract_check: str | None = None,
    security_focus: bool = False,
) -> dict[str, Any]:
    """Build a deterministic reproduction payload for reports and JSON bundles."""
    module_name = str(profile.get("module") or "").strip()
    explicit_target = f"{module_name}:{qualname}" if module_name else qualname
    direct_call_supported = callable_kind != "instance"
    snippet_lines = [
        "from importlib import import_module",
        f"mod = import_module({module_name!r})" if module_name else "mod = None",
        f"args = {pformat(_json_ready_proof(dict(failing_args)), width=88, sort_dicts=False)}",
    ]
    harness_replay = None
    if direct_call_supported and module_name:
        expr = "mod"
        for part in [part for part in qualname.split(".") if part]:
            expr = f"{expr}.{part}"
        snippet_lines.append(f"{expr}(**args)")
    else:
        harness_replay = (
            _bound_method_replay_payload(callable_obj, failing_args)
            if callable_obj is not None
            else None
        )
        if harness_replay is None:
            snippet_lines.append(
                "# This target requires the configured object harness before invoking the method."
            )
        else:
            snippet_lines = harness_replay[0].splitlines()
    if contract_check is not None:
        command = f"uv run ordeal check {explicit_target} --contract {contract_check}"
    elif module_name:
        command = (
            f"uv run ordeal scan {module_name} --mode candidate "
            f"{'--security-focus ' if security_focus else ''}"
            f"--target {explicit_target} -n 1"
        )
    else:
        command = None
    note = None
    if not direct_call_supported:
        if harness_replay is None:
            note = (
                "Bound instance method: exact replay requires resolvable object "
                f"factory/setup/scenario hooks (harness={harness_mode or 'fresh'})."
            )
        else:
            note = (
                "Bound instance method: the snippet reconstructs the discovered object harness "
                f"before replay (harness={harness_mode or 'fresh'})."
            )
    return {
        "target": explicit_target,
        "command": command,
        "python_snippet": "\n".join(snippet_lines),
        "direct_call_supported": direct_call_supported,
        "harness_replay_supported": harness_replay is not None,
        "harness": harness_replay[1] if harness_replay is not None else None,
        "note": note,
    }
