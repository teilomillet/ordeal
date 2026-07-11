"""Git-revision runner behind the public ``ordeal diff`` CLI.

The in-process public API remains :func:`ordeal.diff.diff`.  This module adds
the revision/worktree orchestration needed by the CLI without creating a
second user-facing ``refactor`` concept.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "revisiondifferror.py",
    "resultfrompayload.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "revisiondiff"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
