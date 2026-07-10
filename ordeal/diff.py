"""Differential testing with minimized, replay-scoped evidence.

``diff`` gives two revisions isolated copies of the same generated input and
compares their full observable outcome envelope: return or exception, mutated
arguments, bound receiver state, and explicitly selected side effects. A
divergence produces one immutable minimized witness and a JSON-ready evidence
artifact; sampled agreement remains bounded evidence, not equivalence.
"""

from __future__ import annotations

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


def _encode_system_replay_event(event: SystemEvent) -> dict[str, object]:
    """Encode one system event without invoking target-defined repr methods."""
    if isinstance(event, Operation):
        return {
            "kind": "operation",
            "name": event.name,
            "args": _encode_replay_value(event.args),
            "kwargs": _encode_replay_value(event.kwargs),
        }
    return {
        "kind": "fault",
        "name": event.name,
        "action": event.action,
        "parameters": _encode_replay_value(event.parameters),
    }


def _decode_system_replay_event(payload: Mapping[str, object]) -> SystemEvent:
    """Decode one exact system regression event."""
    if payload.get("kind") == "operation":
        args = _decode_replay_value(payload.get("args", ()))
        kwargs = _decode_replay_value(payload.get("kwargs", {}))
        if not isinstance(args, tuple) or not isinstance(kwargs, Mapping):
            raise TypeError("system operation replay data has invalid args or kwargs")
        return Operation(str(payload["name"]), args=args, kwargs=kwargs)
    if payload.get("kind") == "fault":
        parameters = _decode_replay_value(payload.get("parameters", {}))
        if not isinstance(parameters, Mapping):
            raise TypeError("system fault replay data has invalid parameters")
        return FaultEvent(
            str(payload["name"]),
            str(payload.get("action", "activate")),
            parameters,
        )
    raise ValueError(f"unknown system replay event kind: {payload.get('kind')!r}")


def replay_diff_regression_case(case: Mapping[str, object]) -> None:
    """Replay one saved function or system divergence until it stays fixed."""
    fn_a = _resolve_replay_callable(case["callable_a"])
    fn_b = _resolve_replay_callable(case["callable_b"])
    assert fn_a is not None and fn_b is not None
    compare = _resolve_replay_callable(case.get("compare"))
    normalize = _resolve_replay_callable(case.get("normalize"))
    options: dict[str, Any] = {
        "compare": compare,
        "normalize": normalize,
        "rtol": case.get("rtol"),
        "atol": case.get("atol"),
        "replay_attempts": 1,
    }
    mode = str(case["mode"])
    if mode == "function":
        kwargs = _decode_replay_value(case["kwargs"])
        if not isinstance(kwargs, Mapping) or not all(isinstance(key, str) for key in kwargs):
            raise TypeError("function regression kwargs must be a string-keyed mapping")
        result = diff(fn_a, fn_b, max_examples=1, **options, **dict(kwargs))
    elif mode == "system":
        encoded_sequence = case.get("sequence")
        if not isinstance(encoded_sequence, list):
            raise TypeError("system regression sequence must be a list")
        sequence = [
            _decode_system_replay_event(item)
            for item in encoded_sequence
            if isinstance(item, Mapping)
        ]
        if len(sequence) != len(encoded_sequence):
            raise TypeError("system regression sequence contains an invalid event")
        result = diff(
            fn_a,
            fn_b,
            sequence=sequence,
            state=_resolve_replay_callable(case.get("state")),
            side_effects=_resolve_replay_callable(case.get("side_effects")),
            apply_fault=_resolve_replay_callable(case.get("apply_fault")),
            minimize=False,
            **options,
        )
    else:
        raise ValueError(f"unknown diff regression mode: {mode!r}")
    if not result.no_divergence_found:
        raise AssertionError(
            f"saved {mode} divergence is not fixed: {result.status}"
            + (f" ({result.reason})" if result.reason else "")
        )


def _render_diff_regression(case: Mapping[str, object]) -> str:
    """Render one standalone, source-bindable diff regression."""
    return "\n".join(
        [
            '"""Generated by `ordeal diff`.',
            "",
            f"Target ID: {case['id']}",
            '"""',
            "",
            "from ordeal.diff import replay_diff_regression_case",
            "",
            f"CASE = {dict(case)!r}",
            "",
            "",
            f"def {_DIFF_REGRESSION_TEST}() -> None:",
            '    """Keep the minimized paired divergence fixed."""',
            "    replay_diff_regression_case(CASE)",
            "",
        ]
    )


def _persist_diff_regression(
    *,
    mode: Literal["function", "system"],
    fn_a: Callable[..., Any],
    fn_b: Callable[..., Any],
    artifact: Mapping[str, Any],
    artifact_path: str | None,
    regression_path: str | Path,
    manifest_path: str | Path,
    compare: Callable[[Any, Any], bool] | None,
    normalize: Callable[[Any], Any] | None,
    rtol: float | None,
    atol: float | None,
    kwargs: Mapping[str, Any] | None = None,
    sequence: Sequence[SystemEvent] | None = None,
    state: Callable[[Any], Any] | None = None,
    side_effects: object = None,
    apply_fault: Callable[[Any, FaultEvent], None] | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Persist one generated diff regression and register it for CI."""
    try:
        if artifact.get("status") != "supported":
            raise ValueError(
                "durable diff regressions require supported replay and minimization evidence"
            )
        if artifact_path is None:
            raise ValueError("durable diff regressions require artifact_dir")
        if mode == "function" and side_effects:
            raise TypeError("function side-effect bindings are not yet persistable")
        callable_a = _callable_import_path(fn_a, label="baseline")
        callable_b = _callable_import_path(fn_b, label="candidate")
        assert callable_a is not None and callable_b is not None
        identity = hashlib.sha256(
            f"{mode}\0{callable_a}\0{callable_b}".encode("utf-8")
        ).hexdigest()[:16]
        case: dict[str, object] = {
            "id": f"{mode}:{identity}",
            "mode": mode,
            "callable_a": callable_a,
            "callable_b": callable_b,
            "compare": _callable_import_path(compare, label="compare"),
            "normalize": _callable_import_path(normalize, label="normalize"),
            "rtol": rtol,
            "atol": atol,
        }
        if mode == "function":
            case["kwargs"] = _encode_replay_value(dict(kwargs or {}))
        else:
            case.update(
                {
                    "sequence": [_encode_system_replay_event(event) for event in sequence or ()],
                    "state": _callable_import_path(state, label="state probe"),
                    "side_effects": _callable_import_path(
                        side_effects if callable(side_effects) else None,
                        label="side-effect probe",
                    ),
                    "apply_fault": _callable_import_path(
                        apply_fault,
                        label="fault adapter",
                    ),
                }
            )
        output_path = Path(regression_path)
        if output_path.exists():
            existing = output_path.read_text(encoding="utf-8")
            if not existing.startswith('"""Generated by `ordeal diff`.'):
                raise ValueError(f"refusing to overwrite non-generated regression: {output_path}")
            if f"Target ID: {case['id']}" not in existing:
                raise ValueError(f"regression path already belongs to another diff: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_render_diff_regression(case), encoding="utf-8")
        finding_id = f"fnd_diff_{identity}"
        comparison_artifact = artifact.get("comparison", {})
        revisions_artifact = artifact.get("revisions", {})
        source_guards: list[dict[str, str]] = []

        def guard(path: object, binding: object) -> None:
            """Record one import path and measured source hash for CI."""
            if path is None or not isinstance(binding, Mapping):
                return
            source_sha256 = str(binding.get("source_sha256") or "")
            if source_sha256:
                source_guards.append({"callable": str(path), "source_sha256": source_sha256})

        guard(
            callable_a,
            revisions_artifact.get("a") if isinstance(revisions_artifact, Mapping) else None,
        )
        if isinstance(comparison_artifact, Mapping):
            guard(case.get("compare"), comparison_artifact.get("comparator"))
            guard(case.get("normalize"), comparison_artifact.get("normalizer"))
            guard(case.get("state"), comparison_artifact.get("state_probe"))
            guard(case.get("side_effects"), comparison_artifact.get("side_effect_probe"))
            guard(case.get("apply_fault"), comparison_artifact.get("fault_adapter"))
        registered, error = _register_python_regression(
            manifest_path=Path(manifest_path),
            finding_id=finding_id,
            change_kind=mode,
            target=f"{callable_a} -> {callable_b}",
            test_path=output_path,
            test_name=_DIFF_REGRESSION_TEST,
            evidence_path=Path(artifact_path),
            change_artifact_ids=[str(artifact.get("artifact_id"))],
            test_basis="paired_minimized_witness",
            extra={"source_guards": source_guards},
        )
        if error is not None:
            return output_path.as_posix(), None, None, error
        assert registered is not None
        return output_path.as_posix(), registered.as_posix(), finding_id, None
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        return None, None, None, str(exc)


def diff(
    fn_a: Callable[..., Any],
    fn_b: Callable[..., Any],
    *,
    max_examples: int = 100,
    rtol: float | None = None,
    atol: float | None = None,
    compare: Callable[[Any, Any], bool] | None = None,
    normalize: Callable[[Any], Any] | None = None,
    replay_attempts: int = 2,
    artifact_dir: str | Path | None = None,
    regression_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    side_effects: Mapping[str, SideEffect] | Callable[[Any], Any] | None = None,
    equivalence_proof: Callable[[Callable[..., Any], Callable[..., Any]], bool] | None = None,
    sequence: Sequence[SystemEvent] | None = None,
    state: Callable[[Any], Any] | None = None,
    apply_fault: Callable[[Any, FaultEvent], None] | None = None,
    performance: PerformanceBudget | None = None,
    minimize: bool = True,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> DiffResult | SystemDiffResult:
    """Search two revisions for a full-envelope behavioral divergence.

    Every observed divergence is minimized, replayed on the same input, and
    represented by a source-bound ``ordeal.divergence-evidence/v1`` artifact.
    The artifact is always available in ``result.artifacts``; pass
    ``artifact_dir`` to persist it as JSON. ``normalize`` transforms returned
    values before the default or custom comparator runs. Exceptions, mutated
    arguments, bound receiver state, and selected ``side_effects`` remain part
    of the comparison independently of return-value normalization.

    ``no_divergence_found`` is bounded by the sampled inputs and selected
    observable channels. Only an explicit successful ``equivalence_proof``
    produces ``status == "proven_equivalent"``.

    Providing ``sequence`` switches to system mode. The same ordered
    operations and fault transitions run against fresh instances from both
    zero-argument factories. Public exports, signatures, outcomes, public
    state, selected side effects, and recovery are reported together;
    ``performance`` remains a separate measured contract.

    System example::

        result = diff(
            OldStore,
            NewStore,
            sequence=[
                FaultEvent("timeout", "activate"),
                Operation("read"),
                FaultEvent("timeout", "deactivate"),
                Operation("read"),
            ],
            state=lambda store: store.snapshot(),
        )

    See ``docs/concepts/system-differential.md`` for the layman model and
    ``docs/guides/system-differential.md`` for a complete copyable tutorial.
    """
    if replay_attempts < 1:
        raise ValueError("replay_attempts must be >= 1")
    if max_examples < 1:
        raise ValueError("max_examples must be >= 1")
    if (regression_path is None) != (manifest_path is None):
        raise ValueError("regression_path and manifest_path must be provided together")
    if sequence is not None:
        if fixtures:
            raise TypeError("system diff does not accept function fixtures")
        if equivalence_proof is not None:
            raise TypeError("system diff does not accept equivalence_proof")
        if side_effects is not None and not callable(side_effects):
            raise TypeError("system diff side_effects must be a system probe")
        normalizer = normalize or _identity

        def return_compare(a: Any, b: Any) -> bool:
            try:
                normalized_a = normalizer(a)
                normalized_b = normalizer(b)
                _observe_losslessly(normalized_a, label="normalized system a return value")
                _observe_losslessly(normalized_b, label="normalized system b return value")
                if compare is not None:
                    return bool(compare(normalized_a, normalized_b))
                return _default_compare(normalized_a, normalized_b, rtol, atol)
            except _ReconstructionInconclusive as exc:
                raise ObservationError(str(exc)) from exc

        system_result = _diff_system(
            fn_a,
            fn_b,
            sequence,
            return_compare=return_compare,
            state_compare=_state_equal,
            state=state,
            side_effects=side_effects,
            apply_fault=apply_fault,
            performance=performance,
            minimize=minimize,
            replay_attempts=replay_attempts,
        )
        system_result = _attach_system_artifact(
            system_result,
            factory_a=fn_a,
            factory_b=fn_b,
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
            state=state,
            side_effects=side_effects,
            apply_fault=apply_fault,
            artifact_dir=artifact_dir,
        )
        if system_result.divergent and regression_path is not None and manifest_path is not None:
            (
                system_result.regression_path,
                system_result.manifest_path,
                system_result.finding_id,
                system_result.regression_error,
            ) = _persist_diff_regression(
                mode="system",
                fn_a=fn_a,
                fn_b=fn_b,
                artifact=system_result.artifact or {},
                artifact_path=system_result.artifact_path,
                regression_path=regression_path,
                manifest_path=manifest_path,
                compare=compare,
                normalize=normalize,
                rtol=rtol,
                atol=atol,
                sequence=system_result.sequence,
                state=state,
                side_effects=side_effects,
                apply_fault=apply_fault,
            )
        return system_result
    if any(value is not None for value in (state, apply_fault, performance)):
        raise TypeError("system diff options require sequence=")
    if side_effects is not None and not isinstance(side_effects, Mapping):
        raise TypeError("function diff side_effects must map names to SideEffect objects")
    selected_side_effects = dict(side_effects or {})
    invalid_effects = [
        name
        for name, effect in selected_side_effects.items()
        if not isinstance(effect, SideEffect)
    ]
    if invalid_effects:
        raise TypeError(
            "function diff side_effects values must be SideEffect objects: "
            + ", ".join(repr(name) for name in invalid_effects)
        )
    concrete_example: dict[str, Any] | None = None
    if fixtures and all(not isinstance(value, st.SearchStrategy) for value in fixtures.values()):
        try:
            _bind_named_arguments(fn_a, dict(fixtures))
        except TypeError:
            pass
        else:
            concrete_example = dict(fixtures)
    normalized_strategies: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized_strategies = {
            name: value if isinstance(value, st.SearchStrategy) else st.just(value)
            for name, value in fixtures.items()
        }
    strategies = (
        {} if concrete_example is not None else _infer_strategies(fn_a, normalized_strategies)
    )
    if strategies is None:
        raise ValueError(
            f"Cannot infer strategies for {getattr(fn_a, '__name__', fn_a)}. "
            "Provide fixtures for untyped parameters."
        )

    minimized: _Candidate | None = None
    example_count = [0]

    def evaluate(kwargs: dict[str, Any]) -> None:
        """Evaluate one generated or zero-argument example."""
        example_count[0] += 1
        candidate = _run_pair(
            fn_a,
            fn_b,
            kwargs,
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
            side_effects=selected_side_effects,
        )
        if candidate.differences:
            raise _DivergenceFound(candidate)

    try:
        if concrete_example is not None:
            evaluate(concrete_example)
        elif strategies:

            @given(**strategies)
            @settings(max_examples=max_examples, database=None)
            def test(**kwargs: Any) -> None:
                evaluate(kwargs)

            test()
        else:
            evaluate({})
    except _DivergenceFound as mismatch:
        minimized = mismatch.candidate
    except _ReconstructionInconclusive as exc:
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status="inconclusive",
            reason=str(exc),
        )
    except FlakyFailure:
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status="inconclusive",
            reason="Hypothesis could not replay one stable outcome mismatch",
        )

    if minimized is None:
        status = "no_divergence_observed"
        proof_method = None
        if equivalence_proof is not None and equivalence_proof(fn_a, fn_b):
            status = "proven_equivalent"
            proof_method = _callable_name(equivalence_proof)
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status=status,
            proof_method=proof_method,
        )

    expected_observation = _candidate_observation(minimized)
    expected_signature = expected_observation.signature
    observed_signatures: list[str | None] = []
    replay_matches = 0
    for _ in range(replay_attempts):
        try:
            replayed = _run_pair(
                fn_a,
                fn_b,
                minimized.args,
                compare=compare,
                normalize=normalize,
                rtol=rtol,
                atol=atol,
                side_effects=selected_side_effects,
            )
        except _ReconstructionInconclusive:
            observed_signatures.append(None)
            continue
        replayed_observation = _candidate_observation(replayed)
        observed_signatures.append(replayed_observation.signature)
        if _candidate_replays(
            expected_observation,
            replayed_observation,
            expected_signature=expected_signature,
        ):
            replay_matches += 1

    if replay_matches != replay_attempts:
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status="inconclusive",
            reason=(
                "the minimized outcome mismatch was not stable under exact replay: "
                f"{replay_matches}/{replay_attempts} matched"
            ),
        )

    input_observation = _observe_losslessly(
        minimized.args,
        label="differential witness input",
    )
    artifact = _build_divergence_evidence(
        revisions={
            "a": _callable_binding(fn_a),
            "b": _callable_binding(fn_b),
        },
        comparison=_comparison_binding(
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
        ),
        original_input=input_observation.json_value,
        minimized_input=input_observation.json_value,
        original_input_canonical=input_observation.payload,
        minimized_input_canonical=input_observation.payload,
        original_observations=_candidate_observations(minimized),
        observations=_candidate_observations(minimized),
        differences=minimized.differences,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        expected_signature=expected_signature,
        observed_signatures=observed_signatures,
    )
    artifact_path = _write_artifact(artifact, artifact_dir) if artifact_dir else None
    public_a = _public_outcome(minimized.outcome_a, minimized.normalized_a)
    public_b = _public_outcome(minimized.outcome_b, minimized.normalized_b)
    mismatch = Mismatch(
        args=_observe_losslessly(minimized.args, label="public witness input").json_value,
        output_a=(
            public_a.return_value
            if public_a.returned
            else {
                "exception_type": public_a.exception_type,
                "message": public_a.exception_message,
                "terminal_source_location": public_a.terminal_source_location,
            }
        ),
        output_b=(
            public_b.return_value
            if public_b.returned
            else {
                "exception_type": public_b.exception_type,
                "message": public_b.exception_message,
                "terminal_source_location": public_b.terminal_source_location,
            }
        ),
        artifact=artifact,
        artifact_path=artifact_path,
    )
    try:
        replay_args_json = json.dumps(
            _encode_replay_value(minimized.args),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except TypeError:
        replay_args_json = None
    witness = DiffWitness(
        args=_freeze(minimized.args),
        outcome_a=public_a,
        outcome_b=public_b,
        differences=minimized.differences,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        replay_verified=True,
        replay_args_json=replay_args_json,
        artifact=_freeze(artifact),
        artifact_path=artifact_path,
    )
    result = DiffResult(
        function_a=_callable_name(fn_a),
        function_b=_callable_name(fn_b),
        total=example_count[0],
        mismatches=[mismatch],
        status="divergent",
        witness=witness,
    )
    if regression_path is not None and manifest_path is not None:
        (
            result.regression_path,
            result.manifest_path,
            result.finding_id,
            result.regression_error,
        ) = _persist_diff_regression(
            mode="function",
            fn_a=fn_a,
            fn_b=fn_b,
            artifact=artifact,
            artifact_path=artifact_path,
            regression_path=regression_path,
            manifest_path=manifest_path,
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
            kwargs=minimized.args,
            side_effects=selected_side_effects,
        )
    return result
