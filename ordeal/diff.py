"""Differential testing — compare two implementations.

Run two functions on the same random inputs and assert their outputs
match.  Catches regressions, validates refactors, and verifies
backend ports::

    from ordeal.diff import diff

    result = diff(score_v1, score_v2, max_examples=100)
    assert result.equivalent

    # Floating-point tolerance:
    result = diff(compute_old, compute_new, rtol=1e-6)

    # Custom comparator:
    result = diff(fn_a, fn_b, compare=lambda a, b: a.keys() == b.keys())
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
class Mismatch:
    """A single input where the two functions disagree."""

    args: dict[str, Any]
    output_a: Any
    output_b: Any

    def __str__(self) -> str:
        return (
            f"  args:     {_truncate(self.args)}\n"
            f"  output_a: {_truncate(self.output_a)}\n"
            f"  output_b: {_truncate(self.output_b)}"
        )


@dataclass
class DiffResult:
    """Result of comparing two functions."""

    function_a: str
    function_b: str
    total: int
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        """True if no mismatches were found."""
        return len(self.mismatches) == 0

    def summary(self) -> str:
        """Human-readable report."""
        status = "EQUIVALENT" if self.equivalent else "DIVERGENT"
        lines = [
            f"diff({self.function_a}, {self.function_b}): "
            f"{self.total} examples, {status}"
        ]
        if self.mismatches:
            lines.append(f"  {len(self.mismatches)} mismatch(es):")
            for m in self.mismatches[:3]:
                lines.append(str(m))
            if len(self.mismatches) > 3:
                lines.append(f"  ... and {len(self.mismatches) - 3} more")
        return "\n".join(lines)


def _truncate(obj: Any, limit: int = 120) -> str:
    s = repr(obj)
    return s[:limit] + "..." if len(s) > limit else s


def _default_compare(
    a: Any,
    b: Any,
    rtol: float | None,
    atol: float | None,
) -> bool:
    """Compare two values with optional numeric tolerance."""
    if rtol is not None or atol is not None:
        return _approx_equal(a, b, rtol or 1e-9, atol or 0.0)
    return a == b


def _approx_equal(a: Any, b: Any, rtol: float, atol: float) -> bool:
    """Recursive approximate equality for numbers, sequences, dicts."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        if math.isinf(a) or math.isinf(b):
            return a == b  # inf == inf, -inf == -inf
        return abs(a - b) <= atol + rtol * abs(b)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_approx_equal(x, y, rtol, atol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(_approx_equal(a[k], b[k], rtol, atol) for k in a)
    # numpy/array support
    if hasattr(a, "shape") and hasattr(b, "shape"):
        try:
            import numpy as np

            return bool(np.allclose(a, b, rtol=rtol, atol=atol))
        except (ImportError, TypeError):
            pass
    return a == b


def diff(
    fn_a: Callable[..., Any],
    fn_b: Callable[..., Any],
    *,
    max_examples: int = 100,
    rtol: float | None = None,
    atol: float | None = None,
    compare: Callable[[Any, Any], bool] | None = None,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> DiffResult:
    """Compare two functions for equivalence on random inputs.

    Infers strategies from *fn_a*'s type hints.  Both functions must
    accept the same parameters.

    Simple::

        result = diff(score_v1, score_v2)
        assert result.equivalent

    With tolerance::

        result = diff(old, new, rtol=1e-6)

    With custom comparator::

        result = diff(old, new, compare=lambda a, b: a.status == b.status)

    Args:
        fn_a: Reference function.
        fn_b: Function to compare against.
        max_examples: Number of random inputs.
        rtol: Relative tolerance for numeric comparison.
        atol: Absolute tolerance for numeric comparison.
        compare: Custom comparator ``(output_a, output_b) -> bool``.
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

    strategies = _infer_strategies(fn_a, normalized)
    if strategies is None:
        raise ValueError(
            f"Cannot infer strategies for {fn_a.__name__}. "
            f"Provide fixtures for untyped parameters."
        )

    mismatches: list[Mismatch] = []
    example_count = [0]

    def check_equal(a: Any, b: Any) -> bool:
        if compare is not None:
            return compare(a, b)
        return _default_compare(a, b, rtol, atol)

    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            example_count[0] += 1
            out_a = fn_a(**kwargs)
            out_b = fn_b(**kwargs)
            if not check_equal(out_a, out_b):
                mismatches.append(Mismatch(
                    args=kwargs,
                    output_a=out_a,
                    output_b=out_b,
                ))
                raise AssertionError("outputs differ")

        test()
    except AssertionError:
        pass  # mismatch already recorded

    name_a = getattr(fn_a, "__name__", str(fn_a))
    name_b = getattr(fn_b, "__name__", str(fn_b))
    return DiffResult(
        function_a=name_a,
        function_b=name_b,
        total=example_count[0],
        mismatches=mismatches,
    )
