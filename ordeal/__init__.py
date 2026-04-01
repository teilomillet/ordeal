"""ordeal — Automated chaos testing for Python.

Capabilities (each is independent — use one or all):

1. **Bootstrap a project** — one command, zero to validated tests::

    ordeal init                     # auto-detect package, generate everything
    # also: init_project("myapp")   # Python API

2. **Stateful chaos testing** — Hypothesis-powered rule exploration::

    from ordeal import ChaosTest, chaos_test, rule, always
    from ordeal.faults import timing, io

    @chaos_test  # directly pytest-discoverable
    class MyServiceChaos(ChaosTest):
        faults = [timing.timeout("myapp.db.query"), io.error_on_call("myapp.cache.get")]

        @rule()
        def call_service(self):
            result = my_service.process("input")
            always(result is not None, "never returns None")

3. **Property assertions** (Antithesis-style)::

    always(condition, "name")                 # must hold every time — raises immediately
    sometimes(condition, "name")              # must hold at least once — checked at end
    sometimes(condition, "name", warn=True)   # visible in normal pytest (no --chaos)
    reachable("label")                        # code path must execute at least once
    report()                                  # structured pass/fail summary of all properties

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

    from ordeal import mutate

    result = mutate("mymodule.func", preset="standard")  # auto-detects function vs module
    print(result.summary())   # test gaps + how to fix them

    # CLI: ordeal mutate mymodule.func --preset standard

6. **Coverage-guided exploration** — deeper than random testing::

    ordeal explore  # CLI, reads ordeal.toml — checkpoints, energy scheduling

7. **Property mining** — discover what functions actually do::

    from ordeal import mine
    result = mine(my_function)  # discovers likely properties

8. **Module audit** — measure test quality across an entire module::

    from ordeal import audit
    result = audit("myapp.scoring")  # mutation score, property coverage, gaps

9. **Metamorphic testing** — relation-based property checking::

    from ordeal import metamorphic, Relation
    @metamorphic(Relation("negate", transform=lambda x: -x, expect=lambda a, b: a == -b))
    def compute(x: int) -> int: ...

10. **Differential testing** — compare two implementations::

    from ordeal import diff
    result = diff(fn_a, fn_b)  # find inputs where they disagree

11. **Atheris integration** — coverage-guided fuzzing for buggify() decisions::

    from ordeal.integrations.atheris_engine import fuzz
    fuzz(my_function, max_time=60)  # requires: pip install ordeal[atheris]

12. **Discoverability** — introspect all capabilities programmatically::

    from ordeal import catalog
    c = catalog()  # 12 subsystems: faults, mining, audit, auto, mutations, ...
    for key in sorted(c):
        for item in c[key]:
            print(f"{item['qualname']}  -- {item['doc']}")

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

from ordeal.assertions import always, reachable, report, sometimes, unreachable
from ordeal.buggify import buggify, buggify_value
from ordeal.chaos import ChaosTest, chaos_test
from ordeal.mutations import (
    OPERATORS,
    PRESETS,
    MutationResult,
    NoTestsFoundError,
    generate_starter_tests,
    init_project,
    mutate,
    mutate_function_and_test,
)

try:
    __version__ = _get_version("ordeal")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    # Core
    "ChaosTest",
    "chaos_test",
    # Assertions
    "always",
    "sometimes",
    "reachable",
    "unreachable",
    "report",
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
    "mutate",
    "mutate_function_and_test",
    "MutationResult",
    "PRESETS",
    "OPERATORS",
    "NoTestsFoundError",
    "generate_starter_tests",
    "init_project",
    # Everything in _LAZY_SUBMODULES is also importable via
    # ``from ordeal import X`` — see __getattr__ and __dir__.
]

# Submodules whose public exports are re-exported from ordeal.
# Add a public function or class to any of these → it becomes
# importable via ``from ordeal import X`` with zero registration.
_LAZY_SUBMODULES = (
    "ordeal.mine",
    "ordeal.audit",
    "ordeal.auto",
    "ordeal.metamorphic",
    "ordeal.diff",
    "ordeal.scaling",
    "ordeal.state",
)

_SENTINEL = object()


def __getattr__(name: str) -> object:
    """Lazy import: search submodules for the requested name."""
    import importlib

    for mod_path in _LAZY_SUBMODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        obj = getattr(mod, name, _SENTINEL)
        if obj is not _SENTINEL:
            globals()[name] = obj  # cache for subsequent access
            return obj
    raise AttributeError(f"module 'ordeal' has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include lazy submodule exports in dir() for tab completion."""
    import importlib
    import inspect as _inspect

    names = set(globals().keys())
    for mod_path in _LAZY_SUBMODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            obj = getattr(mod, attr, None)
            if obj is None:
                continue
            # Only list things defined in that module (skip re-imports)
            obj_mod = getattr(obj, "__module__", None)
            if obj_mod == mod_path or (_inspect.isclass(obj) and obj_mod == mod_path):
                names.add(attr)
    return sorted(names)


def catalog() -> dict[str, list]:
    """Discover all ordeal capabilities via runtime introspection.

    Returns a dict with one key per subsystem — each value is a list of
    dicts describing the available items.  Keys: ``faults``, ``invariants``,
    ``assertions``, ``strategies``, ``mutations``, ``integrations``,
    ``mining``, ``audit``, ``auto``, ``metamorphic``, ``diff``, ``scaling``.

    Everything is derived from the source code via ``inspect``; adding a new
    fault, invariant, or capability makes it appear here automatically.

    Example::

        from ordeal import catalog
        c = catalog()
        for key in sorted(c):
            print(f"\\n{key}:")
            for item in c[key]:
                print(f"  {item['qualname']}  -- {item['doc']}")
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
        ),
        "mining": _introspect_module(
            __import__("ordeal.mine", fromlist=["mine"]),
        ),
        "audit": _introspect_module(
            __import__("ordeal.audit", fromlist=["audit"]),
        ),
        "auto": _introspect_module(
            __import__("ordeal.auto", fromlist=["auto"]),
        ),
        "metamorphic": _introspect_module(
            __import__("ordeal.metamorphic", fromlist=["metamorphic"]),
        ),
        "diff": _introspect_module(
            __import__("ordeal.diff", fromlist=["diff"]),
        ),
        "scaling": _introspect_module(
            __import__("ordeal.scaling", fromlist=["scaling"]),
        ),
        "exploration": _introspect_module(
            __import__("ordeal.state", fromlist=["state"]),
        ),
    }
    try:
        result["integrations"].extend(
            _introspect_module(
                __import__("ordeal.integrations.atheris_engine", fromlist=["atheris_engine"]),
            )
        )
    except ImportError:
        pass
    return result


def _introspect_module(mod: object, include: set[str] | None = None) -> list[dict]:
    """Introspect public callables from a module.

    Auto-filters re-imports by checking ``__module__`` — only functions
    defined in *mod* are returned.  The *include* allowlist is still
    honoured when given, but should no longer be needed for most modules.
    """
    import inspect as _inspect

    mod_name = getattr(mod, "__name__", "")
    entries: list[dict] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_"):
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or _inspect.isclass(obj):
            continue
        # Skip re-imports: only keep functions defined in this module
        obj_mod = getattr(obj, "__module__", None)
        if obj_mod and obj_mod != mod_name:
            continue
        if include and attr_name not in include:
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
