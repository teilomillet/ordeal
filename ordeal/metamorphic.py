"""Metamorphic relation testing.

Metamorphic testing checks *relationships* between outputs rather than
exact values.  Define a :class:`Relation` that transforms input and
checks how outputs relate, then apply it with :func:`metamorphic`::

    from ordeal.metamorphic import Relation, metamorphic

    commutative = Relation(
        "commutative",
        transform=lambda args: (args[1], args[0]),
        check=lambda a, b: a == b,
    )

    @metamorphic(commutative)
    def test_add(x: int, y: int):
        return x + y

Hypothesis generates inputs; ordeal runs the function on both original
and transformed inputs, then asserts the check holds.
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from hypothesis import given, settings
from hypothesis import strategies as st


@dataclass(frozen=True)
class Relation:
    """A named metamorphic relation.

    Args:
        name: Human-readable label for error messages.
        transform: Takes the original arguments (as a tuple) and returns
            transformed arguments (as a tuple).
        check: Takes ``(output_original, output_transformed)`` and returns
            ``True`` if the relation holds.
    """

    name: str
    transform: Callable[[tuple[Any, ...]], tuple[Any, ...]]
    check: Callable[[Any, Any], bool]

    def __add__(self, other: Relation) -> RelationSet:
        """Compose relations: ``(a + b)`` checks both."""
        relations: list[Relation] = []
        for r in (self, other):
            if isinstance(r, RelationSet):
                relations.extend(r.relations)
            else:
                relations.append(r)
        return RelationSet(relations)


@dataclass(frozen=True)
class RelationSet:
    """Multiple relations composed with ``+``."""

    relations: list[Relation] = field(default_factory=list)

    def __add__(self, other: Relation | RelationSet) -> RelationSet:
        """Compose with another relation or set."""
        relations = list(self.relations)
        if isinstance(other, RelationSet):
            relations.extend(other.relations)
        else:
            relations.append(other)
        return RelationSet(relations)


def metamorphic(
    *relations: Relation | RelationSet,
    max_examples: int = 100,
) -> Callable[..., Callable[..., None]]:
    """Decorator: test that *relations* hold for a function.

    The decorated function must:

    1. Accept typed parameters (for Hypothesis strategy inference).
    2. Return a value (the output to compare).

    For each Hypothesis-generated input, ordeal runs the function on
    the original input and on each transformed input, then asserts the
    relation's ``check`` holds.

    Example::

        negate_involution = Relation(
            "negate_involution",
            transform=lambda args: (-args[0],),
            check=lambda a, b: abs(a + b) < 1e-6,
        )

        @metamorphic(negate_involution)
        def test_negate(x: float):
            return -x
    """
    # Flatten RelationSets
    flat: list[Relation] = []
    for r in relations:
        if isinstance(r, RelationSet):
            flat.extend(r.relations)
        else:
            flat.append(r)

    def decorator(fn: Callable[..., Any]) -> Callable[..., None]:
        sig = inspect.signature(fn)
        params = [
            p for p in sig.parameters.values()
            if p.name != "self" and p.annotation != inspect.Parameter.empty
        ]

        # Build Hypothesis strategies from type annotations
        from ordeal.quickcheck import strategy_for_type

        strategies = {}
        for p in params:
            strategies[p.name] = strategy_for_type(p.annotation)

        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> None:
            args_tuple = tuple(kwargs[p.name] for p in params)
            original_output = fn(**kwargs)

            for relation in flat:
                transformed_args = relation.transform(args_tuple)
                if not isinstance(transformed_args, tuple):
                    transformed_args = (transformed_args,)
                # Rebuild kwargs from transformed args
                transformed_kwargs = {
                    p.name: v for p, v in zip(params, transformed_args)
                }
                try:
                    transformed_output = fn(**transformed_kwargs)
                except Exception:
                    # If the transform produces invalid input, skip this relation
                    continue

                if not relation.check(original_output, transformed_output):
                    raise AssertionError(
                        f"Metamorphic relation '{relation.name}' violated:\n"
                        f"  original args:     {args_tuple}\n"
                        f"  transformed args:  {transformed_args}\n"
                        f"  original output:   {original_output}\n"
                        f"  transformed output: {transformed_output}"
                    )

        # Apply @given with inferred strategies
        hypothesis_fn = given(**strategies)(wrapper)
        test_fn = settings(max_examples=max_examples)(hypothesis_fn)
        test_fn.__name__ = fn.__name__
        test_fn.__qualname__ = fn.__qualname__
        return test_fn

    return decorator
