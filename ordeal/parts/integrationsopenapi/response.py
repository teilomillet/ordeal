from __future__ import annotations
# ruff: noqa
import asyncio
import functools
import io
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
import hypothesis.strategies as st
from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from ordeal.assertions import tracker
from ordeal.faults import Fault
from ordeal.quickcheck import biased
__all__ = [
    "ChaosAPIResult",
    "auto_faults",
    "with_chaos",
    "chaos_api_test",
]
_log = logging.getLogger(__name__)
_MAX_REF_DEPTH = 10
# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


@dataclass
class _Response:
    """Minimal HTTP response wrapper."""

    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None
# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChaosAPIResult:
    """Structured result of a chaos API test run.

    Attributes:
        total_requests: Number of API requests executed.
        failures: List of failure dicts with ``type`` and ``error`` keys.
            May also include ``endpoint``, ``method``, ``status_code``,
            and ``active_faults`` when available.
        fault_activations: Mapping of fault name to total activation count.
        duration_seconds: Wall-clock time for the test run.
        deferred_ok: Whether all deferred assertions (``sometimes``,
            ``reachable``) passed.
    """

    total_requests: int
    failures: list[dict[str, Any]]
    fault_activations: dict[str, int]
    duration_seconds: float
    deferred_ok: bool
    traces: tuple = ()  # tuple[Trace, ...] when record_traces=True
    # What the caller used — lets introspection see unused capabilities.
    _used_swarm: bool = False
    _used_auto_discover: bool = False
    _used_mutation_targets: bool = False
    _had_app: bool = False
    _requested_stateful: bool = False
    _stateful_links_available: bool = False
    _used_stateful: bool = False

    @property
    def passed(self) -> bool:
        """True if no failures occurred and all deferred assertions passed."""
        return len(self.failures) == 0 and self.deferred_ok

    @property
    def config_used(self) -> dict[str, bool]:
        """Which capabilities were active for this run.

        Compare against the full parameter list of :func:`chaos_api_test`
        to see what's available but wasn't used.
        """
        return {
            "swarm": self._used_swarm,
            "auto_discover": self._used_auto_discover,
            "mutation_targets": self._used_mutation_targets,
            "record_traces": bool(self.traces),
            "app": self._had_app,
        }

    def summary(self) -> str:
        """Structured summary with failure categorization.

        Groups failures by type so the output shows what was checked:
        application errors (5xx), protocol violations (Content-Length,
        Transfer-Encoding, body on 204), content errors (invalid JSON,
        missing Content-Type), security (CORS), and cross-request patterns.
        """
        status = "PASSED" if self.passed else "FAILED"
        nfaults = len(self.fault_activations)
        lines = [
            f"chaos_api_test: {status}"
            f" ({self.total_requests} requests, {nfaults} faults,"
            f" {self.duration_seconds:.1f}s)",
        ]
        if self.fault_activations:
            for name, count in self.fault_activations.items():
                lines.append(f"  {name}: activated {count}x")
        if self._requested_stateful:
            if self._used_stateful:
                lines.append("  Stateful mode: followed OpenAPI links")
            elif self._stateful_links_available:
                lines.append("  Stateful mode: links available but none were exercised")
            else:
                lines.append("  Stateful mode: fallback to parametrized (no supported links)")
        if self.failures:
            # Group by type for clarity
            by_type: dict[str, list[dict]] = {}
            for f in self.failures:
                by_type.setdefault(f.get("type", "unknown"), []).append(f)
            lines.append(f"  Failures: {len(self.failures)}")
            for ftype, items in by_type.items():
                lines.append(f"    [{ftype}] x{len(items)}")
                lines.append(f"      {items[0].get('error', '?')}")
        unused = [k for k, v in self.config_used.items() if not v]
        if unused:
            lines.append(f"  Unused capabilities: {', '.join(unused)}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"ChaosAPIResult({status}, requests={self.total_requests}, "
            f"faults={len(self.fault_activations)}, "
            f"failures={len(self.failures)}, "
            f"{self.duration_seconds:.1f}s)"
        )
# ---------------------------------------------------------------------------
# Fault scheduler
# ---------------------------------------------------------------------------


class _FaultScheduler:
    """Manages fault toggling with optional swarm mode and activation tracking.

    Centralises the fault-management logic shared by :func:`with_chaos`,
    :func:`chaos_api_test`.

    .. note:: Not thread-safe.  Each concurrent context should use its own
       scheduler instance.
    """

    def __init__(
        self,
        faults: list[Fault],
        fault_probability: float = 0.3,
        seed: int | None = None,
        swarm: bool = False,
    ):
        self.faults = faults
        self.probability = fault_probability
        self.rng = random.Random(seed)
        self.activations: dict[str, int] = {f.name: 0 for f in faults}
        self.request_count = 0

        if swarm and faults:
            k = max(1, self.rng.randint(1, len(faults)))
            self.eligible: set[Fault] = set(self.rng.sample(faults, k))
        else:
            self.eligible = set(faults)

    def before_request(self) -> list[str]:
        """Randomly activate faults for one request.

        Returns the names of faults that were activated.
        """
        self.request_count += 1
        active: list[str] = []
        for fault in self.faults:
            try:
                if fault in self.eligible and self.rng.random() < self.probability:
                    fault.activate()
                    self.activations[fault.name] += 1
                    active.append(fault.name)
                else:
                    fault.deactivate()
            except Exception:
                _log.warning("Fault %s raised during toggle", fault.name, exc_info=True)
        return active

    def after_request(self) -> None:
        """Reset all faults after a request."""
        for fault in self.faults:
            try:
                fault.reset()
            except Exception:
                _log.warning("Fault %s raised during reset", fault.name, exc_info=True)
# ---------------------------------------------------------------------------
# Trace collector
# ---------------------------------------------------------------------------


class _TraceCollector:
    """Records API calls as TraceStep entries during a test run."""

    def __init__(self) -> None:
        self.steps: list[Any] = []  # list[TraceStep]
        self._t0 = time.monotonic()
        self._pending_faults: list[str] = []

    def before(self, active_faults: list[str]) -> None:
        """Stash active faults until after_call provides the response."""
        self._pending_faults = active_faults

    def after(self, method: str, path: str, status_code: int | None) -> None:
        """Record a completed API call."""
        from ordeal.trace import TraceStep

        self.steps.append(
            TraceStep(
                kind="api_call",
                name=f"{method} {path}",
                endpoint=path,
                status_code=status_code,
                active_faults=list(self._pending_faults),
                timestamp_offset=time.monotonic() - self._t0,
            )
        )

    def to_trace(self, *, seed: int, label: str, failure: Any = None) -> Any:
        """Build a Trace object from collected steps."""
        from ordeal.trace import Trace, TraceFailure

        tf = None
        if failure is not None:
            tf = TraceFailure(
                error_type=type(failure).__name__,
                error_message=str(failure)[:500],
                step=len(self.steps) - 1,
            )
        return Trace(
            run_id=0,
            seed=seed,
            test_class=label,
            from_checkpoint=None,
            steps=self.steps,
            failure=tf,
            duration=time.monotonic() - self._t0,
        )
# ---------------------------------------------------------------------------
# Auto fault discovery (mutation + semantic + dependency)
# ---------------------------------------------------------------------------

_SEMANTIC_RETURNS: dict[str, list[tuple[str, Any]]] = {
    "int": [("zero", 0), ("negative", -1)],
    "float": [("zero", 0.0), ("nan", float("nan"))],
    "str": [("empty_str", "")],
    "bool": [("false", False), ("true", True)],
    "list": [("empty_list", [])],
    "dict": [("empty_dict", {})],
}
def auto_faults(
    targets: list[str],
    *,
    operators: list[str] | None = None,
    include_semantic: bool = True,
) -> list[Fault]:
    """Generate faults from source code: mutations + semantic + dependency.

    1. **Mutation** — AST mutations (flip comparisons, negate conditions).
    2. **Semantic** — type-aware: returns_none, raises, stale, type sentinels.
    3. **Dependency** — error_on_call for same-module callees.
    """
    import ast
    import importlib
    import inspect
    import textwrap
    import typing

    from ordeal.faults import PatchFault

    ops = operators or ["arithmetic", "comparison", "negate", "return_none", "boundary"]
    faults: list[Fault] = []
    seen_deps: set[str] = set()

    for target in targets:
        module_path, func_name = target.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
        except (ImportError, AttributeError) as exc:
            _log.warning(
                "Skipping %s: cannot resolve (%s). "
                "The target may have been renamed in the installed version.",
                target,
                exc,
            )
            continue
        # Unwrap decorators (@ray.remote, @functools.wraps, etc.)
        func = getattr(func, "_function", func)  # ray.remote
        try:
            func = inspect.unwrap(func)
        except (ValueError, TypeError):
            pass

        try:
            source = textwrap.dedent(inspect.getsource(func))
        except OSError:
            _log.warning("Cannot get source for %s, skipping", target)
            continue

        # --- 1. Mutations ---
        try:
            from ordeal.mutations import generate_mutants

            for mutant, mtree in generate_mutants(source, ops):
                try:
                    code = compile(mtree, f"<mutant:{mutant.description}>", "exec")
                    ns = dict(module.__dict__)
                    exec(code, ns)  # noqa: S102
                    mf = ns.get(func_name)
                    if mf is None:
                        continue
                except Exception:
                    continue
                faults.append(
                    PatchFault(
                        target,
                        lambda orig, _mf=mf: _mf,
                        name=f"mutant:{func_name}:{mutant.description}@L{mutant.line}",
                    )
                )
        except Exception:
            _log.warning("Mutation generation failed for %s", target, exc_info=True)

        # --- 2. Semantic faults ---
        if include_semantic:
            ret_types: set[str] = set()
            try:
                hints = typing.get_type_hints(func)
                ret = hints.get("return")
                if ret is not None:
                    ret_types.update(_extract_type_names(ret))
            except Exception:
                pass
            if not ret_types:
                try:
                    tree = ast.parse(source)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Return) and node.value is not None:
                            ret_types.update(_infer_type_from_ast(node.value))
                except Exception:
                    pass

            faults.append(
                PatchFault(
                    target,
                    lambda orig: lambda *a, **k: None,
                    name=f"returns_none({func_name})",
                )
            )
            faults.append(
                PatchFault(
                    target,
                    lambda orig, _n=func_name: _make_raiser(
                        RuntimeError, f"{_n}: fault-injected failure"
                    ),
                    name=f"raises({func_name})",
                )
            )
            faults.append(
                PatchFault(
                    target,
                    lambda orig: _make_stale(orig),
                    name=f"stale({func_name})",
                )
            )
            for type_name in ret_types:
                for label, value in _SEMANTIC_RETURNS.get(type_name, []):
                    faults.append(
                        PatchFault(
                            target,
                            lambda orig, _v=value: lambda *a, **k: _v,
                            name=f"returns_{label}({func_name})",
                        )
                    )

        # --- 3. Dependency faults ---
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                dep = _resolve_call_target(node, module, module_path)
                if dep and dep not in seen_deps and dep != target:
                    seen_deps.add(dep)
                    from ordeal.faults.io import error_on_call

                    faults.append(error_on_call(dep, error=Exception, message="fault injected"))
        except Exception:
            _log.warning("Dependency scan failed for %s", target, exc_info=True)

    return faults
def _make_raiser(exc_type: type, msg: str) -> Any:
    def _raise(*a: Any, **k: Any) -> Any:
        raise exc_type(msg)

    return _raise
def _make_stale(orig: Any) -> Any:
    cache: list = []

    def _stale(*a: Any, **k: Any) -> Any:
        if not cache:
            cache.append(orig(*a, **k))
        return cache[0]

    return _stale
def _extract_type_names(hint: Any) -> set[str]:
    import types as _types

    names: set[str] = set()
    origin = getattr(hint, "__origin__", None)
    if origin is _types.UnionType or str(origin) == "typing.Union":
        for arg in getattr(hint, "__args__", ()):
            names.update(_extract_type_names(arg))
    elif hint is type(None):
        pass
    elif isinstance(hint, type):
        names.add(hint.__name__)
    elif origin is not None:
        names.add(getattr(origin, "__name__", str(origin)))
    return names
def _infer_type_from_ast(node: Any) -> set[str]:
    import ast

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return {"bool"}
        if isinstance(node.value, int):
            return {"int"}
        if isinstance(node.value, float):
            return {"float"}
        if isinstance(node.value, str):
            return {"str"}
    elif isinstance(node, (ast.List, ast.ListComp)):
        return {"list"}
    elif isinstance(node, (ast.Dict, ast.DictComp)):
        return {"dict"}
    return set()
