from __future__ import annotations
# ruff: noqa
def _clone_event(
    event: SystemEvent,
    *,
    disjoint_from: tuple[Any, ...] = (),
) -> SystemEvent:
    """Return an isolated event copy or explain why replay is unsafe."""
    return isolated_deepcopy(
        event,
        label="system diff event",
        disjoint_from=disjoint_from,
    )
def _invoke(
    system: Any,
    event: SystemEvent,
    apply_fault: Callable[[Any, FaultEvent], None] | None,
) -> _Outcome:
    """Invoke one isolated operation or fault transition."""
    if isinstance(event, Operation):
        return _capture(lambda: getattr(system, event.name)(*event.args, **event.kwargs))
    if apply_fault is not None:
        return _capture(lambda: apply_fault(system, event))
    handler = getattr(system, "apply_fault", None)
    if not callable(handler):
        raise ValueError(
            "fault events require apply_fault= or an apply_fault(event) system method"
        )
    return _capture(lambda: handler(event))
def _public_state(system: Any) -> Any:
    """Snapshot public instance attributes without evaluating properties."""
    try:
        values = vars(system)
    except TypeError:
        return _UNMEASURED
    public = {
        name: value
        for name, value in values.items()
        if not name.startswith("_") and not callable(value)
    }
    return public
def _observe(
    system: Any,
    probe: Callable[[Any], Any] | None,
    *,
    automatic_state: bool,
) -> _ObservedValue | object:
    """Capture a stable state or side-effect observation."""
    if probe is None:
        if not automatic_state:
            return _UNMEASURED
        observed = _public_state(system)
        if observed is _UNMEASURED:
            return _UNMEASURED
    else:
        try:
            observed = probe(system)
        except Exception as exc:
            raise RuntimeError("system diff probe failed") from exc
    detached = isolated_deepcopy(observed, label="system probe observation")
    return _ObservedValue(
        value=detached,
        observation=observe(detached, label="system probe observation"),
    )
def _build_mismatch(
    kind: Literal["outcome", "state", "side_effects", "fault_schedule"],
    index: int,
    event: SystemEvent,
    observed_a: Any,
    observed_b: Any,
    observation_a: CanonicalObservation,
    observation_b: CanonicalObservation,
) -> SystemMismatch:
    """Create one report mismatch bound to a canonical replay observation."""
    event_observation = observe(event, label="system diff event")
    replay_observation = observe(
        {
            "kind": kind,
            "event": event_observation.payload,
            "observed_a": observation_a.payload,
            "observed_b": observation_b.payload,
        },
        label="system mismatch",
    )
    return SystemMismatch(
        kind=kind,
        step=index,
        event=event,
        observed_a=observed_a,
        observed_b=observed_b,
        replay_observation=replay_observation,
        observation_a=observation_a,
        observation_b=observation_b,
    )
def _run_pair(
    factory_a: Callable[[], Any],
    factory_b: Callable[[], Any],
    sequence: Sequence[SystemEvent],
    *,
    return_compare: Callable[[Any, Any], bool],
    state_compare: Callable[[Any, Any], bool],
    state: Callable[[Any], Any] | None,
    side_effects: Callable[[Any], Any] | None,
    apply_fault: Callable[[Any, FaultEvent], None] | None,
) -> tuple[list[StepComparison], list[SystemMismatch], bool, bool, bool | None]:
    """Replay one exact event stream against two fresh systems."""
    system_a = _construct(factory_a)
    system_b = _construct(factory_b)
    steps: list[StepComparison] = []
    mismatches: list[SystemMismatch] = []
    recovering = False
    recovery_steps: list[StepComparison] = []

    for index, event in enumerate(sequence):
        if isinstance(event, FaultEvent):
            recovering = event.action.lower() in _RECOVERY_ACTIONS
        event_a = _clone_event(event)
        event_b = _clone_event(event, disjoint_from=(event_a,))
        outcome_a = _invoke(system_a, event_a, apply_fault)
        outcome_b = _invoke(system_b, event_b, apply_fault)
        outcome_match = _outcomes_match(outcome_a, outcome_b, return_compare)

        state_a = _observe(system_a, state, automatic_state=True)
        state_b = _observe(system_b, state, automatic_state=True)
        state_measured = state_a is not _UNMEASURED and state_b is not _UNMEASURED
        state_match = (
            observations_equal(state_a.observation, state_b.observation)
            if state_measured
            else None
        )

        effects_a = _observe(system_a, side_effects, automatic_state=False)
        effects_b = _observe(system_b, side_effects, automatic_state=False)
        effects_measured = effects_a is not _UNMEASURED and effects_b is not _UNMEASURED
        effects_match = (
            observations_equal(effects_a.observation, effects_b.observation)
            if effects_measured
            else None
        )

        recovery_phase = recovering and isinstance(event, Operation)
        step = StepComparison(
            index=index,
            event=event,
            outcome_a=outcome_a.public_value,
            outcome_b=outcome_b.public_value,
            outcome_match=outcome_match,
            state_a=None if state_a is _UNMEASURED else state_a.public_value,
            state_b=None if state_b is _UNMEASURED else state_b.public_value,
            state_match=state_match,
            side_effects_a=None if effects_a is _UNMEASURED else effects_a.public_value,
            side_effects_b=None if effects_b is _UNMEASURED else effects_b.public_value,
            side_effects_match=effects_match,
            recovery_phase=recovery_phase,
        )
        steps.append(step)
        if recovery_phase:
            recovery_steps.append(step)
        if not outcome_match:
            if outcome_a.replay_observation is None or outcome_b.replay_observation is None:
                raise ObservationError("system outcome has no lossless replay observation")
            mismatches.append(
                _build_mismatch(
                    "outcome",
                    index,
                    event,
                    outcome_a.public_value,
                    outcome_b.public_value,
                    outcome_a.replay_observation,
                    outcome_b.replay_observation,
                )
            )
        if state_match is False:
            mismatches.append(
                _build_mismatch(
                    "state",
                    index,
                    event,
                    state_a.public_value,
                    state_b.public_value,
                    state_a.observation,
                    state_b.observation,
                )
            )
        if effects_match is False:
            mismatches.append(
                _build_mismatch(
                    "side_effects",
                    index,
                    event,
                    effects_a.public_value,
                    effects_b.public_value,
                    effects_a.observation,
                    effects_b.observation,
                )
            )

    state_checked = any(step.state_match is not None for step in steps)
    effects_checked = any(step.side_effects_match is not None for step in steps)
    recovery_parity = all(step.matches for step in recovery_steps) if recovery_steps else None
    return steps, mismatches, state_checked, effects_checked, recovery_parity
def _minimize(
    factory_a: Callable[[], Any],
    factory_b: Callable[[], Any],
    sequence: tuple[SystemEvent, ...],
    *,
    mismatch_signature: CanonicalObservation,
    return_compare: Callable[[Any, Any], bool],
    state_compare: Callable[[Any, Any], bool],
    state: Callable[[Any], Any] | None,
    side_effects: Callable[[Any], Any] | None,
    apply_fault: Callable[[Any, FaultEvent], None] | None,
) -> tuple[SystemEvent, ...]:
    """Delete events while the exact first divergence remains observable."""
    minimized = sequence
    changed = True
    while changed:
        changed = False
        for index in range(len(minimized)):
            candidate = minimized[:index] + minimized[index + 1 :]
            _, mismatches, _, _, _ = _run_pair(
                factory_a,
                factory_b,
                candidate,
                return_compare=return_compare,
                state_compare=state_compare,
                state=state,
                side_effects=side_effects,
                apply_fault=apply_fault,
            )
            if mismatches and observations_equal(
                _mismatch_signature(mismatches[0]),
                mismatch_signature,
            ):
                minimized = candidate
                changed = True
                break
    return minimized
def _run_timed(
    factory: Callable[[], Any],
    sequence: Sequence[SystemEvent],
    apply_fault: Callable[[Any, FaultEvent], None] | None,
) -> float:
    """Time event execution on a fresh system, excluding setup and probes."""
    system = _construct(factory)
    isolated = tuple(_clone_event(event) for event in sequence)
    started = time.perf_counter()
    for event in isolated:
        try:
            if isinstance(event, Operation):
                getattr(system, event.name)(*event.args, **event.kwargs)
            elif apply_fault is not None:
                apply_fault(system, event)
            else:
                getattr(system, "apply_fault")(event)
        except Exception:
            pass
    return time.perf_counter() - started
def _measure_performance(
    factory_a: Callable[[], Any],
    factory_b: Callable[[], Any],
    sequence: Sequence[SystemEvent],
    apply_fault: Callable[[Any, FaultEvent], None] | None,
    budget: PerformanceBudget,
) -> PerformanceResult:
    """Measure the declared workload independently from semantic comparison."""
    for _ in range(budget.warmup):
        _run_timed(factory_a, sequence, apply_fault)
        _run_timed(factory_b, sequence, apply_fault)
    baseline_samples: list[float] = []
    candidate_samples: list[float] = []
    for _ in range(budget.samples):
        baseline_samples.append(_run_timed(factory_a, sequence, apply_fault))
        candidate_samples.append(_run_timed(factory_b, sequence, apply_fault))
    baseline = tuple(baseline_samples)
    candidate = tuple(candidate_samples)
    baseline_median = statistics.median(baseline)
    candidate_median = statistics.median(candidate)
    slowdown = candidate_median / baseline_median if baseline_median > 0 else math.inf
    within_budget = True
    if budget.max_slowdown is not None:
        within_budget = within_budget and slowdown <= budget.max_slowdown
    if budget.max_candidate_seconds is not None:
        within_budget = within_budget and candidate_median <= budget.max_candidate_seconds
    return PerformanceResult(
        baseline_seconds=baseline,
        candidate_seconds=candidate,
        baseline_median_seconds=baseline_median,
        candidate_median_seconds=candidate_median,
        slowdown=slowdown,
        within_budget=within_budget,
        budget=budget,
    )
def _diff_system_checked(
    factory_a: Callable[[], Any],
    factory_b: Callable[[], Any],
    sequence: Sequence[SystemEvent],
    *,
    return_compare: Callable[[Any, Any], bool],
    state_compare: Callable[[Any, Any], bool],
    state: Callable[[Any], Any] | None,
    side_effects: Callable[[Any], Any] | None,
    apply_fault: Callable[[Any, FaultEvent], None] | None,
    performance: PerformanceBudget | None,
    minimize: bool,
    replay_attempts: int,
) -> SystemDiffResult:
    """Build a system refactor report from one shared deterministic sequence."""
    original = tuple(_clone_event(event) for event in sequence)
    preflight_a = _construct(factory_a)
    preflight_b = _construct(factory_b)
    interface = _compare_interfaces(preflight_a, preflight_b)
    harness_errors = _validate_operations(preflight_a, preflight_b, original)
    if harness_errors:
        return SystemDiffResult(
            system_a=_factory_name(factory_a),
            system_b=_factory_name(factory_b),
            interface=interface,
            sequence=original,
            steps=[],
            mismatches=[],
            original_length=len(original),
            state_checked=False,
            side_effects_checked=False,
            fault_schedule_replayed=False,
            recovery_parity=None,
            replay_attempts=replay_attempts,
            replay_matches=0,
            minimization_performed=minimize,
            harness_errors=harness_errors,
            original_sequence=original,
        )
    steps, mismatches, state_checked, effects_checked, recovery_parity = _run_pair(
        factory_a,
        factory_b,
        original,
        return_compare=return_compare,
        state_compare=state_compare,
        state=state,
        side_effects=side_effects,
        apply_fault=apply_fault,
    )
    original_mismatch = mismatches[0] if mismatches else None
    minimized = original
    observation_error: str | None = None
    mismatch_signature: CanonicalObservation | None = None
    if mismatches:
        try:
            mismatch_signature = _mismatch_signature(mismatches[0])
        except ObservationError as exc:
            observation_error = str(exc)
    if minimize and mismatches and mismatch_signature is not None:
        candidate_sequence = _minimize(
            factory_a,
            factory_b,
            original,
            mismatch_signature=mismatch_signature,
            return_compare=return_compare,
            state_compare=state_compare,
            state=state,
            side_effects=side_effects,
            apply_fault=apply_fault,
        )
        if candidate_sequence != original:
            candidate_result = _run_pair(
                factory_a,
                factory_b,
                candidate_sequence,
                return_compare=return_compare,
                state_compare=state_compare,
                state=state,
                side_effects=side_effects,
                apply_fault=apply_fault,
            )
            (
                candidate_steps,
                candidate_mismatches,
                candidate_state,
                candidate_effects,
                candidate_recovery,
            ) = candidate_result
            if candidate_mismatches and observations_equal(
                _mismatch_signature(candidate_mismatches[0]),
                mismatch_signature,
            ):
                minimized = candidate_sequence
                steps = candidate_steps
                mismatches = candidate_mismatches
                state_checked = candidate_state
                effects_checked = candidate_effects
                recovery_parity = candidate_recovery

    replay_matches = 0
    expected_signature: str | None = None
    observed_signatures: list[str | None] = []
    if mismatches and mismatch_signature is not None:
        expected_signature = mismatch_signature.signature
        for _ in range(replay_attempts):
            _, replayed, _, _, _ = _run_pair(
                factory_a,
                factory_b,
                minimized,
                return_compare=return_compare,
                state_compare=state_compare,
                state=state,
                side_effects=side_effects,
                apply_fault=apply_fault,
            )
            if replayed:
                try:
                    replay_signature = _mismatch_signature(replayed[0])
                except ObservationError as exc:
                    observation_error = str(exc)
                    observed_signatures.append(None)
                    continue
                observed_signatures.append(replay_signature.signature)
                if exact_replay_match(
                    mismatch_signature,
                    replay_signature,
                    recorded_expected_signature=expected_signature,
                ):
                    replay_matches += 1
            else:
                observed_signatures.append(None)

    performance_result = (
        _measure_performance(factory_a, factory_b, original, apply_fault, performance)
        if performance is not None
        else None
    )
    return SystemDiffResult(
        system_a=_factory_name(factory_a),
        system_b=_factory_name(factory_b),
        interface=interface,
        sequence=minimized,
        steps=steps,
        mismatches=mismatches,
        original_length=len(original),
        state_checked=state_checked,
        side_effects_checked=effects_checked,
        fault_schedule_replayed=True,
        recovery_parity=recovery_parity,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        minimization_performed=minimize,
        performance=performance_result,
        expected_signature=expected_signature,
        observed_signatures=tuple(observed_signatures),
        reason=observation_error,
        original_sequence=original,
        original_mismatch=original_mismatch,
    )
