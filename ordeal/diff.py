"""Differential testing with minimized, replay-scoped evidence.

``diff`` gives two revisions isolated copies of the same generated input and
compares their full observable outcome envelope: return or exception, mutated
arguments, bound receiver state, and explicitly selected side effects. A
divergence produces one immutable minimized witness and a JSON-ready evidence
artifact; sampled agreement remains bounded evidence, not equivalence.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "sideeffect.py",
    "executerevision.py",
    "encodesystemreplayevent.py",
    "diff.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "diff"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
