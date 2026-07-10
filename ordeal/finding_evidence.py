"""Compact, claim-scoped evidence cards for user-facing findings."""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "jsonready.py",
    "buildcomposefindingevidence.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "findingevidence"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
