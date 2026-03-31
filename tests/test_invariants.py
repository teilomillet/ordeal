"""Tests for ordeal.invariants — named, composable invariant assertions."""

import pytest

from ordeal.invariants import (
    bounded,
    finite,
    monotonic,
    no_inf,
    no_nan,
    non_empty,
    unique,
)


class TestNoNan:
    def test_passes_for_normal_float(self):
        no_nan(3.14)

    def test_fails_for_nan(self):
        with pytest.raises(AssertionError, match="NaN"):
            no_nan(float("nan"))

    def test_passes_for_list_of_floats(self):
        no_nan([1.0, 2.0, 3.0])

    def test_fails_for_list_with_nan(self):
        with pytest.raises(AssertionError, match="NaN at index 1"):
            no_nan([1.0, float("nan"), 3.0])

    def test_custom_name(self):
        with pytest.raises(AssertionError, match="my_check"):
            no_nan(float("nan"), name="my_check")


class TestNoInf:
    def test_passes_for_normal_float(self):
        no_inf(3.14)

    def test_fails_for_inf(self):
        with pytest.raises(AssertionError, match="Inf"):
            no_inf(float("inf"))

    def test_fails_for_neg_inf(self):
        with pytest.raises(AssertionError, match="Inf"):
            no_inf(float("-inf"))


class TestFinite:
    def test_composed_from_no_nan_and_no_inf(self):
        finite(3.14)  # passes

    def test_fails_for_nan(self):
        with pytest.raises(AssertionError):
            finite(float("nan"))

    def test_fails_for_inf(self):
        with pytest.raises(AssertionError):
            finite(float("inf"))


class TestBounded:
    def test_passes_in_range(self):
        check = bounded(0, 1)
        check(0.5)

    def test_fails_below(self):
        check = bounded(0, 1)
        with pytest.raises(AssertionError, match="deviation"):
            check(-0.1)

    def test_fails_above(self):
        check = bounded(0, 1)
        with pytest.raises(AssertionError, match="deviation"):
            check(1.1)

    def test_boundary_values_pass(self):
        check = bounded(0, 1)
        check(0)
        check(1)

    def test_works_with_lists(self):
        check = bounded(0, 10)
        check([1, 2, 3])
        with pytest.raises(AssertionError):
            check([1, 2, 11])


class TestMonotonic:
    def test_passes_for_sorted(self):
        check = monotonic()
        check([1, 2, 3, 4])

    def test_passes_for_equal_adjacent(self):
        check = monotonic()
        check([1, 1, 2, 2])

    def test_fails_for_unsorted(self):
        check = monotonic()
        with pytest.raises(AssertionError):
            check([1, 3, 2])

    def test_strict_fails_for_equal(self):
        check = monotonic(strict=True)
        with pytest.raises(AssertionError):
            check([1, 1, 2])


class TestUnique:
    def test_passes_for_unique(self):
        check = unique()
        check([1, 2, 3])

    def test_fails_for_duplicates(self):
        check = unique()
        with pytest.raises(AssertionError, match="duplicate"):
            check([1, 2, 2])

    def test_with_key(self):
        check = unique(key=lambda x: x.lower())
        with pytest.raises(AssertionError):
            check(["a", "A"])


class TestNonEmpty:
    def test_passes_for_non_empty(self):
        check = non_empty()
        check([1])
        check("hello")

    def test_fails_for_empty(self):
        check = non_empty()
        with pytest.raises(AssertionError, match="empty"):
            check([])


class TestComposition:
    def test_and_composes(self):
        check = no_nan & bounded(0, 1)
        check(0.5)  # passes both
        with pytest.raises(AssertionError, match="NaN"):
            check(float("nan"))
        with pytest.raises(AssertionError, match="deviation"):
            check(1.5)

    def test_triple_composition(self):
        check = no_nan & no_inf & bounded(0, 100)
        check(50.0)
        with pytest.raises(AssertionError):
            check(float("nan"))
        with pytest.raises(AssertionError):
            check(float("inf"))
        with pytest.raises(AssertionError):
            check(200.0)
