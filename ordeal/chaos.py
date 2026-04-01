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

**Adaptive fault scheduling** (MOpt-inspired): instead of selecting
faults uniformly at random, the nemesis tracks per-fault *energy*.
Faults that lead to new coverage get boosted (toggled more often);
faults that never discover new edges decay (toggled less).  This is
analogous to AFL++'s MOpt mutator scheduling — the same idea applied
to fault selection instead of byte-level mutations.  When no coverage
collector is attached (the common pytest path), selection falls back
to uniform random.

**Swarm mode** (``swarm = True``): each test case uses a random *subset*
of faults.  Different runs explore different fault combinations, giving
better aggregate coverage than uniform selection (Groce et al., 2012).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import hypothesis.strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from ordeal.faults import Fault

if TYPE_CHECKING:
    from ordeal.explore import CoverageCollector

# ---------------------------------------------------------------------------
# Adaptive fault scheduling constants
#
# These govern how fault energy evolves based on coverage feedback.
# The mechanism is analogous to AFL++'s MOpt (mutator optimisation):
# operators that produce new coverage get higher selection probability,
# operators that plateau decay toward a minimum floor.
#
#   energy_new  = energy_old * _FAULT_ENERGY_REWARD   (on new edges)
#   energy_new  = energy_old * _FAULT_ENERGY_DECAY    (no new edges)
#   energy      = max(energy, _FAULT_ENERGY_MIN)      (never fully dead)
#
# The minimum floor ensures every fault retains a small chance of being
# selected — important because a fault that was useless early may become
# critical after the system reaches a different state.
# ---------------------------------------------------------------------------
_FAULT_ENERGY_REWARD: float = 1.5
_FAULT_ENERGY_DECAY: float = 0.9
_FAULT_ENERGY_MIN: float = 0.1


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

    Adaptive scheduling:
        When a ``CoverageCollector`` is attached (via ``_coverage_collector``),
        the nemesis uses **adaptive fault scheduling** — the equivalent of
        AFL++'s MOpt technique applied to fault selection.  Each fault
        carries an *energy* score.  After toggling a fault, the nemesis
        snapshots coverage; on the next invocation it checks whether new
        edges appeared:

        - **New edges found**: the last-toggled fault's energy is multiplied
          by ``_FAULT_ENERGY_REWARD`` (1.5x).  This fault gets selected
          more often in future steps.
        - **No new edges**: the fault's energy decays by
          ``_FAULT_ENERGY_DECAY`` (0.9x), reducing its selection weight.
        - **Minimum floor**: energy never drops below ``_FAULT_ENERGY_MIN``
          (0.1) so every fault retains a chance of being selected.

        When no collector is attached (normal ``pytest`` path), selection
        falls back to uniform random — no overhead.

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

    Subprocess / FFI testing:
        Subprocess faults (``subprocess_timeout``, ``subprocess_delay``,
        ``corrupt_stdout``) work in ChaosTest — they patch
        ``subprocess.run`` and the nemesis toggles them like any fault.
        The code under test should call ``subprocess.run`` per rule
        invocation (not start a long-lived ``Popen`` in ``__init__``).
        Wrap the full subprocess lifecycle in a function and call that
        from rules::

            class KernelChaos(ChaosTest):
                faults = [subprocess_timeout("cargo run")]

                @rule()
                def run_episode(self):
                    result = run_kernel(steps=10)  # calls subprocess.run
                    always(result.exit_code == 0, "clean exit")
    """

    faults: ClassVar[list[Fault]] = []
    swarm: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        # Take a copy so swarm can safely shrink the list
        self._faults: list[Fault] = list(self.__class__.faults)
        for f in self._faults:
            f.reset()

        # -- Adaptive fault scheduling (MOpt-style) --
        # Per-fault energy for weighted selection.  Faults that lead to
        # new coverage get boosted; faults that plateau decay.  Indexed
        # by position in self._faults (rebuilt after swarm filtering).
        self._fault_energy: dict[int, float] = {i: 1.0 for i in range(len(self._faults))}

        # Optional coverage collector — set by the Explorer when running
        # coverage-guided exploration.  When None, the nemesis falls back
        # to uniform random selection (no overhead).
        self._coverage_collector: CoverageCollector | None = None

        # Index of the last fault toggled by the nemesis.  Used to
        # attribute coverage gains to the fault that caused them.
        self._last_toggled_fault: int | None = None

        # Edge count snapshot taken right after the last toggle, so the
        # next nemesis invocation can detect new discoveries cheaply.
        self._edges_before: int = 0

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
        # Rebuild energy dict to match the new fault list indices
        self._fault_energy = {i: 1.0 for i in range(len(self._faults))}

    # -- Nemesis rule (adaptive fault scheduling) -----------------------------

    @rule(data=st.data())
    def _nemesis(self, data):  # type: ignore[override]
        """Toggle a fault on or off, weighted by adaptive energy.

        **Adaptive fault scheduling** (MOpt-inspired):

        This is the equivalent of AFL++'s MOpt (mutator optimisation)
        applied to fault selection.  In AFL++, MOpt tracks which byte-level
        mutation operators (bit flip, arithmetic, havoc, etc.) produce new
        coverage, and schedules them proportionally.  Here the same idea
        applies to *faults*: which fault injections lead the system into
        unexplored states?

        **Why it makes exploration more efficient per compute unit:**

        Uniform random selection wastes cycles toggling faults that the
        system handles gracefully (no new code paths reached).  Adaptive
        scheduling concentrates effort on faults that actually drive the
        system into new territory — a timeout that triggers a retry path,
        a disk-full that activates fallback logic, etc.  The result is
        more unique edges discovered per nemesis invocation.

        **How the energy formula works:**

        Each fault ``i`` has an energy ``E[i]`` (initially 1.0).  When the
        nemesis runs:

        1. **Credit assignment**: If a previous toggle is pending
           attribution, compare the current edge count to the snapshot
           taken after that toggle:

           - New edges found: ``E[last] *= _FAULT_ENERGY_REWARD`` (1.5x)
           - No new edges:    ``E[last] *= _FAULT_ENERGY_DECAY``  (0.9x)
           - Floor:           ``E[last]  = max(E[last], _FAULT_ENERGY_MIN)``

        2. **Weighted selection**: Draw a float in ``[0, sum(energies))``
           and walk the cumulative distribution to pick a fault.  Higher
           energy = higher probability of selection.

        3. **Snapshot**: Record the current edge count so the *next*
           nemesis invocation can measure the delta.

        When no ``CoverageCollector`` is attached (normal pytest without
        the Explorer), step 1 is skipped and selection falls back to
        uniform ``sampled_from`` — no overhead in the common case.

        **Connection to AFL++'s MOpt:**

        MOpt (Lyu et al., USENIX Security 2019) models mutation operator
        scheduling as a particle-swarm optimisation problem.  Our approach
        is simpler — multiplicative reward/decay with a floor — but
        captures the core insight: *operators that produce new coverage
        deserve more invocations*.  The constants (reward=1.5, decay=0.9,
        min=0.1) ensure convergence: productive faults dominate selection
        but unproductive faults never fully disappear, preserving the
        ability to escape local optima when the system reaches a new
        state region.
        """
        if not self._faults:
            return

        collector = self._coverage_collector

        # -- Step 1: credit assignment for the previous toggle --
        if collector is not None and self._last_toggled_fault is not None:
            edges_now = len(collector.snapshot())
            if edges_now > self._edges_before:
                # New edges discovered — reward the fault that was toggled
                idx = self._last_toggled_fault
                self._fault_energy[idx] = self._fault_energy[idx] * _FAULT_ENERGY_REWARD
            else:
                # No new edges — decay
                idx = self._last_toggled_fault
                self._fault_energy[idx] = max(
                    _FAULT_ENERGY_MIN,
                    self._fault_energy[idx] * _FAULT_ENERGY_DECAY,
                )

        # -- Step 2: select a fault weighted by energy --
        if collector is not None:
            # Weighted selection via cumulative energy distribution.
            # Draw a float from Hypothesis so the choice is shrinkable.
            total = sum(self._fault_energy[i] for i in range(len(self._faults)))
            threshold = data.draw(
                st.floats(min_value=0.0, max_value=total, allow_nan=False),
                label="nemesis_energy",
            )
            cumulative = 0.0
            fault_idx = len(self._faults) - 1  # fallback to last
            for i in range(len(self._faults)):
                cumulative += self._fault_energy[i]
                if cumulative >= threshold:
                    fault_idx = i
                    break
        else:
            # No collector — uniform random (the normal pytest path)
            fault = data.draw(st.sampled_from(self._faults), label="nemesis_target")
            fault_idx = self._faults.index(fault)

        # -- Toggle the selected fault --
        fault = self._faults[fault_idx]
        if fault.active:
            fault.deactivate()
        else:
            fault.activate()

        # -- Step 3: snapshot for next invocation's credit assignment --
        if collector is not None:
            self._last_toggled_fault = fault_idx
            self._edges_before = len(collector.snapshot())

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
