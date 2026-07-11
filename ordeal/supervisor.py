"""Deterministic supervisor — control all sources of non-determinism.

The fundamental problem with testing: execution is non-deterministic.
``time.time()`` varies, ``random.random()`` varies, thread scheduling
varies, hash ordering varies.  The same code with the same inputs can
produce different behavior on consecutive runs.

This means:
- Failures may not reproduce (the non-determinism that triggered them is gone)
- State space exploration is inefficient (you revisit "the same" state but
  it behaves differently because of hidden entropy)
- You can't fork from a known state (the fork inherits different entropy)

The ``DeterministicSupervisor`` fixes this by controlling every entropy
source the Python runtime exposes:

1. **RNG seeding** — ``random``, ``buggify``, ``numpy`` (if present) all
   use the same seed.  Given the same seed, fault decisions and generated
   inputs are identical.
2. **Time** — ``time.time()`` and ``time.sleep()`` are replaced with
   ``simulate.Clock``.  Execution timing is deterministic.
3. **Process boundary** — ``subprocess.run`` / ``check_output`` /
   ``Popen`` can be registered against deterministic outputs and the
   supervisor clock.  Child-process interactions become replayable.
4. **Hash randomization** — ``PYTHONHASHSEED`` is logged (can't be changed
   at runtime, but knowing it enables exact reproduction).
5. **Scheduling** — cooperative tasks can ``spawn()``, ``yield_now()``,
   and ``sleep()`` against a seed-driven scheduler.  Same seed = same
   interleaving.  Different seeds explore different schedules.
6. **State trajectory** — every (state_hash, action, next_state_hash)
   transition is logged.  The exploration is a Markov chain that can be
   replayed, forked from any point, and analyzed for unexplored transitions.

The ``DeterministicSupervisor`` controls Python-visible entropy sources and
process interactions. Its scope is intentionally the Python runtime: it does
not control OS scheduling or provide VM-level determinism.

Usage::

    from ordeal.supervisor import DeterministicSupervisor

    with DeterministicSupervisor(seed=42) as sup:
        # All RNGs seeded, time is simulated, trajectory is logged
        result = my_function()
        sup.log_transition("called my_function", state_hash=hash(result))

    with DeterministicSupervisor(seed=42, patch_io=True) as sup:
        sup.register_subprocess(["worker", "--once"], stdout="ok
", delay=2.0)
        # Child process call advances simulated time, not wall clock
        subprocess.check_output(["worker", "--once"])

    def worker(sup, log):
        log.append("start")
        yield sup.yield_now()
        log.append("resume")
        yield sup.sleep(5.0)
        log.append("done")

    with DeterministicSupervisor(seed=42) as sup:
        log = []
        sup.spawn("worker", worker, sup, log)
        sup.run_until_idle()

    # Replay: same seed → same execution
    with DeterministicSupervisor(seed=42) as sup:
        result2 = my_function()
        assert result2 == result  # deterministic

    # Inspect the exploration trajectory
    print(sup.trajectory)  # [(state0, action, state1), ...]

With ChaosTest::

    with DeterministicSupervisor(seed=42) as sup:
        # Hypothesis, buggify, Clock all share the seed
        # Every fault toggle, rule execution, and state transition is logged
        TestCase = chaos_for("myapp.scoring")
        test = TestCase("runTest")
        test.runTest()

    # The trajectory shows exactly which faults fired when
    for prev, action, next_s in sup.trajectory:
        print(f"  {prev:#06x} → {action} → {next_s:#06x}")

Scales with compute: deterministic execution means parallel workers
with different seeds explore genuinely different regions of the state
space.  No wasted compute on redundant paths.  Every seed is a unique,
reproducible exploration trajectory.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "transition.py",
    "enter.py",
    "statetree.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "supervisor"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
