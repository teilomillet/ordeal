"""Replay one operation-and-fault story against two system versions.

Pass ``Operation`` and ``FaultEvent`` objects to ``diff(..., sequence=...)``.
The resulting ``SystemDiffResult`` separates semantic parity from an optional
``PerformanceBudget``. Start with ``docs/concepts/system-differential.md`` for
the mental model and ``docs/guides/system-differential.md`` for a complete run.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "operation.py",
    "cloneevent.py",
    "diffsystem.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "systemdiff"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
