from __future__ import annotations
# ruff: noqa
import hashlib
import importlib
import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
import hypothesis.strategies as st
from hypothesis import given, settings
from hypothesis.errors import FlakyFailure
from ordeal._observation import (
    CanonicalObservation,
    ObservationError,
    exact_replay_match,
    isolated_deepcopy,
    observations_equal,
    observe,
)
from ordeal.auto import _infer_strategies
from ordeal.finding_evidence import _build_divergence_evidence
from ordeal.regression_evidence import (
    _decode_replay_value,
    _encode_replay_value,
    _register_python_regression,
)
from ordeal.system_diff import (
    FaultEvent,
    Operation,
    PerformanceBudget,
    SystemDiffResult,
    SystemEvent,
    _diff_system,
)
DiffStatus = Literal[
    "divergent",
    "no_divergence_observed",
    "proven_equivalent",
    "inconclusive",
]
__all__ = [
    "DiffOutcome",
    "DiffResult",
    "DiffWitness",
    "DivergenceWitness",
    "FaultEvent",
    "Mismatch",
    "Operation",
    "OutcomeObservation",
    "PerformanceBudget",
    "SideEffect",
    "SystemDiffResult",
    "diff",
]
@dataclass(frozen=True)
class SideEffect:
    """One explicitly selected external state channel to isolate and compare.

    ``capture`` returns the current state. ``restore`` must replace that state
    with a previously captured value. Ordeal restores the baseline before and
    after each revision invocation.
    """

    capture: Callable[[], Any]
    restore: Callable[[Any], None]
@dataclass(frozen=True)
class DiffOutcome:
    """Immutable public observation of one revision's full outcome envelope."""

    returned: bool
    return_value: Any
    exception_type: type[Exception] | None
    exception_message: str | None
    mutated_arguments: Mapping[str, Any]
    receiver_state: Mapping[str, Any] | None
    side_effects: Mapping[str, Any]
    terminal_source_location: Mapping[str, Any] | None = None
    normalized_value: Any = None
@dataclass(frozen=True)
class DiffWitness:
    """One minimized, immutable, replay-measured divergence witness."""

    args: Mapping[str, Any]
    outcome_a: DiffOutcome
    outcome_b: DiffOutcome
    differences: tuple[str, ...]
    replay_attempts: int = 0
    replay_matches: int = 0
    replay_verified: bool = False
    artifact: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    artifact_path: str | None = None
    replay_args_json: str | None = None

    def __str__(self) -> str:
        """Format a compact full-envelope divergence preview."""
        return (
            f"  args:      {_truncate(self.args)}\n"
            f"  outcome_a: {_truncate(self.outcome_a)}\n"
            f"  outcome_b: {_truncate(self.outcome_b)}"
        )
OutcomeObservation = DiffOutcome
DivergenceWitness = DiffWitness
@dataclass
class Mismatch:
    """A compatibility view of one input where the two revisions disagree.

    ``artifact`` is the complete machine-readable evidence record. ``args`` and
    ``output_a``/``output_b`` preserve the original small debugging surface.
    """

    args: dict[str, Any]
    output_a: Any
    output_b: Any
    artifact: dict[str, Any] | None = None
    artifact_path: str | None = None

    def __str__(self) -> str:
        """Format the compact input and paired return/exception observations."""
        return (
            f"  args:     {_truncate(self.args)}\n"
            f"  output_a: {_truncate(self.output_a)}\n"
            f"  output_b: {_truncate(self.output_b)}"
        )
@dataclass
class DiffResult:
    """Bounded result of searching two revisions for behavioral divergence."""

    function_a: str
    function_b: str
    total: int
    mismatches: list[Mismatch] = field(default_factory=list)
    status: DiffStatus | None = None
    witness: DiffWitness | None = None
    reason: str | None = None
    proof_method: str | None = None
    regression_path: str | None = None
    manifest_path: str | None = None
    finding_id: str | None = None
    regression_error: str | None = None

    def __post_init__(self) -> None:
        """Derive the status for callers constructing legacy result objects."""
        if self.status is None:
            self.status = "divergent" if self.mismatches else "no_divergence_observed"
        if len(self.mismatches) > 1:
            raise ValueError("differential results may expose only one minimized mismatch")
        if self.status == "divergent" and self.witness is None:
            raise ValueError("divergent results require exactly one witness")
        if self.status == "divergent" and not self.witness.replay_verified:
            raise ValueError("divergent results require a replay-verified witness")
        if self.status != "divergent" and self.witness is not None:
            raise ValueError("only divergent results may expose a witness")

    @property
    def divergent(self) -> bool:
        """True when a sampled input established a full-envelope mismatch."""
        return self.status == "divergent"

    @property
    def no_divergence_found(self) -> bool:
        """True when no mismatch was observed; not itself an equivalence proof."""
        return self.status in {"no_divergence_observed", "proven_equivalent"}

    @property
    def equivalent(self) -> bool | None:
        """False if disproved, true only with an explicit proof, else unknown."""
        if self.status == "proven_equivalent":
            return True
        if self.status == "divergent":
            return False
        return None

    @property
    def artifacts(self) -> list[dict[str, Any]]:
        """Return every JSON-ready divergence artifact produced by this run."""
        return [item.artifact for item in self.mismatches if item.artifact is not None]

    @property
    def artifact_paths(self) -> list[str]:
        """Return paths written through ``artifact_dir``."""
        return [item.artifact_path for item in self.mismatches if item.artifact_path]

    def summary(self) -> str:
        """Return a bounded report and compact witness preview."""
        labels = {
            "divergent": "DIVERGENT",
            "inconclusive": "INCONCLUSIVE",
            "proven_equivalent": "PROVEN EQUIVALENT",
            "no_divergence_observed": "NO DIVERGENCE OBSERVED",
        }
        label = labels.get(str(self.status), str(self.status).upper())
        lines = [f"diff({self.function_a}, {self.function_b}): {self.total} examples, {label}"]
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        for mismatch in self.mismatches[:3]:
            lines.append(str(mismatch))
            if mismatch.artifact is not None:
                lines.append(f"  artifact: {mismatch.artifact.get('artifact_id')}")
        if len(self.mismatches) > 3:
            lines.append(f"  ... and {len(self.mismatches) - 3} more")
        return "\n".join(lines)
class _DivergenceFound(Exception):
    """Private signal that lets Hypothesis shrink one mismatch candidate."""

    def __init__(self, candidate: _Candidate) -> None:
        super().__init__("observable outcomes differ")
        self.candidate = candidate
class _ReconstructionInconclusive(Exception):
    """Internal signal for inputs or receivers that cannot be isolated."""
@dataclass
class _CallOutcome:
    """Mutable internal observation retained until artifact construction."""

    value: Any = None
    exception: Exception | None = None
    terminal_source_location: dict[str, Any] | None = None
    mutated_arguments: dict[str, Any] = field(default_factory=dict)
    receiver_state: dict[str, Any] | None = None
    side_effects: dict[str, Any] = field(default_factory=dict)

    @property
    def recorded_value(self) -> Any:
        """Return the legacy mismatch value for this outcome."""
        return self.exception if self.exception is not None else self.value
@dataclass
class _Candidate:
    """One Hypothesis divergence candidate and its comparison-normalized values."""

    args: dict[str, Any]
    outcome_a: _CallOutcome
    outcome_b: _CallOutcome
    normalized_a: Any
    normalized_b: Any
    differences: tuple[str, ...]
def _truncate(obj: Any, limit: int = 120) -> str:
    """Return a bounded representation for summaries."""
    rendered = repr(obj)
    return rendered[:limit] + "..." if len(rendered) > limit else rendered
def _identity(value: Any) -> Any:
    """Return *value* unchanged for source-bound identity normalization."""
    return value
def _default_compare(
    a: Any,
    b: Any,
    rtol: float | None,
    atol: float | None,
) -> bool:
    """Compare two values exactly or with explicit numeric tolerance."""
    if rtol is not None or atol is not None:
        return _approx_equal(a, b, rtol or 1e-9, atol or 0.0)
    if hasattr(a, "shape") and hasattr(b, "shape"):
        try:
            import numpy as np

            return bool(np.array_equal(a, b, equal_nan=True))
        except (ImportError, TypeError, ValueError):
            pass
    return _state_equal(a, b)
def _approx_equal(a: Any, b: Any, rtol: float, atol: float) -> bool:
    """Return recursive approximate equality for common numeric containers."""
    if type(a) is float and type(b) is float:
        if math.isnan(a) and math.isnan(b):
            return True
        if math.isinf(a) or math.isinf(b):
            return a == b
        return abs(a - b) <= atol + rtol * abs(b)
    if type(a) in {list, tuple} and type(b) in {list, tuple}:
        return len(a) == len(b) and all(
            _approx_equal(left, right, rtol, atol) for left, right in zip(a, b)
        )
    if type(a) is dict and type(b) is dict:
        if len(a) != len(b):
            return False
        unmatched = list(b.items())
        for left_key, left_value in a.items():
            left_key_observation = _observe_losslessly(left_key, label="mapping key")
            for index, (right_key, right_value) in enumerate(unmatched):
                right_key_observation = _observe_losslessly(right_key, label="mapping key")
                if observations_equal(left_key_observation, right_key_observation):
                    if not _approx_equal(left_value, right_value, rtol, atol):
                        return False
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    if hasattr(a, "shape") and hasattr(b, "shape"):
        try:
            import numpy as np

            return bool(np.allclose(a, b, rtol=rtol, atol=atol))
        except (ImportError, TypeError, ValueError):
            pass
    return _state_equal(a, b)
def _freeze(value: Any) -> Any:
    """Recursively freeze one public witness value."""
    if isinstance(value, Mapping):
        return MappingProxyType({_freeze(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    if hasattr(value, "shape") and hasattr(value, "copy"):
        frozen = value.copy()
        try:
            frozen.flags.writeable = False
        except (AttributeError, ValueError):
            pass
        return frozen
    return value
def _observe_losslessly(value: Any, *, label: str) -> CanonicalObservation:
    """Return one canonical observation or classify the diff as inconclusive."""
    try:
        return observe(value, label=label)
    except ObservationError as exc:
        raise _ReconstructionInconclusive(str(exc)) from exc
def _clone_value(
    value: Any,
    *,
    label: str,
    disjoint_from: tuple[Any, ...] = (),
) -> Any:
    """Deep-copy one isolation boundary or classify the failure honestly."""
    try:
        return isolated_deepcopy(value, label=label, disjoint_from=disjoint_from)
    except ObservationError as exc:
        raise _ReconstructionInconclusive(str(exc)) from exc
def _clone_inputs(
    kwargs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Preserve a witness and make independent arguments for both revisions."""
    witness = _clone_value(kwargs, label="the witness input")
    kwargs_a = _clone_value(
        kwargs,
        label="revision a arguments",
        disjoint_from=(witness,),
    )
    kwargs_b = _clone_value(
        kwargs,
        label="revision b arguments",
        disjoint_from=(witness, kwargs_a),
    )
    return witness, kwargs_a, kwargs_b
def _bind_named_arguments(
    fn: Callable[..., Any],
    arguments: Mapping[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Convert named generated values into a signature-correct call."""
    signature = inspect.signature(fn)
    parameters = list(signature.parameters.values())
    positional: list[Any] = []
    keyword: dict[str, Any] = {}

    positional_only = [
        parameter
        for parameter in parameters
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
    ]
    supplied_positions = [
        index for index, parameter in enumerate(positional_only) if parameter.name in arguments
    ]
    if supplied_positions:
        for parameter in positional_only[: max(supplied_positions) + 1]:
            if parameter.name in arguments:
                positional.append(arguments[parameter.name])
            elif parameter.default is not inspect.Parameter.empty:
                positional.append(parameter.default)
            else:
                raise TypeError(f"missing required positional-only argument: {parameter.name!r}")

    for parameter in parameters:
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            if parameter.name in arguments:
                values = arguments[parameter.name]
                if not isinstance(values, (list, tuple)):
                    raise TypeError(f"fixture {parameter.name!r} must provide a list or tuple")
                positional.extend(values)
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            if parameter.name in arguments:
                values = arguments[parameter.name]
                if not isinstance(values, Mapping):
                    raise TypeError(f"fixture {parameter.name!r} must provide a mapping")
                keyword.update(values)
            continue
        if parameter.name in arguments:
            keyword[parameter.name] = arguments[parameter.name]

    signature.bind(*positional, **keyword)
    return tuple(positional), keyword
def _terminal_source_location(exc: Exception) -> dict[str, Any] | None:
    """Return the terminal traceback frame used by exact replay signatures."""
    frame = exc.__traceback__
    if frame is None:
        return None
    while frame.tb_next is not None:
        frame = frame.tb_next
    filename = Path(frame.tb_frame.f_code.co_filename).resolve().as_posix()
    return {
        "path": filename,
        "line": frame.tb_lineno,
        "function": frame.tb_frame.f_code.co_name,
    }
def _prepare_callable(fn: Callable[..., Any]) -> tuple[Callable[..., Any], Any | None]:
    """Clone and rebind one receiver or stateful callable independently."""
    receiver = getattr(fn, "__self__", None)
    implementation = getattr(fn, "__func__", None)
    if receiver is not None:
        if inspect.isclass(receiver):
            raise _ReconstructionInconclusive(
                "could not reconstruct class-bound receiver; select class state as "
                "a restorable side effect or compare instance-bound methods"
            )
        cloned_receiver = _clone_value(
            receiver,
            label=f"bound receiver for {_callable_name(fn)}",
        )
        if cloned_receiver is receiver and _receiver_state(receiver):
            raise _ReconstructionInconclusive(
                "bound receiver deepcopy returned shared mutable state"
            )
        if implementation is not None:
            return (
                implementation.__get__(cloned_receiver, type(cloned_receiver)),
                cloned_receiver,
            )
        name = getattr(fn, "__name__", None)
        if name and hasattr(cloned_receiver, name):
            return getattr(cloned_receiver, name), cloned_receiver
        raise _ReconstructionInconclusive("could not bind reconstructed receiver")
    if not inspect.isroutine(fn) and not inspect.isclass(fn) and callable(fn):
        cloned_callable = _clone_value(fn, label=f"callable object {_callable_name(fn)}")
        if cloned_callable is fn and _receiver_state(fn):
            raise _ReconstructionInconclusive("callable deepcopy returned shared mutable state")
        return cloned_callable, cloned_callable
    return fn, None
def _capture_selected_side_effects(
    side_effects: Mapping[str, SideEffect],
) -> dict[str, Any]:
    """Capture independent snapshots of explicitly selected external state."""
    snapshots: dict[str, Any] = {}
    for name, spec in side_effects.items():
        try:
            observed = spec.capture()
        except Exception as exc:
            raise _ReconstructionInconclusive(
                f"could not capture side effect {name!r}: {exc}"
            ) from exc
        snapshots[name] = _clone_value(observed, label=f"side effect {name!r}")
    return snapshots
def _restore_selected_side_effects(
    side_effects: Mapping[str, SideEffect],
    baseline: Mapping[str, Any],
) -> None:
    """Restore every selected external state channel from an isolated snapshot."""
    for name, spec in side_effects.items():
        restored = _clone_value(baseline[name], label=f"side effect {name!r} baseline")
        try:
            spec.restore(restored)
        except Exception as exc:
            raise _ReconstructionInconclusive(
                f"could not restore side effect {name!r}: {exc}"
            ) from exc
def _receiver_state(receiver: Any | None) -> dict[str, Any] | None:
    """Capture a bound receiver's instance dictionary and slots."""
    if receiver is None:
        return None
    state: dict[str, Any] = {}
    try:
        state.update(vars(receiver))
    except TypeError:
        pass
    for cls in type(receiver).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name in {"__dict__", "__weakref__"} or name in state:
                continue
            try:
                state[name] = getattr(receiver, name)
            except AttributeError:
                pass
    return _clone_value(state, label="bound receiver state")
