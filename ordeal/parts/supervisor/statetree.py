from __future__ import annotations
# ruff: noqa
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
