from __future__ import annotations
# ruff: noqa
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
