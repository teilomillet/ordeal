"""Trace recording, serialization, replay, shrinking, and test generation.

A **Trace** captures every decision the Explorer made during one run:
which rules fired, what parameters were drawn, which faults toggled,
and what coverage was observed.  Traces enable:

- **Replay**: reproduce a failure exactly
- **Shrinking**: minimize a failing trace to the smallest reproducing case
- **Test generation**: turn traces into standalone pytest test functions
- **Post-hoc analysis**: inspect the full sequence offline

    from ordeal.trace import Trace, replay, shrink, generate_tests

    trace = Trace.load("run-42.json")
    failure = replay(trace)          # does it reproduce?
    minimal = shrink(trace, MyTest)  # find the smallest version
    code = generate_tests([trace])   # generate pytest tests
"""

from __future__ import annotations

import base64
import functools
import json
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ordeal.chaos import ChaosTest


# ============================================================================
# Trace data structures
# ============================================================================


@dataclass
class TraceStep:
    """A single step in an exploration trace.

    ``active_faults`` is populated on ``fault_toggle`` steps (the
    snapshot after the toggle).  On ``rule`` steps it defaults to
    ``[]`` — the active set is derivable from the preceding
    ``fault_toggle`` steps in the trace.
    """

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
        return _sanitize(asdict(self))

    def save(self, path: str | Path) -> None:
        """Write trace to a JSON file.

        If *path* ends with ``.gz`` (e.g. ``trace.json.gz``), the
        output is gzip-compressed — typically 5-10x smaller than
        plain JSON, useful for long exploration runs.
        """
        import gzip as _gzip

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(asdict(self), cls=_TraceEncoder, indent=2)
        if p.suffix == ".gz":
            with _gzip.open(p, "wt", encoding="utf-8") as f:
                f.write(data)
        else:
            with open(p, "w") as f:
                f.write(data)

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
        """Load a trace from a JSON file.

        Automatically detects gzip-compressed files (``.gz`` suffix).
        """
        import gzip as _gzip

        p = Path(path)
        if p.suffix == ".gz":
            with _gzip.open(p, "rt", encoding="utf-8") as f:
                data = json.load(f, cls=_TraceDecoder)
        else:
            with open(p) as f:
                data = json.load(f, cls=_TraceDecoder)
        return cls.from_dict(data)


# ============================================================================
# Direct dict sanitization (avoids JSON round-trip in to_dict)
# ============================================================================


def _sanitize(obj: Any) -> Any:
    """Recursively convert non-JSON-native types to serializable equivalents.

    Applies the same conversions as ``_TraceEncoder`` (bytes → base64,
    sets → sorted lists, exceptions → dicts) but builds the result dict
    directly instead of round-tripping through ``json.dumps``/``json.loads``.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, bytes):
        return {"__bytes__": base64.b64encode(obj).decode()}
    if isinstance(obj, (set, frozenset)):
        sortable = all(isinstance(x, (int, float, str)) for x in obj)
        return {"__set__": sorted(obj) if sortable else list(obj)}
    if isinstance(obj, Exception):
        return {"__error__": str(obj), "__type__": type(obj).__name__}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return {"__repr__": repr(obj)}


# ============================================================================
# JSON encoder/decoder for non-standard types (used by save/load)
# ============================================================================


def _encode_bytes(obj: Any) -> dict:
    return {"__bytes__": base64.b64encode(obj).decode()}


def _encode_set(obj: Any) -> dict:
    sortable = all(isinstance(x, (int, float, str)) for x in obj)
    return {"__set__": sorted(obj) if sortable else list(obj)}


# Dispatch table: one dict lookup instead of cascading isinstance.
# Exception subclasses are handled by a fallback isinstance check
# since they can't all be registered here.
_ENCODE_DISPATCH: dict[type, Callable[[Any], dict]] = {
    bytes: _encode_bytes,
    bytearray: _encode_bytes,
    set: _encode_set,
    frozenset: _encode_set,
}


class _TraceEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        handler = _ENCODE_DISPATCH.get(type(obj))
        if handler is not None:
            return handler(obj)
        if isinstance(obj, Exception):
            return {"__error__": str(obj), "__type__": type(obj).__name__}
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
    invariant_methods = _get_invariant_methods(type(machine))
    fault_index = {f.name: f for f in machine._faults}
    try:
        for step in steps:
            if step.kind == "fault_toggle":
                _replay_fault_toggle(fault_index, step.name)
            elif step.kind == "rule":
                _replay_rule(machine, step.name, step.params)
            # Check invariants after every step
            for method in invariant_methods:
                method(machine)
    except Exception as e:
        return e
    finally:
        machine.teardown()
    return None


def _replay_fault_toggle(fault_index: dict[str, Any], name: str) -> None:
    """Toggle a fault by its signed name ('+fault' or '-fault').

    Uses a pre-built ``{name: fault}`` dict for O(1) lookup instead
    of scanning the fault list on every toggle.
    """
    if name.startswith("+"):
        f = fault_index.get(name[1:])
        if f is not None:
            f.activate()
    elif name.startswith("-"):
        f = fault_index.get(name[1:])
        if f is not None:
            f.deactivate()


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


@functools.lru_cache(maxsize=32)
def _get_invariant_methods(cls: type) -> tuple[Callable, ...]:
    """Return unbound invariant methods for *cls*, cached per class.

    Avoids walking ``dir()`` on every step during replay and shrinking.
    The tuple of unbound methods is built once per class and reused
    across all subsequent replays of that class.
    """
    methods: list[Callable] = []
    for name in dir(cls):
        attr = getattr(cls, name, None)
        if attr is not None and hasattr(attr, "hypothesis_stateful_invariant"):
            methods.append(attr)
    return tuple(methods)


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
            candidate = steps[:i] + steps[i + chunk_size :]
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
        candidate = steps[:i] + steps[i + 1 :]
        if _replay_steps(candidate, test_class) is not None:
            steps = candidate
        else:
            i += 1
    return steps


def _shrink_faults(steps: list[TraceStep], test_class: type) -> list[TraceStep]:
    """Remove all toggles for each fault and check if failure still reproduces."""
    fault_names = {s.name.lstrip("+-") for s in steps if s.kind == "fault_toggle"}
    for fname in fault_names:
        candidate = [
            s for s in steps if not (s.kind == "fault_toggle" and s.name.lstrip("+-") == fname)
        ]
        if _replay_steps(candidate, test_class) is not None:
            steps = candidate
    return steps


# ============================================================================
# Test generation from traces
# ============================================================================


def generate_tests(
    traces: list[Trace],
    *,
    class_path: str | None = None,
) -> str:
    """Generate pytest test functions from exploration traces.

    Turns the most valuable traces into standalone, replayable tests.
    Each test recreates the exact rule/fault sequence that the explorer
    found.

    Args:
        traces: Traces to convert (typically from ``ExplorationResult``).
        class_path: Override the test class import path.

    Returns:
        Python source code for a test module.
    """
    if not traces:
        return ""

    # Use class path from first trace
    cp = class_path or traces[0].test_class
    module_path, class_name = cp.rsplit(":", 1)

    lines = [
        '"""Generated by ordeal explorer. Do not edit — re-run ordeal explore to regenerate."""',
        "",
        f"from {module_path} import {class_name}",
        "",
    ]

    for trace in traces:
        fn_name = _test_fn_name(trace)
        doc = _test_docstring(trace)
        body = _test_body(trace)
        lines.append("")
        lines.append(f"def {fn_name}():")
        lines.append(f'    """{doc}"""')
        lines.append(f"    machine = {class_name}()")
        lines.append("    try:")
        for stmt in body:
            lines.append(f"        {stmt}")
        lines.append("    finally:")
        lines.append("        machine.teardown()")
        lines.append("")

    return "\n".join(lines)


def _test_fn_name(trace: Trace) -> str:
    """Generate a descriptive test function name."""
    prefix = "test_fail" if trace.failure else "test_path"
    return f"{prefix}_r{trace.run_id}"


def _test_docstring(trace: Trace) -> str:
    """One-line docstring from trace metadata."""
    parts = [f"Run {trace.run_id}"]
    if trace.failure:
        parts.append(f"{trace.failure.error_type}: {trace.failure.error_message[:80]}")
    parts.append(f"{len(trace.steps)} steps")
    if trace.edges_discovered > 0:
        parts.append(f"{trace.edges_discovered} new edges")
    return ", ".join(parts)


def _test_body(trace: Trace) -> list[str]:
    """Convert trace steps to Python statements."""
    stmts: list[str] = []
    for step in trace.steps:
        if step.kind == "fault_toggle":
            if step.name.startswith("+"):
                fault_name = step.name[1:]
                stmts.append(f"# activate fault: {fault_name}")
                stmts.append("for f in machine._faults:")
                stmts.append(f"    if f.name == {fault_name!r}: f.activate()")
            elif step.name.startswith("-"):
                fault_name = step.name[1:]
                stmts.append(f"# deactivate fault: {fault_name}")
                stmts.append("for f in machine._faults:")
                stmts.append(f"    if f.name == {fault_name!r}: f.deactivate()")
        elif step.kind == "rule":
            params = {k: v for k, v in step.params.items() if k != "data"}
            if params:
                param_str = ", ".join(f"{k}={v!r}" for k, v in params.items())
                stmts.append(f"machine.{step.name}({param_str})")
            else:
                stmts.append(f"machine.{step.name}()")
    return stmts
