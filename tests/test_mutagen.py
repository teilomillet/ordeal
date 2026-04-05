"""Tests for ordeal.mutagen strategy-bounded mutation repair."""

from __future__ import annotations

import math
import random

import hypothesis.strategies as st

from ordeal.mutagen import mutate_inputs


def test_mutate_inputs_stays_within_common_strategy_bounds():
    rng = random.Random(7)
    inputs = {
        "count": 5,
        "ratio": 0.5,
        "name": "ab",
        "mode": "alpha",
        "items": [0, 1],
    }
    strategies = {
        "count": st.integers(min_value=0, max_value=5),
        "ratio": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        "name": st.text(min_size=1, max_size=3),
        "mode": st.sampled_from(["alpha", "beta"]),
        "items": st.lists(st.integers(min_value=0, max_value=2), min_size=1, max_size=3),
    }

    for _ in range(25):
        mutated = mutate_inputs(
            inputs,
            rng,
            strategies=strategies,
            stay_within_bounds=True,
        )
        assert 0 <= mutated["count"] <= 5
        assert 0.0 <= mutated["ratio"] <= 1.0
        assert math.isfinite(mutated["ratio"])
        assert 1 <= len(mutated["name"]) <= 3
        assert mutated["mode"] in {"alpha", "beta"}
        assert 1 <= len(mutated["items"]) <= 3
        assert all(0 <= item <= 2 for item in mutated["items"])


def test_mutate_inputs_respect_strategies_alias_enables_bounded_mode():
    mutated = mutate_inputs(
        {"count": 1},
        random.Random(11),
        strategies={"count": st.integers(min_value=0, max_value=1)},
        respect_strategies=True,
    )
    assert 0 <= mutated["count"] <= 1
