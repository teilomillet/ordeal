"""Tests for ordeal.mine — property mining."""

from ordeal.mine import mine


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
        import hypothesis.strategies as st

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
