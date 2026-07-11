"""Compact, claim-scoped evidence cards for user-facing findings."""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "jsonready.py",
    "buildcomposefindingevidence.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "findingevidence"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
