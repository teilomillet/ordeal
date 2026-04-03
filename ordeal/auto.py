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
    property_violations: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if self.passed and not self.property_violations:
            return f"  PASS  {self.name}"
        if not self.passed:
            return f"  FAIL  {self.name}: {self.error}"
        viols = "; ".join(self.property_violations)
        return f"  WARN  {self.name}: {viols}"


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


def _get_public_functions(
    mod: ModuleType, *, include_private: bool = False
) -> list[tuple[str, Any]]:
    """Return (name, callable) pairs for testable callables.

    By default only public functions (no ``_`` prefix).  Set
    *include_private* to also include ``_single_underscore``
    functions — many real codebases have most logic there.
    ``__dunder__`` methods are always excluded.

    Decorated functions (``@ray.remote``, ``@functools.wraps``, etc.)
    are auto-unwrapped so that ``mine()``, ``fuzz()``, and ``scan_module()``
    can inspect signatures and call the real function.
    """
    results = []
    for name in sorted(dir(mod)):
        if name.startswith("__"):
            continue
        if name.startswith("_") and not include_private:
            continue
        obj = getattr(mod, name)
        if callable(obj) and not isinstance(obj, type):
            # Skip re-imports from other modules
            obj_mod = getattr(obj, "__module__", None)
            if include_private and obj_mod and obj_mod != mod.__name__:
                continue
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
    max_examples: int | dict[str, int] = 50,
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

    # Resolve per-function example budgets
    if isinstance(max_examples, int):
        default_examples = max_examples
        examples_map: dict[str, int] = {}
    else:
        default_examples = max_examples.get("__default__", 50)
        examples_map = max_examples

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

        func_examples = examples_map.get(name, default_examples)
        func_result = _test_one_function(
            name,
            func,
            strategies,
            return_type,
            max_examples=func_examples,
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
    """Run no-crash + return-type + mined-property checks on a single function."""
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
    except Exception as e:
        return FunctionResult(name=name, passed=False, error=str(e)[:300])

    # Mine properties to detect semantic anomalies (not just crashes)
    violations: list[str] = []
    try:
        from ordeal.mine import _is_suspicious_property, mine

        mine_result = mine(func, max_examples=min(max_examples, 30))
        for prop in mine_result.properties:
            if _is_suspicious_property(prop):
                violations.append(f"{prop.name} ({prop.confidence:.0%})")
    except Exception:
        pass  # mining failed — still report crash-safety pass

    return FunctionResult(name=name, passed=True, property_violations=violations)


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
    fn = _unwrap(fn)

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
    last_kwargs: dict[str, Any] = {}
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            nonlocal last_kwargs
            last_kwargs = dict(kwargs)
            result = fn(**kwargs)
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


def _infer_faults(mod: ModuleType, mod_name: str) -> list[Fault]:
    """Auto-discover faults by scanning function ASTs for risky calls.

    Detects subprocess, file I/O, and cross-function calls, then
    generates appropriate fault instances.
    """
    import ast
    import textwrap

    faults: list[Fault] = []
    seen: set[str] = set()

    for name, func in _get_public_functions(mod):
        try:
            source = textwrap.dedent(inspect.getsource(func))
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
    for name, func in _get_public_functions(mod):
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
        fault_list = _infer_faults(mod, mod_name)
    else:
        fault_list = list(faults)

    # Auto-discover invariants from mine() when not provided
    if invariants is None:
        invariant_map, global_invs = _infer_invariants(mod, fixtures)
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
    for name, func in _get_public_functions(mod):
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
