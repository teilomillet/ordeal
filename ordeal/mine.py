"""Property mining — discover invariants from execution traces.

Run a function many times with random inputs, observe patterns in
outputs, and report likely properties.  The user confirms which are
real — turning observed regularities into tested invariants::

    from ordeal.mine import mine

    properties = mine(my_function, max_examples=500)
    for p in properties:
        print(p)
    # output >= 0: 100% (500/500)
    # output is float: 100% (500/500)
    # deterministic: 100% (500/500)
    # output in [0, 1]: 98% (490/500)
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import hypothesis.strategies as st
from hypothesis import given, settings

from ordeal.auto import _infer_strategies


@dataclass
class MinedProperty:
    """A likely property observed during mining."""

    name: str
    holds: int
    total: int
    counterexample: dict[str, Any] | None = None

    @property
    def confidence(self) -> float:
        """Fraction of examples where the property held."""
        return self.holds / self.total if self.total > 0 else 0.0

    @property
    def universal(self) -> bool:
        """True if the property held for every example."""
        return self.holds == self.total

    def __str__(self) -> str:
        pct = f"{self.confidence:.0%}"
        status = "ALWAYS" if self.universal else pct
        return f"  {status:>6}  {self.name} ({self.holds}/{self.total})"


@dataclass
class MineResult:
    """All properties discovered for a function."""

    function: str
    examples: int
    properties: list[MinedProperty] = field(default_factory=list)

    @property
    def universal(self) -> list[MinedProperty]:
        """Properties that held on every single example."""
        return [p for p in self.properties if p.universal]

    @property
    def likely(self) -> list[MinedProperty]:
        """Properties with >= 95% confidence but not universal."""
        return [p for p in self.properties if 0.95 <= p.confidence < 1.0]

    def summary(self) -> str:
        """Human-readable report."""
        lines = [f"mine({self.function}): {self.examples} examples"]
        for p in sorted(self.properties, key=lambda p: -p.confidence):
            if p.confidence >= 0.5:
                lines.append(str(p))
        return "\n".join(lines)


# ============================================================================
# Property checkers — each returns (name, holds: bool)
# ============================================================================


def _check_type_consistent(
    outputs: list[Any],
) -> MinedProperty:
    """All outputs have the same type."""
    if not outputs:
        return MinedProperty("output type consistent", 0, 0)
    first_type = type(outputs[0])
    holds = sum(1 for o in outputs if type(o) is first_type)
    ce = None
    if holds < len(outputs):
        for i, o in enumerate(outputs):
            if type(o) is not first_type:
                ce = {"index": i, "expected": first_type.__name__, "got": type(o).__name__}
                break
    return MinedProperty(f"output type is {first_type.__name__}", holds, len(outputs), ce)


def _check_never_none(outputs: list[Any]) -> MinedProperty:
    """Output is never None."""
    holds = sum(1 for o in outputs if o is not None)
    return MinedProperty("never None", holds, len(outputs))


def _check_no_nan(outputs: list[Any]) -> MinedProperty:
    """Output never contains NaN (for floats)."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, float):
            total += 1
            if not math.isnan(o):
                holds += 1
        elif hasattr(o, "shape"):
            total += 1
            try:
                import numpy as np

                if not np.any(np.isnan(o)):
                    holds += 1
            except (ImportError, TypeError):
                holds += 1
    if total == 0:
        return MinedProperty("no NaN", len(outputs), len(outputs))
    return MinedProperty("no NaN", holds, total)


def _check_non_negative(outputs: list[Any]) -> MinedProperty:
    """Output is always >= 0 (for numeric outputs)."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            total += 1
            if o >= 0:
                holds += 1
    if total == 0:
        return MinedProperty("output >= 0", 0, 0)
    return MinedProperty("output >= 0", holds, total)


def _check_bounded_01(outputs: list[Any]) -> MinedProperty:
    """Output is always in [0, 1]."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            total += 1
            if 0.0 <= o <= 1.0:
                holds += 1
    if total == 0:
        return MinedProperty("output in [0, 1]", 0, 0)
    return MinedProperty("output in [0, 1]", holds, total)


def _check_never_empty(outputs: list[Any]) -> MinedProperty:
    """Output is never empty (for sequences/strings)."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, (list, tuple, str, dict)):
            total += 1
            if len(o) > 0:
                holds += 1
    if total == 0:
        return MinedProperty("never empty", 0, 0)
    return MinedProperty("never empty", holds, total)


def _check_deterministic(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """Same input always gives the same output."""
    total = 0
    holds = 0
    for kwargs in inputs[:50]:  # cap at 50 to keep runtime sane
        try:
            out1 = fn(**kwargs)
            out2 = fn(**kwargs)
            total += 1
            if out1 == out2:
                holds += 1
        except Exception:
            pass
    if total == 0:
        return MinedProperty("deterministic", 0, 0)
    return MinedProperty("deterministic", holds, total)


def _check_idempotent(
    fn: Callable[..., Any],
    outputs: list[Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """f(f(x)) == f(x) — only when input and output types align."""
    if not outputs or not inputs:
        return MinedProperty("idempotent", 0, 0)

    # Get first parameter name to feed output back as input
    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if not params:
        return MinedProperty("idempotent", 0, 0)

    first_param = params[0]
    total = 0
    holds = 0
    for i, (output, kwargs) in enumerate(zip(outputs[:30], inputs[:30])):
        if output is None:
            continue
        try:
            kwargs2 = dict(kwargs)
            kwargs2[first_param] = output
            out2 = fn(**kwargs2)
            total += 1
            if out2 == output:
                holds += 1
        except (TypeError, ValueError, AttributeError):
            pass  # output type doesn't fit as input — skip
    if total == 0:
        return MinedProperty("idempotent", 0, 0)
    return MinedProperty("idempotent", holds, total)


# ============================================================================
# Main entry point
# ============================================================================


def mine(
    fn: Callable[..., Any],
    *,
    max_examples: int = 500,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> MineResult:
    """Discover likely properties of a function by running it many times.

    Simple::

        result = mine(my_function)
        for p in result.universal:
            print(p)  # properties that always held

    With fixtures::

        result = mine(my_function, model=mock_model)

    Args:
        fn: The function to mine properties from.
        max_examples: Number of random inputs to try.
        **fixtures: Strategy overrides or plain values.
    """
    # Normalize fixtures
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

    # Collect outputs and inputs
    outputs: list[Any] = []
    inputs: list[dict[str, Any]] = []

    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            result = fn(**kwargs)
            outputs.append(result)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass  # some inputs may crash — that's fine, we analyze what we got

    # Run all property checks
    props: list[MinedProperty] = []
    props.append(_check_type_consistent(outputs))
    props.append(_check_never_none(outputs))
    props.append(_check_no_nan(outputs))
    props.append(_check_non_negative(outputs))
    props.append(_check_bounded_01(outputs))
    props.append(_check_never_empty(outputs))
    props.append(_check_deterministic(fn, inputs))
    props.append(_check_idempotent(fn, outputs, inputs))

    # Filter out properties with 0 observations (not applicable)
    props = [p for p in props if p.total > 0]

    name = getattr(fn, "__name__", str(fn))
    return MineResult(function=name, examples=len(outputs), properties=props)
