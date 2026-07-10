"""ordeal — explores the state space of Python code.

Discovers properties, tests mutations, injects faults, tracks coverage.
Each tool explores one dimension; together they build confidence that
code behaves correctly under all reachable conditions.

``catalog()`` returns every capability at runtime.
``explore(module)`` runs all exploration strategies on a module.
``ordeal.demo`` is a sandbox — any tool works on it.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "sourcetreeversion.py",
    "annotatecatalogentry.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "init"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
