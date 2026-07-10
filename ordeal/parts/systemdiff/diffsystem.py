from __future__ import annotations
# ruff: noqa
def _diff_system(
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
    """Return inconclusive when any selected observation is not lossless."""
    try:
        return _diff_system_checked(
            factory_a,
            factory_b,
            sequence,
            return_compare=return_compare,
            state_compare=state_compare,
            state=state,
            side_effects=side_effects,
            apply_fault=apply_fault,
            performance=performance,
            minimize=minimize,
            replay_attempts=replay_attempts,
        )
    except ObservationError as exc:
        return SystemDiffResult(
            system_a=_factory_name(factory_a),
            system_b=_factory_name(factory_b),
            interface=InterfaceReport(
                exports_a={},
                exports_b={},
                missing_from_a=(),
                missing_from_b=(),
                signature_mismatches=(),
            ),
            sequence=(),
            steps=[],
            mismatches=[],
            original_length=len(sequence),
            state_checked=False,
            side_effects_checked=False,
            fault_schedule_replayed=False,
            recovery_parity=None,
            replay_attempts=replay_attempts,
            replay_matches=0,
            reason=str(exc),
        )
