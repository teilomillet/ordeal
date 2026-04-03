"""Direct regression tests for ordeal.demo.

These tests give mutation testing a real signal for the demo module so
explore()/mutate() do not fall back to mine-oracle warnings in normal use.
"""

from __future__ import annotations

from ordeal import demo


def test_score_bounds_and_midpoints():
    assert demo.score(-10.0) == 0.0
    assert demo.score(0.0) == 0.5
    assert demo.score(1.0) == 1.0
    assert demo.score(10.0) == 1.0


def test_clamp_respects_bounds():
    assert demo.clamp(5, 0, 10) == 5
    assert demo.clamp(-1, 0, 10) == 0
    assert demo.clamp(11, 0, 10) == 10
    assert demo.clamp(0, 0, 10) == 0
    assert demo.clamp(10, 0, 10) == 10


def test_encode_decode_round_trip():
    payload = "ordeal"
    assert demo.encode(payload) == "laedro"
    assert demo.decode(demo.encode(payload)) == payload


def test_normalize_empty_zero_and_general_case():
    assert demo.normalize([]) == []
    assert demo.normalize([0.0, 0.0, 0.0]) == [1.0 / 3.0] * 3

    normalized = demo.normalize([2.0, 3.0, 5.0])
    assert normalized == [0.2, 0.3, 0.5]
    assert sum(normalized) == 1.0


def test_safe_div_and_distance():
    assert demo.safe_div(6.0, 2.0) == 3.0
    assert demo.safe_div(6.0, 0.0) == 0.0
    assert demo.distance(3.0, 4.0) == 5.0
    assert demo.distance(0.0, 0.0) == 0.0
