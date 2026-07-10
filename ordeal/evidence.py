"""Executable, source-backed evidence records for benchmark cases."""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "stablejson.py",
    "verifybugevidence.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "evidence"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
