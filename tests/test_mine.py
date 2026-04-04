"""Tests for ordeal.mine — property mining."""

import hypothesis.strategies as st
from hypothesis import settings as hsettings

from ordeal import ChaosTest, always, invariant, rule
from ordeal.assertions import tracker
from ordeal.mine import (
    _check_associative,
    _check_bijective,
    _check_commutative,
    _check_involution,
    _check_length_relationship,
    _check_monotonic,
    _check_observed_bounds,
    mine,
    mine_module,
    mine_pair,
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


def negate_int(x: int) -> int:
    return -x


def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def float_add(a: float, b: float) -> float:
    return a + b


def float_negate(x: float) -> float:
    return -x


def encode(x: int) -> str:
    return str(x)


def decode(s: str) -> int:
    return int(s)


def role_sensitive(prompt: str, response: str) -> str:
    return prompt + response


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
        assert none_prop.counterexample is not None

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
        assert "not checked:" in s
        assert "observed range" not in s

    def test_summary_highlights_suspicious_findings(self):
        result = mine(sometimes_none, max_examples=200)
        s = result.summary()
        assert "suspicious findings:" in s
        assert "never None" in s
        assert "counterexample:" in s

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

    def test_discovers_commutativity(self):
        result = mine(add, max_examples=100)
        comm = next((p for p in result.properties if p.name == "commutative"), None)
        assert comm is not None
        assert comm.universal

    def test_discovers_non_commutativity(self):
        result = mine(subtract, max_examples=100)
        comm = next((p for p in result.properties if p.name == "commutative"), None)
        assert comm is not None
        assert not comm.universal

    def test_discovers_associativity(self):
        result = mine(add, max_examples=100)
        assoc = next((p for p in result.properties if p.name == "associative"), None)
        assert assoc is not None
        assert assoc.universal

    def test_discovers_non_associativity(self):
        result = mine(subtract, max_examples=100)
        assoc = next((p for p in result.properties if p.name == "associative"), None)
        if assoc is not None:
            assert not assoc.universal

    def test_ignore_properties_suppresses_named_laws(self):
        result = mine(add, max_examples=100, ignore_properties=["commutative", "associative"])
        names = {p.name for p in result.properties}
        assert "commutative" not in names
        assert "associative" not in names

    def test_discovers_involution(self):
        result = mine(negate_int, max_examples=100)
        inv = next((p for p in result.properties if p.name == "involution"), None)
        assert inv is not None
        assert inv.universal  # -(-x) == x

    def test_float_commutativity(self):
        result = mine(float_add, max_examples=200)
        comm = next((p for p in result.properties if p.name == "commutative"), None)
        assert comm is not None
        assert comm.universal  # a + b ≈ b + a for floats

    def test_float_associativity(self):
        result = mine(float_add, max_examples=200)
        assoc = next((p for p in result.properties if p.name == "associative"), None)
        # Float addition is NOT strictly associative, but with approx
        # equality it should hold for most sampled triples
        if assoc is not None:
            assert assoc.confidence > 0.8

    def test_float_involution(self):
        result = mine(float_negate, max_examples=200)
        inv = next((p for p in result.properties if p.name == "involution"), None)
        assert inv is not None
        assert inv.universal  # -(-x) ≈ x for floats

    def test_skips_commutativity_for_role_sensitive_params(self):
        result = mine(role_sensitive, max_examples=50)
        comm = next((p for p in result.properties if p.name == "commutative"), None)
        assert comm is None

    def test_mine_module_allows_relation_suppression(self):
        import sys
        import types

        mod = types.ModuleType("_test_mine_module_relations")
        exec(
            "def encode(x: int) -> str:\n"
            "    return str(x)\n"
            "\n"
            "def decode(s: str) -> int:\n"
            "    return int(s)\n",
            mod.__dict__,
        )
        sys.modules[mod.__name__] = mod
        try:
            result = mine_module(
                mod.__name__,
                max_examples=20,
                cross_max_examples=10,
                ignore_relations=["roundtrip"],
            )
            assert all(prop.relation != "roundtrip" for prop in result.cross_function)
        finally:
            del sys.modules[mod.__name__]

    def test_discovers_non_involution(self):
        result = mine(always_positive, max_examples=100)
        inv = next((p for p in result.properties if p.name == "involution"), None)
        if inv is not None:
            assert not inv.universal  # (x²+1)²+1 != x


class TestMinePair:
    def test_roundtrip_encode_decode(self):
        result = mine_pair(encode, decode, max_examples=100)
        rt = next((p for p in result.properties if "decode(encode" in p.name), None)
        assert rt is not None
        assert rt.universal  # int(str(x)) == x

    def test_roundtrip_negate(self):
        result = mine_pair(negate_int, negate_int, max_examples=100)
        rt = next((p for p in result.properties if "roundtrip" in p.name), None)
        assert rt is not None
        assert rt.universal  # -(-x) == x

    def test_non_roundtrip(self):
        result = mine_pair(clamp, identity, max_examples=100)
        # clamp then identity: identity(clamp(x)) == x only if x in [0,1]
        rt = next((p for p in result.properties if "roundtrip" in p.name), None)
        if rt is not None:
            assert not rt.universal

    def test_pair_summary(self):
        result = mine_pair(encode, decode, max_examples=50)
        assert "encode" in result.function
        assert "decode" in result.function


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


@quickcheck
def test_qc_commutative_add(a: int, b: int):
    """Addition is commutative — checker must agree."""
    _add = lambda a, b: a + b  # noqa: E731
    prop = _check_commutative(_add, [{"a": a, "b": b}], [a + b])
    if prop.total > 0:
        assert prop.universal


@quickcheck
def test_qc_associative_add(a: int, b: int, c: int):
    """Addition is associative — checker must agree."""
    _add = lambda a, b: a + b  # noqa: E731
    inputs = [{"a": a, "b": b}, {"a": b, "b": c}, {"a": c, "b": a}]
    prop = _check_associative(_add, inputs)
    if prop.total > 0:
        assert prop.universal


@quickcheck
def test_qc_involution_negate(x: int):
    """Negation is an involution: -(-x) == x."""
    _neg = lambda x: -x  # noqa: E731
    prop = _check_involution(_neg, [-x], [{"x": x}])
    if prop.total > 0:
        assert prop.universal


class RelationalBattle(ChaosTest):
    """Relational checkers stay consistent under diverse 2-param data."""

    faults = []

    def __init__(self):
        super().__init__()
        self._add = lambda a, b: a + b  # noqa: E731
        self.inputs: list[dict[str, int]] = []
        self.outputs: list[int] = []

    @rule(
        a=st.integers(min_value=-100, max_value=100),
        b=st.integers(min_value=-100, max_value=100),
    )
    def add_pair(self, a, b):
        self.inputs.append({"a": a, "b": b})
        self.outputs.append(a + b)

    @invariant()
    def commutative_holds(self):
        prop = _check_commutative(self._add, self.inputs, self.outputs)
        if prop.total > 0:
            assert prop.universal
            assert prop.holds <= prop.total

    @invariant()
    def associative_holds(self):
        prop = _check_associative(self._add, self.inputs)
        if prop.total > 0:
            assert prop.universal
            assert prop.holds <= prop.total

    def teardown(self):
        self.inputs.clear()
        self.outputs.clear()
        super().teardown()


TestRelationalBattle = RelationalBattle.TestCase
TestRelationalBattle.settings = hsettings(max_examples=50, stateful_step_count=20)


def test_check_bijective_unhashable_inputs():
    """_check_bijective should skip inputs containing unhashable values (e.g. lists)."""
    inputs = [
        {"a": [1, 2, 3], "b": [4, 5, 6]},
        {"a": [7, 8, 9], "b": [10, 11, 12]},
    ]
    outputs = [[0.1, 0.2], [0.3, 0.4]]
    # Should not raise TypeError: unhashable type: 'list'
    result = _check_bijective(inputs, outputs)
    assert result.holds == 0
    assert result.total == 0
