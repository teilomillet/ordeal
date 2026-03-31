"""API chaos testing — backward-compatible bridge.

This module re-exports the built-in OpenAPI engine from
:mod:`ordeal.integrations.openapi` and adds:

- **ChaosAPIHook** — schemathesis hook integration (requires ``ordeal[api]``)

For most users, import directly from :mod:`ordeal.integrations.openapi`
instead.  This module exists so that existing ``from
ordeal.integrations.schemathesis_ext import chaos_api_test`` continues to
work without changes.
"""

from __future__ import annotations

import logging
from typing import Any

from ordeal.faults import Fault

# Re-export the built-in engine's public API so existing imports keep working.
from ordeal.integrations.openapi import (  # noqa: F401
    ChaosAPIResult,
    _FaultScheduler,
    _TraceCollector,
    chaos_api_test,
    with_chaos,
)

__all__ = [
    "ChaosAPIResult",
    "ChaosAPIHook",
    "with_chaos",
    "chaos_api_test",
    "auto_faults",
]

_log = logging.getLogger(__name__)

# Keep a reference to the builtin for wrapping.
from ordeal.integrations.openapi import chaos_api_test as _builtin_chaos_api_test  # noqa: E402

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
            # Infer return types from hints or AST.
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

            # Universal: returns_none, raises, stale.
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
            # Type sentinels.
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
    import ast  # noqa: E401
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
    import ast  # noqa: E401
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


# Override chaos_api_test to support mutation_targets / auto_discover.
def chaos_api_test(  # type: ignore[no-redef]  # noqa: F811
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
    """Run OpenAPI chaos testing with optional auto-fault generation.

    Wraps the built-in engine and adds *mutation_targets* / *auto_discover*.
    """
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

    return _builtin_chaos_api_test(
        schema_url=schema_url,
        app=app,
        wsgi=wsgi,
        schema_path=schema_path,
        faults=all_faults,
        fault_probability=fault_probability,
        seed=seed,
        swarm=swarm if not use_auto else True,
        base_url=base_url,
        auth=auth,
        headers=headers,
        stateful=stateful,
        max_examples=max_examples,
        record_traces=record_traces,
    )


# ---------------------------------------------------------------------------
# Schemathesis bridge (requires ordeal[api])
# ---------------------------------------------------------------------------


def _import_schemathesis():
    """Import schemathesis with a helpful error message."""
    try:
        import schemathesis

        return schemathesis
    except ImportError:
        raise ImportError(
            "schemathesis is required for ChaosAPIHook. Install with: pip install ordeal[api]"
        ) from None


class ChaosAPIHook:
    """Schemathesis hook that injects faults around API calls.

    Requires ``pip install ordeal[api]`` (schemathesis).

    Register with :func:`register`::

        from ordeal.integrations.schemathesis_ext import ChaosAPIHook

        hook = ChaosAPIHook(faults=[...])
        hook.register()          # registers before_call + after_call globally
        # ... run schemathesis tests ...
        hook.unregister()        # clean up

    The hook methods also work standalone (useful for testing)::

        hook.before_call(context, case, kwargs)
        hook.after_call(context, case, response)
    """

    def __init__(
        self,
        faults: list[Fault],
        fault_probability: float = 0.3,
        seed: int | None = None,
        swarm: bool = False,
    ):
        self._scheduler = _FaultScheduler(
            faults,
            fault_probability=fault_probability,
            seed=seed,
            swarm=swarm,
        )
        # Public attributes for introspection.
        self.faults = faults
        self.probability = fault_probability
        self.rng = self._scheduler.rng
        self.eligible = self._scheduler.eligible

    # -- Hook methods (match schemathesis v4 signatures) ---------------------

    def before_call(self, context: Any, case: Any, kwargs: Any = None) -> None:
        """Randomly activate/deactivate faults before each API call."""
        self._scheduler.before_request()

    def after_call(self, context: Any, case: Any, response: Any = None) -> None:
        """Reset all faults after each API call."""
        self._scheduler.after_request()

    # -- Registration helpers ------------------------------------------------

    def register(self) -> None:
        """Register ``before_call`` and ``after_call`` with schemathesis."""
        schemathesis = _import_schemathesis()
        _bc = self.before_call
        _ac = self.after_call

        def before_call(context: Any, case: Any, kwargs: Any) -> None:  # noqa: E306
            _bc(context, case, kwargs)

        def after_call(context: Any, case: Any, response: Any) -> None:  # noqa: E306
            _ac(context, case, response)

        self._registered_before = before_call
        self._registered_after = after_call

        schemathesis.hook(before_call)
        schemathesis.hook(after_call)

    def unregister(self) -> None:
        """Unregister hooks previously registered via :meth:`register`."""
        schemathesis = _import_schemathesis()
        if hasattr(self, "_registered_before"):
            schemathesis.hooks.unregister(self._registered_before)
        if hasattr(self, "_registered_after"):
            schemathesis.hooks.unregister(self._registered_after)
