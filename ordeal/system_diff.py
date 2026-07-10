"""Replay one operation-and-fault story against two system versions.

Pass ``Operation`` and ``FaultEvent`` objects to ``diff(..., sequence=...)``.
The resulting ``SystemDiffResult`` separates semantic parity from an optional
``PerformanceBudget``. Start with ``docs/concepts/system-differential.md`` for
the mental model and ``docs/guides/system-differential.md`` for a complete run.
"""

from __future__ import annotations

import copy
import inspect
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Literal


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
    performance: PerformanceResult | None = None

    @property
    def status(self) -> Literal["divergent", "no_divergence_observed", "inconclusive"]:
        """Return the evidence-scoped semantic status."""
        if not self.interface.matches:
            return "divergent"
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
        if self.mismatches:
            lines.append(
                f"  replay: attempted {self.replay_attempts} / reproduced {self.replay_matches}"
            )
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

    @property
    def public_value(self) -> Any:
        """Return the value exposed in reports."""
        return self.exception if self.exception is not None else self.value


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


def _capture(call: Callable[[], Any]) -> _Outcome:
    """Capture an ordinary exception without hiding process-level exits."""
    try:
        value = call()
    except Exception as exc:
        return _Outcome(exception=exc)
    try:
        return _Outcome(value=copy.deepcopy(value))
    except Exception as exc:
        raise TypeError("system operation return values must support deepcopy") from exc


def _observation_signature(value: Any) -> tuple[str, str, str]:
    """Return a stable-enough exact replay signature for one observation."""
    if isinstance(value, Exception):
        kind = f"{type(value).__module__}.{type(value).__qualname__}"
        return ("exception", kind, str(value))
    kind = f"{type(value).__module__}.{type(value).__qualname__}"
    return ("value", kind, repr(value))


def _mismatch_signature(mismatch: SystemMismatch) -> tuple[Any, ...]:
    """Bind minimization and replay to one concrete divergence."""
    event = mismatch.event
    event_identity = (
        type(event).__name__,
        event.name,
        event.action if isinstance(event, FaultEvent) else None,
        repr(event.parameters) if isinstance(event, FaultEvent) else repr(event.args),
        None if isinstance(event, FaultEvent) else repr(dict(event.kwargs)),
    )
    return (
        mismatch.kind,
        event_identity,
        _observation_signature(mismatch.observed_a),
        _observation_signature(mismatch.observed_b),
    )


def _clone_event(event: SystemEvent) -> SystemEvent:
    """Return an isolated event copy or explain why replay is unsafe."""
    try:
        return copy.deepcopy(event)
    except Exception as exc:
        raise TypeError("system diff events and arguments must support deepcopy") from exc


def _invoke(
    system: Any,
    event: SystemEvent,
    apply_fault: Callable[[Any, FaultEvent], None] | None,
) -> _Outcome:
    """Invoke one isolated operation or fault transition."""
    isolated = _clone_event(event)
    if isinstance(isolated, Operation):
        return _capture(lambda: getattr(system, isolated.name)(*isolated.args, **isolated.kwargs))
    if apply_fault is not None:
        return _capture(lambda: apply_fault(system, isolated))
    handler = getattr(system, "apply_fault", None)
    if not callable(handler):
        raise ValueError(
            "fault events require apply_fault= or an apply_fault(event) system method"
        )
    return _capture(lambda: handler(isolated))


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
    try:
        return copy.deepcopy(public)
    except Exception as exc:
        raise TypeError("system public state must support deepcopy") from exc


def _observe(
    system: Any,
    probe: Callable[[Any], Any] | None,
    *,
    automatic_state: bool,
) -> Any:
    """Capture a stable state or side-effect observation."""
    if probe is None:
        return _public_state(system) if automatic_state else _UNMEASURED
    try:
        observed = probe(system)
    except Exception as exc:
        raise RuntimeError("system diff probe failed") from exc
    try:
        return copy.deepcopy(observed)
    except Exception as exc:
        raise TypeError("system diff probes must return deepcopy-compatible values") from exc


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
        outcome_a = _invoke(system_a, event, apply_fault)
        outcome_b = _invoke(system_b, event, apply_fault)
        outcome_match = _outcomes_match(outcome_a, outcome_b, return_compare)

        state_a = _observe(system_a, state, automatic_state=True)
        state_b = _observe(system_b, state, automatic_state=True)
        state_measured = state_a is not _UNMEASURED and state_b is not _UNMEASURED
        state_match = state_compare(state_a, state_b) if state_measured else None

        effects_a = _observe(system_a, side_effects, automatic_state=False)
        effects_b = _observe(system_b, side_effects, automatic_state=False)
        effects_measured = effects_a is not _UNMEASURED and effects_b is not _UNMEASURED
        effects_match = state_compare(effects_a, effects_b) if effects_measured else None

        recovery_phase = recovering and isinstance(event, Operation)
        step = StepComparison(
            index=index,
            event=event,
            outcome_a=outcome_a.public_value,
            outcome_b=outcome_b.public_value,
            outcome_match=outcome_match,
            state_a=None if state_a is _UNMEASURED else state_a,
            state_b=None if state_b is _UNMEASURED else state_b,
            state_match=state_match,
            side_effects_a=None if effects_a is _UNMEASURED else effects_a,
            side_effects_b=None if effects_b is _UNMEASURED else effects_b,
            side_effects_match=effects_match,
            recovery_phase=recovery_phase,
        )
        steps.append(step)
        if recovery_phase:
            recovery_steps.append(step)
        if not outcome_match:
            mismatches.append(
                SystemMismatch(
                    "outcome", index, event, outcome_a.public_value, outcome_b.public_value
                )
            )
        if state_match is False:
            mismatches.append(SystemMismatch("state", index, event, state_a, state_b))
        if effects_match is False:
            mismatches.append(SystemMismatch("side_effects", index, event, effects_a, effects_b))

    state_checked = any(step.state_match is not None for step in steps)
    effects_checked = any(step.side_effects_match is not None for step in steps)
    recovery_parity = all(step.matches for step in recovery_steps) if recovery_steps else None
    return steps, mismatches, state_checked, effects_checked, recovery_parity


def _minimize(
    factory_a: Callable[[], Any],
    factory_b: Callable[[], Any],
    sequence: tuple[SystemEvent, ...],
    *,
    mismatch_signature: tuple[Any, ...],
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
            if mismatches and _mismatch_signature(mismatches[0]) == mismatch_signature:
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
    """Build a system refactor report from one shared deterministic sequence."""
    original = tuple(_clone_event(event) for event in sequence)
    interface = _compare_interfaces(_construct(factory_a), _construct(factory_b))
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
    minimized = original
    if minimize and mismatches:
        mismatch_signature = _mismatch_signature(mismatches[0])
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
            if (
                candidate_mismatches
                and _mismatch_signature(candidate_mismatches[0]) == mismatch_signature
            ):
                minimized = candidate_sequence
                steps = candidate_steps
                mismatches = candidate_mismatches
                state_checked = candidate_state
                effects_checked = candidate_effects
                recovery_parity = candidate_recovery

    replay_matches = 0
    if mismatches:
        expected_signature = _mismatch_signature(mismatches[0])
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
            if replayed and _mismatch_signature(replayed[0]) == expected_signature:
                replay_matches += 1

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
        performance=performance_result,
    )
