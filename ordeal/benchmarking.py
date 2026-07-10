"""Benchmark-manifest runner for public and private bug corpora.

This module benchmarks the user-facing ``ordeal scan --json`` workflow against
curated bug cases. It supports provenance-backed public reproductions,
original BugsInPy checkouts, and rolling private holdouts with one manifest
format so teams can report public results without optimizing exclusively for a
saturated set.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "jsondump.py",
    "bugbenchmarksuite.py",
    "parsebugbenchmarkmanifest.py",
    "runbugbenchmarkcase.py",
    "artifactcertificationisearned.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "benchmarking"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
