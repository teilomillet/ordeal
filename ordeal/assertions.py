"""Property assertions inspired by Antithesis.

Four assertion types, keyed by name (message string):

- ``always(condition, name)``   — must hold every time called
- ``sometimes(condition, name)``— must hold at least once across all calls
- ``reachable(name)``           — code path must execute at least once
- ``unreachable(name)``         — code path must never execute

**Violation behavior:**

- ``always`` and ``unreachable`` raise ``AssertionError`` immediately on
  violation — whether or not ``--chaos`` / the tracker is active.
  Violations are never silent.  Pass ``mute=True`` to record without
  raising (tracked in the property report, not hidden).
- ``sometimes`` and ``reachable`` are deferred: they only track when
  the ``PropertyTracker`` is active (``--chaos`` or ``auto_configure()``).
  Without it, they are no-ops.

**Tracker (--chaos) adds:**

- Property report at the end of the session (hit counts, pass/fail).
- Deferred checking for ``sometimes`` and ``reachable``.
- Does NOT control whether ``always``/``unreachable`` raise — they
  always raise on violation regardless.

Use ``declare()`` to register deferred properties up front so they can
fail even when never observed::

    declare("timeout handler runs", "reachable")
    declare("cache warms up", "sometimes")

Each function is simple by default and unlocks depth through parameters::

    always(x > 0, "positive")                                     # fatal
    always(x > 0, "positive", mute=True)                          # tracked, not fatal
    sometimes(is_cached, "cache hit")                              # deferred
    sometimes(lambda: cache.hit_rate() > 0, "cache", attempts=100)# immediate

Add ``operation`` and ``fault`` to record reliability coverage without learning
a second assertion API::

    always(
        charge_count == 1,
        "no_duplicate_charge",
        operation="create_order",
        fault="timeout",
    )

The dimensions are evidence labels; they do not inject a fault.  Contextual
``declare()`` calls register expected cells so zero observations appear as
``NOT EXERCISED``.  ``report()["reliability_coverage"]`` exposes the same
PASS / NOT EXERCISED / FAIL matrix as JSON-safe rows and summary counts.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "property.py",
    "declare.py",
    "always.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "assertions"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
