"""ChaosTest — stateful chaos testing powered by Hypothesis.

Subclass ``ChaosTest``, declare faults, write rules, add invariants::

    from ordeal import ChaosTest, rule, invariant, always
    from ordeal.faults import timing, numerical

    class MyServiceChaos(ChaosTest):
        faults = [
            timing.timeout("myservice.api_call"),
            numerical.nan_injection("myservice.score"),
        ]

        @rule()
        def call_service(self):
            result = my_service.process("input")
            always(result is not None, "process never returns None")

        @invariant()
        def no_corruption(self):
            for item in my_service.results():
                always(not math.isnan(item), "no NaN in results")

    # Run with pytest — Hypothesis explores rule sequences + fault schedules
    TestMyServiceChaos = MyServiceChaos.TestCase

The library auto-injects a **nemesis rule** that toggles faults on/off.
Hypothesis explores: which faults fire, when, in what order, interleaved
with your application rules.

**Swarm mode** (``swarm = True``): each test case uses a random *subset*
of faults.  Different runs explore different fault combinations, giving
better aggregate coverage than uniform selection (Groce et al., 2012).
"""

from __future__ import annotations

from typing import ClassVar

import hypothesis.strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from ordeal.faults import Fault


class ChaosTest(RuleBasedStateMachine):
    """Base class for chaos tests.

    Class attributes:
        faults: List of :class:`Fault` instances to inject.  The nemesis
            rule automatically toggles these during exploration.
        swarm:  If ``True``, each test case randomly selects a subset of
            faults.  Improves coverage across many runs.
    """

    faults: ClassVar[list[Fault]] = []
    swarm: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        # Take a copy so swarm can safely shrink the list
        self._faults: list[Fault] = list(self.__class__.faults)
        for f in self._faults:
            f.reset()

    # -- Swarm selection (runs once, before any rules) ----------------------

    @initialize(data=st.data())
    def _swarm_init(self, data):  # type: ignore[override]
        """In swarm mode, randomly select a fault subset for this test case.

        Each fault is independently included or excluded.  Hypothesis
        controls the booleans, so shrinking finds minimal fault sets.
        """
        if not self.__class__.swarm or len(self._faults) <= 1:
            return
        mask = data.draw(
            st.lists(
                st.booleans(),
                min_size=len(self._faults),
                max_size=len(self._faults),
            ).filter(any),  # at least one fault
            label="swarm_mask",
        )
        self._faults = [f for f, keep in zip(self._faults, mask) if keep]

    # -- Nemesis rule -------------------------------------------------------

    @rule(data=st.data())
    def _nemesis(self, data):  # type: ignore[override]
        """Toggle a random fault on or off."""
        if not self._faults:
            return
        fault = data.draw(st.sampled_from(self._faults), label="nemesis_target")
        if fault.active:
            fault.deactivate()
        else:
            fault.activate()

    # -- Lifecycle ----------------------------------------------------------

    @property
    def active_faults(self) -> list[Fault]:
        """Currently active faults (useful for debugging / logging)."""
        return [f for f in self._faults if f.active]

    def state_hash(self) -> int:
        """Override to enable state-aware coverage.

        Return a hash of the *qualitatively* different states — not every
        possible value, but categories that matter for bug-finding.
        The explorer treats a new state hash as a discovery (saves a
        checkpoint, resets saturation counter).

        Example::

            def state_hash(self):
                return hash((
                    self.service.state,        # enum-like: "idle", "active"
                    self.balance > 0,           # sign matters, not exact value
                    len(self.pending) > 10,     # "many" vs "few"
                ))

        Return 0 (default) to disable state-aware coverage.
        """
        return 0

    def teardown(self) -> None:
        """Deactivate all faults (including those not in the swarm subset)."""
        for f in self.__class__.faults:
            f.reset()
        super().teardown()
