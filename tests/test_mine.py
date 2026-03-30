"""Tests for ordeal.mine — property mining."""

import hypothesis.strategies as st
from hypothesis import settings as hsettings

from ordeal import ChaosTest, always, invariant, rule
from ordeal.assertions import tracker
from ordeal.mine import (
    _check_length_relationship,
    _check_monotonic,
    _check_observed_bounds,
    mine,
)
from ordeal.quickcheck import quickcheck


def clamp(x: float) -> float:
    """Always returns a value in [0, 1]."""
    return max(0.0, min(1.0, x))


def identity(x: int) -> int:
    return x


def sometimes_none(x: int) -> int | None:
    if x % 7 == 0:
        return None
    return x * 2


def always_positive(x: float) -> float:
    return x * x + 1.0


def nondeterministic(x: int) -> float:
    import random

    return x + random.random()


def double(x: int) -> int:
    return x * 2


def negate(x: float) -> float:
    return -x


def sort_list(xs: list[int]) -> list[int]:
    return sorted(xs)


def first_half(xs: list[int]) -> list[int]:
    return xs[: len(xs) // 2]


class TestMine:
    def test_discovers_bounded_01(self):
        result = mine(clamp, max_examples=100)
        names = {p.name for p in result.universal}
        assert "output in [0, 1]" in names

    def test_discovers_non_negative(self):
        result = mine(always_positive, max_examples=100)
        names = {p.name for p in result.universal}
        assert "output >= 0" in names

    def test_discovers_never_none(self):
        result = mine(identity, max_examples=100)
        names = {p.name for p in result.universal}
        assert "never None" in names

    def test_discovers_sometimes_none(self):
        result = mine(sometimes_none, max_examples=200)
        none_prop = next(p for p in result.properties if p.name == "never None")
        # Should NOT be universal since sometimes_none returns None
        assert not none_prop.universal

    def test_discovers_determinism(self):
        result = mine(identity, max_examples=50)
        det = next(p for p in result.properties if p.name == "deterministic")
        assert det.universal

    def test_discovers_nondeterminism(self):
        result = mine(nondeterministic, max_examples=50)
        det = next(p for p in result.properties if p.name == "deterministic")
        assert not det.universal

    def test_discovers_idempotence(self):
        result = mine(clamp, max_examples=50)
        idem = next(
            (p for p in result.properties if p.name == "idempotent"),
            None,
        )
        if idem is not None:
            assert idem.universal  # clamp(clamp(x)) == clamp(x)

    def test_summary(self):
        result = mine(clamp, max_examples=50)
        s = result.summary()
        assert "mine(clamp)" in s

    def test_with_fixture(self):
        result = mine(
            clamp,
            max_examples=50,
            x=st.floats(min_value=-10, max_value=10, allow_nan=False),
        )
        assert result.examples > 0

    def test_universal_vs_likely(self):
        result = mine(clamp, max_examples=100)
        # All universal properties should also be in .properties
        for p in result.universal:
            assert p in result.properties

    def test_discovers_monotone_increasing(self):
        result = mine(double, max_examples=100)
        mono = next(
            (p for p in result.properties if "monotonically non-decreasing" in p.name),
            None,
        )
        assert mono is not None
        assert mono.universal

    def test_discovers_monotone_decreasing(self):
        result = mine(negate, max_examples=100)
        mono = next(
            (p for p in result.properties if "monotonically non-increasing" in p.name),
            None,
        )
        assert mono is not None
        assert mono.universal

    def test_discovers_not_monotone(self):
        result = mine(always_positive, max_examples=100)
        mono = next(
            (p for p in result.properties if "monotonically" in p.name),
            None,
        )
        if mono is not None:
            assert not mono.universal

    def test_discovers_observed_bounds(self):
        result = mine(clamp, max_examples=100)
        bounds = next(
            (p for p in result.properties if p.name.startswith("observed range")),
            None,
        )
        assert bounds is not None
        assert bounds.universal

    def test_discovers_length_preserving(self):
        result = mine(sort_list, max_examples=100, xs=st.lists(st.integers()))
        length = next(
            (p for p in result.properties if "len(output) == len(" in p.name),
            None,
        )
        assert length is not None
        assert length.universal

    def test_discovers_length_shrinking(self):
        result = mine(first_half, max_examples=100, xs=st.lists(st.integers()))
        length = next(
            (p for p in result.properties if "len(output) <= len(" in p.name),
            None,
        )
        assert length is not None
        assert length.universal


# ============================================================================
# ordeal-powered tests: @quickcheck, ChaosTest, always()
# ============================================================================


@quickcheck
def test_qc_bounds_always_universal(xs: list[int]):
    """Observed bounds are tautologically universal by construction."""
    prop = _check_observed_bounds(xs)
    if prop.total > 0:
        assert prop.universal


@quickcheck
def test_qc_monotone_identity(xs: list[int]):
    """The identity function must be detected as non-decreasing."""
    if len(xs) < 2 or len(set(xs)) < 2:
        return
    props = _check_monotonic([{"x": x} for x in xs], list(xs))
    assert any("non-decreasing" in p.name and p.universal for p in props)


@quickcheck
def test_qc_monotone_negation(xs: list[int]):
    """Negation must be detected as non-increasing."""
    if len(xs) < 2 or len(set(xs)) < 2:
        return
    props = _check_monotonic([{"x": x} for x in xs], [-x for x in xs])
    assert any("non-increasing" in p.name and p.universal for p in props)


@quickcheck
def test_qc_length_sorted(xs: list[int]):
    """sorted() preserves length — checker must agree."""
    if not xs:
        return
    props = _check_length_relationship([{"xs": xs}], [sorted(xs)])
    eqs = [p for p in props if "==" in p.name]
    assert eqs and eqs[0].universal


class MonotoneBattle(ChaosTest):
    """Checkers stay consistent as diverse numeric pairs accumulate."""

    faults = []

    def __init__(self):
        super().__init__()
        self.inputs: list[dict[str, int]] = []
        self.outputs: list[int] = []

    @rule(x=st.integers(min_value=-100, max_value=100))
    def add_linear(self, x):
        self.inputs.append({"x": x})
        self.outputs.append(x * 3 + 7)

    @rule(x=st.integers(min_value=-100, max_value=100))
    def add_square(self, x):
        self.inputs.append({"x": x})
        self.outputs.append(x * x)

    @rule(x=st.integers(min_value=-100, max_value=100))
    def add_negative(self, x):
        self.inputs.append({"x": x})
        self.outputs.append(-x)

    @invariant()
    def bounds_universal(self):
        prop = _check_observed_bounds(self.outputs)
        if prop.total > 0:
            assert prop.universal

    @invariant()
    def monotone_valid(self):
        for p in _check_monotonic(self.inputs, self.outputs):
            assert 0.0 <= p.confidence <= 1.0
            assert p.holds <= p.total

    def teardown(self):
        self.inputs.clear()
        self.outputs.clear()
        super().teardown()


TestMonotoneBattle = MonotoneBattle.TestCase
TestMonotoneBattle.settings = hsettings(max_examples=50, stateful_step_count=20)


class LengthBattle(ChaosTest):
    """Length checker consistency under diverse list operations."""

    faults = []

    def __init__(self):
        super().__init__()
        self.inputs: list[dict[str, list]] = []
        self.outputs: list[list] = []

    @rule(xs=st.lists(st.integers(), max_size=10))
    def add_sorted(self, xs):
        self.inputs.append({"xs": xs})
        self.outputs.append(sorted(xs))

    @rule(xs=st.lists(st.integers(), max_size=10))
    def add_reversed(self, xs):
        self.inputs.append({"xs": xs})
        self.outputs.append(list(reversed(xs)))

    @rule(xs=st.lists(st.integers(), max_size=10))
    def add_first_half(self, xs):
        self.inputs.append({"xs": xs})
        self.outputs.append(xs[: len(xs) // 2])

    @invariant()
    def length_valid(self):
        for p in _check_length_relationship(self.inputs, self.outputs):
            assert 0.0 <= p.confidence <= 1.0
            assert p.holds <= p.total

    def teardown(self):
        self.inputs.clear()
        self.outputs.clear()
        super().teardown()


TestLengthBattle = LengthBattle.TestCase
TestLengthBattle.settings = hsettings(max_examples=50, stateful_step_count=20)


def test_always_observed_bounds():
    """Demonstrate ordeal's always() assertion with observed bounds."""
    tracker.active = True
    tracker.reset()
    try:
        for vals in [[1, 2, 3], [-5, 0, 5], [42], [0, 0, 0], list(range(100))]:
            prop = _check_observed_bounds(vals)
            always(prop.universal, "observed bounds universal")
        result = next(r for r in tracker.results if r.name == "observed bounds universal")
        assert result.passes == 5
        assert result.failures == 0
    finally:
        tracker.active = False
