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
    """Base class for stateful chaos tests.

    Quick start::

        class MyServiceChaos(ChaosTest):
            faults = [timing.timeout("myapp.db.query")]

            @rule()
            def call_service(self):
                result = my_service.process("input")
                always(result is not None, "never None")

        TestMyService = MyServiceChaos.TestCase  # run with: pytest

    Class attributes:
        faults: Fault instances to inject.  A nemesis rule auto-toggles
            them during exploration — you just declare, ordeal explores.
        swarm:  ``True`` = each test case picks a random *subset* of
            faults via a bitmask (Hypothesis explores which subsets
            matter). Different runs explore different combinations,
            giving better aggregate coverage than always-all-faults.
            Use when you have 3+ faults.

    Key methods to override:
        state_hash(): Return a hash of qualitatively different states to
            enable state-aware exploration.  The explorer checkpoints new
            state hashes and branches from them.  Default returns 0
            (disabled).  Example::

                def state_hash(self):
                    return hash((self.service.state, self.balance > 0))

    Deeper testing:
        - ``pytest --chaos`` enables buggify() + property tracking
        - ``ordeal explore`` runs coverage-guided exploration with
          checkpointing and energy scheduling (reads ordeal.toml)
        - ``swarm = True`` tests random fault subsets per run
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

        Uses an integer bitmask (1 bit per fault) instead of a boolean
        list — smaller, faster to draw and filter.  The range
        ``[1, (1 << n) - 1]`` guarantees at least one fault is kept.
        Hypothesis controls the integer, so shrinking finds minimal sets.
        """
        if not self.__class__.swarm or len(self._faults) <= 1:
            return
        n = len(self._faults)
        mask = data.draw(
            st.integers(min_value=1, max_value=(1 << n) - 1),
            label="swarm_mask",
        )
        self._faults = [f for i, f in enumerate(self._faults) if mask & (1 << i)]

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

    def __repr__(self) -> str:
        cls = self.__class__
        active = [f.name for f in self._faults if f.active]
        parts = [f"{cls.__name__}(faults={len(self._faults)}"]
        if cls.swarm:
            parts.append(f"swarm={len(self._faults)}/{len(cls.faults)}")
        if active:
            parts.append(f"active=[{', '.join(active)}]")
        return ", ".join(parts) + ")"

    def teardown(self) -> None:
        """Deactivate all faults (including those not in the swarm subset)."""
        for f in self.__class__.faults:
            f.reset()
        super().teardown()


def chaos_test(cls: type | None = None, *, faults: list | None = None):
    """Decorator that turns a ChaosTest subclass into a pytest-runnable test.

    Removes the need for the ``TestCase = MyChaos.TestCase`` boilerplate::

        @chaos_test
        class MyServiceChaos(ChaosTest):
            faults = [timing.timeout("myapp.api")]

            @rule()
            def call_service(self):
                ...

    Also works as a factory for inline declaration::

        @chaos_test(faults=[timing.timeout("myapp.api")])
        class MyServiceChaos(ChaosTest):
            @rule()
            def call_service(self):
                ...

    The returned object is the ``TestCase`` class, directly discoverable
    by pytest without any extra wiring.
    """

    def _wrap(klass: type) -> type:
        if faults is not None:
            klass.faults = faults
        return klass.TestCase

    if cls is not None:
        return _wrap(cls)
    return _wrap
