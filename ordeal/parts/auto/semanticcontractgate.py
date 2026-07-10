from __future__ import annotations
# ruff: noqa
def _semantic_contract_gate(
    *,
    func: Any,
    check: ContractCheck,
    value: Any,
    error_obj: BaseException | None,
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
    realism: float,
    fixture_completeness: float,
) -> tuple[bool, bool, int, int, str | None]:
    """Return promotion and replay evidence for one semantic contract failure."""
    if _contract_check_is_static(check):
        replayable, replay_attempts, replay_matches = _replay_contract_failure(
            func,
            check,
            kwargs=kwargs,
        )
        return True, replayable, replay_attempts, replay_matches, None
    skip_reason = _callable_skip_reason(func)
    if skip_reason is not None:
        return False, False, 0, 0, skip_reason
    if getattr(func, "__ordeal_auto_harness__", False) and not getattr(
        func, "__ordeal_harness_verified__", True
    ):
        return (
            False,
            False,
            0,
            0,
            str(
                getattr(func, "__ordeal_harness_dry_run_error__", None)
                or "auto-harness dry-run failed"
            ),
        )
    if fixture_completeness < _SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS:
        return (
            False,
            False,
            0,
            0,
            "fixture completeness stayed below the semantic-contract promotion bar "
            f"({fixture_completeness:.0%} < "
            f"{_SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS:.0%})",
        )

    replayable, replay_attempts, replay_matches = _replay_contract_failure(
        func,
        check,
        kwargs=kwargs,
    )
    aligned_sinks = _aligned_security_sinks(kwargs, profile)
    shaped_output_failure = error_obj is None and value is not None
    replay_demonstrates_impact = replayable and (
        shaped_output_failure or error_obj is not None or bool(aligned_sinks)
    )
    if shaped_output_failure:
        return True, replayable, replay_attempts, replay_matches, None
    if aligned_sinks and realism >= _SEMANTIC_CONTRACT_STRONG_REALISM:
        return True, replayable, replay_attempts, replay_matches, None
    if replay_demonstrates_impact:
        return True, replayable, replay_attempts, replay_matches, None
    return (
        False,
        replayable,
        replay_attempts,
        replay_matches,
        "semantic contract remains exploratory until it fails on a shaped output, "
        "a sink-aligned witness reaches stronger realism, or replay demonstrates impact",
    )
def _evaluate_contract_checks(
    func: Any,
    contract_checks: list[ContractCheck] | None,
    *,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    execute_calls: bool = True,
    evidence_index: ProjectEvidenceIndex | None = None,
    profile: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run explicit contract probes against *func* and collect violations."""
    if not contract_checks:
        return [], []

    violations: list[str] = []
    details: list[dict[str, Any]] = []
    if profile is None:
        profile = _likely_contract_profile(
            func,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            treat_any_as_weak=treat_any_as_weak,
            evidence_index=evidence_index,
        )
    fixture_completeness = _profile_fixture_completeness(profile)
    qualname = str(profile.get("qualname", getattr(func, "__qualname__", "?")))
    seed_examples = list(profile.get("seed_examples", []))
    probe_kwargs = dict(seed_examples[0].kwargs) if seed_examples else _contract_seed_kwargs(func)
    tracked_params = list(probe_kwargs)
    env_param = next(
        (name for name, value in probe_kwargs.items() if isinstance(value, Mapping)),
        None,
    )
    protected_keys = [
        key
        for key in ("PATH", "HOME", "PWD", "TMPDIR")
        if env_param is not None
        and isinstance(probe_kwargs.get(env_param), Mapping)
        and key in probe_kwargs.get(env_param, {})
    ]
    resolved_checks = _resolve_contract_check_entries(
        contract_checks,
        probe_kwargs=probe_kwargs,
        tracked_params=tracked_params,
        protected_keys=protected_keys,
        env_param=env_param,
    )
    for check in resolved_checks:
        kwargs = copy.deepcopy(check.kwargs)
        contract_fit, realism, sink_signal, rationale = _score_contract_fit(kwargs, profile)
        lifecycle_probe = _lifecycle_contract_probe(func, check)
        call_context: Mapping[str, Any] | None = None
        error_obj: BaseException | None = None
        metadata = dict(check.metadata)
        detail_category = (
            "lifecycle_contract"
            if str(metadata.get("kind")) == "lifecycle"
            else "semantic_contract"
        )
        input_source = (
            "static_contract" if _contract_check_is_static(check) else "explicit_contract"
        )
        runtime_faults = [
            str(item)
            for item in list(metadata.get("runtime_faults", []) or [])
            if str(item).strip()
        ]
        if _contract_check_is_static(check) and not execute_calls:
            value = None
            call_context = None
        else:
            try:
                with (
                    (
                        _active_instance_probe(func, lifecycle_probe)
                        if lifecycle_probe is not None
                        else contextlib.nullcontext()
                    ),
                    (
                        _active_contract_faults(func, runtime_faults)
                        if runtime_faults
                        else contextlib.nullcontext()
                    ),
                ):
                    value = _call_sync(func, **kwargs)
                call_context = getattr(func, "__ordeal_last_call_context__", None)
            except BaseException as exc:
                error_obj = exc
                call_context = getattr(func, "__ordeal_last_call_context__", None)
                value = None

        if call_context is None:
            call_context = getattr(func, "__ordeal_last_call_context__", None)

        try:
            passed = _call_contract_predicate(
                check.predicate,
                value,
                func=func,
                call_context=call_context,
                kwargs=kwargs,
                error=error_obj,
            )
        except ContractNotApplicable:
            continue
        except Exception as exc:
            passed = False
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None if error_obj is None else f"{type(error_obj).__name__}: {error_obj}"
        static_context = getattr(func, "__ordeal_last_static_contract_context__", None)

        if passed:
            continue

        summary = check.summary or f"explicit contract failed: {check.name}"
        promoted = True
        replayable = True
        replay_attempts = 1
        replay_matches = 1
        forced_demotion_reason: str | None = None
        if detail_category == "semantic_contract":
            (
                promoted,
                replayable,
                replay_attempts,
                replay_matches,
                forced_demotion_reason,
            ) = _semantic_contract_gate(
                func=func,
                check=check,
                value=value,
                error_obj=error_obj,
                kwargs=kwargs,
                profile=profile,
                realism=realism,
                fixture_completeness=fixture_completeness,
            )
        violations.append(summary)
        detail = {
            "kind": "contract",
            "category": detail_category,
            "name": check.name,
            "summary": summary,
            "failing_args": kwargs,
            "value": repr(value)[:300],
            "contract_fit": contract_fit,
            "reachability": 1.0,
            "realism": realism,
            "sink_signal": max(sink_signal, 1.0),
            "input_source": input_source,
            "replayable": replayable,
            "replay_attempts": replay_attempts,
            "replay_matches": replay_matches,
        }
        if call_context:
            detail["lifecycle_phase"] = call_context.get("lifecycle_phase")
            detail["lifecycle_probe"] = call_context.get("lifecycle_probe")
            detail["teardown_called"] = call_context.get("teardown_called")
            detail["teardown_error"] = call_context.get("teardown_error")
            detail["lifecycle_runtime"] = call_context.get("lifecycle_runtime")
        if isinstance(static_context, Mapping) and static_context:
            detail["static_analysis"] = dict(static_context)
        if error is not None:
            detail["error"] = error[:300]
            if error_obj is not None:
                detail["error_type"] = type(error_obj).__name__
        detail["proof_bundle"] = _build_proof_bundle(
            qualname=qualname,
            error=error_obj,
            failing_args=kwargs,
            input_source=input_source,
            contract_fit=contract_fit,
            reachability=1.0,
            realism=realism,
            rationale=rationale,
            replayable=replayable,
            replay_attempts=replay_attempts,
            replay_matches=replay_matches,
            category=detail_category,
            profile=profile,
            sink_signal=max(sink_signal, 1.0),
            sink_categories=profile.get("sink_categories", ()),
            min_contract_fit=0.0,
            min_reachability=0.0,
            min_realism=0.0,
            min_fixture_completeness=(
                _SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS
                if detail_category == "semantic_contract"
                else None
            ),
            harness_mode=getattr(func, "__ordeal_harness__", None),
            callable_kind=getattr(func, "__ordeal_kind__", None),
            callable_obj=func,
            contract_check=check.name,
            forced_demotion_reason=forced_demotion_reason,
            promoted=promoted,
        )
        if call_context and call_context.get("lifecycle_probe") is not None:
            detail["proof_bundle"]["lifecycle"] = dict(call_context["lifecycle_probe"])
        if isinstance(static_context, Mapping) and static_context:
            detail["proof_bundle"]["static_analysis"] = dict(static_context)
        details.append(detail)

    return violations, details
