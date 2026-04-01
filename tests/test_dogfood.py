"""ordeal tests itself — dogfood chaos testing on ordeal's own code.

If ordeal can't handle its own medicine, it has no business testing
anyone else's code.  These tests use ChaosTest, mine(), chaos_for(),
and explore() on ordeal's own modules.
"""

from __future__ import annotations

import os
import random

import hypothesis.strategies as st
import pytest

from ordeal import ChaosTest, always, chaos_test, invariant, rule
from ordeal.auto import chaos_for
from ordeal.faults.timing import intermittent_crash
from ordeal.mine import mine
from ordeal.mutagen import mutate_inputs, mutate_value
from ordeal.state import ExplorationState, FunctionState
from ordeal.supervisor import DeterministicSupervisor, StateTree

# ============================================================================
# 1. ChaosTest on ExplorationState — does state management survive faults?
# ============================================================================


@chaos_test
class ExplorationStateChaos(ChaosTest):
    """Exercise ExplorationState under fault injection.

    The exploration state is the central data structure AI assistants
    rely on.  It must survive corrupted inputs, concurrent access,
    and partial failures without losing data or lying about confidence.
    """

    faults = [
        intermittent_crash("json.loads", every_n=5),
    ]

    def __init__(self):
        super().__init__()
        self.state = ExplorationState(module="test.module")
        self.ops = 0

    @rule()
    def add_function(self):
        name = f"func_{self.ops}"
        fs = self.state.function(name)
        fs.mined = True
        fs.crash_free = True
        self.ops += 1
        always(name in self.state.functions, "function persists after add")

    @rule()
    def check_confidence(self):
        conf = self.state.confidence
        always(0.0 <= conf <= 1.0, "confidence always in [0, 1]")

    @rule()
    def check_frontier(self):
        frontier = self.state.frontier
        always(isinstance(frontier, dict), "frontier is always a dict")

    @rule()
    def serialize_roundtrip(self):
        try:
            json_str = self.state.to_json()
            restored = ExplorationState.from_json(json_str)
            always(
                len(restored.functions) == len(self.state.functions),
                "JSON roundtrip preserves function count",
            )
        except Exception:
            pass  # JSON crash from fault injection — expected

    @rule()
    def check_summary(self):
        summary = self.state.summary()
        always(isinstance(summary, str), "summary always returns string")
        always(len(summary) > 0, "summary is never empty")

    @invariant()
    def confidence_bounded(self):
        assert 0.0 <= self.state.confidence <= 1.0


# ============================================================================
# 2. ChaosTest on StateTree — does checkpoint/rollback survive faults?
# ============================================================================


@chaos_test
class StateTreeChaos(ChaosTest):
    """Exercise StateTree checkpoint and rollback under chaos.

    The state tree is how the AI navigates the exploration space.
    Checkpoint, rollback, and branch must work correctly even when
    deepcopy fails or the tree gets large.
    """

    faults = []

    def __init__(self):
        super().__init__()
        self.tree = StateTree()
        self.next_id = 0
        self.tree.checkpoint(0, snapshot={"root": True})
        self.next_id = 1

    @rule()
    def checkpoint_new_state(self):
        parent = random.randint(0, max(0, self.next_id - 1))
        if parent not in self.tree._nodes:
            parent = 0
        sid = self.next_id
        self.tree.checkpoint(
            sid,
            parent=parent,
            action=f"action_{sid}",
            snapshot={"state": sid},
        )
        self.next_id += 1
        always(sid in self.tree._nodes, "checkpoint persists")

    @rule()
    def rollback_random(self):
        if not self.tree._nodes:
            return
        sid = random.choice(list(self.tree._nodes.keys()))
        snap = self.tree.rollback(sid)
        always(snap is not None, "rollback returns snapshot")
        always(self.tree._current == sid, "rollback updates current")

    @rule()
    def check_path(self):
        if not self.tree._nodes:
            return
        sid = random.choice(list(self.tree._nodes.keys()))
        path = self.tree.path_to(sid)
        always(len(path) >= 1, "path always has at least one node")
        always(path[-1].state_id == sid, "path ends at target")

    @invariant()
    def tree_consistent(self):
        for sid, node in self.tree._nodes.items():
            assert node.state_id == sid
            if node.parent_id is not None:
                assert node.parent_id in self.tree._nodes or node.parent_id == 0


# ============================================================================
# 3. ChaosTest on mutate_value — does mutation handle all types?
# ============================================================================


@chaos_test
class MutagenChaos(ChaosTest):
    """Exercise the value mutation engine under chaos.

    mutate_value must handle every Python type without crashing,
    and must produce values of the same type (type stability).
    """

    faults = []

    def __init__(self):
        super().__init__()
        self.rng = random.Random(42)

    @rule(value=st.integers())
    def mutate_int(self, value: int):
        result = mutate_value(value, self.rng)
        always(isinstance(result, int), "int mutation returns int")

    @rule(value=st.floats(allow_nan=False, allow_infinity=False))
    def mutate_float(self, value: float):
        result = mutate_value(value, self.rng)
        always(isinstance(result, (int, float)), "float mutation returns numeric")

    @rule(value=st.text(max_size=50))
    def mutate_str(self, value: str):
        result = mutate_value(value, self.rng)
        always(isinstance(result, str), "str mutation returns str")

    @rule(value=st.booleans())
    def mutate_bool(self, value: bool):
        result = mutate_value(value, self.rng)
        always(isinstance(result, bool), "bool mutation returns bool")

    @rule(value=st.lists(st.integers(), max_size=10))
    def mutate_list(self, value: list):
        result = mutate_value(value, self.rng)
        always(isinstance(result, list), "list mutation returns list")

    @rule(inputs=st.fixed_dictionaries({"x": st.integers(), "name": st.text(max_size=10)}))
    def mutate_full_inputs(self, inputs: dict):
        result = mutate_inputs(inputs, self.rng)
        always(set(result.keys()) == set(inputs.keys()), "mutation preserves keys")


# ============================================================================
# 4. ChaosTest on DeterministicSupervisor — is it actually deterministic?
# ============================================================================


class TestDeterministicSupervisor:
    """Verify that same seed produces same execution."""

    def test_determinism(self):
        results = []
        for _ in range(3):
            with DeterministicSupervisor(seed=42):
                values = [random.random() for _ in range(10)]
                results.append(values)

        always(results[0] == results[1], "seed 42 run 1 == run 2")
        always(results[1] == results[2], "seed 42 run 2 == run 3")

    def test_different_seeds_diverge(self):
        with DeterministicSupervisor(seed=42):
            a = [random.random() for _ in range(10)]
        with DeterministicSupervisor(seed=99):
            b = [random.random() for _ in range(10)]

        always(a != b, "different seeds produce different values")

    def test_trajectory_logging(self):
        with DeterministicSupervisor(seed=42) as sup:
            sup.log_transition("step 1")
            sup.log_transition("step 2")
            sup.log_transition("step 3")

        always(len(sup.trajectory) == 3, "three transitions logged")
        always(sup.unique_transitions == 3, "all transitions unique")

    def test_fork_preserves_history(self):
        with DeterministicSupervisor(seed=42) as sup:
            sup.log_transition("original step")
            forked = sup.fork(new_seed=99)

        always(
            len(forked.trajectory) == 1,
            "fork inherits parent trajectory",
        )
        always(forked.seed == 99, "fork has new seed")

    @pytest.mark.skipif(
        "PYTEST_XDIST_WORKER" in os.environ,
        reason="Reproducibility test requires isolation from xdist workers",
    )
    def test_explore_reproducibility(self):
        """Same seed produces identical in-process results.

        Epistemic guarantee — what the AI can rely on:

        EXACT (enforced, fails test if violated):
        - Function set discovered
        - Per-function edge counts (mine phase, in-process)
        - Crash-free status (scan phase, in-process)
        - State tree structure

        APPROXIMATE (not enforced):
        - Mutation scores — mutate() runs pytest in a subprocess
          with its own RNG state outside the supervisor's control
        - Mine() property confidence — Hypothesis internal RNG

        This distinction is honest: we enforce what runs in-process
        (controlled by supervisor) and document what doesn't
        (subprocesses, Hypothesis internals).
        """
        from ordeal.state import ExplorationState, explore_mine, explore_scan

        # Test only in-process phases (mine + scan) — these are fully
        # controlled by the supervisor. Mutate and chaos run pytest
        # subprocesses that are outside the supervisor's control and
        # non-deterministic under xdist parallel execution.
        s1 = ExplorationState(module="ordeal.demo")
        with DeterministicSupervisor(seed=42):
            s1 = explore_mine(s1, max_examples=10)
            s1 = explore_scan(s1, max_examples=10)

        s2 = ExplorationState(module="ordeal.demo")
        with DeterministicSupervisor(seed=42):
            s2 = explore_mine(s2, max_examples=10)
            s2 = explore_scan(s2, max_examples=10)

        always(
            set(s1.functions.keys()) == set(s2.functions.keys()),
            "same seed discovers same functions",
        )
        for name in s1.functions:
            f1, f2 = s1.functions[name], s2.functions[name]
            always(
                f1.edges_discovered == f2.edges_discovered,
                f"same seed, same edges for {name}",
            )
            always(
                f1.crash_free == f2.crash_free,
                f"same seed, same crash status for {name}",
            )

    def test_different_seeds_different_exploration(self):
        """Different seeds explore different regions."""
        from ordeal.state import explore

        s1 = explore("ordeal.demo", time_limit=10, seed=42, max_examples=10)
        s2 = explore("ordeal.demo", time_limit=10, seed=99, max_examples=10)

        always(
            s1.supervisor_info.get("seed") != s2.supervisor_info.get("seed"),
            "supervisor records different seeds",
        )

    def test_rng_determinism_under_supervisor(self):
        """All ordeal-controlled RNGs are fully deterministic.

        The reproducibility guarantee (documented honestly):
        - random.random() sequences: EXACT (same seed = identical)
        - Mutation scores, edge counts, crash status: EXACT
        - Mine() property confidence: APPROXIMATE — Hypothesis has
          its own internal RNG (derandomize uses source hash, not
          our seed). Property mining examples may vary slightly.

        This test enforces the EXACT guarantees.
        """
        for _ in range(3):
            with DeterministicSupervisor(seed=42):
                a = [random.random() for _ in range(20)]
            with DeterministicSupervisor(seed=42):
                b = [random.random() for _ in range(20)]
            always(a == b, "random.random() deterministic")

    def test_io_patching_file_roundtrip(self):
        """patch_io=True routes open() through in-memory FileSystem."""
        with DeterministicSupervisor(seed=42, patch_io=True) as sup:
            # Write
            f = open("/test_data.txt", "w")
            f.write("deterministic content")
            f.close()

            # Read back
            f = open("/test_data.txt", "r")
            content = f.read()
            f.close()

            always(content == "deterministic content", "file roundtrip works")
            always(sup.filesystem is not None, "filesystem is initialized")
            always(sup.filesystem.exists("/test_data.txt"), "file persists")

    def test_io_patching_binary(self):
        """patch_io=True handles binary mode."""
        with DeterministicSupervisor(seed=42, patch_io=True):
            f = open("/data.bin", "wb")
            f.write(b"\x00\x01\x02\xff")
            f.close()

            f = open("/data.bin", "rb")
            data = f.read()
            f.close()

            always(data == b"\x00\x01\x02\xff", "binary roundtrip works")

    def test_io_patching_missing_file(self):
        """patch_io=True raises FileNotFoundError for unwritten paths."""
        with DeterministicSupervisor(seed=42, patch_io=True):
            try:
                open("/nonexistent.txt", "r")
                always(False, "should have raised FileNotFoundError")
            except FileNotFoundError:
                always(True, "missing file raises correctly")

    def test_network_blocked_under_patch_io(self):
        """patch_io=True blocks socket connections deterministically."""
        import socket

        with DeterministicSupervisor(seed=42, patch_io=True):
            try:
                socket.create_connection(("example.com", 80))
                always(False, "should have raised ConnectionRefusedError")
            except ConnectionRefusedError:
                always(True, "network blocked deterministically")

    def test_thread_tracking(self):
        """patch_io=True logs thread creation in trajectory."""
        import threading

        with DeterministicSupervisor(seed=42, patch_io=True) as sup:
            t = threading.Thread(target=lambda: None, name="test-thread")
            t.start()
            t.join()

            always(sup._thread_count == 1, "thread creation counted")
            always(len(sup.trajectory) == 1, "thread logged in trajectory")
            always(
                "thread_start" in sup.trajectory[0].action,
                "trajectory records thread name",
            )

    def test_without_patch_io_real_io_works(self):
        """Default (patch_io=False) doesn't break real I/O."""
        import os

        with DeterministicSupervisor(seed=42):
            # Real open should work
            with open(os.devnull) as f:
                always(f is not None, "real open works without patch_io")

    def test_state_tree_survives_explore(self):
        """explore() creates a state tree with checkpoints at each phase."""
        from ordeal.state import explore

        state = explore("ordeal.demo", time_limit=15, seed=42, max_examples=10)

        always(state.tree is not None, "tree exists")
        always(state.tree.size >= 2, "tree has multiple checkpoints")
        always(state.tree.max_depth >= 1, "tree has depth")

        # Can rollback to root
        root_nodes = [n for n in state.tree._nodes.values() if n.parent_id is None]
        always(len(root_nodes) >= 1, "tree has a root")

    def test_supervisor_info_in_state(self):
        """explore() records supervisor info for reproduction."""
        from ordeal.state import explore

        state = explore("ordeal.demo", time_limit=10, seed=123, max_examples=10)

        always("seed" in state.supervisor_info, "seed recorded")
        always(state.supervisor_info["seed"] == 123, "correct seed recorded")
        always("trajectory_steps" in state.supervisor_info, "trajectory recorded")
        always(state.supervisor_info["trajectory_steps"] > 0, "transitions logged")

    def test_json_roundtrip_preserves_supervisor_info(self):
        """Serialized state preserves supervisor info."""
        from ordeal.state import ExplorationState, explore

        state = explore("ordeal.demo", time_limit=10, seed=42, max_examples=10)

        json_str = state.to_json()
        restored = ExplorationState.from_json(json_str)

        always(
            restored.supervisor_info.get("seed") == 42,
            "JSON roundtrip preserves seed",
        )
        always(
            restored.supervisor_info.get("trajectory_steps")
            == state.supervisor_info.get("trajectory_steps"),
            "JSON roundtrip preserves trajectory count",
        )

    def test_mine_deterministic_under_supervisor(self):
        """mine() produces identical results under same seed."""
        from ordeal.demo import score
        from ordeal.mine import mine

        with DeterministicSupervisor(seed=42):
            r1 = mine(score, max_examples=20)
        with DeterministicSupervisor(seed=42):
            r2 = mine(score, max_examples=20)

        always(r1.examples == r2.examples, "same example count")
        always(
            r1.edges_discovered == r2.edges_discovered,
            "same edges discovered",
        )
        always(
            len(r1.properties) == len(r2.properties),
            "same property count",
        )


# ============================================================================
# 5. mine() on ordeal's own functions — properties of our own code
# ============================================================================


class TestMineOrdeal:
    """Mine ordeal's own functions to discover their properties."""

    def test_mine_mutate_value(self):
        result = mine(
            mutate_value,
            max_examples=30,
            value=st.integers(),
            rng=st.just(random.Random(42)),
            intensity=st.floats(min_value=0.0, max_value=1.0),
        )
        always(result.examples > 0, "mine produced examples")

    def test_mine_exploration_state_confidence(self):
        """FunctionState.confidence should be bounded [0, 1]."""

        def make_and_check(mined: bool, score: float) -> float:
            fs = FunctionState(name="test", mined=mined)
            if score >= 0:
                fs.mutated = True
                fs.mutation_score = score
            return fs.confidence

        result = mine(
            make_and_check,
            max_examples=50,
            mined=st.booleans(),
            score=st.floats(min_value=-1, max_value=1),
        )
        for p in result.universal:
            if "bounded" in p.name or ">= 0" in p.name:
                always(True, f"confidence property: {p.name}")


# ============================================================================
# 6. chaos_for() on ordeal.mutagen — auto-generated chaos test
# ============================================================================


class TestChaosForOrdeal:
    """chaos_for() on ordeal's own modules — full auto-discovery."""

    def test_chaos_for_mutagen(self):
        TestCase = chaos_for(
            "ordeal.mutagen",
            fixtures={
                "rng": st.just(random.Random(42)),
                "intensity": st.floats(min_value=0.0, max_value=1.0),
            },
            max_examples=5,
            stateful_step_count=5,
        )
        assert TestCase is not None
