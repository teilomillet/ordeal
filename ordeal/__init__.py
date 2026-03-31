"""ordeal — Automated chaos testing for Python.

Core API::

    from ordeal import ChaosTest, rule, invariant, always, sometimes
    from ordeal.faults import io, numerical, timing
    from ordeal import strategies

Assertions (Antithesis-style)::

    always(condition, "property name")       # must hold every time
    sometimes(condition, "property name")    # must hold at least once
    reachable("label")                       # code path must execute
    unreachable("label")                     # code path must never execute

Inline fault injection (FoundationDB-style)::

    from ordeal import buggify
    if buggify():
        inject_chaos()

Metamorphic relation testing::

    from ordeal.metamorphic import Relation, metamorphic

Mutation testing::

    from ordeal.mutations import mutate_function_and_test
    result = mutate_function_and_test("mymodule.func", my_test)

Integrations::

    ordeal.integrations.atheris_engine   # coverage-guided fuzzing
    ordeal.integrations.openapi          # API chaos testing
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version

# Re-export Hypothesis stateful testing API for convenience
from hypothesis.stateful import (
    Bundle,
    initialize,
    invariant,
    precondition,
    rule,
)

from ordeal.assertions import always, reachable, sometimes, unreachable
from ordeal.buggify import buggify, buggify_value
from ordeal.chaos import ChaosTest

try:
    __version__ = _get_version("ordeal")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    # Core
    "ChaosTest",
    # Assertions
    "always",
    "sometimes",
    "reachable",
    "unreachable",
    # Buggify
    "buggify",
    "buggify_value",
    # Hypothesis re-exports
    "rule",
    "invariant",
    "initialize",
    "precondition",
    "Bundle",
    # Config
    "auto_configure",
]


def auto_configure(
    buggify_probability: float = 0.1,
    seed: int | None = None,
) -> None:
    """Enable chaos testing mode programmatically.

    Alternative to the ``--chaos`` CLI flag.  Call in ``conftest.py``::

        from ordeal import auto_configure
        auto_configure()
    """
    from ordeal import assertions as _assertions
    from ordeal import buggify as _buggify

    _assertions.tracker.active = True
    _assertions.tracker.reset()
    _buggify.activate(probability=buggify_probability)
    if seed is not None:
        _buggify.set_seed(seed)
