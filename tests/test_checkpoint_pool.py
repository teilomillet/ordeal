"""Tests for shared checkpoint pool — verifying checkpoints flow between workers.

Tests both the mechanics (pickle round-trip, file exchange) and the
exploration benefit (deeper states reached with sharing).
"""

from __future__ import annotations

import copy
import pickle
import tempfile
from pathlib import Path
from typing import ClassVar

from ordeal import ChaosTest, rule
from ordeal.explore import Explorer
from ordeal.faults import Fault, LambdaFault
from tests._deep_target import DeepService

# ============================================================================
# ChaosTest for the deep target
# ============================================================================


class DeepServiceChaos(ChaosTest):
    """ChaosTest that explores DeepService's 4-phase state machine."""

    faults: ClassVar[list[Fault]] = [
        LambdaFault("reset_fault", lambda: None, lambda: None),
    ]

    def __init__(self):
        super().__init__()
        self.service = DeepService()

    @rule()
    def do_accumulate(self):
        self.service.accumulate()

    @rule()
    def do_pivot(self):
        self.service.pivot()

    @rule()
    def do_climb(self):
        self.service.climb()

    @rule()
    def do_strike(self):
        self.service.strike()

    @rule()
    def do_noop(self):
        self.service.noop()

    @rule()
    def do_reset(self):
        self.service.reset_state()

    def teardown(self):
        self.service = DeepService()
        super().teardown()


# ============================================================================
# Pickle round-trip tests
# ============================================================================


class TestStatePickleRoundTrip:
    """Verify ChaosTest state dicts survive pickle for checkpoint sharing.

    We pickle __dict__ (not the whole machine) because Hypothesis-decorated
    methods break pickle's identity check. Reconstruction: cls() + update().
    """

    def _round_trip(self, machine):
        """Simulate what _pool_publish + _pool_subscribe does."""
        snapshot = copy.deepcopy(machine)
        state = {}
        for k, v in snapshot.__dict__.items():
            try:
                pickle.dumps(v)
                state[k] = v
            except Exception:
                pass
        data = pickle.dumps(state)
        restored = machine.__class__()
        restored.__dict__.update(pickle.loads(data))
        return restored

    def test_state_dict_pickles(self):
        """Instance state survives pickle round-trip."""
        machine = DeepServiceChaos()
        machine.service.accumulate()
        machine.service.accumulate()

        restored = self._round_trip(machine)

        assert restored.service.counter == 2
        assert restored.service.state == "idle"

    def test_faults_reinitialized_on_restore(self):
        """Faults with lambdas can't be pickled — they're reinitialized.

        This is correct: the checkpoint's value is the service state
        (the rare state to branch from), not the fault schedule. The
        nemesis rule will toggle faults independently in each worker.
        """
        machine = DeepServiceChaos()
        machine._faults[0].activate()

        restored = self._round_trip(machine)

        # Faults are reinitialized (fresh from __init__), not preserved
        assert not restored._faults[0].active
        # But service state IS preserved
        assert restored.service.counter == machine.service.counter

    def test_deep_state_preserved(self):
        """A machine in phase 2+ survives round-trip and is explorable."""
        machine = DeepServiceChaos()
        for _ in range(6):
            machine.service.accumulate()
        machine.service.pivot()
        assert machine.service.state == "pivoted"

        restored = self._round_trip(machine)
        assert restored.service.state == "pivoted"

        # Continue exploring from restored state
        restored.service.climb()
        restored.service.climb()
        restored.service.climb()
        restored.service.strike()
        assert restored.service.bug_triggered


# ============================================================================
# File-based checkpoint exchange
# ============================================================================


class TestCheckpointFileExchange:
    """Verify the publish/subscribe file mechanism works."""

    def test_publish_creates_file(self):
        """_pool_publish writes a pickle file to the pool directory."""
        explorer = Explorer(DeepServiceChaos, workers=1)
        pool_dir = Path(tempfile.mkdtemp(prefix="test-pool-"))

        try:
            explorer._pool_dir = pool_dir
            explorer._worker_id = 0
            explorer._max_pool_publish = 5

            machine = DeepServiceChaos()
            for _ in range(6):
                machine.service.accumulate()
            machine.service.pivot()

            explorer._pool_publish(machine, new_count=3, step=5, run_id=1)

            files = list(pool_dir.glob("cp-*.pkl"))
            assert len(files) == 1
            assert "w0" in files[0].name

            # Verify the file is a valid snapshot payload
            payload = pickle.loads(files[0].read_bytes())
            assert isinstance(payload, dict)
            assert "state_dict" in payload
            assert "fault_active" in payload
            assert payload["state_dict"]["service"].state == "pivoted"
        finally:
            import shutil

            shutil.rmtree(pool_dir, ignore_errors=True)

    def test_subscribe_loads_other_workers_checkpoints(self):
        """_pool_subscribe loads checkpoints published by other workers."""
        pool_dir = Path(tempfile.mkdtemp(prefix="test-pool-"))

        try:
            # Worker 0 publishes
            publisher = Explorer(DeepServiceChaos, workers=1)
            publisher._pool_dir = pool_dir
            publisher._worker_id = 0
            publisher._max_pool_publish = 5

            machine = DeepServiceChaos()
            for _ in range(6):
                machine.service.accumulate()
            machine.service.pivot()
            publisher._pool_publish(machine, new_count=3, step=5, run_id=1)

            # Worker 1 subscribes
            subscriber = Explorer(DeepServiceChaos, workers=1)
            subscriber._pool_dir = pool_dir
            subscriber._worker_id = 1
            subscriber._last_pool_sync = 0  # force immediate sync

            assert len(subscriber._checkpoints) == 0
            subscriber._pool_subscribe()
            assert len(subscriber._checkpoints) == 1

            # The loaded checkpoint should be in pivoted state
            cp = subscriber._checkpoints[0]
            restored = subscriber._restore_machine(cp.snapshot)
            assert restored.service.state == "pivoted"

        finally:
            import shutil

            shutil.rmtree(pool_dir, ignore_errors=True)

    def test_publish_respects_max_limit(self):
        """Workers stop publishing after max_pool_publish checkpoints."""
        pool_dir = Path(tempfile.mkdtemp(prefix="test-pool-"))

        try:
            explorer = Explorer(DeepServiceChaos, workers=1)
            explorer._pool_dir = pool_dir
            explorer._worker_id = 0
            explorer._max_pool_publish = 3

            machine = DeepServiceChaos()
            for i in range(5):
                explorer._pool_publish(machine, new_count=3, step=i, run_id=i)

            files = list(pool_dir.glob("cp-*.pkl"))
            assert len(files) == 3  # capped at max
        finally:
            import shutil

            shutil.rmtree(pool_dir, ignore_errors=True)

    def test_subscribe_skips_own_checkpoints(self):
        """Workers don't load their own published checkpoints."""
        pool_dir = Path(tempfile.mkdtemp(prefix="test-pool-"))

        try:
            explorer = Explorer(DeepServiceChaos, workers=1)
            explorer._pool_dir = pool_dir
            explorer._worker_id = 0
            explorer._max_pool_publish = 5
            explorer._last_pool_sync = 0

            machine = DeepServiceChaos()
            explorer._pool_publish(machine, new_count=3, step=0, run_id=1)

            # Same worker subscribes — should skip its own file
            explorer._pool_subscribe()
            assert len(explorer._checkpoints) == 0
        finally:
            import shutil

            shutil.rmtree(pool_dir, ignore_errors=True)

    def test_unpicklable_checkpoint_skipped_gracefully(self):
        """If a checkpoint can't be pickled, publish silently skips it."""
        pool_dir = Path(tempfile.mkdtemp(prefix="test-pool-"))

        try:
            explorer = Explorer(DeepServiceChaos, workers=1)
            explorer._pool_dir = pool_dir
            explorer._worker_id = 0
            explorer._max_pool_publish = 5

            # Create a machine with an unpicklable attribute
            machine = DeepServiceChaos()
            machine._unpicklable = lambda: None  # lambdas can't be pickled

            explorer._pool_publish(machine, new_count=3, step=0, run_id=1)

            # Should not crash — the important thing is no exception was raised
            list(pool_dir.glob("cp-*.pkl"))  # verify dir is still valid
        finally:
            import shutil

            shutil.rmtree(pool_dir, ignore_errors=True)


# ============================================================================
# Parallel exploration with shared pool
# ============================================================================


class TestParallelWithPool:
    """Verify parallel exploration actually uses the checkpoint pool."""

    def test_parallel_runs_with_pool(self):
        """Parallel exploration with pool enabled runs successfully."""
        explorer = Explorer(
            DeepServiceChaos,
            target_modules=["tests._deep_target"],
            workers=2,
            seed=42,
            share_checkpoints=True,
        )
        result = explorer.run(max_time=3, steps_per_run=20)
        assert result.total_runs > 0
        assert result.total_steps > 0

    def test_parallel_runs_without_pool(self):
        """Parallel exploration with pool disabled still works."""
        explorer = Explorer(
            DeepServiceChaos,
            target_modules=["tests._deep_target"],
            workers=2,
            seed=42,
            share_checkpoints=False,
        )
        result = explorer.run(max_time=3, steps_per_run=20)
        assert result.total_runs > 0
        assert result.total_steps > 0


class TestPoolAblation:
    """Ablation: does the checkpoint pool actually help?

    Compares the same number of workers (2) with pool on vs off,
    same time budget, same seeds.  This isolates the pool's
    contribution from parallelism itself.

    The pool helps because:
    - Worker A discovers phase-1 state and publishes it
    - Worker B loads that checkpoint and explores from phase 1
    - Without the pool, Worker B must independently reach phase 1

    On the DeepService target (4-phase gated state machine), this
    matters because each phase gate requires 5+ correct sequential
    steps — reaching phase 2 from scratch is much harder than
    branching from a shared phase-1 checkpoint.
    """

    def test_pool_does_not_hurt(self):
        """Pool-enabled should find >= 90% of pool-disabled edges.

        Conservative test: the pool shouldn't make things worse.
        Sharing checkpoints adds overhead (pickle, filesystem I/O),
        so this ensures the overhead doesn't dominate.
        """
        trials = 3
        with_pool: list[int] = []
        without_pool: list[int] = []

        for trial in range(trials):
            seed = 200 + trial * 53

            exp_on = Explorer(
                DeepServiceChaos,
                target_modules=["tests._deep_target"],
                workers=2,
                seed=seed,
                share_checkpoints=True,
            )
            res_on = exp_on.run(max_time=3, steps_per_run=25)
            with_pool.append(res_on.unique_edges)

            exp_off = Explorer(
                DeepServiceChaos,
                target_modules=["tests._deep_target"],
                workers=2,
                seed=seed,
                share_checkpoints=False,
            )
            res_off = exp_off.run(max_time=3, steps_per_run=25)
            without_pool.append(res_off.unique_edges)

        avg_on = sum(with_pool) / trials
        avg_off = sum(without_pool) / trials

        assert avg_on >= avg_off * 0.9, (
            f"Pool should not hurt: with={avg_on:.1f}, "
            f"without={avg_off:.1f}"
        )
