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

    from ordeal import mutate_function_and_test

    result = mutate_function_and_test("mymodule.func", preset="standard")
    print(result.summary())   # test gaps + how to fix them

    # CLI: ordeal mutate mymodule.func --preset standard

6. **Coverage-guided exploration** — deeper than random testing::

    ordeal explore  # CLI, reads ordeal.toml — checkpoints, energy scheduling

7. **Atheris integration** — coverage-guided fuzzing for buggify() decisions::

    from ordeal.integrations.atheris_engine import fuzz
    fuzz(my_function, max_time=60)  # requires: pip install ordeal[atheris]

8. **Discoverability** — introspect all capabilities programmatically::

    from ordeal import catalog
    c = catalog()  # faults, invariants, strategies, mutations
    for fault in c["faults"]:
        print(f"{fault['qualname']}  -- {fault['doc']}")

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
from ordeal.mutations import (
    OPERATORS,
    PRESETS,
    MutationResult,
    NoTestsFoundError,
    mutate_function_and_test,
)

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
    # Discoverability
    "catalog",
    # Mutations
    "mutate_function_and_test",
    "MutationResult",
    "PRESETS",
    "OPERATORS",
    "NoTestsFoundError",
]


def catalog() -> dict[str, list]:
    """Discover all ordeal capabilities via runtime introspection.

    Returns a dict with one key per subsystem — each value is a list of
    dicts describing the available items (faults, invariants, strategies,
    mutation operators/presets).  Everything is derived from the source
    code via ``inspect``; adding a new fault, invariant, or strategy
    makes it appear here automatically.

    Example::

        from ordeal import catalog
        c = catalog()
        for fault in c["faults"]:
            print(f"{fault['qualname']}  -- {fault['doc']}")
        for inv in c["invariants"]:
            print(f"{inv['name']}  -- {inv['doc']}")
    """
    from ordeal.assertions import catalog as _assertions_catalog
    from ordeal.faults import catalog as _faults_catalog
    from ordeal.invariants import catalog as _invariants_catalog
    from ordeal.mutations import catalog as _mutations_catalog
    from ordeal.strategies import catalog as _strategies_catalog

    result = {
        "faults": _faults_catalog(),
        "invariants": _invariants_catalog(),
        "assertions": _assertions_catalog(),
        "strategies": _strategies_catalog(),
        "mutations": _mutations_catalog(),
        "integrations": _introspect_module(
            __import__("ordeal.integrations.openapi", fromlist=["openapi"]),
            include={"chaos_api_test", "with_chaos", "auto_faults"},
        ),
    }
    try:
        result["integrations"].extend(
            _introspect_module(
                __import__("ordeal.integrations.atheris_engine", fromlist=["atheris_engine"]),
                include={"fuzz", "fuzz_chaos_test"},
            )
        )
    except ImportError:
        pass
    return result


def _introspect_module(mod: object, include: set[str] | None = None) -> list[dict]:
    """Introspect public callables from a module."""
    import inspect as _inspect

    entries: list[dict] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_"):
            continue
        if include and attr_name not in include:
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or _inspect.isclass(obj):
            continue
        try:
            sig = str(_inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(...)"
        entries.append(
            {
                "name": attr_name,
                "qualname": f"{mod.__name__}.{attr_name}",
                "signature": sig,
                "doc": (_inspect.getdoc(obj) or "").split("\n")[0],
            }
        )
    return entries


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
