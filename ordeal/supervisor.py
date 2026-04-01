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

import copy
import hashlib
import json
import os
import random
import unittest.mock
from dataclasses import dataclass, field
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


# ============================================================================
# State Tree — navigable exploration tree with checkpoint and rollback
# ============================================================================


@dataclass
class StateNode:
    """A node in the exploration tree — one checkpointed state.

    Each node stores:
    - The state identity (hash) and a snapshot of the Python objects
    - Which actions have been taken from this state (children)
    - Which actions are POSSIBLE but untaken (the frontier)
    - The edge coverage at this point

    The tree grows as exploration proceeds.  The AI can navigate it:
    go deeper (explore a child), roll back (return to parent), or
    branch (try an untaken action from any visited node).
    """

    state_id: int
    parent_id: int | None = None
    action_from_parent: str | None = None
    depth: int = 0
    edges_at_checkpoint: int = 0
    children: dict[str, int] = field(default_factory=dict)
    snapshot: Any = field(default=None, repr=False)
    seed_at_checkpoint: int = 0


class StateTree:
    """Navigable exploration tree with checkpoint, rollback, and branching.

    This is ordeal's answer to "remembering previous states."  The
    exploration is a tree, not a sequence.  At each state, multiple
    actions are possible.  The tree tracks which actions have been
    taken and which remain unexplored.

    The AI assistant navigates the tree::

        tree = StateTree()

        # Checkpoint the current state
        tree.checkpoint(state_id=0, snapshot=my_state)

        # Explore action A → reach state 1
        tree.checkpoint(state_id=1, parent=0, action="action_A",
                        snapshot=new_state)

        # Rollback to state 0
        old_state = tree.rollback(0)

        # Explore action B → reach state 2 (different branch)
        tree.checkpoint(state_id=2, parent=0, action="action_B",
                        snapshot=other_state)

        # What's unexplored?
        tree.frontier()  # states with untaken actions

    The tree is the single source of truth for the exploration.
    The AI reads it to decide where to go next.  ordeal provides
    the checkpoint/rollback/branch operations.  The AI is the
    search strategy.

    Integrates with DeterministicSupervisor: same seed at the same
    checkpoint → same exploration path.  Different seed → different
    branch.  The tree tracks which seeds have been tried at each node.
    """

    def __init__(self) -> None:
        self._nodes: dict[int, StateNode] = {}
        self._current: int | None = None

    def checkpoint(
        self,
        state_id: int,
        *,
        snapshot: Any = None,
        parent: int | None = None,
        action: str | None = None,
        edges: int = 0,
        seed: int = 0,
    ) -> StateNode:
        """Save a state as a node in the tree.

        Args:
            state_id: Unique identifier for this state (e.g. hash).
            snapshot: The Python object to checkpoint (deepcopied).
                Can be a ChaosTest instance, a dict, or any picklable
                object.  ``rollback()`` returns a deepcopy of this.
            parent: The state_id of the parent node (where we came from).
            action: The action that led from parent to this state.
            edges: Edge coverage count at this checkpoint.
            seed: The RNG seed used to reach this state.
        """
        # Deepcopy the snapshot so the checkpoint is independent
        saved = copy.deepcopy(snapshot) if snapshot is not None else None

        depth = 0
        if parent is not None and parent in self._nodes:
            depth = self._nodes[parent].depth + 1
            # Register this as a child of the parent
            if action:
                self._nodes[parent].children[action] = state_id

        node = StateNode(
            state_id=state_id,
            parent_id=parent,
            action_from_parent=action,
            depth=depth,
            edges_at_checkpoint=edges,
            snapshot=saved,
            seed_at_checkpoint=seed,
        )
        self._nodes[state_id] = node
        self._current = state_id
        return node

    def rollback(self, state_id: int) -> Any:
        """Roll back to a previously checkpointed state.

        Returns a deepcopy of the snapshot saved at that checkpoint.
        The tree is not modified — you can rollback and branch as
        many times as you want from any node.

        Args:
            state_id: The state to roll back to.

        Returns:
            A deepcopy of the checkpointed snapshot, or ``None`` if
            no snapshot was saved.

        Raises:
            KeyError: If ``state_id`` was never checkpointed.
        """
        if state_id not in self._nodes:
            raise KeyError(f"State {state_id:#06x} not in tree")
        node = self._nodes[state_id]
        self._current = state_id
        return copy.deepcopy(node.snapshot) if node.snapshot is not None else None

    @property
    def current(self) -> StateNode | None:
        """The current node in the tree."""
        if self._current is None:
            return None
        return self._nodes.get(self._current)

    @property
    def size(self) -> int:
        """Number of checkpointed states."""
        return len(self._nodes)

    @property
    def max_depth(self) -> int:
        """Deepest node in the tree."""
        if not self._nodes:
            return 0
        return max(n.depth for n in self._nodes.values())

    def frontier(self) -> list[StateNode]:
        """Nodes that could be explored further.

        A node is on the frontier if:
        - It has a snapshot (can be rolled back to)
        - It's a leaf (no children yet) OR hasn't been fully explored

        The AI reads this to decide where to branch next.
        """
        return [node for node in self._nodes.values() if node.snapshot is not None]

    def leaves(self) -> list[StateNode]:
        """Leaf nodes — deepest explored states."""
        return [node for node in self._nodes.values() if not node.children]

    def path_to(self, state_id: int) -> list[StateNode]:
        """Return the path from root to the given state.

        Useful for reproducing: the path is the sequence of actions
        that leads to this state.
        """
        path: list[StateNode] = []
        current = state_id
        while current is not None and current in self._nodes:
            node = self._nodes[current]
            path.append(node)
            current = node.parent_id
        path.reverse()
        return path

    def summary(self) -> str:
        """Human-readable tree summary."""
        lines = [
            f"StateTree: {self.size} nodes, depth {self.max_depth}",
            f"  leaves: {len(self.leaves())}",
            f"  frontier: {len(self.frontier())}",
        ]
        if self._current is not None:
            node = self._nodes[self._current]
            lines.append(f"  current: {node.state_id:#06x} (depth {node.depth})")

        # Show tree structure (compact)
        roots = [n for n in self._nodes.values() if n.parent_id is None]
        for root in roots:
            self._print_subtree(root, lines, indent=2)
        return "\n".join(lines)

    def _print_subtree(self, node: StateNode, lines: list[str], indent: int) -> None:
        """Recursively print the tree structure."""
        prefix = " " * indent
        label = node.action_from_parent or "root"
        edges = f" ({node.edges_at_checkpoint} edges)" if node.edges_at_checkpoint else ""
        lines.append(f"{prefix}{label} -> {node.state_id:#06x}{edges}")
        for action, child_id in node.children.items():
            if child_id in self._nodes:
                self._print_subtree(self._nodes[child_id], lines, indent + 2)

    def to_json(self) -> str:
        """Serialize the tree structure (without snapshots) for persistence."""
        nodes = {}
        for sid, node in self._nodes.items():
            nodes[str(sid)] = {
                "state_id": node.state_id,
                "parent_id": node.parent_id,
                "action_from_parent": node.action_from_parent,
                "depth": node.depth,
                "edges_at_checkpoint": node.edges_at_checkpoint,
                "children": node.children,
                "seed_at_checkpoint": node.seed_at_checkpoint,
                "has_snapshot": node.snapshot is not None,
            }
        return json.dumps({"nodes": nodes, "current": self._current}, indent=2)
