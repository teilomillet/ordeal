"""QuickCheck-style property testing with boundary-biased generation.

Inspired by Jane Street's QuickCheck for Core.  Three ideas:

1. **@quickcheck** — infer strategies from type hints, bias toward boundaries::

       @quickcheck
       def test_sort_idempotent(xs: list[int]):
           assert sorted(sorted(xs)) == sorted(xs)

2. **Boundary-biased strategies** — stress edges, not uniform random::

       from ordeal.quickcheck import biased
       biased.integers(0, 100)   # more values near 0, 1, 99, 100
       biased.floats(0.0, 1.0)   # more values near 0.0, 0.5, 1.0

3. **Type-driven generation** — strategies from type hints + dataclasses::

       from ordeal.quickcheck import strategy_for_type
       gen = strategy_for_type(MyDataclass)

The difference from raw Hypothesis: ordeal biases toward boundary values
by default.  Integers cluster near 0 and range endpoints.  Lists are more
often empty or singleton.  Strings hit unicode edge cases.  This catches
more bugs per test run because implementation boundaries (off-by-one, empty
input, overflow) are explored with higher probability.
"""

from __future__ import annotations

import functools
import inspect
import math
import types as pytypes
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

import hypothesis.strategies as st
from hypothesis import given, settings

# ============================================================================
# Boundary-biased strategies
# ============================================================================


class biased:
    """Namespace for boundary-biased strategies."""

    @staticmethod
    def integers(
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> st.SearchStrategy[int]:
        """Integers biased toward 0, +/-1, range endpoints, powers of 2."""
        base = st.integers(min_value=min_value, max_value=max_value)
        boundaries = [
            0,
            1,
            -1,
            2,
            -2,
            10,
            -10,
            100,
            -100,
            255,
            256,
            -256,
            2**15 - 1,
            2**15,
            -(2**15),
            2**31 - 1,
            2**31,
            -(2**31),
            2**63 - 1,
            -(2**63),
        ]
        if min_value is not None:
            boundaries.extend([min_value, min_value + 1])
        if max_value is not None:
            boundaries.extend([max_value, max_value - 1])

        valid = sorted(
            set(
                b
                for b in boundaries
                if (min_value is None or b >= min_value) and (max_value is None or b <= max_value)
            )
        )
        if valid:
            return st.one_of(st.sampled_from(valid), base)
        return base

    @staticmethod
    def floats(
        min_value: float | None = None,
        max_value: float | None = None,
        *,
        allow_nan: bool = False,
        allow_infinity: bool = False,
    ) -> st.SearchStrategy[float]:
        """Floats biased toward 0, +/-1, epsilon, range endpoints."""
        base = st.floats(
            min_value=min_value,
            max_value=max_value,
            allow_nan=allow_nan,
            allow_infinity=allow_infinity,
        )
        boundaries = [0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 1e-10, -1e-10, 1e10, -1e10]
        if min_value is not None:
            boundaries.append(min_value)
        if max_value is not None:
            boundaries.append(max_value)

        valid = sorted(
            set(
                b
                for b in boundaries
                if (min_value is None or b >= min_value)
                and (max_value is None or b <= max_value)
                and not (not allow_nan and math.isnan(b))
                and not (not allow_infinity and math.isinf(b))
            )
        )
        if valid:
            return st.one_of(st.sampled_from(valid), base)
        return base

    @staticmethod
    def strings(
        min_size: int = 0,
        max_size: int = 100,
    ) -> st.SearchStrategy[str]:
        """Strings biased toward empty, single-char, max-length, unicode edges."""
        parts = [st.text(min_size=min_size, max_size=max_size)]
        if min_size == 0:
            parts.append(st.just(""))
        parts.append(st.text(min_size=1, max_size=1))  # single char
        if max_size > 2:
            parts.append(st.text(min_size=max_size - 1, max_size=max_size))
        return st.one_of(*parts)

    @staticmethod
    def bytes_(
        min_size: int = 0,
        max_size: int = 100,
    ) -> st.SearchStrategy[bytes]:
        """Bytes biased toward empty, all-zero, all-0xFF, boundary lengths."""
        parts = [st.binary(min_size=min_size, max_size=max_size)]
        if min_size == 0:
            parts.append(st.just(b""))
        parts.append(st.binary(min_size=1, max_size=1))
        if max_size > 0:
            parts.extend(
                [
                    st.just(b"\x00" * min(max_size, 64)),
                    st.just(b"\xff" * min(max_size, 64)),
                ]
            )
        return st.one_of(*parts)

    @staticmethod
    def lists(
        elements: st.SearchStrategy,
        min_size: int = 0,
        max_size: int = 50,
    ) -> st.SearchStrategy[list]:
        """Lists biased toward empty, singleton, and near-max lengths."""
        parts = [st.lists(elements, min_size=min_size, max_size=max_size)]
        if min_size == 0:
            parts.append(st.just([]))
        if min_size <= 1:
            parts.append(st.lists(elements, min_size=1, max_size=1))
        if max_size >= 3:
            parts.append(
                st.lists(elements, min_size=max(max_size - 2, min_size), max_size=max_size)
            )
        return st.one_of(*parts)


# ============================================================================
# Type-driven strategy derivation
# ============================================================================


def strategy_for_type(tp: type, *, _depth: int = 0) -> st.SearchStrategy:
    """Derive a boundary-biased strategy from a type hint.

    Handles: int, float, str, bool, bytes, None, list[T], dict[K,V],
    tuple[T,...], set[T], Optional[T], Union[T,U], dataclasses, and
    falls back to ``hypothesis.strategies.from_type()`` for the rest.
    """
    if _depth > 5:
        return st.just(None)

    # NoneType
    if tp is type(None):
        return st.none()

    # Primitives — boundary-biased
    if tp is int:
        return biased.integers()
    if tp is float:
        return biased.floats()
    if tp is str:
        return biased.strings()
    if tp is bool:
        return st.booleans()
    if tp is bytes:
        return biased.bytes_()

    origin = get_origin(tp)
    args = get_args(tp)
    next_depth = _depth + 1

    # list[T]
    if origin is list:
        elem = args[0] if args else Any
        return biased.lists(strategy_for_type(elem, _depth=next_depth))

    # dict[K, V]
    if origin is dict:
        k = args[0] if args else Any
        v = args[1] if len(args) > 1 else Any
        return st.dictionaries(
            strategy_for_type(k, _depth=next_depth),
            strategy_for_type(v, _depth=next_depth),
            max_size=10,
        )

    # tuple[T, U, ...] or tuple[T, ...]
    if origin is tuple:
        if args:
            if len(args) == 2 and args[1] is Ellipsis:
                return st.lists(strategy_for_type(args[0], _depth=next_depth)).map(tuple)
            return st.tuples(*(strategy_for_type(a, _depth=next_depth) for a in args))
        return st.tuples()

    # set[T]
    if origin is set:
        elem = args[0] if args else Any
        return st.frozensets(strategy_for_type(elem, _depth=next_depth), max_size=10).map(set)

    # frozenset[T]
    if origin is frozenset:
        elem = args[0] if args else Any
        return st.frozensets(strategy_for_type(elem, _depth=next_depth), max_size=10)

    # Union[T, U] or T | U  (Python 3.10+)
    if origin is Union or origin is pytypes.UnionType:
        non_none = [a for a in args if a is not type(None)]
        has_none = len(non_none) < len(args)
        strats = [strategy_for_type(a, _depth=next_depth) for a in non_none]
        if has_none:
            strats.append(st.none())
        return st.one_of(*strats)

    # dataclass — resolve string annotations via get_type_hints
    if hasattr(tp, "__dataclass_fields__"):
        try:
            resolved = get_type_hints(tp)
        except Exception:
            resolved = {f: fld.type for f, fld in tp.__dataclass_fields__.items()}
        field_strats = {}
        for fname in tp.__dataclass_fields__:
            if fname in resolved:
                field_strats[fname] = strategy_for_type(resolved[fname], _depth=next_depth)
        return st.builds(tp, **field_strats)

    # Fallback: Hypothesis from_type
    try:
        return st.from_type(tp)
    except Exception:
        return st.just(None)


# ============================================================================
# @quickcheck decorator
# ============================================================================


def quickcheck(
    fn: Callable | None = None,
    *,
    max_examples: int = 100,
    **overrides: st.SearchStrategy,
) -> Callable:
    """Run *fn* as a property test, deriving strategies from type hints.

    Strategies are boundary-biased by default.  Pass keyword arguments
    to override specific parameters::

        @quickcheck
        def test_add_commutative(a: int, b: int):
            assert a + b == b + a

        @quickcheck(xs=st.lists(st.integers(min_value=0), max_size=5))
        def test_custom(xs: list[int], y: int):
            ...   # xs uses the override, y uses biased auto-derivation

    Works with plain functions and class methods (``self`` is skipped).
    """

    def decorator(fn: Callable) -> Callable:
        try:
            hints = get_type_hints(fn)
        except Exception:
            hints = {}

        sig = inspect.signature(fn)
        strategies: dict[str, st.SearchStrategy] = {}

        for name in sig.parameters:
            if name == "self":
                continue
            if name in overrides:
                strategies[name] = overrides[name]
            elif name in hints:
                strategies[name] = strategy_for_type(hints[name])

        if not strategies:
            return fn  # nothing to generate — return unchanged

        @given(**strategies)
        @settings(max_examples=max_examples)
        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> None:
            return fn(**kwargs)

        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator


# ============================================================================
# Convenience: from_type alias
# ============================================================================

from_type = strategy_for_type
