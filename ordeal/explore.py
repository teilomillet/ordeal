"""Coverage-guided exploration engine with checkpointing and seed mutation.

It combines stateful rule execution, coverage-guided checkpointing, and
input mutation to:

1. Executes ChaosTest rule sequences (including parameterized rules)
2. Tracks edge coverage of the system under test (AFL-style)
3. **Checkpoints** interesting states when new coverage is found
4. **Branches** from checkpoints — exploring many different actions
   from the same rare state
5. **Mutates** productive rule parameters instead of always generating
   fresh ones — the AFL closed-loop pattern adapted for stateful testing
6. **Shrinks** failing traces to the minimal reproducing sequence
7. **Records traces** for replay and post-hoc analysis

The mutation loop closes the feedback gap between coverage discovery and
input generation.  When a rule execution with specific parameters leads
to new edges, those parameters become seeds on the checkpoint.  On the
next branch from that checkpoint, the explorer sometimes mutates those
seeds instead of generating fresh values via Hypothesis strategies::

    checkpoint restored → select productive seed → mutate params → execute rule
         ↑                                                              ↓
    save checkpoint ← new edges found? ← coverage feedback ← coverage check

This is the same three-dimensional exploration that AFL++ uses — but
adapted for typed, stateful property testing:

- **Swarm** selects which faults are active (the environment)
- **Energy** selects which checkpoint to branch from (the state)
- **Mutation** selects which parameter values to try (the input)

Each dimension is orthogonal: different faults × different states ×
different parameter mutations = coverage at the intersection of features.

See also:

- Zest (Padhye et al., ISSTA 2019): parametric generator mutation for
  structured inputs — the closest published analog, but function-level only
- AFLNet (Pham et al., ICST 2020): stateful protocol fuzzing with
  message-sequence mutation — byte-level, not typed
- ``ordeal.mutagen``: the value-level mutation engine used here

Example::

    from ordeal.explore import Explorer

    explorer = Explorer(
        MyServiceChaos,
        target_modules=["myapp"],
    )
    result = explorer.run(max_time=60)
    print(result.summary())
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "coveragecollector.py",
    "ringwrite.py",
    "propertyeventssince.py",
    "excerptstream.py",
    "savestate.py",
    "loadstate.py",
    "pairwisefaultconfig.py",
    "swarmstats.py",
    "core.py",
    "savecheckpoint.py",
    "core2.py",
    "parallelretryreason.py",
    "workerfn.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "explore"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
