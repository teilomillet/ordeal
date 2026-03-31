"""ordeal — Automated chaos testing for Python.

Capabilities (each is independent — use one or all):

1. **Stateful chaos testing** — Hypothesis-powered rule exploration with fault injection::

    from ordeal import ChaosTest, rule, invariant, always
    from ordeal.faults import timing, io

    class MyServiceChaos(ChaosTest):
        faults = [timing.timeout("myapp.db.query"), io.error_on_call("myapp.cache.get")]
        swarm = True  # random fault subsets for better coverage

        @rule()
        def call_service(self):
            result = my_service.process("input")
            always(result is not None, "never returns None")

    TestMyService = MyServiceChaos.TestCase  # run with pytest

2. **Property assertions** (Antithesis-style) — ``always``/``sometimes``/``reachable``::

    always(condition, "name")       # must hold every time — raises immediately
    sometimes(condition, "name")    # must hold at least once — checked at end
    reachable("label")              # code path must execute at least once
    unreachable("label")            # code path must never execute

3. **Inline fault injection** (FoundationDB BUGGIFY) — no-op in production::

    from ordeal import buggify
    if buggify():                   # True only when chaos mode is active
        data = corrupt(data)

4. **API chaos testing** — built-in OpenAPI engine, no extra deps::

    from ordeal.integrations.openapi import chaos_api_test
    result = chaos_api_test(app=my_fastapi_app, faults=[...])
    result = chaos_api_test(app=my_app, auto_discover=True)  # auto-generate faults
    print(result.summary())  # pass/fail + contextual hints

5. **Mutation testing** — verify your tests catch real bugs::

    from ordeal.mutations import mutate_function_and_test
    result = mutate_function_and_test("myapp.scoring.compute", my_test_suite)

6. **Coverage-guided exploration** — deeper than random testing::

    ordeal explore  # CLI, reads ordeal.toml — checkpoints, energy scheduling

7. **Atheris integration** — coverage-guided fuzzing for buggify() decisions::

    from ordeal.integrations.atheris_engine import fuzz
    fuzz(my_function, max_time=60)  # requires: pip install ordeal[atheris]

Running chaos tests::

    pytest --chaos                  # enable chaos mode globally
    pytest --chaos --chaos-seed 42  # reproducible chaos
    auto_configure()                # or enable programmatically in conftest.py
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
from ordeal.mutations import MutationResult, mutate_function_and_test

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
    # Mutations
    "mutate_function_and_test",
    "MutationResult",
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
