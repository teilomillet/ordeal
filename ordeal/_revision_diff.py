"""Git-revision runner behind the public ``ordeal diff`` CLI.

The in-process public API remains :func:`ordeal.diff.diff`.  This module adds
the revision/worktree orchestration needed by the CLI without creating a
second user-facing ``refactor`` concept.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "revisiondifferror.py",
    "resultfrompayload.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "revisiondiff"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
