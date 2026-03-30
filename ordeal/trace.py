"""Trace recording, serialization, replay, and shrinking.

A **Trace** captures every decision the Explorer made during one run:
which rules fired, what parameters were drawn, which faults toggled,
and what coverage was observed.  Traces enable:

- **Replay**: reproduce a failure exactly
- **Shrinking**: minimize a failing trace to the smallest reproducing case
- **Post-hoc analysis**: inspect the full sequence offline

    from ordeal.trace import Trace, replay, shrink

    trace = Trace.load("run-42.json")
    failure = replay(trace)          # does it reproduce?
    minimal = shrink(trace, MyTest)  # find the smallest version
"""
from __future__ import annotations

import base64
import copy
import json
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ordeal.chaos import ChaosTest


# ============================================================================
# Trace data structures
# ============================================================================

@dataclass
class TraceStep:
    """A single step in an exploration trace."""

    kind: str  # "rule" | "fault_toggle"
    name: str  # rule name or "+fault_name" / "-fault_name"
    params: dict[str, Any] = field(default_factory=dict)
    active_faults: list[str] = field(default_factory=list)
    edge_count: int = 0
    timestamp_offset: float = 0.0


@dataclass
class TraceFailure:
    """Serializable failure info (no live Exception reference)."""

    error_type: str
    error_message: str
    step: int


@dataclass
class Trace:
    """Complete trace of one exploration run."""

    run_id: int
    seed: int
    test_class: str  # "module.path:ClassName"
    from_checkpoint: int | None  # checkpoint run_id, or None
    steps: list[TraceStep] = field(default_factory=list)
    failure: TraceFailure | None = None
    edges_discovered: int = 0
    duration: float = 0.0

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        return json.loads(json.dumps(asdict(self), cls=_TraceEncoder))

    def save(self, path: str | Path) -> None:
        """Write trace to a JSON file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(asdict(self), f, cls=_TraceEncoder, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trace:
        """Reconstruct a Trace from a dict (loaded from JSON)."""
        steps = [TraceStep(**s) for s in data.get("steps", [])]
        fail_data = data.get("failure")
        failure = TraceFailure(**fail_data) if fail_data else None
        return cls(
            run_id=data["run_id"],
            seed=data["seed"],
            test_class=data["test_class"],
            from_checkpoint=data.get("from_checkpoint"),
            steps=steps,
            failure=failure,
            edges_discovered=data.get("edges_discovered", 0),
            duration=data.get("duration", 0.0),
        )

    @classmethod
    def load(cls, path: str | Path) -> Trace:
        """Load a trace from a JSON file."""
        with open(path) as f:
            data = json.load(f, cls=_TraceDecoder)
        return cls.from_dict(data)


# ============================================================================
# JSON encoder/decoder for non-standard types
# ============================================================================

class _TraceEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, bytes):
            return {"__bytes__": base64.b64encode(obj).decode()}
        if isinstance(obj, (set, frozenset)):
            return {"__set__": sorted(obj) if all(isinstance(x, (int, float, str)) for x in obj) else list(obj)}
        if isinstance(obj, Exception):
            return {"__error__": str(obj), "__type__": type(obj).__name__}
        try:
            return super().default(obj)
        except TypeError:
            return {"__repr__": repr(obj)}


class _TraceDecoder(json.JSONDecoder):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(object_hook=self._decode, **kwargs)

    @staticmethod
    def _decode(obj: dict) -> Any:
        if "__bytes__" in obj:
            return base64.b64decode(obj["__bytes__"])
        if "__set__" in obj:
            return set(obj["__set__"])
        return obj


# ============================================================================
# Replay
# ============================================================================

def replay(
    trace: Trace,
    test_class: type | None = None,
) -> Exception | None:
    """Replay a trace, returning the exception if it reproduces.

    Args:
        trace: The trace to replay.
        test_class: Override the class (default: import from trace metadata).

    Returns:
        The exception if the failure reproduces, or ``None``.
    """
    if test_class is None:
        test_class = _import_class(trace.test_class)

    return _replay_steps(trace.steps, test_class)


def _replay_steps(
    steps: list[TraceStep],
    test_class: type,
) -> Exception | None:
    """Replay a step sequence on a fresh machine. Returns exception or None."""
    machine = test_class()
    try:
        for step in steps:
            if step.kind == "fault_toggle":
                _replay_fault_toggle(machine, step.name)
            elif step.kind == "rule":
                _replay_rule(machine, step.name, step.params)
            # Check invariants after every step
            _check_invariants(machine)
    except Exception as e:
        return e
    finally:
        machine.teardown()
    return None


def _replay_fault_toggle(machine: ChaosTest, name: str) -> None:
    """Toggle a fault by its signed name ('+fault' or '-fault')."""
    if name.startswith("+"):
        fault_name = name[1:]
        for f in machine._faults:
            if f.name == fault_name:
                f.activate()
                return
    elif name.startswith("-"):
        fault_name = name[1:]
        for f in machine._faults:
            if f.name == fault_name:
                f.deactivate()
                return


def _replay_rule(machine: ChaosTest, name: str, params: dict[str, Any]) -> None:
    """Call a rule method with recorded parameters."""
    method = getattr(machine, name)
    # Filter out non-serializable proxy params
    clean_params = {k: v for k, v in params.items() if k != "data"}
    if "data" in params:
        from ordeal.explore import _DataProxy
        clean_params["data"] = _DataProxy()
    try:
        method(**clean_params)
    except TypeError:
        method()  # fallback: call with no args


def _check_invariants(machine: ChaosTest) -> None:
    """Run all @invariant methods on the machine."""
    for name in dir(type(machine)):
        attr = getattr(type(machine), name, None)
        if attr is not None and hasattr(attr, "hypothesis_stateful_invariant"):
            getattr(machine, name)()


def _import_class(class_path: str) -> type:
    """Import 'module.path:ClassName'."""
    import importlib
    module_path, class_name = class_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


# ============================================================================
# Shrinking
# ============================================================================

def shrink(
    trace: Trace,
    test_class: type | None = None,
    *,
    max_time: float = 30.0,
) -> Trace:
    """Shrink a failing trace to the minimal reproducing sequence.

    Three phases, applied iteratively until fixpoint:

    1. **Delta debugging**: remove large chunks (halves, quarters, ...)
    2. **Single-step elimination**: try removing each step individually
    3. **Fault simplification**: remove unnecessary fault toggle pairs

    Args:
        trace: A trace whose failure should reproduce.
        test_class: Override the class (default: import from trace metadata).
        max_time: Wall-clock time limit for shrinking.

    Returns:
        A new Trace with the minimal step sequence.
    """
    if test_class is None:
        test_class = _import_class(trace.test_class)

    steps = list(trace.steps)
    start = _time.monotonic()
    prev_len = len(steps) + 1

    while len(steps) < prev_len:
        if _time.monotonic() - start > max_time:
            break
        prev_len = len(steps)
        steps = _shrink_ddmin(steps, test_class)
        steps = _shrink_one_by_one(steps, test_class)
        steps = _shrink_faults(steps, test_class)

    shrunk = Trace(
        run_id=trace.run_id,
        seed=trace.seed,
        test_class=trace.test_class,
        from_checkpoint=None,
        steps=steps,
        failure=trace.failure,
        edges_discovered=trace.edges_discovered,
        duration=trace.duration,
    )
    return shrunk


def _shrink_ddmin(steps: list[TraceStep], test_class: type) -> list[TraceStep]:
    """Delta debugging: remove the largest possible chunks."""
    granularity = 2
    while granularity <= len(steps):
        chunk_size = max(1, len(steps) // granularity)
        i = 0
        removed_any = False
        while i < len(steps):
            candidate = steps[:i] + steps[i + chunk_size:]
            if _replay_steps(candidate, test_class) is not None:
                steps = candidate
                removed_any = True
            else:
                i += chunk_size
        if removed_any:
            granularity = 2
        else:
            granularity *= 2
    return steps


def _shrink_one_by_one(steps: list[TraceStep], test_class: type) -> list[TraceStep]:
    """Try removing each step individually."""
    i = 0
    while i < len(steps):
        candidate = steps[:i] + steps[i + 1:]
        if _replay_steps(candidate, test_class) is not None:
            steps = candidate
        else:
            i += 1
    return steps


def _shrink_faults(steps: list[TraceStep], test_class: type) -> list[TraceStep]:
    """Remove all toggles for each fault and check if failure still reproduces."""
    fault_names = {
        s.name.lstrip("+-") for s in steps if s.kind == "fault_toggle"
    }
    for fname in fault_names:
        candidate = [
            s for s in steps
            if not (s.kind == "fault_toggle" and s.name.lstrip("+-") == fname)
        ]
        if _replay_steps(candidate, test_class) is not None:
            steps = candidate
    return steps
