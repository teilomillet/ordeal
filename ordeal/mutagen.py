"""Value-level mutation — AFL's bit-flip loop for Python values.

Real fuzzers don't generate inputs from scratch.  They start from a
known-good input that reached interesting coverage, then **mutate** it:
flip a bit, swap a byte, nudge a value.  If the mutation reaches NEW
coverage, it becomes a new seed for further mutation.  This is the core
loop that makes AFL and libFuzzer scale with compute.

ordeal's Hypothesis integration generates from type-level strategies
(``st.integers()``, ``st.text()``).  This module adds the complementary
approach: take a **concrete Python value** and perturb it.

The mutation is type-aware at the Python level::

    mutate_value(42, rng)          → 43, 41, 0, -42, 2**31
    mutate_value("admin", rng)     → "bdmin", "admiN", "", "admin\x00"
    mutate_value(3.14, rng)        → 3.15, -3.14, 0.0, float('nan'), float('inf')
    mutate_value([1, 2, 3], rng)   → [1, 3], [1, 2, 3, 0], [1, 99, 3]
    mutate_value(True, rng)        → False

Combined with coverage feedback, this is the closed loop::

    input → fn() → coverage → mutation → new input → fn() → more coverage
                                  ↑                              ↓
                            keep productive                 discard if
                              mutations                    no new edges

The ``mutate_inputs`` function takes a full kwargs dict (like those
in ``MineResult.collected_inputs``) and returns a mutated copy.  Wire
this into mine()'s collection loop to explore near known-good inputs
instead of generating blind random ones.

Scales with compute: each mutation is cheap (O(1) per value).  More
CPU time = more mutations = more coverage, as long as the feedback
loop prunes unproductive ones.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "mutatevalue.py",
    "repairtoconstraint.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "mutagen"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
