"""Source-backed evidence-closure planning for :command:`ordeal scan`.

The reliability map joins static seams and candidate properties with the
bounded runtime evidence already held by :mod:`ordeal.state`.  Static
inferences are hypotheses; only executed fault/property cells may PASS or FAIL.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "callname.py",
    "operationrecords.py",
    "runfaultprobe.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "reliability"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
