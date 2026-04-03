"""Tests for ordeal.chaos — ChaosTest stateful testing."""

import signal
import time

import pytest
from hypothesis import settings
from hypothesis.stateful import invariant, rule

from ordeal.chaos import ChaosTest, RuleTimeoutError, chaos_test
from ordeal.faults import LambdaFault

_has_sigalrm = hasattr(signal, "SIGALRM")

# -- A simple system under test ---------------------------------------------


class Counter:
    """Trivial system to chaos-test."""

    def __init__(self):
        self.value = 0
        self.log: list[int] = []

    def increment(self, n: int = 1) -> None:
        self.value += n
        self.log.append(self.value)

    def decrement(self, n: int = 1) -> None:
        self.value -= n
        self.log.append(self.value)

    def reset(self) -> None:
        self.value = 0
        self.log.clear()


# -- Faults that affect the Counter ----------------------------------------

_overflow_active = False


class OverflowFault(LambdaFault):
    """When active, Counter.increment adds 1000 instead of n."""

    def __init__(self):
        super().__init__(
            "overflow",
            on_activate=self._on,
            on_deactivate=self._off,
        )
        self._original_increment = None

    def _on(self):
        global _overflow_active
        _overflow_active = True

    def _off(self):
        global _overflow_active
        _overflow_active = False


# -- ChaosTest definition ---------------------------------------------------


class CounterChaos(ChaosTest):
    faults = [OverflowFault()]

    def __init__(self):
        super().__init__()
        self.counter = Counter()

    @rule()
    def do_increment(self):
        if _overflow_active:
            self.counter.increment(1000)
        else:
            self.counter.increment(1)

    @rule()
    def do_decrement(self):
        self.counter.decrement(1)

    @invariant()
    def log_matches_value(self):
        if self.counter.log:
            assert self.counter.log[-1] == self.counter.value

    def teardown(self):
        self.counter.reset()
        super().teardown()


# Hypothesis-standard way to run a stateful test with pytest
TestCounterChaos = CounterChaos.TestCase

# Override Hypothesis settings for faster CI
TestCounterChaos.settings = settings(
    max_examples=20,
    stateful_step_count=10,
)


# -- Test that ChaosTest works without faults --------------------------------


class NoFaultsChaos(ChaosTest):
    """ChaosTest with no faults — should still work."""

    def __init__(self):
        super().__init__()
        self.items: list[int] = []

    @rule()
    def add(self):
        self.items.append(len(self.items))

    @invariant()
    def sorted_check(self):
        assert self.items == sorted(self.items)


TestNoFaultsChaos = NoFaultsChaos.TestCase
TestNoFaultsChaos.settings = settings(max_examples=10, stateful_step_count=8)


# ============================================================================
# @chaos_test decorator — no TestCase boilerplate
# ============================================================================


@chaos_test
class DecoratedChaos(ChaosTest):
    """ChaosTest via @chaos_test — directly discoverable by pytest."""

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def increment(self):
        self.counter += 1

    @invariant()
    def non_negative(self):
        assert self.counter >= 0


DecoratedChaos.settings = settings(max_examples=10, stateful_step_count=5)


# -- rule_timeout tests -------------------------------------------------------


class HangingChaos(ChaosTest):
    """A ChaosTest where a rule hangs — simulates buggify-induced deadlock."""

    rule_timeout = 1.0  # 1 second

    @rule()
    def hangs_forever(self):
        time.sleep(60)  # simulate buggify-induced infinite block


@pytest.mark.skipif(not _has_sigalrm, reason="SIGALRM not available on Windows")
def test_rule_timeout_interrupts_hanging_rule():
    """rule_timeout should raise RuleTimeoutError instead of hanging."""
    TestCase = HangingChaos.TestCase
    TestCase.settings = settings(max_examples=1, stateful_step_count=3)
    with pytest.raises(RuleTimeoutError, match="timed out after"):
        TestCase().runTest()


class FastChaos(ChaosTest):
    """A ChaosTest where rules complete quickly — timeout should not fire."""

    rule_timeout = 5.0

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def fast_rule(self):
        self.counter += 1


TestFastChaos = FastChaos.TestCase
TestFastChaos.settings = settings(max_examples=10, stateful_step_count=5)


class NoTimeoutChaos(ChaosTest):
    """rule_timeout = 0 disables the timeout entirely."""

    rule_timeout = 0

    def __init__(self):
        super().__init__()
        self.value = 0

    @rule()
    def do_work(self):
        self.value += 1


TestNoTimeoutChaos = NoTimeoutChaos.TestCase
TestNoTimeoutChaos.settings = settings(max_examples=5, stateful_step_count=3)
