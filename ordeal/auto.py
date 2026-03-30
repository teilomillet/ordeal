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

import importlib
import inspect
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, get_args, get_origin, get_type_hints

import hypothesis.strategies as st
from hypothesis import given, settings
from hypothesis.stateful import rule

from ordeal.chaos import ChaosTest
from ordeal.faults import Fault
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
    failing_args: dict[str, Any] | None = None

    def __str__(self) -> str:
        if self.passed:
            return f"  PASS  {self.name}"
        return f"  FAIL  {self.name}: {self.error}"


@dataclass
class ScanResult:
    """Result of scanning a module."""

    module: str
    functions: list[FunctionResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if every tested function passed."""
        return all(f.passed for f in self.functions)

    @property
    def total(self) -> int:
        return len(self.functions)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.functions if not f.passed)

    def summary(self) -> str:
        lines = [f"scan_module({self.module!r}): {self.total} functions, {self.failed} failed"]
        for f in self.functions:
            lines.append(str(f))
        if self.skipped:
            lines.append(f"  ({len(self.skipped)} skipped: no type hints)")
        return "\n".join(lines)


@dataclass
class FuzzResult:
    """Result of fuzzing a single function."""

    function: str
    examples: int
    failures: list[Exception] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    def summary(self) -> str:
        if self.passed:
            return f"fuzz({self.function}): {self.examples} examples, passed"
        return (
            f"fuzz({self.function}): {self.examples} examples, "
            f"{len(self.failures)} failure(s): {self.failures[0]}"
        )


# ============================================================================
# Helpers
# ============================================================================

def _resolve_module(module: str | ModuleType) -> ModuleType:
    if isinstance(module, str):
        return importlib.import_module(module)
    return module


def _get_public_functions(mod: ModuleType) -> list[tuple[str, Any]]:
    """Return (name, callable) pairs for public, non-class callables."""
    results = []
    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if callable(obj) and not isinstance(obj, type):
            results.append((name, obj))
    return results


def _infer_strategies(
    func: Any,
    fixtures: dict[str, st.SearchStrategy] | None = None,
) -> dict[str, st.SearchStrategy] | None:
    """Infer strategies from type hints + fixtures. Returns None if incomplete."""
    try:
        hints = get_type_hints(func)
    except Exception:
        return None

    sig = inspect.signature(func)
    strategies: dict[str, st.SearchStrategy] = {}

    for name, param in sig.parameters.items():
        if name == "self" or name == "cls":
            continue
        if fixtures and name in fixtures:
            strategies[name] = fixtures[name]
        elif name in hints:
            strategies[name] = strategy_for_type(hints[name])
        elif param.default is not inspect.Parameter.empty:
            continue  # has default, can skip
        else:
            return None  # required param with no type hint and no fixture

    return strategies if strategies else None


def _type_matches(value: Any, expected: type) -> bool:
    """Check if value matches expected type, handling generics."""
    if expected is type(None):
        return value is None
    origin = get_origin(expected)
    if origin is not None:
        return isinstance(value, origin)
    try:
        return isinstance(value, expected)
    except TypeError:
        return True  # can't check (e.g. Union), assume ok


# ============================================================================
# 1. scan_module
# ============================================================================

def scan_module(
    module: str | ModuleType,
    *,
    max_examples: int = 50,
    check_return_type: bool = True,
    fixtures: dict[str, st.SearchStrategy] | None = None,
) -> ScanResult:
    """Smoke-test every public function in *module*.

    For each function with type hints, generates random inputs and checks:

    - **No crash**: calling with valid inputs doesn't raise
    - **Return type**: if annotated, the return value matches (optional)

    Simple::

        result = scan_module("myapp.scoring")
        assert result.passed

    With fixtures for params that can't be inferred::

        result = scan_module("myapp", fixtures={"model": model_strategy})

    Args:
        module: Module path or object to scan.
        max_examples: Hypothesis examples per function.
        check_return_type: Verify return type annotations.
        fixtures: Strategy overrides for specific parameter names.
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__
    result = ScanResult(module=mod_name)

    for name, func in _get_public_functions(mod):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            result.skipped.append((name, "missing type hints"))
            continue

        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}
        return_type = hints.get("return")

        func_result = _test_one_function(
            name, func, strategies, return_type,
            max_examples=max_examples,
            check_return_type=check_return_type,
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
) -> FunctionResult:
    """Run no-crash + return-type checks on a single function."""
    try:
        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            result = func(**kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(
                        f"Expected return type {return_type}, "
                        f"got {type(result).__name__}: {result!r}"
                    )
        test()
        return FunctionResult(name=name, passed=True)
    except Exception as e:
        return FunctionResult(name=name, passed=False, error=str(e)[:300])


# ============================================================================
# 2. fuzz
# ============================================================================

def fuzz(
    fn: Any,
    *,
    max_examples: int = 1000,
    check_return_type: bool = False,
    **fixtures: st.SearchStrategy,
) -> FuzzResult:
    """Deep-fuzz a single function with auto-inferred strategies.

    Simple::

        result = fuzz(myapp.scoring.compute)
        assert result.passed

    With fixture overrides::

        result = fuzz(myapp.scoring.compute, model=model_strategy)

    Args:
        fn: The function to fuzz.
        max_examples: Number of random inputs to try.
        check_return_type: Verify return type annotation.
        **fixtures: Strategy overrides for specific parameters.
    """
    strategies = _infer_strategies(fn, fixtures or None)
    if strategies is None:
        raise ValueError(
            f"Cannot infer strategies for {fn.__name__}. "
            f"Provide fixtures for untyped parameters."
        )

    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    return_type = hints.get("return")

    failures: list[Exception] = []
    try:
        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            result = fn(**kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(
                        f"Expected {return_type}, got {type(result).__name__}"
                    )
        test()
    except Exception as e:
        failures.append(e)

    return FuzzResult(
        function=fn.__qualname__ or fn.__name__,
        examples=max_examples,
        failures=failures,
    )


# ============================================================================
# 3. chaos_for
# ============================================================================

def chaos_for(
    module: str | ModuleType,
    *,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    invariants: list[Invariant] | None = None,
    faults: list[Fault] | None = None,
    max_examples: int = 50,
    stateful_step_count: int = 30,
) -> type:
    """Auto-generate a ChaosTest from a module's public API.

    Each public function becomes a ``@rule``.  The nemesis toggles
    *faults*.  After each step, *invariants* are checked on every
    return value.

    Simple::

        TestScoring = chaos_for("myapp.scoring")

    With fixtures and invariants::

        TestScoring = chaos_for(
            "myapp.scoring",
            fixtures={"model": model_strategy},
            invariants=[finite, bounded(0, 1)],
            faults=[timing.timeout("myapp.scoring.predict")],
        )

    Returns a pytest-discoverable ``TestCase`` class.

    Args:
        module: Module path or object.
        fixtures: Strategy overrides for parameter names.
        invariants: Checked on every return value after each rule call.
        faults: Fault instances for the nemesis to toggle.
        max_examples: Hypothesis examples.
        stateful_step_count: Max rules per test case.
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__
    invs = invariants or []
    fault_list = faults or []

    # Collect rule methods
    rules_dict: dict[str, Any] = {}
    for name, func in _get_public_functions(mod):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            continue
        method = _make_rule_method(name, func, strategies, invs)
        rules_dict[method.__name__] = method

    if not rules_dict:
        raise ValueError(
            f"No testable functions found in {mod_name}. "
            f"Ensure functions have type hints or provide fixtures."
        )

    # Build class
    namespace: dict[str, Any] = {
        "faults": fault_list,
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

    @rule(**strategies)
    def method(self: Any, **kwargs: Any) -> None:
        result = func(**kwargs)
        if result is not None:
            for inv in invariants:
                try:
                    inv(result)
                except TypeError:
                    pass  # invariant doesn't apply to this return type

    method.__name__ = f"call_{func_name}"
    method.__qualname__ = f"AutoChaos.call_{func_name}"
    return method
