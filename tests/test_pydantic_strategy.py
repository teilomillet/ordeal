"""Tests for Pydantic model strategy support in ordeal.quickcheck."""

import pytest

from ordeal.quickcheck import strategy_for_type

# Pydantic is optional — skip all if not installed
pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel, Field  # noqa: E402

# -- Test models -----------------------------------------------------------


class SimpleModel(BaseModel):
    name: str
    age: int
    score: float


class ModelWithDefaults(BaseModel):
    label: str = "default_label"
    count: int = 0
    active: bool = True


class ModelWithOptional(BaseModel):
    required: str
    optional_field: str | None = None


class ConstrainedModel(BaseModel):
    bounded_int: int = Field(ge=0, le=100)
    positive_float: float = Field(gt=0.0)
    short_string: str = Field(min_length=1, max_length=10)


class NestedModel(BaseModel):
    inner: SimpleModel
    tag: str = "nested"


# -- Tests -----------------------------------------------------------------


class TestPydanticStrategy:
    def test_simple_model(self):
        strat = strategy_for_type(SimpleModel)
        # Generate a few examples to verify they're valid
        from hypothesis import given, settings

        results = []

        @given(instance=strat)
        @settings(max_examples=20)
        def check(instance):
            assert isinstance(instance, SimpleModel)
            assert isinstance(instance.name, str)
            assert isinstance(instance.age, int)
            assert isinstance(instance.score, float)
            results.append(instance)

        check()
        assert len(results) > 0

    def test_model_with_defaults(self):
        strat = strategy_for_type(ModelWithDefaults)

        from hypothesis import given, settings

        @given(instance=strat)
        @settings(max_examples=20)
        def check(instance):
            assert isinstance(instance, ModelWithDefaults)
            assert isinstance(instance.label, str)
            assert isinstance(instance.count, int)

        check()

    def test_model_with_optional(self):
        strat = strategy_for_type(ModelWithOptional)

        from hypothesis import given, settings

        seen_none = False
        seen_value = False

        @given(instance=strat)
        @settings(max_examples=50)
        def check(instance):
            nonlocal seen_none, seen_value
            assert isinstance(instance, ModelWithOptional)
            assert isinstance(instance.required, str)
            if instance.optional_field is None:
                seen_none = True
            else:
                seen_value = True

        check()
        # With enough examples, we should see both
        assert seen_none or seen_value

    def test_constrained_model(self):
        strat = strategy_for_type(ConstrainedModel)

        from hypothesis import given, settings

        @given(instance=strat)
        @settings(max_examples=30)
        def check(instance):
            assert isinstance(instance, ConstrainedModel)
            assert 0 <= instance.bounded_int <= 100
            assert instance.positive_float > 0
            assert 1 <= len(instance.short_string) <= 10

        check()

    def test_nested_model(self):
        strat = strategy_for_type(NestedModel)

        from hypothesis import given, settings

        @given(instance=strat)
        @settings(max_examples=10)
        def check(instance):
            assert isinstance(instance, NestedModel)
            assert isinstance(instance.inner, SimpleModel)
            assert isinstance(instance.tag, str)

        check()

    def test_base_model_itself_not_matched(self):
        from ordeal.quickcheck import _is_pydantic_model

        assert not _is_pydantic_model(BaseModel)
        assert _is_pydantic_model(SimpleModel)
        assert _is_pydantic_model(ConstrainedModel)
