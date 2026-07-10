"""Replay one operation-and-fault story against two system versions.

Pass ``Operation`` and ``FaultEvent`` objects to ``diff(..., sequence=...)``.
The resulting ``SystemDiffResult`` separates semantic parity from an optional
``PerformanceBudget``. Start with ``docs/concepts/system-differential.md`` for
the mental model and ``docs/guides/system-differential.md`` for a complete run.
"""

from __future__ import annotations

import inspect
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Literal

from ordeal._observation import (
    CanonicalObservation,
    ObservationError,
    exact_replay_match,
    isolated_deepcopy,
    observations_equal,
    observe,
)


@dataclass(frozen=True)
class Operation:
    """Call one named method on both systems at the same timeline position.

    ``args`` and ``kwargs`` are deep-copied independently so mutations in the
    old version cannot change what the new version receives.

    Example::

        Operation("create_order", args=("A7",), kwargs={"quantity": 2})
    """

    name: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the operation and detach its argument containers."""
        if not self.name:
            raise ValueError("Operation.name must not be empty")
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "kwargs", dict(self.kwargs))


@dataclass(frozen=True)
class FaultEvent:
    """Apply the same named fault transition to both system versions.

    Interleave these with ``Operation`` objects. Actions named ``deactivate``,
    ``recover``, ``restart``, or ``clear`` begin the measured recovery phase.

    Example::

        FaultEvent("backend_timeout", "activate", {"after_ms": 50})
    """

    name: str
    action: str = "activate"
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the event and detach its parameter container."""
        if not self.name:
            raise ValueError("FaultEvent.name must not be empty")
        if not self.action:
            raise ValueError("FaultEvent.action must not be empty")
        object.__setattr__(self, "parameters", dict(self.parameters))


type SystemEvent = Operation | FaultEvent


@dataclass(frozen=True)
class PerformanceBudget:
    """Set a latency limit without turning slowness into behavior divergence.

    At least one of ``max_slowdown`` or ``max_candidate_seconds`` is required.
    Ordeal records repeated samples and medians for the original sequence;
    ``within_budget`` lives on the separate ``PerformanceResult``.
    """

    max_slowdown: float | None = None
    max_candidate_seconds: float | None = None
    samples: int = 5
    warmup: int = 1

    def __post_init__(self) -> None:
        """Reject thresholds that cannot express a useful budget."""
        if self.max_slowdown is None and self.max_candidate_seconds is None:
            raise ValueError("PerformanceBudget requires max_slowdown or max_candidate_seconds")
        if self.max_slowdown is not None and self.max_slowdown <= 0:
            raise ValueError("max_slowdown must be greater than zero")
        if self.max_candidate_seconds is not None and self.max_candidate_seconds <= 0:
            raise ValueError("max_candidate_seconds must be greater than zero")
        if self.samples <= 0:
            raise ValueError("samples must be greater than zero")
        if self.warmup < 0:
            raise ValueError("warmup must not be negative")


@dataclass(frozen=True)
class InterfaceReport:
    """Explain missing public names and changed callable signatures."""

    exports_a: Mapping[str, str]
    exports_b: Mapping[str, str]
    missing_from_a: tuple[str, ...]
    missing_from_b: tuple[str, ...]
    signature_mismatches: tuple[str, ...]

    @property
    def matches(self) -> bool:
        """Whether both public surfaces have the same exports and signatures."""
        return not (self.missing_from_a or self.missing_from_b or self.signature_mismatches)


@dataclass(frozen=True)
class SystemMismatch:
    """One outcome, state, or side-effect difference at a specific event."""

    kind: Literal["outcome", "state", "side_effects", "fault_schedule"]
    step: int
    event: SystemEvent
    observed_a: Any
    observed_b: Any
    replay_observation: CanonicalObservation | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    observation_a: CanonicalObservation | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    observation_b: CanonicalObservation | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class StepComparison:
    """Show both observations and parity flags for one timeline event."""

    index: int
    event: SystemEvent
    outcome_a: Any
    outcome_b: Any
    outcome_match: bool
    state_a: Any = None
    state_b: Any = None
    state_match: bool | None = None
    side_effects_a: Any = None
    side_effects_b: Any = None
    side_effects_match: bool | None = None
    recovery_phase: bool = False

    @property
    def matches(self) -> bool:
        """Whether every measured semantic contract matched at this step."""
        return (
            self.outcome_match
            and self.state_match is not False
            and self.side_effects_match is not False
        )


@dataclass(frozen=True)
class PerformanceResult:
    """Repeated timings, medians, slowdown, and the separate budget verdict."""

    baseline_seconds: tuple[float, ...]
    candidate_seconds: tuple[float, ...]
    baseline_median_seconds: float
    candidate_median_seconds: float
    slowdown: float
    within_budget: bool
    budget: PerformanceBudget


@dataclass
class SystemDiffResult:
    """Report interface, behavior, recovery, replay, and speed independently.

    ``sequence`` is the minimized shared story and ``fault_schedule`` is its
    fault-only view. Read ``status`` for semantic parity, replay counts for
    stability, and ``performance.within_budget`` for speed.
    """

    system_a: str
    system_b: str
    interface: InterfaceReport
    sequence: tuple[SystemEvent, ...]
    steps: list[StepComparison]
    mismatches: list[SystemMismatch]
    original_length: int
    state_checked: bool
    side_effects_checked: bool
    fault_schedule_replayed: bool
    recovery_parity: bool | None
    replay_attempts: int
    replay_matches: int
    minimization_performed: bool = False
    performance: PerformanceResult | None = None
    expected_signature: str | None = None
    observed_signatures: tuple[str | None, ...] = ()
    reason: str | None = None
    harness_errors: tuple[str, ...] = ()
    original_sequence: tuple[SystemEvent, ...] = ()
    original_mismatch: SystemMismatch | None = field(default=None, repr=False)
    artifact: dict[str, Any] | None = None
    artifact_path: str | None = None
    regression_path: str | None = None
    manifest_path: str | None = None
    finding_id: str | None = None
    regression_error: str | None = None

    @property
    def status(self) -> Literal["divergent", "no_divergence_observed", "inconclusive"]:
        """Return the evidence-scoped semantic status."""
        if self.reason is not None:
            return "inconclusive"
        if not self.interface.matches:
            return "divergent"
        if self.harness_errors:
            return "inconclusive"
        if not self.mismatches:
            return "no_divergence_observed"
        if self.replay_matches == self.replay_attempts:
            return "divergent"
        return "inconclusive"

    @property
    def divergent(self) -> bool:
        """Whether stable evidence disproved a measured semantic contract."""
        return self.status == "divergent"

    @property
    def no_divergence_found(self) -> bool:
        """Whether every measured semantic contract matched in this run."""
        return self.status == "no_divergence_observed"

    @property
    def equivalent(self) -> bool | None:
        """False when disproved, otherwise unknown beyond the measured sequence."""
        return False if self.status == "divergent" else None

    @property
    def minimized_length(self) -> int:
        """Number of events in the reported counterexample sequence."""
        return len(self.sequence)

    @property
    def fault_schedule(self) -> tuple[FaultEvent, ...]:
        """The exact minimized fault plan replayed against both systems."""
        return tuple(event for event in self.sequence if isinstance(event, FaultEvent))

    @property
    def replay_verified(self) -> bool | None:
        """Whether every exact replay reproduced the runtime divergence."""
        if not self.mismatches:
            return None
        return self.replay_matches == self.replay_attempts

    @property
    def artifacts(self) -> tuple[dict[str, Any], ...]:
        """Return the shared source-bound change artifact when one exists."""
        return (self.artifact,) if self.artifact is not None else ()

    def summary(self) -> str:
        """Return a compact report with behavior and performance kept separate."""
        behavior = {
            "divergent": "DIVERGENT",
            "inconclusive": "INCONCLUSIVE",
            "no_divergence_observed": "NO DIVERGENCE OBSERVED",
        }[self.status]
        lines = [
            f"system diff({self.system_a}, {self.system_b}): {behavior}",
            f"  interface: {'MATCH' if self.interface.matches else 'MISMATCH'}",
            f"  sequence: {self.minimized_length}/{self.original_length} events",
            f"  outcomes: {_contract_status(self.steps, 'outcome_match')}",
            f"  state: {_optional_contract_status(self.steps, 'state_match')}",
            f"  side effects: {_optional_contract_status(self.steps, 'side_effects_match')}",
            f"  fault schedule: {'MATCH' if self.fault_schedule_replayed else 'MISMATCH'}",
            f"  recovery: {_tri_state(self.recovery_parity)}",
        ]
        if not self.interface.matches:
            lines.append(
                "  interface details: "
                f"missing_from_a={self.interface.missing_from_a}, "
                f"missing_from_b={self.interface.missing_from_b}, "
                f"signatures={self.interface.signature_mismatches}"
            )
        if self.harness_errors:
            lines.append("  harness: " + "; ".join(self.harness_errors))
        if self.mismatches:
            lines.append(
                f"  replay: attempted {self.replay_attempts} / reproduced {self.replay_matches}"
            )
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        if self.performance is None:
            lines.append("  performance: NOT MEASURED")
        else:
            perf = self.performance
            status = "WITHIN BUDGET" if perf.within_budget else "BUDGET EXCEEDED"
            lines.append(
                "  performance: "
                f"{status}; baseline={perf.baseline_median_seconds:.6f}s, "
                f"candidate={perf.candidate_median_seconds:.6f}s, "
                f"slowdown={perf.slowdown:.3f}x"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class _Outcome:
    """A return value or ordinary exception captured from one event."""

    value: Any = None
    exception: Exception | None = None
    value_observation: CanonicalObservation | None = None
    replay_observation: CanonicalObservation | None = None

    @property
    def public_value(self) -> Any:
        """Return the value exposed in reports."""
        if self.exception is not None:
            if self.replay_observation is None:
                return None
            return self.replay_observation.json_value
        if self.value_observation is None:
            return None
        return self.value_observation.public_value


@dataclass(frozen=True)
class _ObservedValue:
    """One detached probe value and its structural observation."""

    value: Any
    observation: CanonicalObservation

    @property
    def public_value(self) -> Any:
        """Return the report-facing view without target-defined repr."""
        return self.observation.public_value


_RECOVERY_ACTIONS = frozenset({"clear", "deactivate", "recover", "restart"})
_UNMEASURED = object()


def _contract_status(steps: Sequence[StepComparison], attribute: str) -> str:
    """Return MATCH or MISMATCH for a required step contract."""
    return "MATCH" if all(bool(getattr(step, attribute)) for step in steps) else "MISMATCH"


def _optional_contract_status(steps: Sequence[StepComparison], attribute: str) -> str:
    """Return NOT CHECKED, MATCH, or MISMATCH for an optional contract."""
    values = [getattr(step, attribute) for step in steps]
    measured = [value for value in values if value is not None]
    if not measured:
        return "NOT CHECKED"
    return "MATCH" if all(measured) else "MISMATCH"


def _tri_state(value: bool | None) -> str:
    """Render an optional boolean contract status."""
    if value is None:
        return "NOT CHECKED"
    return "MATCH" if value else "MISMATCH"


def _factory_name(factory: Callable[[], Any]) -> str:
    """Return a stable display name for a factory or class."""
    return str(getattr(factory, "__name__", factory))


def _construct(factory: Callable[[], Any]) -> Any:
    """Construct one fresh system or fail closed on an invalid factory."""
    if not callable(factory):
        raise TypeError("system diff requires a zero-argument factory for each version")
    try:
        inspect.signature(factory).bind()
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{_factory_name(factory)} must be a zero-argument factory for system diff"
        ) from exc
    return factory()


def _operation_validation_error(system: Any, event: Operation) -> str | None:
    """Return why one operation cannot be invoked, without executing it."""
    try:
        target = getattr(system, event.name)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    if not callable(target):
        return f"{event.name!r} is not callable"
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    try:
        signature.bind(*event.args, **dict(event.kwargs))
    except TypeError as exc:
        return f"TypeError: {exc}"
    return None


def _validate_operations(
    system_a: Any,
    system_b: Any,
    sequence: Sequence[SystemEvent],
) -> tuple[str, ...]:
    """Return workload operations that are invalid against both revisions."""
    errors: list[str] = []
    for index, event in enumerate(sequence):
        if not isinstance(event, Operation):
            continue
        error_a = _operation_validation_error(system_a, event)
        error_b = _operation_validation_error(system_b, event)
        if error_a is not None and error_b is not None:
            errors.append(
                f"operation {event.name!r} at step {index} is invalid for both revisions: "
                f"a={error_a}; b={error_b}"
            )
    return tuple(errors)


def _public_exports(system: Any) -> dict[str, str]:
    """Collect public static exports without evaluating descriptors."""
    owner = system if isinstance(system, ModuleType) else type(system)
    exports: dict[str, str] = {}

    def record(name: str, raw: Any) -> None:
        """Record one public member and its callable signature when available."""
        value = raw.__func__ if isinstance(raw, (classmethod, staticmethod)) else raw
        kind = "property" if isinstance(raw, property) else type(value).__name__
        try:
            signature = str(inspect.signature(value)) if callable(value) else ""
        except (TypeError, ValueError):
            signature = "<unknown>"
        exports[name] = f"{kind}{signature}"

    for name, raw in inspect.getmembers_static(owner):
        if name.startswith("_"):
            continue
        record(name, raw)
    try:
        instance_members = vars(system)
    except TypeError:
        instance_members = {}
    for name, raw in instance_members.items():
        if not name.startswith("_") and name not in exports:
            record(name, raw)
    return exports


def _compare_interfaces(system_a: Any, system_b: Any) -> InterfaceReport:
    """Compare public names and exact rendered signatures."""
    exports_a = _public_exports(system_a)
    exports_b = _public_exports(system_b)
    names_a = set(exports_a)
    names_b = set(exports_b)
    shared = names_a & names_b
    return InterfaceReport(
        exports_a=exports_a,
        exports_b=exports_b,
        missing_from_a=tuple(sorted(names_b - names_a)),
        missing_from_b=tuple(sorted(names_a - names_b)),
        signature_mismatches=tuple(
            sorted(name for name in shared if exports_a[name] != exports_b[name])
        ),
    )


def _outcomes_match(
    outcome_a: _Outcome,
    outcome_b: _Outcome,
    return_compare: Callable[[Any, Any], bool],
) -> bool:
    """Compare return values or exact exception types and messages."""
    if outcome_a.exception is not None or outcome_b.exception is not None:
        if outcome_a.exception is None or outcome_b.exception is None:
            return False
        return type(outcome_a.exception) is type(outcome_b.exception) and str(
            outcome_a.exception
        ) == str(outcome_b.exception)
    return return_compare(outcome_a.value, outcome_b.value)


def _terminal_source_location(exc: Exception) -> dict[str, Any] | None:
    """Return the terminal traceback frame used by exact replay identity."""
    frame = exc.__traceback__
    if frame is None:
        return None
    while frame.tb_next is not None:
        frame = frame.tb_next
    return {
        "path": inspect.getabsfile(frame.tb_frame.f_code),
        "line": frame.tb_lineno,
        "function": frame.tb_frame.f_code.co_name,
    }


def _capture(call: Callable[[], Any]) -> _Outcome:
    """Capture a structurally representable return or ordinary exception."""
    try:
        value = call()
    except Exception as exc:
        exception_payload = {
            "kind": "exception",
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "terminal_source_location": _terminal_source_location(exc),
            "structural_exception": observe(exc, label="system exception").payload,
        }
        return _Outcome(
            exception=exc,
            replay_observation=observe(
                exception_payload,
                label="system exception outcome",
            ),
        )
    detached = isolated_deepcopy(value, label="system operation return value")
    value_observation = observe(detached, label="system operation return value")
    return _Outcome(
        value=detached,
        value_observation=value_observation,
        replay_observation=observe(
            {"kind": "return", "value": value_observation.payload},
            label="system return outcome",
        ),
    )


def _mismatch_signature(mismatch: SystemMismatch) -> CanonicalObservation:
    """Return the canonical divergence observation used by minimize and replay."""
    if mismatch.replay_observation is None:
        raise ObservationError("system mismatch has no lossless replay observation")
    return mismatch.replay_observation


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
