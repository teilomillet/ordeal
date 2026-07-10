from __future__ import annotations
# ruff: noqa
def _test_one_function(
    name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    return_type: type | None,
    *,
    max_examples: int,
    check_return_type: bool,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    contract_checks: list[ContractCheck] | None = None,
    expected_preconditions: list[str] | None = None,
    ignore_contracts: list[str] | None = None,
    mode: ScanMode = "evidence",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: Sequence[str] | None = None,
    shell_injection_check: bool = False,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    security_focus: bool = False,
    minimize_findings: bool = False,
    evidence_index: ProjectEvidenceIndex | None = None,
) -> FunctionResult:
    """Run no-crash + return-type + mined-property checks on a single function."""
    mode = _normalize_scan_mode(mode)

    def _replay_failure(exc: Exception) -> tuple[bool, int, int]:
        if not last_invocation_recorded:
            return False, 0, 0
        attempts = 2
        matches = 0
        expected_signature = _exception_replay_signature(exc)
        for _ in range(attempts):
            try:
                _call_sync(func, **dict(last_kwargs))
            except Exception as replay_exc:
                if _exception_replay_signature(replay_exc) == expected_signature:
                    matches += 1
        return matches == attempts, attempts, matches

    last_kwargs: dict[str, Any] = {}
    last_invocation_recorded = False
    last_input_source = "boundary"
    profile = _likely_contract_profile(
        func,
        security_focus=security_focus,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
        evidence_index=evidence_index,
    )
    fixture_completeness = _profile_fixture_completeness(profile)
    seed_examples = list(profile.get("seed_examples", []))
    strategies = _bias_strategies_with_seed_examples(strategies, seed_examples)
    auto_checks, sink_categories = _auto_contract_checks(
        func,
        seed_examples,
        auto_contracts=auto_contracts,
        ignore_contracts=ignore_contracts,
        shell_injection_check=shell_injection_check,
        security_focus=security_focus,
    )
    effective_contract_checks = [*(contract_checks or []), *auto_checks]
    static_contract_checks = [
        check for check in effective_contract_checks if _contract_check_is_static(check)
    ]
    runtime_contract_checks = [
        check for check in effective_contract_checks if not _contract_check_is_static(check)
    ]

    def _tool_limitation(exc: Exception) -> tuple[str, str] | None:
        """Classify failures produced before the target's behavior was observed."""
        call_context = getattr(func, "__ordeal_last_call_context__", None)
        call_stage = str(
            (call_context or {}).get("failure_stage")
            or (call_context or {}).get("call_stage")
            or ""
        ).strip()
        if call_stage and call_stage not in {"invoke", "teardown"}:
            return (
                "harness_construction",
                f"object harness failed during {call_stage}: {type(exc).__name__}: {exc}",
            )

        traceback_codes: set[Any] = set()
        current = exc.__traceback__
        while current is not None:
            traceback_codes.add(current.tb_frame.f_code)
            current = current.tb_next
        target = _unwrap(func)
        target_code = getattr(target, "__code__", None)
        reached_target = target_code is not None and target_code in traceback_codes
        message = str(exc)
        hypothesis_failure = type(exc).__module__.startswith("hypothesis") or any(
            marker in message
            for marker in (
                "Could not resolve typing.Any to a strategy",
                "no such thing as a runtime instance of typing.Any",
                "Cannot sample from a length-zero sequence",
                "defines no arguments",
            )
        )
        if hypothesis_failure and not reached_target:
            return (
                "strategy_generation",
                f"input strategy could not be generated: {type(exc).__name__}: {exc}",
            )
        if not last_invocation_recorded and not reached_target:
            return (
                "input_generation",
                f"Ordeal could not construct an input: {type(exc).__name__}: {exc}",
            )
        return None

    if static_contract_checks:
        contract_violations, contract_details = _evaluate_contract_checks(
            func,
            static_contract_checks,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            treat_any_as_weak=treat_any_as_weak,
            execute_calls=False,
            evidence_index=evidence_index,
            profile=profile,
        )
        if contract_violations:
            return FunctionResult(
                name=name,
                passed=False,
                execution_ok=True,
                verdict=(
                    "lifecycle_contract"
                    if any(
                        detail.get("category") == "lifecycle_contract"
                        for detail in contract_details
                    )
                    else "semantic_contract"
                ),
                contract_violations=contract_violations,
                contract_violation_details=contract_details,
                sink_categories=sink_categories,
                input_sources=[
                    {"source": example.source, "evidence": example.evidence}
                    for example in profile.get("seed_examples", [])
                ],
                input_source="static_contract",
            )

    def _origin_for_kwargs(kwargs: Mapping[str, Any], fallback: str) -> str:
        for example in profile.get("seed_examples", []):
            if dict(example.kwargs) == dict(kwargs):
                return example.source
        return fallback

    def _check_result(result: Any) -> None:
        if check_return_type and return_type is not None:
            if not _type_matches(result, return_type):
                raise AssertionError(
                    f"Expected return type {return_type}, got {type(result).__name__}: {result!r}"
                )

    def _run_one(kwargs: Mapping[str, Any], fallback: str) -> None:
        nonlocal last_kwargs
        nonlocal last_input_source
        nonlocal last_invocation_recorded
        last_kwargs = dict(kwargs)
        last_input_source = _origin_for_kwargs(kwargs, fallback)
        last_invocation_recorded = True
        result = _call_sync(func, **dict(kwargs))
        _check_result(result)

    try:
        for candidate in _candidate_inputs(
            func,
            fixtures=fixtures,
            mutate_observed_inputs=any(
                (
                    seed_from_tests,
                    seed_from_fixtures,
                    seed_from_docstrings,
                    seed_from_call_sites,
                )
            ),
            mode=mode,
            security_focus=security_focus,
            seed_examples=seed_examples,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
        ):
            _run_one(candidate.kwargs, candidate.origin)

        # Hypothesis rejects @given() with no inferred arguments.
        if not strategies:
            _run_one({}, "random_fuzz")
        else:

            @given(**strategies)
            @settings(max_examples=max_examples, database=None)
            def test(**kwargs: Any) -> None:
                _run_one(kwargs, "random_fuzz")

            test()
    except Exception as e:
        call_context = getattr(func, "__ordeal_last_call_context__", None)
        limitation = _tool_limitation(e)
        if limitation is not None:
            limitation_kind, blocking_reason = limitation
            return FunctionResult(
                name=name,
                passed=True,
                execution_ok=False,
                verdict="blocked",
                error=str(e)[:300],
                error_type=type(e).__name__,
                limitation_kind=limitation_kind,
                blocking_reason=blocking_reason[:500],
                sink_categories=sink_categories,
                input_sources=[
                    {"source": example.source, "evidence": example.evidence}
                    for example in profile.get("seed_examples", [])
                ],
                input_source=None,
            )
        precondition = (
            _documented_precondition_failure(
                func,
                e,
                last_kwargs,
                expected_patterns=expected_preconditions,
            )
            if last_invocation_recorded
            else None
        )
        if precondition is not None:
            return FunctionResult(
                name=name,
                passed=True,
                execution_ok=True,
                verdict="expected_precondition_failure",
                error_type=precondition["error_type"],
                failing_args=dict(last_kwargs) if last_invocation_recorded else None,
                contract_violations=[str(precondition["summary"])],
                contract_violation_details=[precondition],
                sink_categories=sink_categories,
                input_sources=[
                    {"source": example.source, "evidence": example.evidence}
                    for example in profile.get("seed_examples", [])
                ],
                input_source=last_input_source,
            )
        replayable, replay_attempts, replay_matches = _replay_failure(e)
        contract_fit, realism, sink_signal, rationale = _score_contract_fit(last_kwargs, profile)
        reachability = _reachability_score(last_input_source, last_kwargs, profile)
        aligned_sinks = _aligned_security_sinks(last_kwargs, profile)
        aligned_critical_sinks = _critical_security_sinks(aligned_sinks)
        effective_min_contract_fit = min_contract_fit
        effective_min_reachability = min_reachability
        effective_min_realism = min_realism
        effective_min_fixture_completeness: float | None = None
        if security_focus and aligned_critical_sinks:
            effective_min_contract_fit = max(0.45, min_contract_fit - 0.1)
            effective_min_reachability = max(0.35, min_reachability - 0.1)
            effective_min_realism = max(0.5, min_realism - 0.05)
        if security_focus and mode == "real_bug":
            effective_min_fixture_completeness = _SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS
        robustness_case = _looks_like_declared_contract_robustness(
            last_kwargs,
            profile,
            realism=realism,
            reachability=reachability,
        )
        crash_category = _classify_crash(
            mode=mode,
            replayable=replayable,
            contract_fit=contract_fit,
            reachability=reachability,
            realism=realism,
            robustness_case=robustness_case,
            min_contract_fit=effective_min_contract_fit,
            min_reachability=effective_min_reachability,
            min_realism=effective_min_realism,
            require_replayable=require_replayable,
        )
        crash_category, forced_demotion_reason = _bootstrap_failure_demotion(
            func,
            category=crash_category,
            call_context=call_context,
        )
        crash_category, security_focus_demotion_reason = _security_focus_demotion(
            category=crash_category,
            mode=mode,
            security_focus=security_focus,
            input_source=last_input_source,
            replayable=replayable,
            fixture_completeness=fixture_completeness,
            aligned_sink_categories=aligned_sinks,
        )
        if security_focus_demotion_reason is not None:
            forced_demotion_reason = security_focus_demotion_reason
        verdict = _verdict_for_crash(crash_category)
        proof_bundle = None
        if proof_bundles and last_invocation_recorded:
            proof_bundle = _build_proof_bundle(
                qualname=str(profile.get("qualname", name)),
                error=e,
                failing_args=last_kwargs,
                input_source=last_input_source,
                contract_fit=contract_fit,
                reachability=reachability,
                realism=realism,
                rationale=rationale,
                replayable=replayable,
                replay_attempts=replay_attempts,
                replay_matches=replay_matches,
                category=crash_category,
                profile=profile,
                sink_signal=sink_signal,
                sink_categories=sink_categories,
                aligned_sink_categories=aligned_sinks,
                min_contract_fit=effective_min_contract_fit,
                min_reachability=effective_min_reachability,
                min_realism=effective_min_realism,
                min_fixture_completeness=effective_min_fixture_completeness,
                harness_mode=getattr(func, "__ordeal_harness__", None),
                callable_kind=getattr(func, "__ordeal_kind__", None),
                callable_obj=func,
                security_focus=security_focus,
                forced_demotion_reason=forced_demotion_reason,
            )
            if call_context:
                lifecycle_details = {
                    "phase": call_context.get("lifecycle_phase"),
                    "probe": call_context.get("lifecycle_probe"),
                    "runtime": call_context.get("lifecycle_runtime"),
                    "teardown_called": call_context.get("teardown_called"),
                    "teardown_error": call_context.get("teardown_error"),
                }
                if any(value is not None for value in lifecycle_details.values()):
                    proof_bundle["lifecycle"] = lifecycle_details
        if crash_category == "likely_bug" and not _scan_crash_promoted(
            category=crash_category,
            replayable=replayable,
            proof_bundle=proof_bundle,
            sink_categories=sink_categories,
        ):
            verdict = "exploratory_crash"
            if isinstance(proof_bundle, dict):
                proof_verdict = dict(proof_bundle.get("verdict", {}))
                proof_verdict["promoted"] = False
                if not proof_verdict.get("demotion_reason") and (
                    _proof_bundle_critical_sinks(proof_bundle) is not None
                    or _critical_security_sinks(sink_categories)
                ):
                    proof_verdict["demotion_reason"] = (
                        "critical sink findings require a replayable proof bundle before promotion"
                    )
                proof_bundle["verdict"] = proof_verdict
        return FunctionResult(
            name=name,
            passed=verdict not in _PROMOTED_SCAN_VERDICTS,
            execution_ok=False,
            verdict=verdict,
            error=str(e)[:300],
            error_type=type(e).__name__,
            failing_args=dict(last_kwargs) if last_invocation_recorded else None,
            crash_category=crash_category,
            replayable=replayable,
            replay_attempts=replay_attempts,
            replay_matches=replay_matches,
            minimization=(
                {
                    "status": "verified",
                    "method": "hypothesis",
                    "original_complexity": None,
                    "minimized_complexity": None,
                    "replay_attempts": replay_attempts,
                    "replay_matches": replay_matches,
                    "boundary": (
                        "Hypothesis shrank within the declared input strategies; this does not "
                        "prove global minimality."
                    ),
                }
                if bool(strategies) and last_input_source == "random_fuzz" and replayable
                else {
                    "status": "not_run",
                    "method": None,
                    "original_complexity": None,
                    "minimized_complexity": None,
                    "replay_attempts": 0,
                    "replay_matches": 0,
                    "boundary": "No minimization claim is supported for this witness.",
                }
            ),
            contract_fit=contract_fit,
            reachability=reachability,
            realism=realism,
            sink_signal=sink_signal,
            sink_categories=sink_categories,
            input_sources=[
                {"source": example.source, "evidence": example.evidence}
                for example in profile.get("seed_examples", [])
            ],
            input_source=last_input_source,
            proof_bundle=proof_bundle,
        )

    contract_violations, contract_details = _evaluate_contract_checks(
        func,
        runtime_contract_checks,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
        evidence_index=evidence_index,
        profile=profile,
    )
    violations: list[str] = []
    details: list[dict[str, Any]] = []
    if mode != "real_bug":
        try:
            from ordeal.mine import _is_suspicious_property, mine

            mine_kwargs = {
                "max_examples": min(max_examples, 30),
                "ignore_properties": ignore_properties or [],
                "ignore_relations": ignore_relations or [],
                "property_overrides": property_overrides or {},
                "relation_overrides": relation_overrides or {},
            }
            if minimize_findings:
                mine_kwargs["minimize_findings"] = True
            mine_result = mine(func, **mine_kwargs)
            for prop in mine_result.properties:
                if _is_suspicious_property(prop):
                    label = f"{prop.name} ({prop.confidence:.0%})"
                    violations.append(label)
                    details.append(
                        {
                            "name": prop.name,
                            "summary": label,
                            "confidence": round(prop.confidence, 4),
                            "holds": prop.holds,
                            "total": prop.total,
                            "counterexample": prop.counterexample,
                            "replayable": prop.replayable,
                            "replay_attempts": prop.replay_attempts,
                            "replay_matches": prop.replay_matches,
                            "replay_match_basis": prop.replay_match_basis,
                            "minimization": prop.minimization,
                        }
                    )
        except Exception:
            pass  # mining failed — still report crash-safety pass
    return FunctionResult(
        name=name,
        passed=not bool(contract_violations),
        execution_ok=True,
        verdict=(
            "lifecycle_contract"
            if any(detail.get("category") == "lifecycle_contract" for detail in contract_details)
            else "semantic_contract"
            if contract_violations
            else "exploratory_property"
            if violations
            else "clean"
        ),
        property_violations=violations,
        property_violation_details=details,
        contract_violations=contract_violations,
        contract_violation_details=contract_details,
        sink_categories=sink_categories,
        input_sources=[
            {"source": example.source, "evidence": example.evidence}
            for example in profile.get("seed_examples", [])
        ],
    )
