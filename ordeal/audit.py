"""Audit a module's test coverage — current vs ordeal migration.

One command to justify adoption::

    ordeal audit myapp.scoring --test-dir tests/

Output::

    myapp.scoring
      current:  33 tests | 343 lines | 98% coverage [verified]
      migrated: 12 tests | 130 lines | 96% coverage [verified]
      saving:   64% fewer tests | 62% less code | same coverage
      suggest:  L117: test with structured input

**Epistemic guarantees:**

- Every number is either ``[verified]`` or ``FAILED: reason``.
  The audit never silently returns 0%.
- Coverage is measured via coverage.py JSON reports (stable schema),
  not by parsing terminal output (fragile).
- Mined properties state confidence intervals, not "always" claims.
- Every failure mode produces a visible ``warnings`` entry.

**How coverage is measured:**

When ``coverage.py`` is available, the audit runs pytest under its tracer
and parses a structured JSON report. When it is not available, ordeal
falls back to an internal tracer and computes executed/missing lines
directly. Both paths are cross-checked for internal consistency.

**How the migrated test is generated:**

For each public function with type hints, ordeal generates a ``fuzz()``
call (crash-safety test) plus comments describing mined properties.
The generated file is written to ``.ordeal/test_<mod>_migrated.py``
so the developer can inspect, run, and debug it.

**Limitations (stated, not hidden):**

- ``fuzz()`` only checks crash safety, not behavioral correctness.
  The coverage number reflects "lines executed during fuzzing",
  not "lines tested for correct behavior".
- Mined properties are probabilistic (N samples), not proofs.
  The Wilson score interval gives the lower confidence bound.
- Test suggestions are heuristic (source pattern matching).
  They may be wrong if the source changed after coverage was measured.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "formatrelativechange.py",
    "moduleaudit.py",
    "validationworkers.py",
    "moduleaudittodict.py",
    "auditstatehash.py",
    "classtargetcallables.py",
    "normalizeauditfunctioncollection.py",
    "measureauditcoverageswithcoveragepy.py",
    "measurecoveragewithpytestcov.py",
    "measuregeneratedcoveragewithcoverage.py",
    "generatedcallablehelper.py",
    "audit.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "audit"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
