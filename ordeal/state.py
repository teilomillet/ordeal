"""Unified exploration state — what ordeal knows about your code.

Every ordeal tool (mine, mutate, scan, chaos_for, fuzz, explore)
explores one dimension of the state space.  ``ExplorationState``
accumulates their results into a single, persistent, queryable
picture.  AI assistants read this to decide what to explore next.

Quick start — explore everything in one pass::

    from ordeal.state import explore

    state = explore("myapp.scoring")
    print(state.confidence)       # 0.72
    print(state.frontier)         # what's unexplored
    print(state.findings)         # bugs and anomalies

Resume exploration (state persists)::

    state = explore("myapp.scoring", state=state)
    print(state.confidence)       # 0.89 — growing

Use tools individually — they enrich the same state::

    from ordeal import mine, mutate
    state = ExplorationState("myapp.scoring")
    state = explore_mine(state)
    state = explore_mutate(state)
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "sourcehash.py",
    "explorationstate.py",
    "explorescan.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "state"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
