"""ChaosTest — stateful chaos testing powered by Hypothesis.

Subclass ``ChaosTest``, declare faults, write rules, add invariants::

    from ordeal import ChaosTest, rule, invariant, always
    from ordeal.faults import timing, numerical

    class MyServiceChaos(ChaosTest):
        faults = [
            timing.timeout("myservice.api_call"),
            numerical.nan_injection("myservice.score"),
        ]

        @rule()
        def call_service(self):
            result = my_service.process("input")
            always(result is not None, "process never returns None")

        @invariant()
        def no_corruption(self):
            for item in my_service.results():
                always(not math.isnan(item), "no NaN in results")

    # Run with pytest — Hypothesis explores rule sequences + fault schedules
    TestMyServiceChaos = MyServiceChaos.TestCase

The library auto-injects a **nemesis rule** that toggles faults on/off.
Hypothesis explores: which faults fire, when, in what order, interleaved
with your application rules.

**Adaptive fault scheduling** (MOpt-inspired): instead of selecting
faults uniformly at random, the nemesis tracks per-fault *energy*.
Faults that lead to new coverage get boosted (toggled more often);
faults that never discover new edges decay (toggled less).  This is
analogous to AFL++'s MOpt mutator scheduling — the same idea applied
to fault selection instead of byte-level mutations.  When no coverage
collector is attached (the common pytest path), selection falls back
to uniform random.

**Swarm mode** (``swarm = True``): each test case uses a random *subset*
of faults.  Different runs explore different fault combinations, giving
better aggregate coverage than uniform selection (Groce et al., 2012).
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "ruletimeouterror.py",
    "chaostest.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "chaos"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
