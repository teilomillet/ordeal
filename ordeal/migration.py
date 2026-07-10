"""Evidence-first migration workflow from a base module to a candidate.

The workflow composes ordeal's existing capabilities in one ordered run:

``audit base -> mine candidate -> diff -> classify -> save -> mutate -> scan``

Mined properties remain candidate contracts. They are never promoted to
correctness claims. Explicit :class:`ordeal.auto.ContractCheck` instances are
the domain assertions used by mutation testing and the candidate-only scan.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "migrationstage.py",
    "caseforchange.py",
    "migrate.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "migration"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
