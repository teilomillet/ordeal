"""Lossless structural observations and exact replay matching for diff modes.

The differential engines must not use candidate-defined ``__eq__`` or
``__repr__`` methods as evidence.  This module records supported values as a
typed object graph, validates that deep copies do not retain mutable aliases,
and provides the one exact-replay predicate used by every diff mode.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "observationerror.py",
    "observationsequal.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "observation"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
