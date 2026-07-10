"""Mutation testing — validate that your tests catch real bugs.

Generates mutated versions of target code and runs tests against each.
If a mutant survives (tests still pass), the tests are missing something.

Quick start
-----------

Pick a preset and go — tests are auto-discovered via pytest::

    from ordeal import mutate_function_and_test

    result = mutate_function_and_test("myapp.scoring.compute", preset="standard")
    print(result.summary())   # shows test gaps + how to fix them

Or from the command line::

    ordeal mutate myapp.scoring.compute                # standard preset
    ordeal mutate myapp.scoring.compute -p essential    # fast check (4 operators)
    ordeal mutate myapp.scoring.compute -p thorough     # all 14 operators

Presets
-------

Each preset is a curated set of mutation operators — pick the level
that matches your situation:

- ``"essential"`` (4 ops) — arithmetic, comparison, negate, return_none.
  Catches wrong math, wrong comparisons, flipped conditions, and missing
  return values. Fast; good for first-time use and quick feedback loops.

- ``"standard"`` (8 ops) — essential + boundary, constant, logical,
  delete_statement. Adds off-by-one errors, magic numbers, and/or logic,
  and dead code detection. **Recommended default for CI.**

- ``"thorough"`` (14 ops) — every operator. Adds exception swallowing,
  argument swaps, break/continue swaps, and more. Use before releases
  or when you want comprehensive validation.

You can also pass ``operators=["arithmetic", "comparison"]`` for full
control — but ``preset`` and ``operators`` are mutually exclusive.

Entry points
------------

1. **Function-level** (recommended) — ``mutate_function_and_test()``
2. **Module-level** — ``mutate_and_test()``
3. **CLI** — ``ordeal mutate <target>``
4. **Config** — ``[mutations]`` section in ``ordeal.toml``

Reading the output
------------------

``result.summary()`` prints each surviving mutant with:

- **Location** — file line and column of the mutation.
- **Description** — what was changed (e.g. ``+ -> -``).
- **Fix guidance** — exactly what test to write to kill this mutant.

Discover all operators and presets programmatically::

    from ordeal.mutations import catalog
    for entry in catalog():
        print(f"{entry['name']} ({entry['type']})  -- {entry['doc']}")
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "normalizevalidationmode.py",
    "normalizesemantictag.py",
    "core.py",
    "propertystrength.py",
    "harden.py",
    "reviewannotationexpr.py",
    "pinassertion.py",
    "discovermodules.py",
    "boundaryapplicator.py",
    "bytecodeequal.py",
    "mutatedmodule.py",
    "scoremutationtestfile.py",
    "validationmatrix.py",
    "modulemineoraclefallback.py",
    "isruntimeequivalent.py",
    "mutatefunctionandtest.py",
    "parallelfunctiontest.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "mutations"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
