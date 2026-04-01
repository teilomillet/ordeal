"""Tests for rule swarm — random rule subsets per exploration run.

The cache GC pattern from the Bug Bash podcast: a cache with insert and
delete operations only triggers its GC path when the cache grows large.
With uniform random rule selection, inserts and deletes roughly cancel
out, so the cache never grows large enough to trigger GC.  Rule swarm
sometimes disables the delete rule, forcing only inserts → cache grows
→ GC triggers → bug found.
"""

from __future__ import annotations

from hypothesis.stateful import rule

from ordeal.chaos import ChaosTest
from ordeal.explore import ExplorationResult, Explorer

# ============================================================================
# Test fixture: the cache GC pattern
# ============================================================================


class _CacheWithGC(ChaosTest):
    """Cache that only fails when it grows large enough to trigger GC.

    With uniform random insert/delete, the cache stays small.
    With rule swarm (delete sometimes disabled), inserts accumulate.
    """

    faults = []

    def __init__(self):
        super().__init__()
        self.cache: dict[int, str] = {}
        self._next_key = 0

    @rule()
    def insert(self):
        self.cache[self._next_key] = f"value-{self._next_key}"
        self._next_key += 1
        # GC triggers at size 8 — trivial with swarm, hard without
        if len(self.cache) >= 8:
            raise ValueError(f"GC bug: cache size {len(self.cache)}")

    @rule()
    def delete(self):
        if self.cache:
            key = min(self.cache)
            del self.cache[key]


class _SingleRule(ChaosTest):
    """Only one rule — swarm should be a no-op."""

    faults = []

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 3:
            raise ValueError("boom")


# ============================================================================
# Tests
# ============================================================================


class TestRuleSwarm:
    def test_swarm_finds_gc_bug(self):
        """Rule swarm should find the cache GC bug that uniform misses."""
        explorer = Explorer(
            _CacheWithGC,
            rule_swarm=True,
            seed=42,
        )
        result = explorer.run(max_time=5, max_runs=200, shrink=False)
        # With swarm, some runs disable delete → cache grows → GC triggers
        assert result.failures, "swarm should find the GC bug"
        assert result.rule_swarm_runs > 0

    def test_swarm_disabled_by_default(self):
        """Without rule_swarm, no swarm runs should occur."""
        explorer = Explorer(
            _CacheWithGC,
            rule_swarm=False,
            seed=42,
        )
        result = explorer.run(max_time=2, max_runs=50, shrink=False)
        assert result.rule_swarm_runs == 0

    def test_swarm_noop_with_single_rule(self):
        """Swarm with one rule should still work (no subset to take)."""
        explorer = Explorer(
            _SingleRule,
            rule_swarm=True,
        )
        result = explorer.run(max_time=2, max_runs=10, shrink=False)
        # Single rule: swarm can't subset, so rule_swarm_runs stays 0
        assert result.rule_swarm_runs == 0
        # But the bug should still be found
        assert result.failures

    def test_swarm_in_summary(self):
        """ExplorationResult.summary() should mention rule swarm."""
        result = ExplorationResult(
            total_runs=100,
            rule_swarm_runs=60,
        )
        s = result.summary()
        assert "Rule swarm" in s
        assert "60/100" in s
