"""Tests for shared checkpoint pool — verifying checkpoints flow between workers.

Tests both the mechanics (pickle round-trip, ring buffer exchange, energy
propagation) and the exploration benefit (deeper states reached with sharing).
"""

from __future__ import annotations

import copy
import pickle
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ordeal import ChaosTest, rule
from ordeal.explore import (
    _POOL_HEADER_SIZE,
    _POOL_RING_SIZE,
    _POOL_SLOT_DATA_MAX,
    Explorer,
    _pool_encode_payload,
    _ring_read,
    _ring_update_energy,
    _ring_write,
)
from ordeal.faults import Fault, LambdaFault
from ordeal.trace import Trace, TraceFailure, TraceStep
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


class SnapshotHookChaos(ChaosTest):
    """ChaosTest that exercises snapshot filtering and restore hooks."""

    faults: ClassVar[list[Fault]] = []

    def __init__(self):
        super().__init__()
        self.state = "idle"
        self.ephemeral = object()
        self.restore_marker = False

    @rule()
    def tick(self):
        self.state = "busy"

    def checkpoint_snapshot_filter(self, name: str, value: object) -> bool:
        return name != "ephemeral"

    def restore_checkpoint_state(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        self.ephemeral = "recreated"
        self.restore_marker = True


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

    def test_snapshot_hooks_filter_and_restore_ephemeral_state(self):
        explorer = Explorer(SnapshotHookChaos, workers=1)
        machine = SnapshotHookChaos()
        machine.tick()

        snapshot = explorer._snapshot_machine(machine)
        assert "ephemeral" not in snapshot.state_dict

        restored = explorer._restore_machine(snapshot)
        assert restored.state == "busy"
        assert restored.ephemeral == "recreated"
        assert restored.restore_marker is True


class _HookedSnapshotChaos(ChaosTest):
    """ChaosTest exercising explicit checkpoint snapshot/restore hooks."""

    faults: ClassVar[list[Fault]] = []

    def __init__(self):
        super().__init__()
        self.counter = 3
        self.client = object()
        self.rebuilt = False

    def checkpoint_snapshot_filter(self, name: str, value: object) -> bool:
        return super().checkpoint_snapshot_filter(name, value) and name != "client"

    def restore_checkpoint_state(self, snapshot: dict[str, object]) -> None:
        super().restore_checkpoint_state(snapshot)
        self.client = "recreated"
        self.rebuilt = True


class TestCheckpointHooks:
    def test_snapshot_filter_and_restore_hook(self):
        explorer = Explorer(_HookedSnapshotChaos, workers=1)
        machine = _HookedSnapshotChaos()

        snapshot = explorer._snapshot_machine(machine)
        restored = explorer._restore_machine(snapshot)

        assert "client" not in snapshot.state_dict
        assert restored.counter == 3
        assert restored.client == "recreated"
        assert restored.rebuilt is True


# ============================================================================
# Ring buffer primitive tests
# ============================================================================


def _make_ring() -> bytearray:
    """Create a zeroed ring buffer for testing."""
    return bytearray(_POOL_RING_SIZE)


class TestRingBufferPrimitives:
    """Verify the low-level ring buffer read/write/energy helpers."""

    def test_write_then_read(self):
        """A written slot is readable and contains correct data."""
        buf = _make_ring()
        data = pickle.dumps({"state_dict": {"x": 42}, "fault_active": {}})

        ok = _ring_write(
            memoryview(buf), slot=0, seq=1, writer_id=0, energy=2.5, data=data, new_edges=3, step=7
        )
        assert ok

        entry = _ring_read(memoryview(buf), slot=0)
        assert entry is not None
        assert entry["sequence"] == 1
        assert entry["writer_id"] == 0
        assert abs(entry["energy"] - 2.5) < 0.001
        assert entry["new_edge_count"] == 3
        assert entry["step"] == 7
        assert entry["slot"] == 0

        payload = pickle.loads(entry["data"])
        assert payload["state_dict"]["x"] == 42

    def test_empty_slot_returns_none(self):
        """An unwritten (zeroed) slot returns None."""
        buf = _make_ring()
        assert _ring_read(memoryview(buf), slot=5) is None

    def test_oversized_data_rejected(self):
        """Data larger than slot capacity is rejected."""
        buf = _make_ring()
        data = b"\x00" * (_POOL_SLOT_DATA_MAX + 1)
        ok = _ring_write(
            memoryview(buf), slot=0, seq=1, writer_id=0, energy=1.0, data=data, new_edges=0, step=0
        )
        assert not ok

    def test_corrupted_data_detected(self):
        """A checksum mismatch (simulating torn read) returns None."""
        buf = _make_ring()
        data = pickle.dumps({"state_dict": {}, "fault_active": {}})
        _ring_write(
            memoryview(buf), slot=0, seq=1, writer_id=0, energy=1.0, data=data, new_edges=0, step=0
        )

        # Corrupt one byte in the data region
        base = _POOL_HEADER_SIZE + 32  # slot 0, data starts at offset 32
        buf[base] ^= 0xFF

        entry = _ring_read(memoryview(buf), slot=0)
        assert entry is None  # CRC32 mismatch → rejected

    def test_energy_update_propagates(self):
        """Energy updates are visible to subsequent reads."""
        buf = _make_ring()
        data = pickle.dumps({"state_dict": {}, "fault_active": {}})
        _ring_write(
            memoryview(buf), slot=3, seq=1, writer_id=0, energy=1.0, data=data, new_edges=0, step=0
        )

        _ring_update_energy(memoryview(buf), slot=3, energy=7.5)

        entry = _ring_read(memoryview(buf), slot=3)
        assert entry is not None
        assert abs(entry["energy"] - 7.5) < 0.001

    def test_multiple_slots_independent(self):
        """Writing to different slots doesn't interfere."""
        buf = _make_ring()
        mv = memoryview(buf)

        for i in range(4):
            data = pickle.dumps({"state_dict": {"slot": i}, "fault_active": {}})
            _ring_write(
                mv, slot=i, seq=i + 1, writer_id=i, energy=float(i), data=data, new_edges=i, step=i
            )

        for i in range(4):
            entry = _ring_read(mv, slot=i)
            assert entry is not None
            assert entry["writer_id"] == i
            payload = pickle.loads(entry["data"])
            assert payload["state_dict"]["slot"] == i

    def test_slot_overwrite(self):
        """Overwriting a slot updates its contents."""
        buf = _make_ring()
        mv = memoryview(buf)

        data1 = pickle.dumps({"state_dict": {"v": 1}, "fault_active": {}})
        _ring_write(mv, slot=0, seq=1, writer_id=0, energy=1.0, data=data1, new_edges=0, step=0)

        data2 = pickle.dumps({"state_dict": {"v": 2}, "fault_active": {}})
        _ring_write(mv, slot=0, seq=2, writer_id=0, energy=3.0, data=data2, new_edges=5, step=10)

        entry = _ring_read(mv, slot=0)
        assert entry["sequence"] == 2
        assert abs(entry["energy"] - 3.0) < 0.001
        payload = pickle.loads(entry["data"])
        assert payload["state_dict"]["v"] == 2


# ============================================================================
# Ring buffer checkpoint exchange (replaces file-based tests)
# ============================================================================


class TestCheckpointRingExchange:
    """Verify the Explorer's publish/subscribe via the ring buffer."""

    def _make_explorer_pair(
        self,
        *,
        num_workers=2,
        slots_per_worker=128,
        auth_key: bytes | None = None,
    ):
        """Create two Explorers sharing a ring buffer."""
        buf = bytearray(_POOL_RING_SIZE)
        mv = memoryview(buf)

        def setup(explorer, worker_id):
            explorer._pool_ring = mv
            explorer._pool_auth_key = auth_key
            explorer._worker_id = worker_id
            explorer._pool_num_workers = num_workers
            explorer._pool_slots_per_worker = slots_per_worker
            explorer._pool_last_sync = 0  # force immediate sync

        pub = Explorer(DeepServiceChaos, workers=1)
        sub = Explorer(DeepServiceChaos, workers=1)
        setup(pub, 0)
        setup(sub, 1)
        return pub, sub, buf

    def test_publish_writes_to_ring(self):
        """_pool_publish writes a checkpoint into the worker's ring slot."""
        pub, _, buf = self._make_explorer_pair()

        machine = DeepServiceChaos()
        for _ in range(6):
            machine.service.accumulate()
        machine.service.pivot()

        pub._pool_publish(machine, new_count=3, step=5, run_id=1)

        # Read the raw slot (worker 0's first slot = slot 0)
        entry = _ring_read(memoryview(buf), slot=0)
        assert entry is not None
        assert entry["writer_id"] == 0
        payload = pickle.loads(entry["data"])
        assert payload["state_dict"]["service"].state == "pivoted"

    def test_subscribe_loads_other_workers_checkpoints(self):
        """_pool_subscribe loads checkpoints published by other workers."""
        pub, sub, _ = self._make_explorer_pair()

        machine = DeepServiceChaos()
        for _ in range(6):
            machine.service.accumulate()
        machine.service.pivot()
        pub._pool_publish(machine, new_count=3, step=5, run_id=1)

        assert len(sub._checkpoints) == 0
        sub._pool_subscribe()
        assert len(sub._checkpoints) == 1

        cp = sub._checkpoints[0]
        restored = sub._restore_machine(cp.snapshot)
        assert restored.service.state == "pivoted"

    def test_subscribe_skips_own_slots(self):
        """Workers don't import checkpoints from their own slots."""
        pub, _, _ = self._make_explorer_pair()

        machine = DeepServiceChaos()
        pub._pool_publish(machine, new_count=3, step=0, run_id=1)

        # Same worker subscribes — should skip its own slot range
        pub._pool_subscribe()
        assert len(pub._checkpoints) == 0

    def test_subscribe_tracks_sequences(self):
        """Same checkpoint isn't imported twice."""
        pub, sub, _ = self._make_explorer_pair()

        machine = DeepServiceChaos()
        pub._pool_publish(machine, new_count=3, step=0, run_id=1)

        sub._pool_subscribe()
        assert len(sub._checkpoints) == 1

        # Subscribe again — same sequence, no new imports
        sub._pool_last_sync = 0
        sub._pool_subscribe()
        assert len(sub._checkpoints) == 1

    def test_round_robin_slots(self):
        """Multiple publishes use consecutive slots within the worker's range."""
        pub, sub, buf = self._make_explorer_pair()
        machine = DeepServiceChaos()

        for i in range(5):
            pub._pool_publish(machine, new_count=3, step=i, run_id=i)

        # Worker 0 owns slots 0..127, should have written slots 0..4
        for i in range(5):
            entry = _ring_read(memoryview(buf), slot=i)
            assert entry is not None
            assert entry["step"] == i

    def test_unpicklable_checkpoint_handled_gracefully(self):
        """Unpicklable attributes are silently skipped, not crashed."""
        pub, sub, _ = self._make_explorer_pair()

        machine = DeepServiceChaos()
        machine._unpicklable = lambda: None

        # Should not raise — unpicklable attrs are filtered before pickle
        pub._pool_publish(machine, new_count=3, step=0, run_id=1)

        sub._pool_subscribe()
        # Checkpoint is either present (if service state pickled) or absent
        # (if everything was unpicklable) — no crash either way

    def test_subscribe_rejects_unauthenticated_payloads(self):
        """Workers skip ring entries that fail checkpoint payload authentication."""
        auth_key = b"shared-secret-for-tests"
        _, sub, buf = self._make_explorer_pair(auth_key=auth_key)

        forged = pickle.dumps({"state_dict": {"service": "bad"}, "fault_active": {}})
        _ring_write(
            memoryview(buf),
            slot=0,
            seq=1,
            writer_id=0,
            energy=1.0,
            data=forged,
            new_edges=1,
            step=0,
        )

        sub._pool_subscribe()
        assert sub._checkpoints == []

        signed = _pool_encode_payload(
            {"state_dict": {"service": "good"}, "fault_active": {}},
            auth_key,
        )
        _ring_write(
            memoryview(buf),
            slot=0,
            seq=2,
            writer_id=0,
            energy=1.0,
            data=signed,
            new_edges=1,
            step=1,
        )

        sub._pool_last_sync = 0
        sub._pool_subscribe()
        assert len(sub._checkpoints) == 1

    def test_energy_propagation_across_workers(self):
        """Energy updates from one worker are visible to others."""
        pub, sub, buf = self._make_explorer_pair()

        machine = DeepServiceChaos()
        pub._pool_publish(machine, new_count=1, step=0, run_id=1)

        sub._pool_subscribe()
        assert len(sub._checkpoints) == 1

        cp = sub._checkpoints[0]
        assert cp._pool_slot >= 0  # came from ring buffer

        # Simulate: exploring from this checkpoint found new edges
        sub._pool_ring = memoryview(buf)
        sub._update_checkpoint_energy(cp, new_edges=5)

        # Energy was written back to the ring buffer
        entry = _ring_read(memoryview(buf), cp._pool_slot)
        assert entry is not None
        assert entry["energy"] > 3.0  # initial + 5 * 2.0 reward


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


class _FakePool:
    def __init__(self, results, captured_args):
        self._results = results
        self._captured_args = captured_args

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def map(self, fn, worker_args):
        self._captured_args.extend(worker_args)
        return self._results


class _FakeContext:
    def __init__(self, results, captured_args):
        self._results = results
        self._captured_args = captured_args

    def Pool(self, workers):
        return _FakePool(self._results, self._captured_args)


class TestParallelAggregation:
    def test_parallel_splits_max_runs_across_workers(self, monkeypatch):
        captured_args = []
        worker_results = [
            {
                "worker_id": i,
                "total_runs": 1,
                "total_steps": 2,
                "unique_edges": 1,
                "checkpoints_saved": 0,
                "duration_seconds": 0.1,
                "failures": [],
                "worker_error": None,
                "edge_log": [],
                "edges": [i],
            }
            for i in range(3)
        ]
        monkeypatch.setattr(
            "ordeal.explore.mp.get_context",
            lambda method: _FakeContext(worker_results, captured_args),
        )

        explorer = Explorer(
            DeepServiceChaos,
            target_modules=["tests._deep_target"],
            workers=4,
            seed=42,
            share_edges=False,
            share_checkpoints=False,
        )
        result = explorer._run_parallel(
            max_time=1.0,
            max_runs=3,
            steps_per_run=5,
            shrink=False,
            max_shrink_time=0.1,
            patience=0,
            progress=None,
        )

        assert [arg["max_runs"] for arg in captured_args] == [1, 1, 1]
        assert result.total_runs == 3

    def test_parallel_deduplicates_failures_and_preserves_trace(self, monkeypatch):
        captured_args = []
        trace = Trace(
            run_id=7,
            seed=42,
            test_class="tests.test_checkpoint_pool:DeepServiceChaos",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="do_accumulate", params={})],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=0),
        )
        payload = {
            "worker_id": 0,
            "run_id": 7,
            "step": 1,
            "active_faults": [],
            "rule_log": ["do_accumulate"],
            "error_type": "ValueError",
            "error_module": "builtins",
            "error_qualname": "ValueError",
            "error_message": "boom",
            "error_traceback": "Traceback: boom",
            "trace": trace.to_dict(),
            "trace_hash": trace.content_hash(),
        }
        worker_results = [
            {
                "worker_id": i,
                "total_runs": 1,
                "total_steps": 1,
                "unique_edges": 1,
                "checkpoints_saved": 0,
                "duration_seconds": 0.1,
                "failures": [payload],
                "worker_error": None,
                "edge_log": [],
                "edges": [i],
            }
            for i in range(2)
        ]
        monkeypatch.setattr(
            "ordeal.explore.mp.get_context",
            lambda method: _FakeContext(worker_results, captured_args),
        )

        explorer = Explorer(
            DeepServiceChaos,
            target_modules=["tests._deep_target"],
            workers=2,
            seed=42,
            share_edges=False,
            share_checkpoints=False,
            record_traces=True,
        )
        monkeypatch.setattr(
            explorer,
            "_parallel_retry_reason",
            lambda worker_results, result: None,
        )
        result = explorer._run_parallel(
            max_time=1.0,
            max_runs=2,
            steps_per_run=5,
            shrink=False,
            max_shrink_time=0.1,
            patience=0,
            progress=None,
        )

        assert len(result.failures) == 1
        assert "ValueError" in str(result.failures[0])
        assert result.failures[0].trace is not None
        assert result.failures[0].error_traceback == "Traceback: boom"

    def test_parallel_zero_edge_result_falls_back_to_sequential(self, monkeypatch):
        captured_args = []
        worker_results = [
            {
                "worker_id": i,
                "total_runs": 1,
                "total_steps": 1,
                "unique_edges": 0,
                "checkpoints_saved": 0,
                "duration_seconds": 0.1,
                "failures": [],
                "worker_error": None,
                "edge_log": [],
                "edges": [],
            }
            for i in range(4)
        ]
        monkeypatch.setattr(
            "ordeal.explore.mp.get_context",
            lambda method: _FakeContext(worker_results, captured_args),
        )

        reason_box = {}

        def _fake_rerun(**kwargs):
            reason_box["reason"] = kwargs["reason"]
            return SimpleNamespace(
                total_runs=1,
                total_steps=1,
                checkpoints_saved=0,
                unique_edges=2,
                failures=[],
                edge_log=[],
                traces=[],
                duration_seconds=0.1,
                ngram=2,
                seed_replays=[],
                coverage_gaps=[],
                lines_covered=0,
                lines_total=0,
                parallel_fallback_reason=kwargs["reason"],
            )

        explorer = Explorer(
            DeepServiceChaos,
            target_modules=["tests._deep_target"],
            workers=4,
            seed=42,
            share_edges=False,
            share_checkpoints=False,
        )
        monkeypatch.setattr(explorer, "_rerun_sequential_after_parallel", _fake_rerun)

        result = explorer._run_parallel(
            max_time=1.0,
            max_runs=4,
            steps_per_run=5,
            shrink=False,
            max_shrink_time=0.1,
            patience=0,
            progress=None,
        )

        assert result.parallel_fallback_reason == "0 edges discovered"
        assert reason_box["reason"] == "0 edges discovered"


@pytest.mark.slow
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
        Sharing checkpoints via the ring buffer adds serialization
        overhead, so this ensures the overhead doesn't dominate.
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
            f"Pool should not hurt: with={avg_on:.1f}, without={avg_off:.1f}"
        )
