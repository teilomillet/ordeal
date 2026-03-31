"""Built-in OpenAPI chaos testing engine (zero external dependencies).

Combines OpenAPI-driven request generation with ordeal's fault injection.
Faults fire on the server side while Hypothesis exercises every API endpoint.

No extra install needed -- uses only ``hypothesis`` (already required) and
the Python standard library.

Three schema sources:

- **URL** (requires a running server)::

    result = chaos_api_test(
        schema_url="http://localhost:8080/openapi.json",
        faults=[timing.slow("myapp.db.query", delay=2.0)],
    )

- **ASGI app** (in-process, no server needed -- ideal for FastAPI/Starlette)::

    result = chaos_api_test(
        app=my_fastapi_app,
        faults=[timing.slow("myapp.db.query", delay=2.0)],
    )

- **WSGI app** (in-process, no server needed -- ideal for Flask/Django)::

    result = chaos_api_test(
        app=my_flask_app,
        faults=[timing.slow("myapp.db.query", delay=2.0)],
        wsgi=True,
    )
"""

from __future__ import annotations

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

    @property
    def passed(self) -> bool:
        """True if no failures occurred and all deferred assertions passed."""
        return len(self.failures) == 0 and self.deferred_ok


# ---------------------------------------------------------------------------
# Fault scheduler
# ---------------------------------------------------------------------------


class _FaultScheduler:
    """Manages fault toggling with optional swarm mode and activation tracking.

    Centralises the fault-management logic shared by :func:`with_chaos`,
    :func:`chaos_api_test`, and the schemathesis bridge.

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
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)

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


def _discover_handlers(app: Any, *, max_depth: int = 3) -> list[str]:
    """BFS through app routes and call graph up to max_depth."""
    import ast
    import importlib as _il
    import inspect
    import textwrap

    targets: list[str] = []
    seen: set[str] = set()
    routes = getattr(app, "routes", None)
    if not routes:
        return targets
    queue: list[tuple[str, Any, int]] = []
    for route in routes:
        ep = getattr(route, "endpoint", None)
        if ep is None:
            continue
        name = getattr(ep, "__name__", "")
        mod = getattr(ep, "__module__", None)
        if not mod or not name:
            continue
        if any(s in name.lower() for s in ("openapi", "swagger", "docs", "schema")):
            continue
        path = f"{mod}.{name}"
        if path not in seen:
            seen.add(path)
            targets.append(path)
            queue.append((mod, ep, 0))
    while queue:
        mod_name, func, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            source = textwrap.dedent(inspect.getsource(func))
            tree = ast.parse(source)
        except Exception:
            continue
        mod_obj = _il.import_module(mod_name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            cn = node.func.id
            callee = getattr(mod_obj, cn, None)
            if callee is None or not callable(callee) or inspect.isclass(callee):
                continue
            if getattr(callee, "__module__", None) != mod_name:
                continue
            cp = f"{mod_name}.{cn}"
            if cp not in seen:
                seen.add(cp)
                targets.append(cp)
                queue.append((mod_name, callee, depth + 1))
    return targets


def _resolve_call_target(node: Any, module: Any, module_path: str) -> str | None:
    import ast
    import inspect as _inspect

    func = node.func
    if isinstance(func, ast.Name):
        obj = getattr(module, func.id, None)
        if obj is None or _inspect.isclass(obj) or not callable(obj):
            return None
        if getattr(obj, "__module__", None) in ("builtins", "_operator"):
            return None
        return f"{module_path}.{func.id}"
    return None


# ---------------------------------------------------------------------------
# $ref resolver
# ---------------------------------------------------------------------------


def _resolve_refs(node: Any, root: dict) -> Any:
    """Recursively resolve JSON Schema ``$ref`` pointers against *root*."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                _log.warning("External $ref not supported: %s", ref)
                return {}
            parts = ref[2:].split("/")
            target = root
            for part in parts:
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[part]
            return _resolve_refs(target, root)
        return {k: _resolve_refs(v, root) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, root) for item in node]
    return node


# ---------------------------------------------------------------------------
# JSON Schema -> Hypothesis strategy
# ---------------------------------------------------------------------------


def _schema_to_strategy(
    node: dict | None,
    root: dict,
    *,
    _depth: int = 0,
) -> st.SearchStrategy:
    """Convert a JSON Schema node to a boundary-biased Hypothesis strategy."""
    if _depth > _MAX_REF_DEPTH:
        return st.just(None)

    if node is None or not isinstance(node, dict):
        return st.just(None)

    # Resolve $ref inline
    if "$ref" in node:
        resolved = _resolve_refs(node, root)
        return _schema_to_strategy(resolved, root, _depth=_depth + 1)

    # Composition keywords
    if "oneOf" in node:
        subs = [_schema_to_strategy(s, root, _depth=_depth + 1) for s in node["oneOf"]]
        return st.one_of(*subs) if subs else st.just(None)

    if "anyOf" in node:
        subs = [_schema_to_strategy(s, root, _depth=_depth + 1) for s in node["anyOf"]]
        return st.one_of(*subs) if subs else st.just(None)

    if "allOf" in node:
        merged: dict[str, Any] = {}
        for sub in node["allOf"]:
            resolved = _resolve_refs(sub, root) if "$ref" in sub else sub
            for k, v in resolved.items():
                if k == "properties" and k in merged:
                    merged[k] = {**merged[k], **v}
                elif k == "required" and k in merged:
                    merged[k] = list(set(merged[k]) | set(v))
                else:
                    merged[k] = v
        return _schema_to_strategy(merged, root, _depth=_depth + 1)

    # Enum
    if "enum" in node:
        return st.sampled_from(node["enum"])

    # Const
    if "const" in node:
        return st.just(node["const"])

    schema_type = node.get("type")

    # OpenAPI 3.0 nullable
    nullable = node.get("nullable", False)

    def _wrap_nullable(strategy: st.SearchStrategy) -> st.SearchStrategy:
        return st.one_of(st.none(), strategy) if nullable else strategy

    if schema_type == "integer":
        return _wrap_nullable(
            biased.integers(
                min_value=node.get("minimum"),
                max_value=node.get("maximum"),
            )
        )

    if schema_type == "number":
        return _wrap_nullable(
            biased.floats(
                min_value=node.get("minimum"),
                max_value=node.get("maximum"),
                allow_nan=False,
            )
        )

    if schema_type == "string":
        fmt = node.get("format")
        if fmt == "date-time":
            return _wrap_nullable(st.datetimes().map(lambda d: d.isoformat()))
        if fmt == "date":
            return _wrap_nullable(st.dates().map(str))
        if fmt == "uuid":
            return _wrap_nullable(st.uuids().map(str))
        if fmt == "uri" or fmt == "url":
            return _wrap_nullable(st.just("https://example.com/path"))
        if fmt == "email":
            return _wrap_nullable(st.from_regex(r"[a-z]{3,8}@example\.com", fullmatch=True))
        if "pattern" in node:
            return _wrap_nullable(st.from_regex(node["pattern"], fullmatch=True))
        return _wrap_nullable(
            biased.strings(
                min_size=node.get("minLength", 0),
                max_size=node.get("maxLength", 100),
            )
        )

    if schema_type == "boolean":
        return _wrap_nullable(st.booleans())

    if schema_type == "null":
        return st.none()

    if schema_type == "array":
        items = node.get("items", {})
        item_strat = _schema_to_strategy(items, root, _depth=_depth + 1)
        return _wrap_nullable(
            biased.lists(
                item_strat,
                min_size=node.get("minItems", 0),
                max_size=node.get("maxItems", 10),
            )
        )

    if schema_type == "object" or "properties" in node:
        properties = node.get("properties", {})
        required_keys = set(node.get("required", []))
        required_dict = {
            k: _schema_to_strategy(v, root, _depth=_depth + 1)
            for k, v in properties.items()
            if k in required_keys
        }
        optional_dict = {
            k: _schema_to_strategy(v, root, _depth=_depth + 1)
            for k, v in properties.items()
            if k not in required_keys
        }
        if optional_dict:
            return _wrap_nullable(st.fixed_dictionaries(required_dict, optional=optional_dict))
        return _wrap_nullable(st.fixed_dictionaries(required_dict))

    # No type specified — try to infer from other keywords
    if "properties" in node or "required" in node:
        return _schema_to_strategy({**node, "type": "object"}, root, _depth=_depth)

    _log.debug("Unrecognized schema node, falling back to None: %s", node)
    return st.just(None)


# ---------------------------------------------------------------------------
# OpenAPI parser
# ---------------------------------------------------------------------------


@dataclass
class _Endpoint:
    """Parsed representation of a single API endpoint."""

    method: str  # GET, POST, PUT, DELETE, PATCH
    path: str  # /items/{item_id}
    path_params: list[dict[str, Any]] = field(default_factory=list)
    query_params: list[dict[str, Any]] = field(default_factory=list)
    header_params: list[dict[str, Any]] = field(default_factory=list)
    request_body: dict | None = None  # JSON Schema for body
    response_codes: set[int] = field(default_factory=set)


def _parse_endpoints(spec: dict) -> list[_Endpoint]:
    """Extract endpoints from a resolved OpenAPI 3.x spec."""
    root = spec
    endpoints: list[_Endpoint] = []

    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        # Path-level parameters apply to all operations
        path_level_params = _resolve_refs(path_item.get("parameters", []), root)

        for method in ("get", "post", "put", "delete", "patch"):
            if method not in path_item:
                continue
            operation = path_item[method]
            if not isinstance(operation, dict):
                continue

            # Merge path-level + operation-level params (operation wins)
            op_params = _resolve_refs(operation.get("parameters", []), root)
            params_by_key: dict[tuple[str, str], dict] = {}
            for p in path_level_params:
                resolved = _resolve_refs(p, root)
                params_by_key[(resolved.get("in", ""), resolved.get("name", ""))] = resolved
            for p in op_params:
                resolved = _resolve_refs(p, root)
                params_by_key[(resolved.get("in", ""), resolved.get("name", ""))] = resolved

            all_params = list(params_by_key.values())
            path_params = [p for p in all_params if p.get("in") == "path"]
            query_params = [p for p in all_params if p.get("in") == "query"]
            header_params = [p for p in all_params if p.get("in") == "header"]

            # Request body
            body_schema = None
            rb = _resolve_refs(operation.get("requestBody", {}), root)
            content = rb.get("content", {}) if isinstance(rb, dict) else {}
            if "application/json" in content:
                body_schema = _resolve_refs(content["application/json"].get("schema", {}), root)

            # Response codes
            response_codes: set[int] = set()
            for code_str in operation.get("responses", {}):
                try:
                    response_codes.add(int(code_str))
                except ValueError:
                    pass  # "default", "2XX", etc.

            endpoints.append(
                _Endpoint(
                    method=method.upper(),
                    path=path,
                    path_params=path_params,
                    query_params=query_params,
                    header_params=header_params,
                    request_body=body_schema,
                    response_codes=response_codes,
                )
            )

    return endpoints


# ---------------------------------------------------------------------------
# Endpoint -> Hypothesis strategy
# ---------------------------------------------------------------------------

_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


@dataclass
class _APICase:
    """Generated test case for one API call."""

    method: str
    path: str  # with path params substituted
    headers: dict[str, str]
    query_params: dict[str, str]
    body: Any  # JSON-serializable or None
    endpoint_path: str  # original path template


def _endpoint_strategy(endpoint: _Endpoint, root: dict) -> st.SearchStrategy[_APICase]:
    """Build a Hypothesis strategy that generates test cases for *endpoint*."""
    # Path params
    path_strats: dict[str, st.SearchStrategy] = {}
    for p in endpoint.path_params:
        schema = p.get("schema", {"type": "string"})
        path_strats[p["name"]] = _schema_to_strategy(schema, root).map(str)

    # Query params
    query_strats: dict[str, st.SearchStrategy] = {}
    required_query = {p["name"] for p in endpoint.query_params if p.get("required")}
    for p in endpoint.query_params:
        schema = p.get("schema", {"type": "string"})
        query_strats[p["name"]] = _schema_to_strategy(schema, root).map(str)

    # Body
    body_strat = (
        _schema_to_strategy(endpoint.request_body, root)
        if endpoint.request_body
        else st.just(None)
    )

    # Build path/query strategies
    if path_strats:
        path_dict_strat = st.fixed_dictionaries(path_strats)
    else:
        path_dict_strat = st.just({})

    if query_strats:
        required_q = {k: v for k, v in query_strats.items() if k in required_query}
        optional_q = {k: v for k, v in query_strats.items() if k not in required_query}
        if optional_q:
            query_dict_strat = st.fixed_dictionaries(required_q, optional=optional_q)
        else:
            query_dict_strat = st.fixed_dictionaries(required_q)
    else:
        query_dict_strat = st.just({})

    @st.composite
    def build_case(draw: st.DrawFn) -> _APICase:
        path_vals = draw(path_dict_strat)
        query_vals = draw(query_dict_strat)
        body_val = draw(body_strat)

        # Substitute path params
        resolved_path = endpoint.path
        for name, val in path_vals.items():
            resolved_path = resolved_path.replace(f"{{{name}}}", urllib.parse.quote(val, safe=""))

        return _APICase(
            method=endpoint.method,
            path=resolved_path,
            headers={},
            query_params=query_vals,
            body=body_val,
            endpoint_path=endpoint.path,
        )

    return build_case()


# ---------------------------------------------------------------------------
# Test clients
# ---------------------------------------------------------------------------


class _ASGIClient:
    """Minimal ASGI test client (no framework dependency)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _Response:
        parsed = urllib.parse.urlsplit(path)
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "path": parsed.path,
            "query_string": (parsed.query or "").encode(),
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }

        status_code = 0
        resp_headers: dict[str, str] = {}
        resp_body = bytearray()
        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": body or b"",
                    "more_body": False,
                }
            # After body, wait for disconnect (shouldn't normally reach here)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status_code, resp_headers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers", []):
                    resp_headers[k.decode().lower()] = v.decode()
            elif message["type"] == "http.response.body":
                resp_body.extend(message.get("body", b""))

        async def run() -> None:
            await self.app(scope, receive, send)

        # Run the ASGI app synchronously
        try:
            asyncio.get_running_loop()
            # Already in an event loop — run in a separate thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, run()).result()
        except RuntimeError:
            asyncio.run(run())

        return _Response(status_code=status_code, headers=resp_headers, body=bytes(resp_body))

    def get_schema(self, schema_path: str = "/openapi.json") -> dict:
        """Fetch and parse the OpenAPI schema from the app."""
        resp = self.request("GET", schema_path)
        return json.loads(resp.body)


class _WSGIClient:
    """Minimal WSGI test client (PEP 3333)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _Response:
        parsed = urllib.parse.urlsplit(path)
        body_bytes = body or b""

        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query or "",
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": "testserver",
            "wsgi.input": io.BytesIO(body_bytes),
            "wsgi.errors": io.StringIO(),
            "wsgi.url_scheme": "http",
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(body_bytes)),
        }

        if body_bytes:
            environ["CONTENT_TYPE"] = "application/json"

        for k, v in (headers or {}).items():
            key = f"HTTP_{k.upper().replace('-', '_')}"
            environ[key] = v

        status_code = 0
        resp_headers: dict[str, str] = {}

        def start_response(
            status: str,
            response_headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Any:
            nonlocal status_code, resp_headers
            status_code = int(status.split(" ", 1)[0])
            for k, v in response_headers:
                resp_headers[k.lower()] = v

        result = self.app(environ, start_response)
        try:
            resp_body = b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()

        return _Response(status_code=status_code, headers=resp_headers, body=resp_body)

    def get_schema(self, schema_path: str = "/openapi.json") -> dict:
        """Fetch and parse the OpenAPI schema from the app."""
        resp = self.request("GET", schema_path)
        return json.loads(resp.body)


class _URLClient:
    """HTTP client for remote servers via urllib (stdlib)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _Response:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                return _Response(
                    status_code=resp.status,
                    headers=resp_headers,
                    body=resp_body,
                )
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            return _Response(
                status_code=e.code,
                headers=resp_headers,
                body=resp_body,
            )

    def get_schema(self, schema_url: str = "/openapi.json") -> dict:
        """Fetch and parse the OpenAPI schema from a URL."""
        # If schema_url is a full URL, use it directly
        if schema_url.startswith("http"):
            url = schema_url
        else:
            url = f"{self.base_url}{schema_url}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def with_chaos(
    faults: list[Fault],
    *,
    fault_probability: float = 0.3,
    seed: int | None = None,
    swarm: bool = False,
) -> Callable:
    """Decorator that wraps a test function with fault injection.

    Before each call, randomly activates/deactivates faults.
    After the call, resets all faults to avoid cross-request interference.

    Args:
        faults: Fault instances to inject.
        fault_probability: Probability of each fault being active per request.
        seed: Random seed for reproducibility.
        swarm: Use swarm mode -- pick a random subset of faults once, then
            toggle only those for the lifetime of the wrapper.
    """
    scheduler = _FaultScheduler(
        faults,
        fault_probability=fault_probability,
        seed=seed,
        swarm=swarm,
    )

    def decorator(test_fn: Callable) -> Callable:
        @functools.wraps(test_fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracker.active = True
            scheduler.before_request()
            try:
                return test_fn(*args, **kwargs)
            finally:
                scheduler.after_request()

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def _validate_response(
    response: _Response,
    endpoint: _Endpoint,
    active_faults: list[str],
) -> dict[str, Any] | None:
    """Return a failure dict if the response indicates an error, else None."""
    # 5xx is always a failure
    if response.status_code >= 500:
        return {
            "type": "server_error",
            "error": f"{endpoint.method} {endpoint.path} returned {response.status_code}",
            "endpoint": endpoint.path,
            "method": endpoint.method,
            "status_code": response.status_code,
            "active_faults": active_faults,
        }
    return None


# ---------------------------------------------------------------------------
# Batteries-included entry point
# ---------------------------------------------------------------------------


def chaos_api_test(
    schema_url: str | None = None,
    *,
    app: Any = None,
    wsgi: bool = False,
    schema_path: str = "/openapi.json",
    faults: list[Fault] | None = None,
    fault_probability: float = 0.3,
    seed: int | None = None,
    swarm: bool = False,
    base_url: str | None = None,
    auth: Any = None,
    headers: dict[str, str] | None = None,
    stateful: bool = True,
    max_examples: int = 100,
    record_traces: bool = False,
    mutation_targets: list[str] | None = None,
    auto_discover: bool = False,
) -> ChaosAPIResult:
    """Run OpenAPI chaos testing against an API with fault injection.

    This is the batteries-included entry point.  Loads the OpenAPI schema,
    generates test cases via Hypothesis, and randomly injects faults while
    exercising every API endpoint.

    Supports three schema sources (exactly one of *schema_url* or *app*
    must be provided):

    - **URL**: pass *schema_url* (requires a running server).
    - **ASGI**: pass *app* (in-process, no server needed).
    - **WSGI**: pass *app* and ``wsgi=True`` (in-process, no server needed).

    Args:
        schema_url: URL to an OpenAPI schema (e.g.
            ``"http://localhost:8080/openapi.json"``).
        app: An ASGI or WSGI application instance for in-process testing.
        wsgi: Set ``True`` when *app* is a WSGI application (default assumes
            ASGI).
        schema_path: Path to the schema endpoint within *app* (default
            ``"/openapi.json"``).  Only used with *app*.
        faults: Fault instances to inject server-side.
        fault_probability: Probability of each fault being active per request.
        seed: Random seed for reproducibility.
        swarm: Use swarm mode -- random fault subset per run for better
            aggregate coverage.
        base_url: Override base URL for API calls (URL mode only).
        auth: String auth header value (e.g. ``"Bearer ..."``) or use
            *headers* for full control.
        headers: Extra headers to include in every request.
        stateful: Reserved for future link-based stateful testing.
        max_examples: Maximum test cases to generate.
        record_traces: If ``True``, record API calls as ordeal traces.
        mutation_targets: Dotted paths to functions for auto-fault generation
            via AST mutations, semantic faults, and dependency faults.
        auto_discover: If ``True`` and *app* is provided, BFS app routes to
            auto-discover fault targets.

    Returns:
        :class:`ChaosAPIResult` with request counts, failures, fault
        activation stats, and deferred assertion results.
    """
    if app is None and schema_url is None:
        raise ValueError("Provide either 'schema_url' or 'app'")

    # Build fault list: explicit faults + auto-generated
    all_faults = list(faults or [])
    use_auto = False
    if mutation_targets:
        all_faults.extend(auto_faults(mutation_targets))
        use_auto = True
    elif auto_discover and app is not None:
        discovered = _discover_handlers(app)
        if discovered:
            all_faults.extend(auto_faults(discovered))
            use_auto = True
    faults = all_faults

    if auth is not None:
        if isinstance(auth, str):
            headers = {**(headers or {}), "Authorization": auth}
        else:
            _log.warning(
                "auth must be a string (e.g. 'Bearer ...'). "
                "Use headers={'Authorization': '...'} for full control."
            )

    if stateful:
        _log.debug("Link-based stateful testing is not yet supported.")

    # Select client and fetch spec
    if app is not None:
        client: _ASGIClient | _WSGIClient | _URLClient = (
            _WSGIClient(app) if wsgi else _ASGIClient(app)
        )
        spec = client.get_schema(schema_path)
    else:
        assert schema_url is not None
        # Derive base_url from schema_url if not provided
        parsed = urllib.parse.urlsplit(schema_url)
        effective_base = base_url or f"{parsed.scheme}://{parsed.netloc}"
        client = _URLClient(effective_base)
        spec = client.get_schema(schema_url)

    # Parse endpoints
    endpoints = _parse_endpoints(spec)
    if not endpoints:
        _log.warning("No endpoints found in OpenAPI spec")
        return ChaosAPIResult(
            total_requests=0,
            failures=[],
            fault_activations={f.name: 0 for f in faults},
            duration_seconds=0.0,
            deferred_ok=True,
        )

    # Build composite strategy across all endpoints
    endpoint_strategies = [_endpoint_strategy(ep, spec) for ep in endpoints]
    composite = st.one_of(*endpoint_strategies)

    # Set up scheduler and tracking (auto-faults always use swarm)
    scheduler = _FaultScheduler(
        faults,
        fault_probability=fault_probability,
        seed=seed,
        swarm=swarm if not use_auto else True,
    )

    existing_props = {p.name for p in tracker.results}
    prev_active = tracker.active
    tracker.active = True

    collector = _TraceCollector() if record_traces else None
    failures: list[dict[str, Any]] = []
    extra_headers = headers or {}
    t0 = time.monotonic()
    first_exc: Exception | None = None

    # Map endpoint paths to parsed endpoints for validation
    ep_map: dict[str, _Endpoint] = {ep.path: ep for ep in endpoints}

    try:

        @given(case=composite)
        @h_settings(
            max_examples=max_examples,
            database=None,
            suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
        )
        def _test(case: _APICase) -> None:
            # Merge headers
            call_headers = {**extra_headers, **case.headers}
            if case.body is not None:
                call_headers.setdefault("content-type", "application/json")

            active = scheduler.before_request()
            if collector is not None:
                collector.before(active)

            try:
                body_bytes = json.dumps(case.body).encode() if case.body is not None else None

                # Build path with query string
                path = case.path
                if case.query_params:
                    qs = urllib.parse.urlencode(case.query_params)
                    path = f"{path}?{qs}"

                response = client.request(case.method, path, call_headers, body_bytes)

                if collector is not None:
                    collector.after(case.method, case.endpoint_path, response.status_code)

                # Validate
                ep = ep_map.get(case.endpoint_path)
                if ep is not None:
                    fail = _validate_response(response, ep, active)
                    if fail is not None:
                        failures.append(fail)
            finally:
                scheduler.after_request()

        _test()

    except Exception as exc:
        first_exc = exc
        failures.append({"type": "unexpected", "error": str(exc)})
    finally:
        scheduler.after_request()
        tracker.active = prev_active

    duration = time.monotonic() - t0

    # Check deferred assertions registered during this run
    new_failures = [p for p in tracker.failures if p.name not in existing_props]
    deferred_ok = len(new_failures) == 0
    for prop in new_failures:
        failures.append({"type": "deferred_assertion", "error": prop.summary})

    # Build trace if requested
    traces: tuple = ()
    if collector is not None:
        label = schema_url or (f"{'wsgi' if wsgi else 'asgi'}:{schema_path}")
        traces = (collector.to_trace(seed=seed or 0, label=label, failure=first_exc),)

    return ChaosAPIResult(
        total_requests=scheduler.request_count,
        failures=failures,
        fault_activations=dict(scheduler.activations),
        duration_seconds=duration,
        deferred_ok=deferred_ok,
        traces=traces,
    )
