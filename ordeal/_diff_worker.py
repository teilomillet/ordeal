"""Private subprocess worker for revision-isolated differential testing.

This module deliberately avoids importing :mod:`ordeal` at startup.  When the
project being compared is ordeal itself, importing the installed package would
otherwise shadow the checked-out revision in the temporary worktree.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "targetresolutionerror.py",
    "outcomesequal.py",
    "prepare.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "diffworker"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
