"""Zero-boilerplate testing. Point at code, get tests.

Three primitives:

1. **scan_module** — smoke-test every public function::

       result = scan_module("myapp.scoring")
       assert result.passed

2. **fuzz** — deep-fuzz a single function::

       result = fuzz(myapp.scoring.compute)
       result = fuzz(myapp.scoring.compute, model=model_strategy)

3. **chaos_for** — auto-generate a ChaosTest from a module's API::

       TestScoring = chaos_for("myapp.scoring", invariants=[finite])
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import importlib.util
import inspect
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Literal, Union, get_args, get_origin

import hypothesis.strategies as st
from hypothesis import given, settings
from hypothesis.stateful import rule

from ordeal.chaos import ChaosTest
from ordeal.faults import Fault
from ordeal.introspection import safe_get_annotations
from ordeal.invariants import Invariant
from ordeal.quickcheck import strategy_for_type

# ============================================================================
# Result types
# ============================================================================


@dataclass
class FunctionResult:
    """Result of testing a single function."""

    name: str
    passed: bool
    error: str | None = None
    error_type: str | None = None
    failing_args: dict[str, Any] | None = None
    crash_category: str | None = None
    property_violations: list[str] = field(default_factory=list)
    property_violation_details: list[dict[str, Any]] = field(default_factory=list)
    contract_violations: list[str] = field(default_factory=list)
    contract_violation_details: list[dict[str, Any]] = field(default_factory=list)
    replayable: bool | None = None
    replay_attempts: int = 0
    replay_matches: int = 0

    def __str__(self) -> str:
        if self.passed and not self.property_violations and not self.contract_violations:
            return f"  PASS  {self.name}"
        if not self.passed:
            label = "WARN" if self.crash_category == "speculative_crash" else "FAIL"
            return f"  {label}  {self.name}: {self.error}"
        if self.contract_violations:
            viols = "; ".join(self.contract_violations)
            return f"  NOTE  {self.name}: {viols}"
        viols = "; ".join(self.property_violations)
        return f"  WARN  {self.name}: {viols}"


@dataclass(frozen=True)
class ContractCheck:
    """Explicit semantic contract probe for a scanned callable."""

    name: str
    predicate: Callable[[Any], bool] = field(repr=False)
    kwargs: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None


@dataclass
class ScanResult:
    """Result of scanning a module."""

    module: str
    functions: list[FunctionResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    expected_failure_names: list[str] = field(default_factory=list)

    @property
    def results(self) -> list[FunctionResult]:
        """Deprecated alias for ``.functions``."""
        import warnings

        warnings.warn(
            "ScanResult.results was renamed to .functions",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.functions

    @property
    def expected_failures(self) -> list[FunctionResult]:
        """Functions that failed but were listed in *expected_failures*."""
        return [
            f for f in self.functions if not f.passed and f.name in self.expected_failure_names
        ]

    @property
    def passed(self) -> bool:
        """True if every tested function passed or is an expected failure."""
        return all(f.passed or f.name in self.expected_failure_names for f in self.functions)

    @property
    def total(self) -> int:
        return len(self.functions)

    @property
    def failed(self) -> int:
        return sum(
            1 for f in self.functions if not f.passed and f.name not in self.expected_failure_names
        )

    def summary(self) -> str:
        lines = [f"scan_module({self.module!r}): {self.total} functions, {self.failed} failed"]
        for f in self.functions:
            if not f.passed and f.name in self.expected_failure_names:
                lines.append(f"  XFAIL {f.name}: {f.error}")
            else:
                lines.append(str(f))
        if self.expected_failure_names:
            lines.append(f"  ({len(self.expected_failures)} expected failure(s))")
        if self.skipped:
            reasons: dict[str, int] = {}
            for _, reason in self.skipped:
                reasons[reason] = reasons.get(reason, 0) + 1
            reason_bits = ", ".join(
                f"{count} {reason}" if count > 1 else f"1 {reason}"
                for reason, count in sorted(reasons.items())
            )
            lines.append(f"  ({len(self.skipped)} skipped: {reason_bits})")
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)


@dataclass
class FuzzResult:
    """Result of fuzzing a single function."""

    function: str
    examples: int
    failures: list[Exception] = field(default_factory=list)
    failing_args: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    def summary(self) -> str:
        if self.passed:
            return f"fuzz({self.function}): {self.examples} examples, passed"
        lines = [
            f"fuzz({self.function}): {self.examples} examples, "
            f"{len(self.failures)} failure(s): {self.failures[0]}"
        ]
        if self.failing_args is not None:
            lines.append(f"  Failing input: {self.failing_args!r}")
        return "\n".join(lines)


# ============================================================================
# Common parameter name → strategy inference
# ============================================================================

# Maps parameter names (and patterns) to sensible Hypothesis strategies.
# This eliminates the most common fixture boilerplate — users only need
# to provide strategies for truly domain-specific types.
COMMON_NAME_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    # Text
    "text": st.text(min_size=0, max_size=200),
    "prompt": st.text(min_size=1, max_size=200),
    "response": st.text(min_size=0, max_size=500),
    "message": st.text(min_size=1, max_size=200),
    "content": st.text(min_size=0, max_size=500),
    "query": st.text(min_size=1, max_size=200),
    "input": st.text(min_size=0, max_size=200),
    "output": st.text(min_size=0, max_size=500),
    "label": st.text(min_size=1, max_size=50),
    "name": st.text(min_size=1, max_size=50),
    "key": st.text(min_size=1, max_size=50),
    "description": st.text(min_size=0, max_size=200),
    # Numeric
    "seed": st.integers(min_value=0, max_value=2**31 - 1),
    "random_seed": st.integers(min_value=0, max_value=2**31 - 1),
    "threshold": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "alpha": st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    "probability": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "weight": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "tolerance": st.floats(min_value=1e-10, max_value=0.1, allow_nan=False),
    "tol": st.floats(min_value=1e-10, max_value=0.1, allow_nan=False),
    "count": st.integers(min_value=0, max_value=100),
    "n": st.integers(min_value=1, max_value=100),
    "size": st.integers(min_value=1, max_value=100),
    "max_tokens": st.integers(min_value=1, max_value=500),
    "max_iterations": st.integers(min_value=1, max_value=20),
    "num_prompts": st.integers(min_value=1, max_value=50),
    "top_k": st.integers(min_value=1, max_value=10),
    "batch_size": st.integers(min_value=1, max_value=32),
    # Boolean
    "verbose": st.booleans(),
    "strict": st.booleans(),
    "normalize": st.booleans(),
}

# Suffix patterns: if param name ends with these, use this strategy
_SUFFIX_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    "_text": st.text(min_size=0, max_size=200),
    "_path": st.text(min_size=1, max_size=50),
    "_count": st.integers(min_value=0, max_value=100),
    "_size": st.integers(min_value=1, max_value=100),
    "_rate": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_prob": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_threshold": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_weight": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_flag": st.booleans(),
    "_enabled": st.booleans(),
}


# User-registered strategies (project-specific, added at runtime)
_REGISTERED_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {}
_REGISTERED_OBJECT_FACTORIES: dict[str, Any] = {}
_REGISTERED_OBJECT_SETUPS: dict[str, Any] = {}
_REGISTERED_OBJECT_SCENARIOS: dict[str, Any] = {}
_BOUNDARY_SMOKE_VALUES: dict[object, tuple[object, ...]] = {
    bool: (False, True),
    int: (0, 1, -1),
    float: (0.0, 1.0, -1.0),
    str: ("", "a"),
    bytes: (b"", b"x"),
}


def register_fixture(name: str, strategy: st.SearchStrategy[Any]) -> None:
    """Register a named fixture strategy for auto-scan.

    Registered strategies have highest priority after explicit fixtures.
    Call this in ``conftest.py`` to teach ordeal about project-specific
    types::

        from ordeal.auto import register_fixture
        import hypothesis.strategies as st

        register_fixture("model", st.builds(make_mock_model))
        register_fixture("direction", st.builds(make_unit_vector))

    After registration, ``scan_module`` and ``fuzz`` will auto-resolve
    parameters named ``model`` or ``direction`` without explicit fixtures.
    """
    _REGISTERED_STRATEGIES[name] = strategy


def register_object_factory(name: str, factory: Any) -> None:
    """Register an object factory for class-method targets.

    Use this for methods that need a prebuilt instance or collaborators.
    The factory can be sync or async and may return the instance directly.
    """
    _REGISTERED_OBJECT_FACTORIES[name] = factory


def register_object_setup(name: str, setup: Any) -> None:
    """Register a per-instance setup hook for class-method targets."""
    _REGISTERED_OBJECT_SETUPS[name] = setup


def register_object_scenario(name: str, scenario: Any) -> None:
    """Register a collaborator scenario hook for class-method targets."""
    _REGISTERED_OBJECT_SCENARIOS[name] = scenario


def _strategy_for_name(name: str) -> st.SearchStrategy[Any] | None:
    """Try to infer a strategy from the parameter name alone."""
    # 1. User-registered (project-specific, highest priority)
    if name in _REGISTERED_STRATEGIES:
        return _REGISTERED_STRATEGIES[name]
    # 2. Built-in common names
    if name in COMMON_NAME_STRATEGIES:
        return COMMON_NAME_STRATEGIES[name]
    # 3. Suffix patterns
    for suffix, strategy in _SUFFIX_STRATEGIES.items():
        if name.endswith(suffix):
            return strategy
    return None


# ============================================================================
# Helpers
# ============================================================================


def _resolve_module(module: str | ModuleType) -> ModuleType:
    if isinstance(module, str):
        return importlib.import_module(module)
    return module


def _unwrap(func: Any) -> Any:
    """Unwrap decorated functions to reach the original callable.

    Handles Ray ``@ray.remote`` (`._function``), ``functools.wraps``
    (``__wrapped__`` chains), and Celery-style patterns.
    """
    import inspect

    func = getattr(func, "_function", func)
    if getattr(func, "__ordeal_keep_wrapped__", False):
        return func
    try:
        func = inspect.unwrap(
            func,
            stop=lambda wrapped: getattr(wrapped, "__ordeal_keep_wrapped__", False),
        )
    except (ValueError, TypeError):
        pass
    return func


def _resolve_awaitable(value: Any) -> Any:
    """Resolve an awaitable value without forcing callers to use async APIs."""
    if not inspect.isawaitable(value):
        return value
    try:
        return asyncio.run(value)
    except RuntimeError as exc:
        if "asyncio.run()" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(value)
        finally:
            loop.close()


def _call_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call *func* and synchronously resolve any returned awaitable."""
    return _resolve_awaitable(func(*args, **kwargs))


def _signature_without_first_context(func: Any) -> inspect.Signature:
    """Return a callable signature with a leading self/cls parameter removed."""
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    if params and params[0].name in {"self", "cls"}:
        sig = sig.replace(parameters=params[1:])
    return sig


def _object_hook_candidates(owner: type) -> list[str]:
    """Return the registry keys that may refer to *owner*."""
    candidates = [
        f"{owner.__module__}:{owner.__qualname__}",
        f"{owner.__module__}.{owner.__qualname__}",
        f"{owner.__module__}:{owner.__name__}",
        f"{owner.__module__}.{owner.__name__}",
        owner.__qualname__,
        owner.__name__,
    ]
    return list(dict.fromkeys(candidates))


def _resolve_object_hook(owner: type, hooks: dict[str, Any] | None) -> Any | None:
    """Resolve a registered object hook for *owner* from several key styles."""
    if not hooks:
        return None
    for candidate in _object_hook_candidates(owner):
        if candidate in hooks:
            return hooks[candidate]
    return None


def _apply_instance_hook(instance: Any, hook: Any | None) -> Any:
    """Apply a setup or scenario hook and keep any replacement instance."""
    if hook is None:
        return instance
    result = _call_sync(hook, instance)
    return instance if result is None else result


def _make_sync_callable(
    func: Any,
    *,
    qualname: str | None = None,
    keep_wrapped: bool = False,
) -> Any:
    """Wrap *func* so callers can invoke sync or async callables uniformly."""

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return _call_sync(func, *args, **kwargs)

    try:
        wrapped.__signature__ = inspect.signature(func)
    except (TypeError, ValueError):
        pass
    if qualname is not None:
        wrapped.__qualname__ = qualname
    if keep_wrapped:
        wrapped.__ordeal_keep_wrapped__ = True
    return wrapped


def _resolve_method_callable(
    owner: type,
    method_name: str,
    raw_attr: Any,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    """Resolve a class attribute into a sync-capable callable."""
    qualname = f"{owner.__qualname__}.{method_name}"
    if isinstance(raw_attr, staticmethod) or isinstance(raw_attr, classmethod):
        bound = getattr(owner, method_name)
        if inspect.iscoroutinefunction(bound):
            return qualname, _make_sync_callable(
                bound,
                qualname=qualname,
                keep_wrapped=True,
            )
        return qualname, bound

    factory = _resolve_object_hook(owner, object_factories)
    setup = _resolve_object_hook(owner, object_setups)
    scenario = _resolve_object_hook(owner, object_scenarios)
    if inspect.isfunction(raw_attr):
        if factory is None:
            return qualname, _make_unbound_method_placeholder(owner, method_name, raw_attr)
        return (
            qualname,
            _make_bound_method_callable(
                owner,
                method_name,
                raw_attr,
                factory=factory,
                setup=setup,
                scenario=scenario,
            ),
        )

    return qualname, _make_sync_callable(getattr(owner, method_name), qualname=qualname)


def _make_bound_method_callable(
    owner: type,
    method_name: str,
    method: Any,
    *,
    factory: Any,
    setup: Any | None = None,
    scenario: Any | None = None,
) -> Any:
    """Build a sync wrapper that creates a fresh object per invocation."""
    target = _unwrap(method)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        instance = _call_sync(factory)
        instance = _apply_instance_hook(instance, setup)
        instance = _apply_instance_hook(instance, scenario)
        bound = getattr(instance, method_name)
        return _call_sync(bound, *args, **kwargs)

    try:
        wrapped.__signature__ = _signature_without_first_context(target)
    except (TypeError, ValueError):
        pass
    wrapped.__qualname__ = f"{owner.__qualname__}.{method_name}"
    wrapped.__ordeal_requires_factory__ = False
    wrapped.__ordeal_owner__ = owner
    wrapped.__ordeal_keep_wrapped__ = True
    return wrapped


def _make_unbound_method_placeholder(
    owner: type,
    method_name: str,
    method: Any,
) -> Any:
    """Build a placeholder callable for a method that still needs a factory."""
    target = _unwrap(method)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(f"{owner.__qualname__}.{method_name} needs an object factory")

    try:
        wrapped.__signature__ = _signature_without_first_context(target)
    except (TypeError, ValueError):
        pass
    wrapped.__qualname__ = f"{owner.__qualname__}.{method_name}"
    wrapped.__ordeal_requires_factory__ = True
    wrapped.__ordeal_owner__ = owner
    wrapped.__ordeal_skip_reason__ = "missing object factory"
    wrapped.__ordeal_keep_wrapped__ = True
    return wrapped


def _callable_skip_reason(func: Any) -> str | None:
    """Return a human-readable reason a generated callable is not runnable."""
    if getattr(func, "__ordeal_requires_factory__", False):
        return getattr(func, "__ordeal_skip_reason__", "missing object factory")
    return None


def _resolve_explicit_target(
    target: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    """Resolve ``module:callable`` or ``module:Class.method`` targets."""
    module_name, sep, attr_path = target.partition(":")
    if not sep or not attr_path:
        raise ValueError("Explicit targets must use 'module:callable' syntax")

    obj = _resolve_module(module_name)
    parts = [part for part in attr_path.split(".") if part]
    if not parts:
        raise ValueError("Explicit targets must name a callable")

    for part in parts[:-1]:
        obj = getattr(obj, part)

    final_name = parts[-1]
    if inspect.isclass(obj):
        static_attr = inspect.getattr_static(obj, final_name, None)
        if static_attr is None:
            raise AttributeError(f"{target} does not exist")
        return _resolve_method_callable(
            obj,
            final_name,
            static_attr,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
        )

    resolved = getattr(obj, final_name)
    if inspect.isclass(resolved):
        raise TypeError(f"{target} resolves to a class, not a callable")
    if not callable(resolved):
        raise TypeError(f"{target} does not resolve to a callable")

    qualname = (
        final_name
        if obj.__class__.__module__ == "builtins"
        else f"{getattr(obj, '__qualname__', obj.__class__.__name__)}.{final_name}"
    )
    return qualname, resolved


def _selected_public_functions(
    mod: ModuleType,
    *,
    targets: Sequence[str] | None = None,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
) -> list[tuple[str, Any]]:
    """Return discovered callables filtered to *targets* when provided."""
    discovered = _get_public_functions(
        mod,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
    )
    if not targets:
        return discovered

    discovered_map = {name: func for name, func in discovered}
    selected: list[tuple[str, Any]] = []
    seen: set[str] = set()
    module_prefix = f"{mod.__name__}."

    for raw_target in targets:
        target = str(raw_target).strip()
        if not target:
            continue
        if target in discovered_map:
            name = target
            func = discovered_map[target]
        elif target.startswith(module_prefix) and target[len(module_prefix) :] in discovered_map:
            name = target[len(module_prefix) :]
            func = discovered_map[name]
        elif ":" in target:
            base_module = target.split(":", 1)[0]
            if base_module != mod.__name__:
                raise ValueError(f"target {target!r} does not belong to module {mod.__name__!r}")
            name, func = _resolve_explicit_target(
                target,
                object_factories=object_factories,
                object_setups=object_setups,
                object_scenarios=object_scenarios,
            )
        else:
            raise ValueError(f"target {target!r} was not discovered in module {mod.__name__!r}")

        if name in seen:
            continue
        seen.add(name)
        selected.append((name, func))
    return selected


def _command_tokens(value: Any) -> list[str] | None:
    """Return command tokens for shell-like return values."""
    if isinstance(value, os.PathLike):
        return [os.fspath(value)]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, (str, os.PathLike)) for item in value
    ):
        return [os.fspath(item) for item in value]
    return None


def _tracked_string_args(
    kwargs: Mapping[str, Any],
    tracked_params: Sequence[str] | None,
) -> list[str]:
    """Return the string argument values tracked by a semantic contract."""
    names = list(
        tracked_params
        or [name for name, value in kwargs.items() if isinstance(value, (str, os.PathLike))]
    )
    tracked: list[str] = []
    for name in names:
        value = kwargs.get(name)
        if isinstance(value, str):
            tracked.append(value)
        elif isinstance(value, os.PathLike):
            tracked.append(os.fspath(value))
    return tracked


def shell_safe_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a shell-safety probe for command construction helpers."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            return False
        for raw in _tracked_string_args(kwargs, tracked_params):
            if any(ch in raw for ch in " \t;&|`$><()[]{}*?"):
                if tokens.count(raw) != 1:
                    return False
        return True

    return ContractCheck(
        name="shell_safe",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="shell-unsafe string interpolation",
    )


def quoted_paths_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a path-quoting probe for command builders."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            return False
        for raw in _tracked_string_args(kwargs, tracked_params):
            if "/" in raw or "\\" in raw or " " in raw:
                if tokens.count(raw) != 1:
                    return False
        return True

    return ContractCheck(
        name="quoted_paths",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="path quoting or escaping regression",
    )


def command_arg_stability_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a probe that ensures tracked args survive command construction."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            return False
        for raw in _tracked_string_args(kwargs, tracked_params):
            if tokens.count(raw) != 1:
                return False
        return True

    return ContractCheck(
        name="command_arg_stability",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="command construction invariant failed",
    )


def protected_env_keys_contract(
    *,
    kwargs: dict[str, Any],
    protected_keys: Sequence[str],
    env_param: str | None = None,
) -> ContractCheck:
    """Build a probe that checks protected env keys survive updates."""
    resolved_env_param = env_param or next(
        (name for name, value in kwargs.items() if isinstance(value, Mapping)),
        None,
    )

    def predicate(value: Any) -> bool:
        if resolved_env_param is None:
            return False
        original = kwargs.get(resolved_env_param)
        if not isinstance(original, Mapping) or not isinstance(value, Mapping):
            return False
        return all(value.get(key) == original.get(key) for key in protected_keys)

    return ContractCheck(
        name="protected_env_keys",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="protected env-var contract violated",
    )


def builtin_contract_check(
    name: str,
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
    protected_keys: Sequence[str] | None = None,
    env_param: str | None = None,
) -> ContractCheck:
    """Build one built-in semantic contract probe by *name*."""
    match name:
        case "shell_safe":
            return shell_safe_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "quoted_paths":
            return quoted_paths_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "command_arg_stability":
            return command_arg_stability_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "protected_env_keys":
            return protected_env_keys_contract(
                kwargs=kwargs,
                protected_keys=list(protected_keys or []),
                env_param=env_param,
            )
        case _:
            raise ValueError(f"unknown built-in contract check: {name}")


def _get_public_functions(
    mod: ModuleType,
    *,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
) -> list[tuple[str, Any]]:
    """Return (name, callable) pairs for testable callables.

    By default this includes public module functions and public class
    methods. Instance methods are wrapped only when a registered or
    explicit object factory is available; otherwise they are returned as
    placeholder callables that report a missing object factory.

    Discovery is based on the module's own ``__dict__`` and class
    ``__dict__`` entries so lazy package exports do not become accidental
    scan targets or raise ``AttributeError`` during iteration.

    Decorated functions (``@ray.remote``, ``@functools.wraps``, etc.)
    are auto-unwrapped so that ``mine()``, ``fuzz()``, and ``scan_module()``
    can inspect signatures and call the real function.
    """
    merged_factories = dict(_REGISTERED_OBJECT_FACTORIES)
    if object_factories:
        merged_factories.update(object_factories)
    merged_setups = dict(_REGISTERED_OBJECT_SETUPS)
    if object_setups:
        merged_setups.update(object_setups)
    merged_scenarios = dict(_REGISTERED_OBJECT_SCENARIOS)
    if object_scenarios:
        merged_scenarios.update(object_scenarios)

    results: list[tuple[str, Any]] = []
    for name, obj in sorted(vars(mod).items()):
        if name.startswith("__"):
            continue
        if name.startswith("_") and not include_private:
            continue
        if callable(obj) and not isinstance(obj, type):
            obj_mod = getattr(obj, "__module__", None)
            if obj_mod and obj_mod != mod.__name__:
                continue
            target = _unwrap(obj)
            if inspect.iscoroutinefunction(target):
                results.append(
                    (
                        name,
                        _make_sync_callable(target, qualname=name, keep_wrapped=True),
                    )
                )
            else:
                results.append((name, target))
            continue
        if not inspect.isclass(obj) or getattr(obj, "__module__", None) != mod.__name__:
            continue

        for meth_name, static_attr in sorted(vars(obj).items()):
            if meth_name.startswith("__"):
                continue
            if meth_name.startswith("_") and not include_private:
                continue
            if isinstance(static_attr, property):
                continue
            if not (
                isinstance(static_attr, (staticmethod, classmethod))
                or inspect.isfunction(static_attr)
            ):
                continue
            results.append(
                _resolve_method_callable(
                    obj,
                    meth_name,
                    static_attr,
                    object_factories=merged_factories,
                    object_setups=merged_setups,
                    object_scenarios=merged_scenarios,
                )
            )
    return results


_FIXTURE_REGISTRY_MODULES: set[str] = set()


def _load_fixture_registry_path(path: Path) -> str | None:
    """Import one fixture registry file and return a warning on failure."""
    resolved = path.resolve()
    key = str(resolved)
    if key in _FIXTURE_REGISTRY_MODULES:
        return None
    spec = importlib.util.spec_from_file_location(
        f"_ordeal_fixture_registry_{len(_FIXTURE_REGISTRY_MODULES)}",
        resolved,
    )
    if spec is None or spec.loader is None:
        return f"could not load fixture registry: {resolved}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _FIXTURE_REGISTRY_MODULES.add(key)
    return None


def load_fixture_registry_modules(modules: list[str]) -> list[str]:
    """Import explicit registry modules so ``register_fixture()`` takes effect."""
    warnings: list[str] = []
    for module_name in modules:
        if module_name in _FIXTURE_REGISTRY_MODULES:
            continue
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            warnings.append(f"fixture registry load failed for {module_name}: {exc}")
            continue
        _FIXTURE_REGISTRY_MODULES.add(module_name)
    return warnings


def load_project_fixture_registries(
    *,
    root: Path | None = None,
    extra_modules: list[str] | None = None,
) -> list[str]:
    """Import local registries so ``register_fixture()`` takes effect."""
    base = (root or Path.cwd()).resolve()
    warnings: list[str] = []
    candidates = [
        base / "conftest.py",
        base / "tests" / "conftest.py",
        base / "test" / "conftest.py",
        base / "src" / "tests" / "conftest.py",
    ]

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        try:
            warning = _load_fixture_registry_path(resolved)
            if warning:
                warnings.append(warning)
        except Exception as exc:
            warnings.append(f"fixture registry load failed for {resolved}: {exc}")
    if extra_modules:
        warnings.extend(load_fixture_registry_modules(list(extra_modules)))
    return warnings


def _infer_strategies(
    func: Any,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
) -> dict[str, st.SearchStrategy[Any]] | None:
    """Infer strategies from fixtures → name patterns → type hints.

    Resolution order per parameter:
    1. Explicit fixture (user-provided)
    2. Common name pattern (COMMON_NAME_STRATEGIES)
    3. Type hint (via strategy_for_type)
    4. Default value → skip
    5. None → can't infer, return None for entire function
    """
    target = _unwrap(func)
    if _callable_skip_reason(target) is not None:
        return None

    hints: dict[str, Any] = {}
    for candidate in (target, getattr(target, "__func__", None)):
        if candidate is None:
            continue
        hints = safe_get_annotations(candidate)
        if hints:
            break

    sig = inspect.signature(target)
    strategies: dict[str, st.SearchStrategy[Any]] = {}

    for name, param in sig.parameters.items():
        if name == "self" or name == "cls":
            continue
        has_default = param.default is not inspect.Parameter.empty
        # 1. Explicit fixture (always wins)
        if fixtures and name in fixtures:
            strategies[name] = fixtures[name]
        # 2. Default is None → sample both None and the typed value.
        #    Previously this skipped the param entirely, which blocked
        #    mine() on any function with Optional params.
        elif has_default and param.default is None:
            if name in hints:
                # Optional[T] → sample T | None.
                # If the type hint already includes None (e.g. Optional[str],
                # str | None), strategy_for_type handles it via the Union path.
                # Only add st.none() if the hint doesn't already include None.
                hint = hints[name]
                origin = get_origin(hint)
                args = get_args(hint)
                already_optional = (origin is Union and type(None) in args) or hint is type(None)
                strat = strategy_for_type(hint)
                if not already_optional:
                    strat = st.one_of(strat, st.none())
                strategies[name] = strat
            else:
                continue
        # 3. Common name pattern
        elif (name_strat := _strategy_for_name(name)) is not None:
            strategies[name] = name_strat
        # 4. Type hint
        elif name in hints:
            strategies[name] = strategy_for_type(hints[name])
        # 5. Has non-None default → let Python use it
        elif has_default:
            continue
        # 6. Can't infer
        else:
            return None

    return strategies if strategies else None


def _append_boundary_case(
    cases: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> None:
    """Append *candidate* when it is not already present."""
    if any(existing == candidate for existing in cases):
        return
    cases.append(candidate)


def _boundary_values_for_hint(hint: Any) -> list[Any]:
    """Return deterministic boundary values for common type hints."""
    import types as pytypes

    origin = get_origin(hint)
    if origin is Literal:
        return list(get_args(hint))

    if origin is Union or (hasattr(pytypes, "UnionType") and origin is pytypes.UnionType):
        values: list[Any] = []
        for arg in get_args(hint):
            if arg is type(None):
                values.append(None)
            else:
                values.extend(_boundary_values_for_hint(arg))
        return values

    if origin is list:
        return [[]]
    if origin is tuple:
        return [()]
    if origin is dict:
        return [{}]
    if origin is set:
        return [set()]
    if origin is frozenset:
        return [frozenset()]

    return list(_BOUNDARY_SMOKE_VALUES.get(hint, ()))


def _boundary_smoke_inputs(
    func: Any,
    *,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic boundary inputs when no explicit fixtures are set."""
    if fixtures:
        return []

    try:
        sig = inspect.signature(func)
    except Exception:
        return []
    hints = safe_get_annotations(func)

    params = [param for name, param in sig.parameters.items() if name not in {"self", "cls"}]
    if not params:
        return [{}]

    base_kwargs: dict[str, Any] = {}
    per_param_values: list[tuple[str, list[Any]]] = []
    for param in params:
        values: list[Any] = []
        if param.default is not inspect.Parameter.empty:
            values.append(param.default)
        if param.name in hints:
            values.extend(_boundary_values_for_hint(hints[param.name]))
        if not values:
            return []
        base_kwargs[param.name] = values[0]
        per_param_values.append((param.name, values))

    cases: list[dict[str, Any]] = []
    _append_boundary_case(cases, dict(base_kwargs))
    for name, values in per_param_values:
        for value in values:
            candidate = dict(base_kwargs)
            candidate[name] = value
            _append_boundary_case(cases, candidate)
    return cases


def _type_matches(value: Any, expected: type) -> bool:
    """Check if value matches expected type, handling generics and unions."""
    import types as pytypes

    if expected is type(None):
        return value is None
    origin = get_origin(expected)
    # Union[str, None] or str | None — check each member
    is_union = origin is Union or (
        hasattr(pytypes, "UnionType") and isinstance(expected, pytypes.UnionType)
    )
    if is_union:
        args = get_args(expected)
        return any(_type_matches(value, a) for a in args)
    if origin is not None:
        # list[int] → check isinstance(value, list)
        return isinstance(value, origin)
    try:
        return isinstance(value, expected)
    except TypeError:
        return True  # can't check, assume ok


_DOC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "for",
    "from",
    "have",
    "into",
    "must",
    "same",
    "that",
    "the",
    "this",
    "when",
    "with",
}


def _documented_precondition_failure(
    func: Any,
    exc: Exception,
    kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a detail dict when *exc* matches a documented precondition."""
    doc = inspect.getdoc(func) or ""
    lowered_doc = doc.lower()
    if "raise" not in lowered_doc:
        return None

    exc_name = type(exc).__name__
    exc_name_lower = exc_name.lower()
    if exc_name_lower not in lowered_doc:
        return None

    message = str(exc)
    message_tokens = {
        token
        for token in re.findall(r"[a-z_]{4,}", message.lower())
        if token not in _DOC_STOPWORDS
    }
    doc_tokens = set(re.findall(r"[a-z_]{4,}", lowered_doc))
    param_names = {name.lower() for name in kwargs}

    if not (message_tokens & doc_tokens) and not (param_names & doc_tokens):
        return None

    summary = f"expected precondition failure: {exc_name}: {message}"
    return {
        "kind": "precondition",
        "category": "expected_precondition_failure",
        "summary": summary[:300],
        "error": message[:300],
        "error_type": exc_name,
        "failing_args": dict(kwargs),
    }


def _evaluate_contract_checks(
    func: Any,
    contract_checks: list[ContractCheck] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run explicit contract probes against *func* and collect violations."""
    if not contract_checks:
        return [], []

    violations: list[str] = []
    details: list[dict[str, Any]] = []
    for check in contract_checks:
        kwargs = dict(check.kwargs)
        try:
            value = _call_sync(func, **kwargs)
        except Exception as exc:
            summary = check.summary or f"explicit contract failed: {check.name}"
            violations.append(summary)
            details.append(
                {
                    "kind": "contract",
                    "category": "semantic_contract",
                    "name": check.name,
                    "summary": summary,
                    "error": str(exc)[:300],
                    "error_type": type(exc).__name__,
                    "failing_args": kwargs,
                }
            )
            continue

        try:
            passed = bool(_call_sync(check.predicate, value))
        except Exception as exc:
            passed = False
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None

        if passed:
            continue

        summary = check.summary or f"explicit contract failed: {check.name}"
        violations.append(summary)
        detail = {
            "kind": "contract",
            "category": "semantic_contract",
            "name": check.name,
            "summary": summary,
            "failing_args": kwargs,
            "value": repr(value)[:300],
        }
        if error is not None:
            detail["error"] = error[:300]
        details.append(detail)

    return violations, details


# ============================================================================
# 1. scan_module
# ============================================================================


def scan_module(
    module: str | ModuleType,
    *,
    max_examples: int | dict[str, int] = 50,
    check_return_type: bool = True,
    targets: Sequence[str] | None = None,
    include_private: bool = False,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    expected_failures: list[str] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    contract_checks: dict[str, list[ContractCheck]] | None = None,
) -> ScanResult:
    """Smoke-test every public callable in *module*.

    For each callable with type hints, generates random inputs and checks:

    - **No crash**: calling with valid inputs doesn't raise
    - **Return type**: if annotated, the return value matches (optional)

    Simple::

        result = scan_module("myapp.scoring")
        assert result.passed

    With fixtures for params that can't be inferred::

        result = scan_module("myapp", fixtures={"model": model_strategy})

    With per-function example budgets::

        result = scan_module("myapp", max_examples={
            "compute": 200,    # fuzz this one harder
            "__default__": 50, # everything else
        })

    With expected failures for known-broken functions::

        result = scan_module("myapp", expected_failures=["broken_func"])
        assert result.passed  # broken_func failure won't count

    Args:
        module: Module path or object to scan.
        max_examples: Hypothesis examples per function. Either a single int
            (same budget for all functions) or a dict mapping function names
            to budgets, with ``"__default__"`` as fallback (default: 50).
        check_return_type: Verify return type annotations.
        targets: Optional explicit callable targets within the module. Accepts
            local names like ``"Env.build_env_vars"`` or explicit targets like
            ``"pkg.mod:Env.build_env_vars"``.
        include_private: Also include single-underscore names.
        fixtures: Strategy overrides for specific parameter names.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_scenarios: Optional per-class collaborator scenarios run after setup.
        expected_failures: Function names that are expected to fail.
            Failures from these functions are tracked separately and
            do not cause ``result.passed`` to be ``False``.
        ignore_properties: Property names to suppress in mined warnings.
        ignore_relations: Relation names to suppress in mined warnings.
        property_overrides: Per-function property suppressions.
        relation_overrides: Per-function relation suppressions.
        contract_checks: Explicit semantic contract probes keyed by
            callable name. Each probe runs with explicit ``kwargs`` and
            reports a contract violation when its predicate fails.
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__
    result = ScanResult(
        module=mod_name,
        expected_failure_names=list(expected_failures) if expected_failures else [],
    )

    # Resolve per-function example budgets
    if isinstance(max_examples, int):
        default_examples = max_examples
        examples_map: dict[str, int] = {}
    else:
        default_examples = max_examples.get("__default__", 50)
        examples_map = max_examples

    for name, func in _selected_public_functions(
        mod,
        targets=targets,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
    ):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            reason = _callable_skip_reason(func) or "missing type hints"
            result.skipped.append((name, reason))
            continue

        return_type = safe_get_annotations(func).get("return")

        func_examples = examples_map.get(name, default_examples)
        func_result = _test_one_function(
            name,
            func,
            strategies,
            return_type,
            max_examples=func_examples,
            check_return_type=check_return_type,
            fixtures=fixtures,
            ignore_properties=sorted(
                {
                    *(ignore_properties or []),
                    *(property_overrides or {}).get(name, []),
                }
            ),
            ignore_relations=sorted(
                {
                    *(ignore_relations or []),
                    *(relation_overrides or {}).get(name, []),
                }
            ),
            property_overrides=property_overrides,
            relation_overrides=relation_overrides,
            contract_checks=(contract_checks or {}).get(name),
        )
        result.functions.append(func_result)

    return result


def _test_one_function(
    name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    return_type: type | None,
    *,
    max_examples: int,
    check_return_type: bool,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    contract_checks: list[ContractCheck] | None = None,
) -> FunctionResult:
    """Run no-crash + return-type + mined-property checks on a single function."""

    def _replay_failure(exc: Exception) -> tuple[bool, int, int]:
        if not last_kwargs:
            return False, 0, 0
        attempts = 2
        matches = 0
        expected_type = type(exc)
        expected_text = str(exc)
        for _ in range(attempts):
            try:
                func(**dict(last_kwargs))
            except Exception as replay_exc:
                if type(replay_exc) is expected_type and str(replay_exc) == expected_text:
                    matches += 1
        return matches == attempts, attempts, matches

    last_kwargs: dict[str, Any] = {}
    try:
        for kwargs in _boundary_smoke_inputs(func, fixtures=fixtures):
            last_kwargs = dict(kwargs)
            result = _call_sync(func, **dict(kwargs))
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(
                        f"Expected return type {return_type}, "
                        f"got {type(result).__name__}: {result!r}"
                    )

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            nonlocal last_kwargs
            last_kwargs = dict(kwargs)
            result = _call_sync(func, **kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(
                        f"Expected return type {return_type}, "
                        f"got {type(result).__name__}: {result!r}"
                    )

        test()
    except Exception as e:
        precondition = _documented_precondition_failure(func, e, last_kwargs)
        if precondition is not None:
            return FunctionResult(
                name=name,
                passed=True,
                error_type=precondition["error_type"],
                failing_args=last_kwargs or None,
                contract_violations=[str(precondition["summary"])],
                contract_violation_details=[precondition],
            )
        replayable, replay_attempts, replay_matches = _replay_failure(e)
        crash_category = "likely_bug" if replayable else "speculative_crash"
        return FunctionResult(
            name=name,
            passed=False,
            error=str(e)[:300],
            error_type=type(e).__name__,
            failing_args=last_kwargs or None,
            crash_category=crash_category,
            replayable=replayable,
            replay_attempts=replay_attempts,
            replay_matches=replay_matches,
        )

    # Mine properties to detect semantic anomalies (not just crashes)
    violations: list[str] = []
    details: list[dict[str, Any]] = []
    try:
        from ordeal.mine import _is_suspicious_property, mine

        mine_result = mine(
            func,
            max_examples=min(max_examples, 30),
            ignore_properties=ignore_properties or [],
            ignore_relations=ignore_relations or [],
            property_overrides=property_overrides or {},
            relation_overrides=relation_overrides or {},
        )
        for prop in mine_result.properties:
            if _is_suspicious_property(prop):
                label = f"{prop.name} ({prop.confidence:.0%})"
                violations.append(label)
                details.append(
                    {
                        "name": prop.name,
                        "summary": label,
                        "confidence": round(prop.confidence, 4),
                        "holds": prop.holds,
                        "total": prop.total,
                        "counterexample": prop.counterexample,
                    }
                )
    except Exception:
        pass  # mining failed — still report crash-safety pass

    contract_violations, contract_details = _evaluate_contract_checks(func, contract_checks)
    return FunctionResult(
        name=name,
        passed=True,
        property_violations=violations,
        property_violation_details=details,
        contract_violations=contract_violations,
        contract_violation_details=contract_details,
    )


# ============================================================================
# 2. fuzz
# ============================================================================


def fuzz(
    fn: Any,
    *,
    max_examples: int = 1000,
    check_return_type: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> FuzzResult:
    """Deep-fuzz a single function with auto-inferred strategies.

    Simple::

        result = fuzz(myapp.scoring.compute)
        assert result.passed

    With fixture overrides (strategies or plain values)::

        result = fuzz(myapp.scoring.compute, model=model_strategy)
        result = fuzz(myapp.scoring.compute, max_tokens=5)  # auto-wrapped

    Args:
        fn: The function to fuzz.
        max_examples: Number of random inputs to try.
        check_return_type: Verify return type annotation.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_scenarios: Optional per-class collaborator scenarios run after setup.
        **fixtures: Strategy overrides or plain values (auto-wrapped in st.just).
    """
    fn_name = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    if isinstance(fn, str):
        fn_name, fn = _resolve_explicit_target(
            fn,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
        )

    # Auto-wrap plain values in st.just()
    normalized: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized = {}
        for k, v in fixtures.items():
            if isinstance(v, st.SearchStrategy):
                normalized[k] = v
            else:
                normalized[k] = st.just(v)
    strategies = _infer_strategies(fn, normalized)
    if strategies is None:
        reason = _callable_skip_reason(fn)
        if reason is not None:
            raise ValueError(f"Cannot fuzz {fn_name}: {reason}")
        raise ValueError(
            f"Cannot infer strategies for {fn_name}. Provide fixtures for untyped parameters."
        )

    return_type = safe_get_annotations(fn).get("return")

    failures: list[Exception] = []
    last_kwargs: dict[str, Any] = {}
    try:
        for kwargs in _boundary_smoke_inputs(fn, fixtures=normalized):
            last_kwargs = dict(kwargs)
            result = _call_sync(fn, **dict(kwargs))
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            nonlocal last_kwargs
            last_kwargs = dict(kwargs)
            result = _call_sync(fn, **kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

        test()
    except Exception as e:
        failures.append(e)

    failing_args = last_kwargs if failures and last_kwargs else None
    return FuzzResult(
        function=fn.__qualname__ or fn.__name__,
        examples=max_examples,
        failures=failures,
        failing_args=failing_args,
    )


# ============================================================================
# 3. chaos_for — auto-infer faults + invariants
# ============================================================================

# Patterns in function ASTs that map to specific fault types.
# Keys are (module_attr, func_name) pairs found in ast.Call nodes.
_FAULT_PATTERNS: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
    # pattern → [(fault_module, fault_factory, kwargs), ...]
    "subprocess.run": [
        ("io", "subprocess_timeout", {}),
        ("io", "subprocess_delay", {}),
        ("io", "corrupt_stdout", {}),
    ],
    "subprocess.check_output": [
        ("io", "subprocess_timeout", {}),
    ],
    "subprocess.Popen": [
        ("io", "subprocess_timeout", {}),
    ],
    "open": [
        ("io", "disk_full", {}),
        ("io", "permission_denied", {}),
    ],
}


def _infer_faults(
    mod: ModuleType,
    mod_name: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
) -> list[Fault]:
    """Auto-discover faults by scanning function ASTs for risky calls.

    Detects subprocess, file I/O, and cross-function calls, then
    generates appropriate fault instances.
    """
    import ast
    import textwrap

    faults: list[Fault] = []
    seen: set[str] = set()

    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
    ):
        try:
            source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Extract call target as string (e.g. "subprocess.run", "open")
            call_str = _call_to_string(node)
            if not call_str:
                continue

            # Check against known patterns
            for pattern, fault_specs in _FAULT_PATTERNS.items():
                if pattern not in call_str:
                    continue
                for fault_mod, fault_fn, kwargs in fault_specs:
                    key = f"{fault_mod}.{fault_fn}"
                    if key in seen:
                        continue
                    seen.add(key)
                    fault_module = importlib.import_module(f"ordeal.faults.{fault_mod}")
                    factory = getattr(fault_module, fault_fn)
                    # Faults that need a target get the module name
                    params = inspect.signature(factory).parameters
                    if "target" in params:
                        faults.append(factory(f"{mod_name}.{name}", **kwargs))
                    else:
                        faults.append(factory(**kwargs))

            # Cross-function calls → error_on_call
            if call_str.startswith(mod_name + ".") and call_str not in seen:
                seen.add(call_str)
                from ordeal.faults.io import error_on_call

                faults.append(error_on_call(call_str))

    return faults


def _call_to_string(node: Any) -> str | None:
    """Extract a dotted string from an ast.Call node's func attribute."""
    import ast

    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        current = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None


def _infer_invariants(
    mod: ModuleType,
    fixtures: dict[str, Any] | None,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
) -> tuple[dict[str, list[Invariant]], list[Invariant]]:
    """Auto-discover invariants by mining function properties.

    Runs mine() on each function with a small example count,
    then maps universal properties to invariant objects.
    """
    from ordeal.invariants import bounded, finite, no_nan, non_empty
    from ordeal.mine import mine

    # Map mined property names to invariant constructors
    _PROPERTY_TO_INVARIANT: dict[str, Invariant | None] = {
        "no NaN": no_nan,
        "output >= 0": bounded(0, float("inf")),
        "output in [0, 1]": bounded(0, 1),
        "never empty": non_empty(),
    }

    invariant_map: dict[str, list[Invariant]] = {}
    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
    ):
        strats = _infer_strategies(func, fixtures)
        if strats is None:
            continue
        try:
            result = mine(func, max_examples=30)
        except Exception:
            continue

        func_invs: list[Invariant] = []
        has_numeric = False
        for prop in result.universal:
            inv = _PROPERTY_TO_INVARIANT.get(prop.name)
            if inv is not None:
                func_invs.append(inv)
            if "output >= 0" in prop.name or "output in [" in prop.name:
                has_numeric = True

        # If function returns numeric values and no specific bound was found,
        # at least check for finite
        if has_numeric and not any(isinstance(i, type(finite)) for i in func_invs):
            func_invs.append(finite)

        if func_invs:
            invariant_map[name] = func_invs

    return invariant_map, []


def chaos_for(
    module: str | ModuleType,
    *,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    invariants: list[Invariant] | dict[str, Invariant] | None = None,
    faults: list[Fault] | None = None,
    max_examples: int = 50,
    stateful_step_count: int = 30,
    rule_timeout: float = 30.0,
) -> type:
    """Auto-generate a ChaosTest from a module's public API.

    Each public function becomes a ``@rule``.  The nemesis toggles
    *faults*.  After each step, *invariants* are checked on every
    return value.

    Zero-config — discovers everything automatically::

        TestScoring = chaos_for("myapp.scoring")
        # Scans code for subprocess/file/network calls → generates faults
        # Mines each function with random inputs → generates invariants

    With explicit overrides::

        TestScoring = chaos_for(
            "myapp.scoring",
            faults=[timing.timeout("myapp.db.query")],
            invariants={"compute": bounded(0, 1)},
        )

    Pass ``faults=[]`` or ``invariants=[]`` to disable auto-discovery.

    Returns a pytest-discoverable ``TestCase`` class.

    Args:
        module: Module path or object.
        fixtures: Strategy overrides for parameter names.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        invariants: ``None`` = auto-mine, list = global, dict = per-function.
        faults: ``None`` = auto-infer from code, list = explicit.
        max_examples: Hypothesis examples.
        stateful_step_count: Max rules per test case.
        rule_timeout: Per-rule timeout in seconds (default 30, 0 to disable).
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__

    # Auto-discover faults from code analysis when not provided
    if faults is None:
        fault_list = _infer_faults(
            mod,
            mod_name,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
        )
    else:
        fault_list = list(faults)

    # Auto-discover invariants from mine() when not provided
    if invariants is None:
        invariant_map, global_invs = _infer_invariants(
            mod,
            fixtures,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
        )
    elif isinstance(invariants, dict):
        invariant_map: dict[str, list[Invariant]] = {
            k: [v] if isinstance(v, Invariant) else list(v) for k, v in invariants.items()
        }
        global_invs: list[Invariant] = []
    else:
        invariant_map = {}
        global_invs = list(invariants)

    # Collect rule methods
    rules_dict: dict[str, Any] = {}
    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
    ):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            continue
        # Per-function invariants override global; if neither, empty list
        func_invs = invariant_map.get(name, global_invs)
        method = _make_rule_method(name, func, strategies, func_invs)
        rules_dict[method.__name__] = method

    if not rules_dict:
        raise ValueError(
            f"No testable functions found in {mod_name}. "
            f"Ensure functions have type hints or provide fixtures."
        )

    # Build class
    namespace: dict[str, Any] = {
        "faults": fault_list,
        "rule_timeout": rule_timeout,
        **rules_dict,
    }

    class_name = f"AutoChaos_{mod_name.replace('.', '_')}"
    AutoChaos = type(class_name, (ChaosTest,), namespace)

    TestCase = AutoChaos.TestCase
    TestCase.settings = settings(
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )
    return TestCase


def _make_rule_method(
    func_name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    invariants: list[Invariant],
) -> Any:
    """Create a @rule method that calls func and checks invariants on the result."""
    safe_name = func_name.replace(".", "_")

    @rule(**strategies)
    def method(self: Any, **kwargs: Any) -> None:
        result = _call_sync(func, **kwargs)
        if result is not None:
            for inv in invariants:
                try:
                    inv(result)
                except TypeError:
                    pass  # invariant doesn't apply to this return type

    method.__name__ = f"call_{safe_name}"
    method.__qualname__ = f"AutoChaos.call_{safe_name}"
    return method
