"""Tests for ordeal.quickcheck — type-driven, boundary-biased property testing."""
from __future__ import annotations

from dataclasses import dataclass

import hypothesis.strategies as st
from hypothesis import find, given, settings

from ordeal.quickcheck import biased, quickcheck, strategy_for_type


# Module-level dataclasses (must be here so get_type_hints can resolve them)
@dataclass
class _Point:
    x: int
    y: int


@dataclass
class _Inner:
    value: int


@dataclass
class _Outer:
    name: str
    inner: _Inner


# ============================================================================
# @quickcheck decorator
# ============================================================================

@quickcheck
def test_sort_idempotent(xs: list[int]):
    assert sorted(sorted(xs)) == sorted(xs)


@quickcheck
def test_reverse_involution(xs: list[int]):
    assert list(reversed(list(reversed(xs)))) == xs


@quickcheck
def test_addition_commutative(a: int, b: int):
    assert a + b == b + a


@quickcheck
def test_multiplication_commutative(a: int, b: int):
    assert a * b == b * a


@quickcheck
def test_string_split_rejoin(s: str):
    """Splitting on a literal and rejoining is identity."""
    assert "|".join(s.split("|")) == s


@quickcheck
def test_dict_keys_subset(d: dict[str, int]):
    for k in d:
        assert k in d.keys()


@quickcheck
def test_set_union_superset(a: set[int], b: set[int]):
    assert a | b >= a
    assert a | b >= b


@quickcheck
def test_tuple_length(t: tuple[int, str, bool]):
    assert len(t) == 3


@quickcheck
def test_optional_is_int_or_none(x: int | None):
    assert x is None or isinstance(x, int)


# ============================================================================
# @quickcheck with overrides
# ============================================================================

@quickcheck(xs=st.lists(st.integers(min_value=0, max_value=10), max_size=5))
def test_with_override(xs: list[int]):
    assert all(0 <= x <= 10 for x in xs)
    assert len(xs) <= 5


# ============================================================================
# @quickcheck on class methods
# ============================================================================

class TestClassMethods:
    @quickcheck
    def test_abs_non_negative(self, x: int):
        assert abs(x) >= 0

    @quickcheck
    def test_len_non_negative(self, xs: list[float]):
        assert len(xs) >= 0


# ============================================================================
# Boundary-biased strategies
# ============================================================================

class TestBiasedIntegers:
    def test_includes_zero(self):
        result = find(biased.integers(), lambda x: x == 0)
        assert result == 0

    def test_includes_boundary(self):
        result = find(biased.integers(min_value=10, max_value=20), lambda x: x == 10)
        assert result == 10

    def test_respects_bounds(self):
        @given(x=biased.integers(min_value=5, max_value=15))
        @settings(max_examples=200)
        def check(x):
            assert 5 <= x <= 15
        check()


class TestBiasedFloats:
    def test_includes_zero(self):
        result = find(biased.floats(), lambda x: x == 0.0)
        assert result == 0.0

    def test_respects_bounds(self):
        @given(x=biased.floats(min_value=0.0, max_value=1.0))
        @settings(max_examples=200)
        def check(x):
            assert 0.0 <= x <= 1.0
        check()


class TestBiasedLists:
    def test_includes_empty(self):
        result = find(biased.lists(st.integers()), lambda x: len(x) == 0)
        assert result == []

    def test_includes_singleton(self):
        result = find(biased.lists(st.integers()), lambda x: len(x) == 1)
        assert len(result) == 1


# ============================================================================
# strategy_for_type
# ============================================================================

class TestStrategyForType:
    def test_int(self):
        result = find(strategy_for_type(int), lambda x: x == 0)
        assert result == 0

    def test_float(self):
        result = find(strategy_for_type(float), lambda x: x == 1.0)
        assert result == 1.0

    def test_str(self):
        result = find(strategy_for_type(str), lambda x: x == "")
        assert result == ""

    def test_bool(self):
        result = find(strategy_for_type(bool), lambda x: x is True)
        assert result is True

    def test_bytes(self):
        result = find(strategy_for_type(bytes), lambda x: x == b"")
        assert result == b""

    def test_list_int(self):
        result = find(strategy_for_type(list[int]), lambda x: len(x) == 0)
        assert result == []

    def test_dict_str_int(self):
        result = find(strategy_for_type(dict[str, int]), lambda x: len(x) == 0)
        assert result == {}

    def test_tuple_int_str(self):
        strat = strategy_for_type(tuple[int, str])
        result = find(strat, lambda x: isinstance(x, tuple) and len(x) == 2)
        assert isinstance(result[0], int)
        assert isinstance(result[1], str)

    def test_set_int(self):
        result = find(strategy_for_type(set[int]), lambda x: len(x) == 0)
        assert result == set()

    def test_optional(self):
        result = find(strategy_for_type(int | None), lambda x: x is None)
        assert result is None

    def test_union(self):
        strat = strategy_for_type(int | str)

        @given(x=strat)
        @settings(max_examples=50)
        def check(x):
            assert isinstance(x, (int, str))
        check()

    def test_dataclass(self):
        result = find(strategy_for_type(_Point), lambda p: p.x == 0 and p.y == 0)
        assert result.x == 0
        assert result.y == 0

    def test_nested_dataclass(self):
        strat = strategy_for_type(_Outer)

        @given(x=strat)
        @settings(max_examples=30)
        def check(x):
            assert isinstance(x, _Outer)
            assert isinstance(x.inner, _Inner)
            assert isinstance(x.inner.value, int)
        check()
