from __future__ import annotations
# ruff: noqa
def _execute_revision(
    fn: Callable[..., Any],
    kwargs: dict[str, Any],
    *,
    side_effects: Mapping[str, SideEffect],
    baseline: Mapping[str, Any],
) -> _CallOutcome:
    """Invoke one isolated revision and capture its complete selected envelope."""
    bound, receiver = _prepare_callable(fn)
    _restore_selected_side_effects(side_effects, baseline)
    value: Any = None
    exception: Exception | None = None
    location: dict[str, Any] | None = None
    try:
        try:
            call_args, call_kwargs = _bind_named_arguments(bound, kwargs)
            value = bound(*call_args, **call_kwargs)
        except Exception as exc:
            exception = exc
            location = _terminal_source_location(exc)
            _observe_losslessly(exc, label="raised exception")
        else:
            value = _clone_value(value, label="return value")
        mutated = _clone_value(kwargs, label="mutated arguments")
        receiver_after = _receiver_state(receiver)
        selected_after = _capture_selected_side_effects(side_effects)
    finally:
        _restore_selected_side_effects(side_effects, baseline)
    return _CallOutcome(
        value=value,
        exception=exception,
        terminal_source_location=location,
        mutated_arguments=mutated,
        receiver_state=receiver_after,
        side_effects=selected_after,
    )
def _state_equal(left: Any, right: Any) -> bool:
    """Compare values through the shared structural observation layer."""
    left_observation = _observe_losslessly(left, label="left comparison value")
    right_observation = _observe_losslessly(right, label="right comparison value")
    return observations_equal(left_observation, right_observation)
def _compare_outcomes(
    outcome_a: _CallOutcome,
    outcome_b: _CallOutcome,
    *,
    compare: Callable[[Any, Any], bool] | None,
    normalize: Callable[[Any], Any] | None,
    rtol: float | None,
    atol: float | None,
) -> tuple[tuple[str, ...], Any, Any]:
    """Compare return/exception plus every selected state channel."""
    differences: list[str] = []
    normalized_a: Any = None
    normalized_b: Any = None
    if outcome_a.exception is not None or outcome_b.exception is not None:
        if outcome_a.exception is None or outcome_b.exception is None:
            differences.append("exception")
        elif not (
            type(outcome_a.exception) is type(outcome_b.exception)
            and str(outcome_a.exception) == str(outcome_b.exception)
        ):
            differences.append("exception")
    else:
        normalizer = normalize or _identity
        normalized_a = normalizer(outcome_a.value)
        normalized_b = normalizer(outcome_b.value)
        _observe_losslessly(normalized_a, label="normalized revision a return value")
        _observe_losslessly(normalized_b, label="normalized revision b return value")
        matches = (
            compare(normalized_a, normalized_b)
            if compare is not None
            else _default_compare(normalized_a, normalized_b, rtol, atol)
        )
        if not matches:
            differences.append("return_value")
    if not _state_equal(outcome_a.mutated_arguments, outcome_b.mutated_arguments):
        differences.append("mutated_arguments")
    if not _state_equal(outcome_a.receiver_state, outcome_b.receiver_state):
        differences.append("receiver_state")
    if not _state_equal(outcome_a.side_effects, outcome_b.side_effects):
        differences.append("side_effects")
    return tuple(differences), normalized_a, normalized_b
def _run_pair(
    fn_a: Callable[..., Any],
    fn_b: Callable[..., Any],
    kwargs: dict[str, Any],
    *,
    compare: Callable[[Any, Any], bool] | None,
    normalize: Callable[[Any], Any] | None,
    rtol: float | None,
    atol: float | None,
    side_effects: Mapping[str, SideEffect],
) -> _Candidate:
    """Execute both revisions from one isolated input and external baseline."""
    witness, kwargs_a, kwargs_b = _clone_inputs(kwargs)
    baseline = _capture_selected_side_effects(side_effects)
    try:
        outcome_a = _execute_revision(
            fn_a,
            kwargs_a,
            side_effects=side_effects,
            baseline=baseline,
        )
        outcome_b = _execute_revision(
            fn_b,
            kwargs_b,
            side_effects=side_effects,
            baseline=baseline,
        )
    finally:
        _restore_selected_side_effects(side_effects, baseline)
    differences, normalized_a, normalized_b = _compare_outcomes(
        outcome_a,
        outcome_b,
        compare=compare,
        normalize=normalize,
        rtol=rtol,
        atol=atol,
    )
    return _Candidate(
        args=witness,
        outcome_a=outcome_a,
        outcome_b=outcome_b,
        normalized_a=normalized_a,
        normalized_b=normalized_b,
        differences=differences,
    )
def _outcome_payload(outcome: _CallOutcome, normalized: Any) -> dict[str, Any]:
    """Return the canonical artifact observation for one revision."""
    if outcome.exception is not None:
        exception_observation = _observe_losslessly(
            outcome.exception,
            label="raised exception",
        )
        primary: dict[str, Any] = {
            "kind": "exception",
            "exception_type": (
                f"{type(outcome.exception).__module__}.{type(outcome.exception).__qualname__}"
            ),
            "message": str(outcome.exception),
            "terminal_source_location": outcome.terminal_source_location,
            "canonical_exception": exception_observation.payload,
        }
    else:
        value_observation = _observe_losslessly(outcome.value, label="return value")
        normalized_observation = _observe_losslessly(
            normalized,
            label="normalized return value",
        )
        primary = {
            "kind": "return",
            "value": value_observation.json_value,
            "normalized_value": normalized_observation.json_value,
            "canonical_value": value_observation.payload,
            "canonical_normalized_value": normalized_observation.payload,
        }
    mutated_observation = _observe_losslessly(
        outcome.mutated_arguments,
        label="mutated arguments",
    )
    receiver_observation = _observe_losslessly(
        outcome.receiver_state,
        label="receiver state",
    )
    side_effect_observation = _observe_losslessly(
        outcome.side_effects,
        label="side effects",
    )
    primary.update(
        {
            "mutated_arguments": mutated_observation.json_value,
            "receiver_state": receiver_observation.json_value,
            "side_effects": side_effect_observation.json_value,
            "canonical_mutated_arguments": mutated_observation.payload,
            "canonical_receiver_state": receiver_observation.payload,
            "canonical_side_effects": side_effect_observation.payload,
        }
    )
    return primary
def _candidate_observations(candidate: _Candidate) -> dict[str, Any]:
    """Return paired artifact observations for one divergence candidate."""
    return {
        "a": _outcome_payload(candidate.outcome_a, candidate.normalized_a),
        "b": _outcome_payload(candidate.outcome_b, candidate.normalized_b),
    }
def _candidate_observation(candidate: _Candidate) -> CanonicalObservation:
    """Return the comparison-governed paired outcome used by exact replay."""
    replay_observations: dict[str, dict[str, Any]] = {}
    for label, observation in _candidate_observations(candidate).items():
        projected = dict(observation)
        if projected.get("kind") == "return":
            projected.pop("value", None)
            projected.pop("canonical_value", None)
        replay_observations[label] = projected
    return _observe_losslessly(
        {
            "observations": replay_observations,
            "differences": candidate.differences,
        },
        label="paired differential replay projection",
    )
def _candidate_replays(
    expected: CanonicalObservation,
    observed: CanonicalObservation,
    *,
    expected_signature: str,
) -> bool:
    """Return whether the same witness reproduced the complete paired envelope."""
    return exact_replay_match(
        expected,
        observed,
        recorded_expected_signature=expected_signature,
    )
def _callable_name(fn: Callable[..., Any]) -> str:
    """Return a stable module-qualified callable label where possible."""
    module = str(getattr(fn, "__module__", "") or "").strip()
    qualname = str(getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn))
    return f"{module}.{qualname}" if module else qualname
def _callable_binding(fn: Callable[..., Any]) -> dict[str, Any]:
    """Bind one callable identity to its inspectable source text and location."""
    target: Any = inspect.unwrap(fn)
    if not (
        inspect.isfunction(target) or inspect.ismethod(target) or inspect.isclass(target)
    ) and hasattr(target, "__call__"):
        target = inspect.unwrap(target.__call__)
    source_sha256: str | None = None
    source_location: dict[str, Any] | None = None
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        source = None
    if source is not None:
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    try:
        path = inspect.getsourcefile(target) or inspect.getfile(target)
        _, start_line = inspect.getsourcelines(target)
    except (OSError, TypeError):
        pass
    else:
        source_location = {
            "path": Path(path).resolve().as_posix(),
            "line": start_line,
        }
    return {
        "target": _callable_name(fn),
        "source_sha256": source_sha256,
        "source_location": source_location,
    }
def _comparison_binding(
    *,
    compare: Callable[[Any, Any], bool] | None,
    normalize: Callable[[Any], Any] | None,
    rtol: float | None,
    atol: float | None,
) -> dict[str, Any]:
    """Describe and source-bind the exact comparison pipeline."""
    comparator = _callable_binding(compare or _default_compare)
    comparator.update(
        {
            "kind": (
                "custom"
                if compare is not None
                else "tolerance"
                if rtol is not None or atol is not None
                else "exact"
            ),
            "rtol": rtol,
            "atol": atol,
        }
    )
    normalizer = _callable_binding(normalize or _identity)
    normalizer["kind"] = "custom" if normalize is not None else "identity"
    return {
        "comparator": comparator,
        "normalizer": normalizer,
        "exception_matching": "exact type and message across revisions",
        "replay_matching": (
            "normalized return observations plus exact envelope channels and terminal "
            "exception source locations"
        ),
    }
def _public_outcome(outcome: _CallOutcome, normalized: Any) -> DiffOutcome:
    """Freeze one internal outcome for the public witness."""
    exception_type: type[Exception] | None = None
    exception_message = None
    if outcome.exception is not None:
        exception_type = type(outcome.exception)
        exception_message = str(outcome.exception)
    value = _observe_losslessly(outcome.value, label="public return value").public_value
    normalized_value = _observe_losslessly(
        normalized,
        label="public normalized value",
    ).public_value
    mutated = _observe_losslessly(
        outcome.mutated_arguments,
        label="public mutated arguments",
    ).public_value
    receiver = _observe_losslessly(
        outcome.receiver_state,
        label="public receiver state",
    ).public_value
    effects = _observe_losslessly(
        outcome.side_effects,
        label="public side effects",
    ).public_value
    return DiffOutcome(
        returned=outcome.exception is None,
        return_value=_freeze(value),
        exception_type=exception_type,
        exception_message=exception_message,
        mutated_arguments=_freeze(mutated),
        receiver_state=_freeze(receiver),
        side_effects=_freeze(effects),
        terminal_source_location=_freeze(outcome.terminal_source_location),
        normalized_value=_freeze(normalized_value),
    )
def _write_artifact(payload: dict[str, Any], artifact_dir: str | Path) -> str:
    """Atomically persist one canonical divergence artifact and return its path."""
    directory = Path(artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    artifact_id = str(payload["artifact_id"])
    path = directory / f"{artifact_id}.json"
    temporary = directory / f".{artifact_id}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path.resolve().as_posix()
def _system_event_payload(event: SystemEvent) -> dict[str, Any]:
    """Return one portable ordered event for a system witness."""
    if isinstance(event, Operation):
        return {
            "kind": "operation",
            "name": event.name,
            "args": _observe_losslessly(
                event.args,
                label="system operation arguments",
            ).json_value,
            "kwargs": _observe_losslessly(
                dict(event.kwargs),
                label="system operation keyword arguments",
            ).json_value,
        }
    return {
        "kind": "fault",
        "name": event.name,
        "action": event.action,
        "parameters": _observe_losslessly(
            dict(event.parameters),
            label="system fault parameters",
        ).json_value,
    }
def _system_mismatch_observations(mismatch: Any) -> dict[str, Any]:
    """Return paired JSON-safe observations for one system mismatch."""
    observation_a = _observe_losslessly(
        mismatch.observed_a,
        label="system baseline mismatch observation",
    )
    observation_b = _observe_losslessly(
        mismatch.observed_b,
        label="system candidate mismatch observation",
    )
    return {
        "a": {
            "kind": mismatch.kind,
            "step": mismatch.step,
            "value": observation_a.json_value,
            "canonical_value": (
                mismatch.observation_a.payload
                if mismatch.observation_a is not None
                else observation_a.payload
            ),
        },
        "b": {
            "kind": mismatch.kind,
            "step": mismatch.step,
            "value": observation_b.json_value,
            "canonical_value": (
                mismatch.observation_b.payload
                if mismatch.observation_b is not None
                else observation_b.payload
            ),
        },
    }
def _attach_system_artifact(
    result: SystemDiffResult,
    *,
    factory_a: Callable[[], Any],
    factory_b: Callable[[], Any],
    compare: Callable[[Any, Any], bool] | None,
    normalize: Callable[[Any], Any] | None,
    rtol: float | None,
    atol: float | None,
    state: Callable[[Any], Any] | None,
    side_effects: Callable[[Any], Any] | None,
    apply_fault: Callable[[Any, FaultEvent], None] | None,
    artifact_dir: str | Path | None,
) -> SystemDiffResult:
    """Attach the shared divergence card to a replay-supported system mismatch."""
    if result.status != "divergent" or not result.mismatches:
        return result
    mismatch = result.mismatches[0]
    original_mismatch = result.original_mismatch or mismatch
    comparison = _comparison_binding(
        compare=compare,
        normalize=normalize,
        rtol=rtol,
        atol=atol,
    )
    comparison.update(
        {
            "mode": "system",
            "state_probe": _callable_binding(state) if state is not None else None,
            "side_effect_probe": (
                _callable_binding(side_effects) if side_effects is not None else None
            ),
            "fault_adapter": (_callable_binding(apply_fault) if apply_fault is not None else None),
        }
    )
    original_sequence = result.original_sequence or result.sequence
    original_input_observation = _observe_losslessly(
        original_sequence,
        label="original system witness sequence",
    )
    minimized_input_observation = _observe_losslessly(
        result.sequence,
        label="system witness sequence",
    )
    minimization_method = (
        "event deletion preserving the first canonical mismatch"
        if result.minimization_performed
        else "not_run"
    )
    minimization_boundary = (
        "Only the supplied ordered operations and fault transitions were minimized."
        if result.minimization_performed
        else "No event reducer was run because minimize=False."
    )
    artifact = _build_divergence_evidence(
        revisions={
            "a": _callable_binding(factory_a),
            "b": _callable_binding(factory_b),
        },
        comparison=comparison,
        original_input={"sequence": [_system_event_payload(event) for event in original_sequence]},
        minimized_input={"sequence": [_system_event_payload(event) for event in result.sequence]},
        original_input_canonical=original_input_observation.payload,
        minimized_input_canonical=minimized_input_observation.payload,
        original_observations=_system_mismatch_observations(original_mismatch),
        observations=_system_mismatch_observations(mismatch),
        differences=[mismatch.kind],
        replay_attempts=result.replay_attempts,
        replay_matches=result.replay_matches,
        expected_signature=str(result.expected_signature or ""),
        observed_signatures=list(result.observed_signatures),
        witness_source="ordered_system_sequence",
        minimization_method=minimization_method,
        minimization_boundary=minimization_boundary,
    )
    result.artifact = artifact
    result.artifact_path = _write_artifact(artifact, artifact_dir) if artifact_dir else None
    return result
_DIFF_REGRESSION_TEST = "test_ordeal_diff_regression"
def _callable_import_path(value: Callable[..., Any] | None, *, label: str) -> str | None:
    """Return an exact import path or reject a process-local callback."""
    if value is None:
        return None
    module_name = str(getattr(value, "__module__", ""))
    qualname = str(getattr(value, "__qualname__", ""))
    if not module_name or not qualname or "<locals>" in qualname or "<lambda>" in qualname:
        raise TypeError(f"{label} must be an importable module-level callable")
    resolved: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        resolved = getattr(resolved, part)
    if resolved is not value:
        raise TypeError(f"{label} import path does not resolve to the measured callable")
    return f"{module_name}:{qualname}"
def _resolve_replay_callable(path: object) -> Callable[..., Any] | None:
    """Resolve a callable path written by :func:`_callable_import_path`."""
    if path is None:
        return None
    module_name, separator, qualname = str(path).partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(f"invalid replay callable path: {path!r}")
    resolved: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        resolved = getattr(resolved, part)
    if not callable(resolved):
        raise TypeError(f"replay target is not callable: {path}")
    return resolved
