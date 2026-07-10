"""Differential testing with minimized, replay-scoped evidence.

``diff`` gives two revisions isolated copies of the same generated input and
compares their full observable outcome envelope: return or exception, mutated
arguments, bound receiver state, and explicitly selected side effects. A
divergence produces one immutable minimized witness and a JSON-ready evidence
artifact; sampled agreement remains bounded evidence, not equivalence.
"""

from __future__ import annotations

import copy
import hashlib
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

from ordeal.auto import _infer_strategies
from ordeal.finding_evidence import (
    _build_divergence_evidence,
    _json_ready,
    _sha256_json,
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
    return bool(a == b)


def _approx_equal(a: Any, b: Any, rtol: float, atol: float) -> bool:
    """Return recursive approximate equality for common numeric containers."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        if math.isinf(a) or math.isinf(b):
            return a == b
        return abs(a - b) <= atol + rtol * abs(b)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            _approx_equal(left, right, rtol, atol) for left, right in zip(a, b)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_approx_equal(a[key], b[key], rtol, atol) for key in a)
    if hasattr(a, "shape") and hasattr(b, "shape"):
        try:
            import numpy as np

            return bool(np.allclose(a, b, rtol=rtol, atol=atol))
        except (ImportError, TypeError, ValueError):
            pass
    return bool(a == b)


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


def _clone_value(value: Any, *, label: str) -> Any:
    """Deep-copy one isolation boundary or classify the failure honestly."""
    try:
        return copy.deepcopy(value)
    except Exception as exc:
        raise _ReconstructionInconclusive(f"could not reconstruct {label}: {exc}") from exc


def _clone_inputs(
    kwargs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Preserve a witness and make independent arguments for both revisions."""
    witness = _clone_value(kwargs, label="the witness input")
    kwargs_a = _clone_value(kwargs, label="revision a arguments")
    kwargs_b = _clone_value(kwargs, label="revision b arguments")
    return witness, kwargs_a, kwargs_b


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
            value = bound(**kwargs)
        except Exception as exc:
            exception = exc
            location = _terminal_source_location(exc)
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
    """Compare selected state directly without lossy serialization or hashing."""
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseException):
        return _state_equal(left.args, right.args)
    if isinstance(left, Mapping):
        return left.keys() == right.keys() and all(
            _state_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _state_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, (set, frozenset)):
        return left == right
    if hasattr(left, "shape") and hasattr(right, "shape"):
        try:
            import numpy as np

            return bool(np.array_equal(left, right, equal_nan=True))
        except (ImportError, TypeError, ValueError):
            return False
    try:
        return bool(left == right)
    except (TypeError, ValueError):
        return False


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
        primary: dict[str, Any] = {
            "kind": "exception",
            "exception_type": (
                f"{type(outcome.exception).__module__}.{type(outcome.exception).__qualname__}"
            ),
            "message": str(outcome.exception),
            "terminal_source_location": outcome.terminal_source_location,
        }
    else:
        primary = {
            "kind": "return",
            "value": _json_ready(outcome.value),
            "normalized_value": _json_ready(normalized),
        }
    primary.update(
        {
            "mutated_arguments": _json_ready(outcome.mutated_arguments),
            "receiver_state": _json_ready(outcome.receiver_state),
            "side_effects": _json_ready(outcome.side_effects),
        }
    )
    return primary


def _candidate_observations(candidate: _Candidate) -> dict[str, Any]:
    """Return paired artifact observations for one divergence candidate."""
    return {
        "a": _outcome_payload(candidate.outcome_a, candidate.normalized_a),
        "b": _outcome_payload(candidate.outcome_b, candidate.normalized_b),
    }


def _candidate_signature(candidate: _Candidate) -> str:
    """Hash the paired observations and exact differing envelope channels."""
    return _sha256_json(
        {
            "observations": _candidate_observations(candidate),
            "differences": candidate.differences,
        }
    )


def _outcome_replays(expected: _CallOutcome, observed: _CallOutcome) -> bool:
    """Return whether one raw outcome exactly matches its minimized observation."""
    if expected.exception is not None or observed.exception is not None:
        if expected.exception is None or observed.exception is None:
            return False
        primary_matches = (
            type(expected.exception) is type(observed.exception)
            and str(expected.exception) == str(observed.exception)
            and expected.terminal_source_location == observed.terminal_source_location
        )
    else:
        primary_matches = _state_equal(expected.value, observed.value)
    return (
        primary_matches
        and _state_equal(expected.mutated_arguments, observed.mutated_arguments)
        and _state_equal(expected.receiver_state, observed.receiver_state)
        and _state_equal(expected.side_effects, observed.side_effects)
    )


def _candidate_replays(expected: _Candidate, observed: _Candidate) -> bool:
    """Return whether the same witness reproduced the complete paired envelope."""
    return (
        expected.differences == observed.differences
        and _outcome_replays(expected.outcome_a, observed.outcome_a)
        and _outcome_replays(expected.outcome_b, observed.outcome_b)
        and _state_equal(expected.normalized_a, observed.normalized_a)
        and _state_equal(expected.normalized_b, observed.normalized_b)
    )


def _callable_name(fn: Callable[..., Any]) -> str:
    """Return a stable module-qualified callable label where possible."""
    module = str(getattr(fn, "__module__", "") or "").strip()
    qualname = str(getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn))
    return f"{module}.{qualname}" if module else qualname


def _callable_binding(fn: Callable[..., Any]) -> dict[str, Any]:
    """Bind one callable identity to its inspectable source text and location."""
    target: Any = inspect.unwrap(fn)
    if not (inspect.isfunction(target) or inspect.ismethod(target)) and hasattr(
        target, "__call__"
    ):
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
            "exact paired observations including terminal exception source locations"
        ),
    }


def _public_outcome(outcome: _CallOutcome, normalized: Any) -> DiffOutcome:
    """Freeze one internal outcome for the public witness."""
    exception_type: type[Exception] | None = None
    exception_message = None
    if outcome.exception is not None:
        exception_type = type(outcome.exception)
        exception_message = str(outcome.exception)
    return DiffOutcome(
        returned=outcome.exception is None,
        return_value=_freeze(outcome.value),
        exception_type=exception_type,
        exception_message=exception_message,
        mutated_arguments=_freeze(outcome.mutated_arguments),
        receiver_state=_freeze(outcome.receiver_state),
        side_effects=_freeze(outcome.side_effects),
        terminal_source_location=_freeze(outcome.terminal_source_location),
        normalized_value=_freeze(normalized),
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
    if sequence is not None:
        if fixtures:
            raise TypeError("system diff does not accept function fixtures")
        if equivalence_proof is not None:
            raise TypeError("system diff does not accept equivalence_proof")
        if side_effects is not None and not callable(side_effects):
            raise TypeError("system diff side_effects must be a system probe")
        normalizer = normalize or _identity

        def return_compare(a: Any, b: Any) -> bool:
            normalized_a = normalizer(a)
            normalized_b = normalizer(b)
            if compare is not None:
                return bool(compare(normalized_a, normalized_b))
            return _default_compare(normalized_a, normalized_b, rtol, atol)

        return _diff_system(
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
    normalized_strategies: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized_strategies = {
            name: value if isinstance(value, st.SearchStrategy) else st.just(value)
            for name, value in fixtures.items()
        }
    strategies = _infer_strategies(fn_a, normalized_strategies)
    if strategies is None:
        raise ValueError(
            f"Cannot infer strategies for {getattr(fn_a, '__name__', fn_a)}. "
            "Provide fixtures for untyped parameters."
        )

    minimized: _Candidate | None = None
    example_count = [0]
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
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

        test()
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

    expected_signature = _candidate_signature(minimized)
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
        signature = _candidate_signature(replayed)
        observed_signatures.append(signature)
        if _candidate_replays(minimized, replayed):
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
        original_input=minimized.args,
        minimized_input=minimized.args,
        original_observations=_candidate_observations(minimized),
        observations=_candidate_observations(minimized),
        differences=minimized.differences,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        expected_signature=expected_signature,
        observed_signatures=observed_signatures,
    )
    artifact_path = _write_artifact(artifact, artifact_dir) if artifact_dir else None
    mismatch = Mismatch(
        args=copy.deepcopy(minimized.args),
        output_a=minimized.outcome_a.recorded_value,
        output_b=minimized.outcome_b.recorded_value,
        artifact=artifact,
        artifact_path=artifact_path,
    )
    witness = DiffWitness(
        args=_freeze(minimized.args),
        outcome_a=_public_outcome(minimized.outcome_a, minimized.normalized_a),
        outcome_b=_public_outcome(minimized.outcome_b, minimized.normalized_b),
        differences=minimized.differences,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        replay_verified=True,
        artifact=_freeze(artifact),
        artifact_path=artifact_path,
    )
    return DiffResult(
        function_a=_callable_name(fn_a),
        function_b=_callable_name(fn_b),
        total=example_count[0],
        mismatches=[mismatch],
        status="divergent",
        witness=witness,
    )
