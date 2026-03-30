"""Tests for ordeal.diff — differential testing."""

from ordeal.diff import diff


def add_v1(x: int, y: int) -> int:
    return x + y


def add_v2(x: int, y: int) -> int:
    return x + y


def add_buggy(x: int, y: int) -> int:
    if x == 0:
        return y + 1  # off-by-one when x=0
    return x + y


def scale_v1(x: float, factor: float) -> float:
    return x * factor


def scale_v2(x: float, factor: float) -> float:
    return x * factor + 1e-10  # tiny drift


class TestDiff:
    def test_equivalent_functions(self):
        result = diff(add_v1, add_v2, max_examples=50)
        assert result.equivalent
        assert result.total == 50

    def test_detects_mismatch(self):
        result = diff(add_v1, add_buggy, max_examples=100)
        assert not result.equivalent
        assert len(result.mismatches) > 0

    def test_summary(self):
        result = diff(add_v1, add_v2, max_examples=10)
        s = result.summary()
        assert "EQUIVALENT" in s

    def test_summary_divergent(self):
        result = diff(add_v1, add_buggy, max_examples=100)
        s = result.summary()
        assert "DIVERGENT" in s

    def test_with_tolerance(self):
        result = diff(scale_v1, scale_v2, max_examples=50, atol=1e-8)
        assert result.equivalent

    def test_without_tolerance_catches_drift(self):
        result = diff(scale_v1, scale_v2, max_examples=50)
        assert not result.equivalent

    def test_custom_comparator(self):
        def loose(a: int, b: int) -> bool:
            return abs(a - b) <= 1

        result = diff(add_v1, add_buggy, max_examples=50, compare=loose)
        assert result.equivalent

    def test_with_fixture(self):
        import hypothesis.strategies as st

        result = diff(
            add_v1, add_v2,
            max_examples=20,
            x=st.integers(0, 10),
            y=st.integers(0, 10),
        )
        assert result.equivalent

    def test_plain_value_fixture(self):
        result = diff(add_v1, add_v2, max_examples=10, x=5, y=3)
        assert result.equivalent
