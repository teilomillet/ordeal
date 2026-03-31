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
from typing import Any, Union, get_args, get_origin, get_type_hints

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
    try:
        func = inspect.unwrap(func)
    except (ValueError, TypeError):
        pass
    return func


def _get_public_functions(mod: ModuleType) -> list[tuple[str, Any]]:
    """Return (name, callable) pairs for public, non-class callables.

    Decorated functions (``@ray.remote``, ``@functools.wraps``, etc.)
    are auto-unwrapped so that ``mine()``, ``fuzz()``, and ``scan_module()``
    can inspect signatures and call the real function.
    """
    results = []
    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if callable(obj) and not isinstance(obj, type):
            results.append((name, _unwrap(obj)))
    return results


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
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    sig = inspect.signature(func)
    strategies: dict[str, st.SearchStrategy[Any]] = {}

    for name, param in sig.parameters.items():
        if name == "self" or name == "cls":
            continue
        has_default = param.default is not inspect.Parameter.empty
        # 1. Explicit fixture (always wins)
        if fixtures and name in fixtures:
            strategies[name] = fixtures[name]
        # 2. Default is None → keep it None (don't generate random junk
        #    for optional params — the function chose None as default
        #    for a reason)
        elif has_default and param.default is None:
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


# ============================================================================
# 1. scan_module
# ============================================================================


def scan_module(
    module: str | ModuleType,
    *,
    max_examples: int = 50,
    check_return_type: bool = True,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    expected_failures: list[str] | None = None,
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

    With expected failures for known-broken functions::

        result = scan_module("myapp", expected_failures=["broken_func"])
        assert result.passed  # broken_func failure won't count

    Args:
        module: Module path or object to scan.
        max_examples: Hypothesis examples per function.
        check_return_type: Verify return type annotations.
        fixtures: Strategy overrides for specific parameter names.
        expected_failures: Function names that are expected to fail.
            Failures from these functions are tracked separately and
            do not cause ``result.passed`` to be ``False``.
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__
    result = ScanResult(
        module=mod_name,
        expected_failure_names=list(expected_failures) if expected_failures else [],
    )

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
            name,
            func,
            strategies,
            return_type,
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
        **fixtures: Strategy overrides or plain values (auto-wrapped in st.just).
    """
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
        raise ValueError(
            f"Cannot infer strategies for {fn.__name__}. Provide fixtures for untyped parameters."
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
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

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
