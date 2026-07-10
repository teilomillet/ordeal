"""Pytest plugin for ordeal.

Registers automatically via the ``pytest11`` entry point.  No
configuration needed — pytest discovers it on import.

**What ``--chaos`` activates (3 things):**

1. ``PropertyTracker`` — ``always()``/``sometimes()``/``reachable()``/
   ``unreachable()`` results are recorded for the property report.
   (Note: ``always()`` and ``unreachable()`` raise on violation
   regardless of ``--chaos`` — violations are never silent.)
2. ``buggify()`` — returns ``True`` probabilistically instead of
   always returning ``False``.
3. ``@pytest.mark.chaos`` tests — collected instead of skipped.

**Reliability coverage:** assertions with ``operation`` and ``fault`` record an
operation × fault × property matrix.  The terminal summary prints PASS,
NOT EXERCISED, and FAIL rows.  Under pytest-xdist, workers publish raw counters
and the controller merges them before deriving final statuses.

**What works WITHOUT ``--chaos``:**

- ``ChaosTest.TestCase`` — Hypothesis drives rule/nemesis exploration.
- ``@invariant()`` with ``assert`` — standard Python assertions.
- ``always()`` / ``unreachable()`` — raise on violation (always).
- Faults — the nemesis toggles them regardless.

CLI flags::

    --chaos                 Enable chaos testing mode
    --chaos-seed SEED       Seed for reproducible chaos
    --buggify-prob FLOAT    Probability for buggify() calls (default 0.1)
    --rule-timeout FLOAT    Per-rule timeout for ChaosTest (seconds, default 30; 0 to disable)

Markers::

    @pytest.mark.chaos      Mark a test for chaos mode (skipped without --chaos)

Fixtures::

    chaos_enabled           Activate chaos for a single test (no --chaos needed)
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "pytestaddoption.py",
    "tomlfixtures.py",
    "pytestterminalsummary.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "plugin"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
