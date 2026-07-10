from __future__ import annotations
# ruff: noqa
def _build_proof_bundle(
    *,
    qualname: str,
    error: Exception | None,
    failing_args: Mapping[str, Any],
    input_source: str | None,
    contract_fit: float,
    reachability: float,
    realism: float,
    rationale: Sequence[str],
    replayable: bool,
    replay_attempts: int,
    replay_matches: int,
    category: str,
    profile: Mapping[str, Any],
    sink_signal: float,
    sink_categories: Sequence[str] = (),
    aligned_sink_categories: Sequence[str] | None = None,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    min_fixture_completeness: float | None = None,
    harness_mode: str | None = None,
    callable_kind: str | None = None,
    callable_obj: Any | None = None,
    contract_check: str | None = None,
    security_focus: bool = False,
    forced_demotion_reason: str | None = None,
    promoted: bool | None = None,
) -> dict[str, Any]:
    """Build the proof payload carried through reports and agent output."""
    evidence_class = (
        "candidate_issue"
        if category == "likely_bug"
        else "expected_precondition"
        if category == "expected_precondition_failure"
        else category
    )
    full_qualname = _proof_target_qualname(qualname, profile)
    matched_sources = [
        {
            "source": example.source,
            "evidence": example.evidence,
        }
        for example in profile.get("seed_examples", [])
        if getattr(example, "kwargs", None) == dict(failing_args)
    ]
    supporting_evidence = _proof_supporting_evidence(failing_args, profile)
    fixture_completeness = _profile_fixture_completeness(profile)
    replayability_score = (
        replay_matches / replay_attempts if replay_attempts > 0 else (1.0 if replayable else 0.0)
    )
    callable_sink_categories = [str(item) for item in sink_categories]
    impact_sink_categories = [str(item) for item in list(aligned_sink_categories or ())]
    demotion_reason = _proof_demotion_reason(
        category=category,
        replayable=replayable,
        contract_fit=contract_fit,
        reachability=reachability,
        realism=realism,
        fixture_completeness=fixture_completeness,
        min_contract_fit=min_contract_fit,
        min_reachability=min_reachability,
        min_realism=min_realism,
        min_fixture_completeness=min_fixture_completeness,
        forced_reason=forced_demotion_reason,
    )
    witness = {
        "input": _json_ready_proof(dict(failing_args)),
        "source": input_source,
        "seed_sources": matched_sources,
        "supporting_evidence": supporting_evidence,
    }
    contract_basis = {
        "category": category,
        "evidence_class": evidence_class,
        "fit": round(contract_fit, 4),
        "reachability": round(reachability, 4),
        "realism": round(realism, 4),
        "fixture_completeness": round(fixture_completeness, 4),
        "basis": list(rationale),
        "likely_contract": _json_ready_proof(profile.get("params", {})),
        "supporting_evidence": supporting_evidence,
        "input_source": input_source,
        "matched_seed_sources": matched_sources,
        "security_focus": bool(security_focus),
        "sink_categories": list(impact_sink_categories),
        "callable_sink_categories": list(callable_sink_categories),
        "critical_sinks": _critical_security_sinks(impact_sink_categories),
    }
    failure_path = {
        "target": full_qualname,
        "qualname": full_qualname,
    }
    if error is not None:
        failure_path["error_type"] = type(error).__name__
        failure_path["error"] = str(error)[:300]
        failure_path["traceback"] = _traceback_path(error)
    if contract_check is not None:
        failure_path["contract_check"] = contract_check
    impact_summary = (
        _sink_likely_impact(impact_sink_categories, error)
        if impact_sink_categories and error is not None
        else _likely_impact(category, sink_signal)
    )
    minimal_reproduction = _proof_minimal_reproduction(
        qualname=qualname,
        failing_args=failing_args,
        profile=profile,
        harness_mode=harness_mode,
        callable_kind=callable_kind,
        callable_obj=callable_obj,
        contract_check=contract_check,
        security_focus=security_focus,
    )
    critical_sinks = _critical_security_sinks(impact_sink_categories)
    promoted_verdict = (
        bool(promoted)
        if promoted is not None
        else category in {"likely_bug", "semantic_contract", "lifecycle_contract"}
    )
    return {
        "version": 2,
        "witness": witness,
        "valid_input_witness": {
            **witness,
            "contract_fit": round(contract_fit, 4),
            "reachability": round(reachability, 4),
            "realism": round(realism, 4),
            "rationale": list(rationale),
        },
        "contract_basis": contract_basis,
        "contract_validity": {
            "category": category,
            "evidence_class": evidence_class,
            "likely_contract": _json_ready_proof(profile.get("params", {})),
            "rationale": list(rationale),
            "supporting_evidence": supporting_evidence,
        },
        "confidence_breakdown": {
            "replayability": round(replayability_score, 4),
            "contract_fit": round(contract_fit, 4),
            "reachability": round(reachability, 4),
            "realism": round(realism, 4),
            "fixture_completeness": round(fixture_completeness, 4),
            "sink_signal": round(sink_signal, 4),
            "replay_attempts": replay_attempts,
            "replay_matches": replay_matches,
        },
        "failure_path": failure_path,
        "failing_path": failure_path,
        "minimal_reproduction": minimal_reproduction,
        "reproduction": {
            "replayable": replayable,
            "replay_attempts": replay_attempts,
            "replay_matches": replay_matches,
            "match_basis": _REPLAY_MATCH_BASIS,
            "failing_args": _json_ready_proof(dict(failing_args)),
            **minimal_reproduction,
        },
        "impact": {
            "summary": impact_summary,
            "class": (
                "lifecycle"
                if category == "lifecycle_contract"
                else (list(impact_sink_categories)[0] if impact_sink_categories else category)
            ),
            "evidence_class": evidence_class,
            "sink_categories": list(impact_sink_categories),
            "callable_sink_categories": list(callable_sink_categories),
            "critical_sinks": critical_sinks,
            "trust_boundary_signal": round(sink_signal, 4),
            "security_focus": bool(security_focus),
        },
        "sink_categories": list(impact_sink_categories),
        "callable_sink_categories": list(callable_sink_categories),
        "likely_impact": impact_summary,
        "verdict": {
            "category": category,
            "evidence_class": evidence_class,
            "promoted": promoted_verdict,
            "demotion_reason": demotion_reason,
        },
    }
@contextlib.contextmanager
def _temporary_callable_attr(func: Any, name: str, value: Any) -> Any:
    """Temporarily set one attribute on *func* for a contract execution."""
    marker = object()
    previous = getattr(func, name, marker)
    setattr(func, name, value)
    try:
        yield
    finally:
        if previous is marker:
            with contextlib.suppress(AttributeError):
                delattr(func, name)
        else:
            setattr(func, name, previous)
def _lifecycle_contract_probe(func: Any, check: ContractCheck) -> Callable[..., Any] | None:
    """Build an instance probe that injects lifecycle faults for *check*."""
    metadata = dict(check.metadata)
    if metadata.get("kind") != "lifecycle":
        return None
    if getattr(func, "__ordeal_kind__", None) != "instance":
        return None

    phase = str(
        metadata.get("phase") or getattr(func, "__ordeal_lifecycle_phase__", None) or "cleanup"
    )
    fault = str(metadata.get("fault", "raise") or "raise")
    configured_handler = metadata.get("handler_name")
    followup_phases = [
        str(item) for item in list(metadata.get("followup_phases", []) or []) if str(item).strip()
    ]
    runtime_faults = [
        str(item) for item in list(metadata.get("runtime_faults", []) or []) if str(item).strip()
    ]

    def probe(*, instance: Any, owner: type | None, method_name: str) -> Any:
        target_handlers = _discover_lifecycle_handlers(instance, phase)
        if method_name in target_handlers and len(target_handlers) > 1:
            target_handlers = [name for name in target_handlers if name != method_name]
        followup_handlers = {
            item: _discover_lifecycle_handlers(instance, item) for item in followup_phases
        }
        combined = list(dict.fromkeys([*target_handlers, *sum(followup_handlers.values(), [])]))
        if not combined:
            return None, {
                "lifecycle_probe": {
                    "phase": phase,
                    "fault": fault,
                    "owner": getattr(owner, "__qualname__", None),
                    "method_name": method_name,
                    "target_handlers": [],
                    "followup_handlers": followup_handlers,
                    "attempts": [],
                    "injected_handler": None,
                    "runtime_faults": runtime_faults,
                }
            }

        attempts: list[str] = []
        patched: list[tuple[str, Any]] = []
        inject_via_probe = not runtime_faults
        injected_handler = (
            (
                str(configured_handler)
                if configured_handler and str(configured_handler) in combined
                else combined[0]
            )
            if inject_via_probe
            else None
        )

        def _make_wrapper(bound: Any, current_name: str, *, inject: bool) -> Any:
            is_async = inspect.iscoroutinefunction(getattr(bound, "__func__", bound))
            if is_async:

                @functools.wraps(bound)
                async def wrapped(*args: Any, **kwargs: Any) -> Any:
                    attempts.append(current_name)
                    if inject:
                        raise _lifecycle_fault_exception(fault)
                    result = bound(*args, **kwargs)
                    if inspect.isawaitable(result):
                        return await result
                    return result
            else:

                @functools.wraps(bound)
                def wrapped(*args: Any, **kwargs: Any) -> Any:
                    attempts.append(current_name)
                    if inject:
                        raise _lifecycle_fault_exception(fault)
                    return _call_sync(bound, *args, **kwargs)

            return wrapped

        for current_name in combined:
            bound = getattr(instance, current_name, None)
            if bound is None or not callable(bound):
                continue
            patched.append((current_name, bound))
            setattr(
                instance,
                current_name,
                _make_wrapper(bound, current_name, inject=current_name == injected_handler),
            )

        def cleanup() -> None:
            for current_name, bound in reversed(patched):
                setattr(instance, current_name, bound)

        return cleanup, {
            "lifecycle_probe": {
                "phase": phase,
                "fault": fault,
                "owner": getattr(owner, "__qualname__", None),
                "method_name": method_name,
                "target_handlers": list(target_handlers),
                "followup_handlers": {
                    key: list(value) for key, value in followup_handlers.items()
                },
                "attempts": attempts,
                "injected_handler": injected_handler,
                "runtime_faults": runtime_faults,
            }
        }

    return probe
def _call_contract_predicate(
    predicate: Callable[..., Any],
    value: Any,
    *,
    func: Any,
    call_context: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
    error: BaseException | None = None,
) -> bool:
    """Call a contract predicate with optional lifecycle-aware context."""
    supported = {
        "value": value,
        "result": value,
        "func": func,
        "kwargs": dict(kwargs),
        "error": error,
        "exception": error,
    }
    if call_context:
        supported.update(
            {
                "instance": call_context.get("instance"),
                "before_state": call_context.get("before_state"),
                "after_state": call_context.get("after_state"),
                "args": call_context.get("args"),
                "method_name": call_context.get("method_name"),
                "owner": call_context.get("owner"),
                "harness": call_context.get("harness"),
                "lifecycle_phase": call_context.get("lifecycle_phase"),
                "lifecycle_probe": call_context.get("lifecycle_probe"),
                "teardown_called": call_context.get("teardown_called"),
                "teardown_error": call_context.get("teardown_error"),
                "lifecycle_runtime": call_context.get("lifecycle_runtime"),
            }
        )

    try:
        signature = inspect.signature(predicate)
    except (TypeError, ValueError):
        return bool(predicate(value))

    kwargs_to_pass: dict[str, Any] = {}
    has_var_keywords = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
    )
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if name in supported:
            kwargs_to_pass[name] = supported[name]

    if has_var_keywords:
        for name, item in supported.items():
            kwargs_to_pass.setdefault(name, item)

    if kwargs_to_pass:
        return bool(predicate(**kwargs_to_pass))
    return bool(predicate(value))
def _resolve_contract_check_entries(
    checks: Sequence[Any] | None,
    *,
    probe_kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
    protected_keys: Sequence[str] | None = None,
    env_param: str | None = None,
    phase: str | None = None,
    followup_phases: Sequence[str] | None = None,
    fault: str = "raise",
    handler_name: str | None = None,
) -> list[ContractCheck]:
    """Resolve contract check objects, names, and named packs."""
    resolved: list[ContractCheck] = []
    resolved_env_param = env_param or next(
        (name for name, value in probe_kwargs.items() if isinstance(value, Mapping)),
        None,
    )
    resolved_protected_keys = list(protected_keys or [])
    if not resolved_protected_keys and resolved_env_param is not None:
        env_value = probe_kwargs.get(resolved_env_param)
        if isinstance(env_value, Mapping):
            resolved_protected_keys = [
                key for key in ("PATH", "HOME", "PWD", "TMPDIR") if key in env_value
            ]
    for raw in checks or ():
        if isinstance(raw, ContractCheck):
            resolved.append(raw)
            continue
        if isinstance(raw, Mapping):
            raw_name = str(
                raw.get("name") or raw.get("pack") or raw.get("contract") or raw.get("check") or ""
            ).strip()
            if not raw_name:
                raise ValueError("contract check spec needs a name or pack")
            merged_kwargs = dict(probe_kwargs)
            merged_kwargs.update(dict(raw.get("kwargs") or {}))
            for concrete_name in _expand_contract_names_ordered([raw_name]):
                resolved.append(
                    builtin_contract_check(
                        concrete_name,
                        kwargs=merged_kwargs,
                        tracked_params=tracked_params,
                        protected_keys=resolved_protected_keys,
                        env_param=resolved_env_param,
                        phase=str(raw.get("phase") or phase or "").strip() or None,
                        followup_phases=(
                            list(raw.get("followup_phases") or followup_phases or [])
                        ),
                        fault=str(raw.get("fault") or fault),
                        handler_name=str(raw.get("handler_name") or handler_name or "").strip()
                        or None,
                    )
                )
            continue
        if isinstance(raw, (str, bytes)):
            name = raw.decode() if isinstance(raw, bytes) else raw
            for concrete_name in _expand_contract_names_ordered([name]):
                resolved.append(
                    builtin_contract_check(
                        concrete_name,
                        kwargs=probe_kwargs,
                        tracked_params=tracked_params,
                        protected_keys=resolved_protected_keys,
                        env_param=resolved_env_param,
                        phase=phase,
                        followup_phases=followup_phases,
                        fault=fault,
                        handler_name=handler_name,
                    )
                )
            continue
        raise TypeError(f"unsupported contract check entry: {type(raw).__name__}")
    return resolved
def _replay_contract_failure(
    func: Any,
    check: ContractCheck,
    *,
    kwargs: Mapping[str, Any],
) -> tuple[bool, int, int]:
    """Replay one explicit contract failure and confirm it still fails."""
    attempts = 2
    matches = 0
    for _ in range(attempts):
        error_obj: BaseException | None = None
        call_context = None
        if _contract_check_is_static(check):
            value = None
        else:
            try:
                value = _call_sync(func, **dict(kwargs))
            except BaseException as exc:
                error_obj = exc
                value = None
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
            return False, attempts, matches
        except Exception:
            passed = False
        if not passed:
            matches += 1
    return matches == attempts, attempts, matches
