"""Tests for ordeal.strategies — adversarial data generation."""
import math

from hypothesis import given, settings

from ordeal.strategies import (
    adversarial_strings,
    corrupted_bytes,
    edge_integers,
    mixed_types,
    nan_floats,
)


class TestCorruptedBytes:
    @given(data=corrupted_bytes())
    @settings(max_examples=50)
    def test_produces_bytes(self, data):
        assert isinstance(data, bytes)


class TestAdversarialStrings:
    @given(data=adversarial_strings())
    @settings(max_examples=50)
    def test_produces_strings(self, data):
        assert isinstance(data, str)


class TestNanFloats:
    @given(data=nan_floats())
    @settings(max_examples=50)
    def test_produces_floats(self, data):
        assert isinstance(data, float)

    def test_includes_nan(self):
        """Verify NaN is reachable (draw enough samples)."""
        from hypothesis import find

        result = find(nan_floats(), lambda x: math.isnan(x))
        assert math.isnan(result)

    def test_includes_inf(self):
        from hypothesis import find

        result = find(nan_floats(), lambda x: math.isinf(x))
        assert math.isinf(result)


class TestEdgeIntegers:
    @given(data=edge_integers())
    @settings(max_examples=50)
    def test_produces_ints(self, data):
        assert isinstance(data, int)

    def test_includes_zero(self):
        from hypothesis import find

        result = find(edge_integers(), lambda x: x == 0)
        assert result == 0


class TestMixedTypes:
    @given(data=mixed_types())
    @settings(max_examples=100)
    def test_produces_values(self, data):
        # Should not raise — just verify it generates something
        pass
