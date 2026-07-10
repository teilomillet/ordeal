"""Trace recording, serialization, replay, shrinking, ablation, and test generation.

A **Trace** captures every decision the Explorer made during one run:
which rules fired, what parameters were drawn, which faults toggled,
and what coverage was observed.  Traces enable:

- **Replay**: reproduce a failure exactly
- **Shrinking**: minimize a failing trace to the smallest reproducing case
- **Ablation**: determine which faults are necessary for a failure
- **Test generation**: turn traces into standalone pytest test functions
- **Post-hoc analysis**: inspect the full sequence offline

    from ordeal.trace import Trace, replay, shrink, ablate_faults

    trace = Trace.load("run-42.json")
    failure = replay(trace)          # does it reproduce?
    minimal = shrink(trace, MyTest)  # find the smallest version
    faults = ablate_faults(minimal)  # which faults are necessary?
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "tracestep.py",
    "generatetests.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "trace"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
