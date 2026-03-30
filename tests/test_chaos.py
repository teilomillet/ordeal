"""Tests for ordeal.chaos — ChaosTest stateful testing."""
import math

from hypothesis import settings
from hypothesis.stateful import rule, invariant

from ordeal.chaos import ChaosTest
from ordeal.faults import LambdaFault


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
