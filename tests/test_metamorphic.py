"""Tests for ordeal.metamorphic — metamorphic relation testing."""

import pytest

from ordeal.metamorphic import Relation, RelationSet, metamorphic

# ---------------------------------------------------------------------------
# Relation construction
# ---------------------------------------------------------------------------


class TestRelation:
    def test_basic_construction(self):
        r = Relation("id", transform=lambda a: a, check=lambda a, b: a == b)
        assert r.name == "id"

    def test_compose_two_relations(self):
        r1 = Relation("r1", transform=lambda a: a, check=lambda a, b: True)
        r2 = Relation("r2", transform=lambda a: a, check=lambda a, b: True)
        combined = r1 + r2
        assert isinstance(combined, RelationSet)
        assert len(combined.relations) == 2

    def test_compose_three_relations(self):
        r1 = Relation("r1", transform=lambda a: a, check=lambda a, b: True)
        r2 = Relation("r2", transform=lambda a: a, check=lambda a, b: True)
        r3 = Relation("r3", transform=lambda a: a, check=lambda a, b: True)
        combined = r1 + r2 + r3
        assert len(combined.relations) == 3


# ---------------------------------------------------------------------------
# @metamorphic decorator
# ---------------------------------------------------------------------------


class TestMetamorphicDecorator:
    def test_identity_relation_passes(self):
        identity = Relation(
            "identity",
            transform=lambda args: args,
            check=lambda a, b: a == b,
        )

        @metamorphic(identity, max_examples=20)
        def test_abs(x: int):
            return abs(x)

        # Should not raise — abs(x) == abs(x) trivially
        test_abs()

    def test_commutativity(self):
        commutative = Relation(
            "commutative",
            transform=lambda args: (args[1], args[0]),
            check=lambda a, b: a == b,
        )

        @metamorphic(commutative, max_examples=20)
        def test_add(x: int, y: int):
            return x + y

        test_add()

    def test_failing_relation_raises(self):
        wrong = Relation(
            "always_different",
            transform=lambda args: args,
            check=lambda a, b: a != b,
        )

        @metamorphic(wrong, max_examples=20)
        def test_id(x: int):
            return x

        with pytest.raises(AssertionError, match="always_different"):
            test_id()

    def test_multiple_relations(self):
        identity = Relation(
            "identity",
            transform=lambda args: args,
            check=lambda a, b: a == b,
        )
        double = Relation(
            "double_input_doubles_output",
            transform=lambda args: (args[0] * 2,),
            check=lambda a, b: b == a * 2,
        )

        @metamorphic(identity, double, max_examples=20)
        def test_times_two(x: int):
            return x * 2

        test_times_two()

    def test_relation_set_via_plus(self):
        r1 = Relation("r1", transform=lambda args: args, check=lambda a, b: a == b)
        r2 = Relation("r2", transform=lambda args: args, check=lambda a, b: a == b)

        @metamorphic(r1 + r2, max_examples=20)
        def test_identity(x: int):
            return x

        test_identity()

    def test_transform_exception_skipped(self):
        """If transform produces invalid input that causes fn to raise, skip."""
        bad_transform = Relation(
            "bad",
            transform=lambda args: (0,),
            check=lambda a, b: True,
        )

        @metamorphic(bad_transform, max_examples=20)
        def test_divide(x: int):
            return 100 // (x if x != 0 else 1)

        # Should not crash — the transform produces x=0 but fn handles it
        test_divide()
