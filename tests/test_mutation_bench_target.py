"""Ordinary direct tests for mutation benchmark calibration."""

from __future__ import annotations

import pytest

import tests._mutation_bench_target as bench_target


def test_tiny_add_basics():
    assert bench_target.tiny_add(1, 2) == 3
    assert bench_target.tiny_add(-1, 5) == 4


def test_tiny_add_zero_identity():
    assert bench_target.tiny_add(0, 0) == 0
    assert bench_target.tiny_add(7, 0) == 7


@pytest.mark.parametrize(
    ("x", "lo", "hi", "expected"),
    [
        (-5, 0, 10, 0),
        (0, 0, 10, 0),
        (4, 0, 10, 4),
        (10, 0, 10, 10),
        (15, 0, 10, 10),
        (-2, -5, 5, -2),
    ],
)
def test_medium_clamp_cases(x: int, lo: int, hi: int, expected: int):
    assert bench_target.medium_clamp(x, lo, hi) == expected


@pytest.mark.parametrize(
    ("a", "b", "c", "mode", "expected"),
    [
        (1, 2, 3, "sum", 6),
        (4, 5, 3, "spread", 6),
        (-3, 4, 2, "weighted", 5),
        (3, 4, 2, "weighted", 3),
        (6, 6, 5, "other", 20),
        (-4, 2, 3, "other", -10),
        (2, 3, 1, "sum", 6),
        (2, 3, 1, "spread", 4),
        (-1, 1, 3, "weighted", 6),
        (5, -1, 4, "weighted", -4),
        (3, 3, 12, "sum", 18),
        (3, 3, 12, "spread", -6),
    ],
)
def test_heavy_score_cases(a: int, b: int, c: int, mode: str, expected: int):
    assert bench_target.heavy_score(a, b, c, mode) == expected


def test_heavy_score_caps_upper_bound():
    assert bench_target.heavy_score(9, 9, 9, "sum") == 20


def test_heavy_score_caps_lower_bound():
    assert bench_target.heavy_score(-9, -9, 9, "spread") == -10
