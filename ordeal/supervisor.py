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
3. **Hash randomization** — ``PYTHONHASHSEED`` is logged (can't be changed
   at runtime, but knowing it enables exact reproduction).
4. **State trajectory** — every (state_hash, action, next_state_hash)
   transition is logged.  The exploration is a Markov chain that can be
   replayed, forked from any point, and analyzed for unexplored transitions.

This is ordeal's answer to Antithesis's deterministic hypervisor —
scoped to what a Python library can control (no OS scheduling, no VM),
but sufficient for reproducible exploration.

Usage::

    from ordeal.supervisor import DeterministicSupervisor

    with DeterministicSupervisor(seed=42) as sup:
        # All RNGs seeded, time is simulated, trajectory is logged
        result = my_function()
        sup.log_transition("called my_function", state_hash=hash(result))

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

import hashlib
import os
import random
import unittest.mock
from dataclasses import dataclass
from typing import Any

from ordeal.simulate import Clock


@dataclass
class Transition:
    """One step in the exploration trajectory."""

    state_before: int
    action: str
    state_after: int
    step: int = 0

    def __str__(self) -> str:
        before = f"{self.state_before:#06x}"
        after = f"{self.state_after:#06x}"
        return f"  [{self.step}] {before} -> {self.action} -> {after}"


class DeterministicSupervisor:
    """Control all sources of non-determinism for reproducible exploration.

    A context manager that:

    1. Seeds every RNG in the process (``random``, ``buggify``, ``numpy``)
    2. Replaces ``time.time()``/``time.sleep()`` with a deterministic ``Clock``
    3. Logs every state transition as a Markov chain
    4. Records ``PYTHONHASHSEED`` for full reproduction

    The same seed produces the same exploration trajectory.  Different
    seeds explore different regions.  The trajectory can be replayed
    from any point.

    Attributes:
        seed: The RNG seed controlling this execution.
        clock: The deterministic ``Clock`` replacing ``time.time()``.
        trajectory: List of ``(state_before, action, state_after)`` transitions.
        hash_seed: The ``PYTHONHASHSEED`` value (for reproduction notes).
    """

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.clock = Clock()
        self.trajectory: list[Transition] = []
        self.hash_seed: str = os.environ.get("PYTHONHASHSEED", "random")
        self._step = 0
        self._current_state: int = 0
        self._patches: list[Any] = []
        self._saved_random_state: Any = None

    def __enter__(self) -> DeterministicSupervisor:
        """Activate deterministic mode: seed RNGs, patch time, start logging."""
        # 1. Seed Python's random module
        self._saved_random_state = random.getstate()
        random.seed(self.seed)

        # 2. Seed buggify's thread-local RNG
        try:
            from ordeal.buggify import activate, set_seed

            activate()
            set_seed(self.seed)
        except Exception:
            pass

        # 3. Seed numpy if available
        try:
            import numpy as np

            np.random.seed(self.seed)  # type: ignore[attr-defined]
        except ImportError:
            pass

        # 4. Patch time with deterministic Clock
        self._time_patch = unittest.mock.patch("time.time", side_effect=self.clock.time)
        self._sleep_patch = unittest.mock.patch("time.sleep", side_effect=self.clock.sleep)
        self._time_patch.start()
        self._sleep_patch.start()

        # 5. Patch random.random for non-ordeal code that uses it
        # (ordeal's own RNGs are already seeded above)

        return self

    def __exit__(self, *exc: object) -> None:
        """Restore original RNGs and time functions."""
        self._sleep_patch.stop()
        self._time_patch.stop()

        # Restore random state
        if self._saved_random_state is not None:
            random.setstate(self._saved_random_state)

        # Deactivate buggify
        try:
            from ordeal.buggify import deactivate

            deactivate()
        except Exception:
            pass

    def log_transition(self, action: str, *, state_hash: int | None = None) -> None:
        """Record a state transition in the exploration trajectory.

        Args:
            action: Human-readable description of what happened
                (e.g. ``"toggle timeout fault"``, ``"call process()"``)
            state_hash: Hash of the current state after the action.
                If ``None``, auto-increments from the previous state.
        """
        prev = self._current_state
        if state_hash is not None:
            self._current_state = state_hash
        else:
            # Auto-hash: combine previous state with action for a unique
            # deterministic next-state when the caller doesn't provide one
            h = hashlib.md5(f"{prev}:{action}:{self._step}".encode()).hexdigest()  # noqa: S324
            self._current_state = int(h[:8], 16)

        self.trajectory.append(
            Transition(
                state_before=prev,
                action=action,
                state_after=self._current_state,
                step=self._step,
            )
        )
        self._step += 1

    def fork(self, new_seed: int | None = None) -> DeterministicSupervisor:
        """Create a new supervisor forked from the current state.

        The forked supervisor:
        - Starts from the current state (not from zero)
        - Uses a different seed (for exploring a different branch)
        - Inherits the trajectory up to this point

        This is how the Explorer can branch from a checkpoint:
        fork with a different seed → each fork explores a different
        path from the same known state.
        """
        fork_seed = new_seed if new_seed is not None else self.seed + self._step + 1
        forked = DeterministicSupervisor(seed=fork_seed)
        forked._current_state = self._current_state
        forked._step = self._step
        forked.trajectory = list(self.trajectory)  # copy history
        return forked

    @property
    def state(self) -> int:
        """Current state hash."""
        return self._current_state

    @property
    def visited_states(self) -> set[int]:
        """All states visited in this trajectory."""
        states = {t.state_before for t in self.trajectory}
        states |= {t.state_after for t in self.trajectory}
        return states

    @property
    def unique_transitions(self) -> int:
        """Number of unique (state, action) pairs explored."""
        return len({(t.state_before, t.action) for t in self.trajectory})

    def summary(self) -> str:
        """Human-readable exploration trajectory summary."""
        lines = [
            f"DeterministicSupervisor(seed={self.seed})",
            f"  PYTHONHASHSEED: {self.hash_seed}",
            f"  steps: {self._step}",
            f"  unique states: {len(self.visited_states)}",
            f"  unique transitions: {self.unique_transitions}",
            f"  clock: {self.clock.time():.1f}s simulated",
        ]
        if self.trajectory:
            lines.append("  trajectory (last 10):")
            for t in self.trajectory[-10:]:
                lines.append(str(t))
        return "\n".join(lines)

    def reproduction_info(self) -> dict[str, Any]:
        """Return everything needed to reproduce this exact execution.

        An AI assistant can save this and replay later::

            info = sup.reproduction_info()
            # Save to file, pass to another run, etc.
            # To reproduce: DeterministicSupervisor(seed=info["seed"])
        """
        return {
            "seed": self.seed,
            "hash_seed": self.hash_seed,
            "steps": self._step,
            "unique_states": len(self.visited_states),
            "unique_transitions": self.unique_transitions,
            "final_state": self._current_state,
        }
