"""Tests for ordeal.explore internals and ordeal.chaos internals."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import hypothesis.strategies as st
from hypothesis import settings as hsettings
from hypothesis.stateful import invariant, rule

from ordeal.chaos import ChaosTest
from ordeal.explore import (
    CoverageCollector,
    ExplorationResult,
    Failure,
    ProgressSnapshot,
    _DataProxy,
    _RuleInfo,
)
from ordeal.faults import LambdaFault
from ordeal.trace import Trace

# ============================================================================
# CoverageCollector._is_target
# ============================================================================


class TestCoverageCollectorIsTarget:
    def test_matches_simple_module(self):
        cc = CoverageCollector(["myapp"])
        assert cc._is_target("/path/to/myapp/service.py")

    def test_no_match_partial(self):
        cc = CoverageCollector(["app"])
        assert not cc._is_target("/path/to/myapp/service.py")

    def test_matches_nested_path(self):
        cc = CoverageCollector(["myapp.core"])
        assert cc._is_target("/path/to/myapp/core/engine.py")

    def test_no_match_unrelated(self):
        cc = CoverageCollector(["myapp"])
        assert not cc._is_target("/path/to/other/module.py")

    def test_handles_backslashes(self):
        cc = CoverageCollector(["myapp"])
        assert cc._is_target("C:\\code\\myapp\\service.py")

    def test_matches_file_segment(self):
        cc = CoverageCollector(["service"])
        assert cc._is_target("/path/to/service.py")

    def test_empty_targets(self):
        cc = CoverageCollector([])
        assert not cc._is_target("/any/path.py")


# ============================================================================
# CoverageCollector lifecycle
# ============================================================================


class TestCoverageCollectorLifecycle:
    def test_start_stop_returns_edges(self):
        cc = CoverageCollector(["tests"])
        cc.start()
        _ = sum(range(10))
        edges = cc.stop()
        assert isinstance(edges, frozenset)

    def test_snapshot_returns_frozenset(self):
        cc = CoverageCollector(["tests"])
        cc.start()
        snap = cc.snapshot()
        assert isinstance(snap, frozenset)
        cc.stop()

    def test_snapshot_caching(self):
        cc = CoverageCollector(["tests"])
        cc.start()
        _ = sum(range(10))
        s1 = cc.snapshot()
        s2 = cc.snapshot()
        assert s1 is s2
        cc.stop()

    def test_no_edges_for_non_target(self):
        cc = CoverageCollector(["nonexistent_module_xyz"])
        cc.start()
        _ = sum(range(100))
        assert len(cc.stop()) == 0

    def test_start_resets(self):
        cc = CoverageCollector(["tests"])
        cc.start()
        _ = sum(range(10))
        cc.stop()
        cc.start()
        snap = cc.snapshot()
        cc.stop()
        assert isinstance(snap, frozenset)


# ============================================================================
# CoverageCollector._flush_local
# ============================================================================


class TestCoverageCollectorRestoresTrace:
    def test_stop_does_not_leave_ordeal_tracer(self):
        """After stop(), the ordeal tracer must not remain installed."""
        cc = CoverageCollector(["tests"])
        prev = sys.gettrace()
        cc.start()
        # During collection, our tracer is installed
        tracer = sys.gettrace()
        assert tracer is not None
        assert hasattr(tracer, "__self__") and isinstance(tracer.__self__, CoverageCollector)
        cc.stop()
        # After stop, our tracer is gone
        after = sys.gettrace()
        if after is not None:
            assert not (hasattr(after, "__self__") and isinstance(after.__self__, CoverageCollector))


class TestCoverageCollectorFlush:
    def test_flush_moves_edges(self):
        cc = CoverageCollector(["tests"])
        cc.start()
        cc._tls.edge_buf = [1, 2, 3]
        cc._flush_local()
        assert cc._tls.edge_buf == []
        assert {1, 2, 3}.issubset(cc._edges)
        cc.stop()

    def test_flush_empty_is_noop(self):
        cc = CoverageCollector(["tests"])
        cc.start()
        cc._tls.edge_buf = []
        cc._flush_local()
        cc.stop()


# ============================================================================
# _DataProxy
# ============================================================================


class TestDataProxy:
    def test_draw_integers(self):
        proxy = _DataProxy()
        result = proxy.draw(st.integers(min_value=0, max_value=100))
        assert isinstance(result, int)
        assert 0 <= result <= 100

    def test_draw_text(self):
        proxy = _DataProxy()
        result = proxy.draw(st.text(min_size=1, max_size=5))
        assert isinstance(result, str)

    def test_draw_sampled_from(self):
        proxy = _DataProxy()
        result = proxy.draw(st.sampled_from(["a", "b", "c"]))
        assert result in ("a", "b", "c")

    def test_records_draws(self):
        proxy = _DataProxy()
        proxy.draw(st.just(42), label="my_label")
        assert len(proxy.draws) == 1
        assert proxy.draws[0][0] == "my_label"
        assert proxy.draws[0][1] == 42


# ============================================================================
# _RuleInfo
# ============================================================================


class TestRuleInfo:
    def test_basic(self):
        ri = _RuleInfo(name="tick", strategies={})
        assert ri.name == "tick"
        assert not ri.has_data

    def test_with_strategies(self):
        ri = _RuleInfo(name="set_val", strategies={"x": st.integers()}, has_data=True)
        assert "x" in ri.strategies
        assert ri.has_data


# ============================================================================
# ChaosTest internals
# ============================================================================


class TestChaosTestInternals:
    def test_faults_are_copied(self):
        f = LambdaFault("f1", lambda: None, lambda: None)

        class T(ChaosTest):
            faults = [f]

            @rule()
            def tick(self):
                pass

        m = T()
        assert m._faults is not T.faults
        m.teardown()

    def test_faults_reset_on_init(self):
        f = LambdaFault("f1", lambda: None, lambda: None)
        f.activate()

        class T(ChaosTest):
            faults = [f]

            @rule()
            def tick(self):
                pass

        T()
        assert not f.active

    def test_active_faults(self):
        f1 = LambdaFault("f1", lambda: None, lambda: None)
        f2 = LambdaFault("f2", lambda: None, lambda: None)

        class T(ChaosTest):
            faults = [f1, f2]

            @rule()
            def tick(self):
                pass

        m = T()
        assert m.active_faults == []
        f1.activate()
        assert len(m.active_faults) == 1
        m.teardown()

    def test_state_hash_default_zero(self):
        class T(ChaosTest):
            faults = []

            @rule()
            def tick(self):
                pass

        m = T()
        assert m.state_hash() == 0
        m.teardown()

    def test_state_hash_custom(self):
        class T(ChaosTest):
            faults = []

            def __init__(self):
                super().__init__()
                self.st = "idle"

            @rule()
            def tick(self):
                self.st = "active"

            def state_hash(self):
                return hash(self.st)

        m = T()
        h1 = m.state_hash()
        m.tick()
        assert m.state_hash() != h1
        m.teardown()

    def test_teardown_resets_class_faults(self):
        f1 = LambdaFault("f1", lambda: None, lambda: None)
        f2 = LambdaFault("f2", lambda: None, lambda: None)

        class T(ChaosTest):
            faults = [f1, f2]

            @rule()
            def tick(self):
                pass

        m = T()
        f1.activate()
        f2.activate()
        m.teardown()
        assert not f1.active and not f2.active

    def test_nemesis_toggles(self):
        f = LambdaFault("test_f", lambda: None, lambda: None)

        class T(ChaosTest):
            faults = [f]

            @rule()
            def tick(self):
                pass

        m = T()
        data = MagicMock()
        data.draw = MagicMock(return_value=m._faults[0])
        m._nemesis(data)
        assert m._faults[0].active
        m._nemesis(data)
        assert not m._faults[0].active
        m.teardown()

    def test_nemesis_noop_without_faults(self):
        class T(ChaosTest):
            faults = []

            @rule()
            def tick(self):
                pass

        m = T()
        m._nemesis(MagicMock())
        m.teardown()


# ============================================================================
# ChaosTest.TestCase integration
# ============================================================================


class _CounterChaos(ChaosTest):
    faults = [LambdaFault("flip", lambda: None, lambda: None)]

    def __init__(self):
        super().__init__()
        self.count = 0

    @rule()
    def increment(self):
        self.count += 1

    @invariant()
    def non_negative(self):
        assert self.count >= 0


TestCounterChaos = _CounterChaos.TestCase
TestCounterChaos.settings = hsettings(max_examples=5, stateful_step_count=5)


# ============================================================================
# Swarm mode
# ============================================================================


class _SwarmChaos(ChaosTest):
    faults = [LambdaFault(f"f{i}", lambda: None, lambda: None) for i in range(5)]
    swarm = True

    @rule()
    def tick(self):
        pass


TestSwarmChaos = _SwarmChaos.TestCase
TestSwarmChaos.settings = hsettings(max_examples=5, stateful_step_count=5)


class TestSwarmBehavior:
    def test_swarm_reduces_fault_list(self):
        seen_lengths: set[int] = set()
        for seed in range(20):
            m = _SwarmChaos()
            if len(m._faults) > 1:
                mask = (seed % ((1 << len(m._faults)) - 1)) + 1
                m._faults = [f for i, f in enumerate(m._faults) if mask & (1 << i)]
            seen_lengths.add(len(m._faults))
            m.teardown()
        assert len(seen_lengths) > 1


# ============================================================================
# Data structures
# ============================================================================


class TestExplorationResult:
    def test_basic(self):
        r = ExplorationResult(
            total_runs=100,
            total_steps=500,
            unique_edges=42,
            checkpoints_saved=5,
            duration_seconds=10.0,
            failures=[],
            traces=[],
        )
        assert r.total_runs == 100

    def test_with_failure(self):
        f = Failure(
            error=ValueError("boom"),
            step=5,
            run_id=1,
            active_faults=["f1"],
            rule_log=["tick"],
            trace=None,
        )
        r = ExplorationResult(
            total_runs=10,
            total_steps=50,
            unique_edges=20,
            checkpoints_saved=2,
            duration_seconds=5.0,
            failures=[f],
            traces=[],
        )
        assert len(r.failures) == 1
        assert "boom" in str(r.failures[0])


class TestFailureStr:
    def test_str_no_trace(self):
        f = Failure(
            error=ValueError("boom"),
            step=5,
            run_id=1,
            active_faults=["f1", "f2"],
            rule_log=["tick", "check"],
            trace=None,
        )
        s = str(f)
        assert "boom" in s

    def test_str_with_trace(self):
        t = Trace(
            run_id=1, seed=0, test_class="x:Y", from_checkpoint=None, steps=[MagicMock()] * 3
        )
        f = Failure(
            error=RuntimeError("err"),
            step=2,
            run_id=1,
            active_faults=[],
            rule_log=["a"],
            trace=t,
        )
        s = str(f)
        assert "err" in s


class TestProgressSnapshot:
    def test_fields(self):
        s = ProgressSnapshot(
            elapsed=5.0,
            total_runs=100,
            total_steps=500,
            unique_edges=42,
            checkpoints=3,
            failures=1,
            runs_per_second=20.0,
        )
        assert s.elapsed == 5.0
        assert s.runs_per_second == 20.0
