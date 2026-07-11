"""Benchmark-manifest runner for public and private bug corpora.

This module benchmarks the user-facing ``ordeal scan --json`` workflow against
curated bug cases. It supports provenance-backed public reproductions,
original BugsInPy checkouts, and rolling private holdouts with one manifest
format so teams can report public results without optimizing exclusively for a
saturated set.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

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
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
