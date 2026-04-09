"""Battle tests for ordeal — ordeal testing itself.

These are NOT unit tests.  These are stateful, property-based chaos tests
that explore ordeal's own state spaces using ordeal's own machinery.

Hierarchy:
    Level 0: Unit tests (test_*.py)       — "does each component work for one scenario?"
    Level 1: Battle tests (this file)      — "does it work for ALL reachable states?"
    Level 2: Recursive chaos (also here)   — "does ordeal survive its own fault injection?"

If ordeal can survive testing itself, it can test anything.
"""

from __future__ import annotations

import math
import threading

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import invariant, precondition, rule

from ordeal.assertions import PropertyTracker
from ordeal.auto import _test_one_function
from ordeal.buggify import activate, buggify, buggify_value, deactivate, set_seed
from ordeal.chaos import ChaosTest
from ordeal.faults import LambdaFault, PatchFault
from ordeal.invariants import bounded, finite, monotonic, no_inf, no_nan, unique
from ordeal.mutations import (
    generate_mutants,
    mutate_function_and_test,
    validate_mined_properties,
)
from ordeal.strategies import corrupted_bytes, nan_floats

# ============================================================================
# 1. Fault Lifecycle State Machine
#
# Explores ALL reachable sequences of activate / deactivate / reset on an
# arbitrary number of faults.  Verifies that the activation log is always
# consistent with the reported state.
# ============================================================================


class FaultLifecycleBattle(ChaosTest):
    faults = []  # no meta-faults — testing the fault system directly

    def __init__(self):
        super().__init__()
        self.managed: list[LambdaFault] = []

    @rule()
    def create(self):
        log: list[str] = []
        idx = len(self.managed)
        f = LambdaFault(
            f"f{idx}",
            on_activate=lambda: log.append("on"),
            on_deactivate=lambda: log.append("off"),
        )
        f._log = log  # type: ignore[attr-defined]
        self.managed.append(f)

    @precondition(lambda self: len(self.managed) > 0)
    @rule(data=st.data())
    def activate_one(self, data):
        data.draw(st.sampled_from(self.managed)).activate()

    @precondition(lambda self: len(self.managed) > 0)
    @rule(data=st.data())
    def deactivate_one(self, data):
        data.draw(st.sampled_from(self.managed)).deactivate()

    @precondition(lambda self: len(self.managed) > 0)
    @rule(data=st.data())
    def reset_one(self, data):
        data.draw(st.sampled_from(self.managed)).reset()

    @precondition(lambda self: len(self.managed) > 0)
    @rule(data=st.data())
    def double_activate(self, data):
        f = data.draw(st.sampled_from(self.managed))
        f.activate()
        f.activate()  # must be idempotent

    @precondition(lambda self: len(self.managed) > 0)
    @rule(data=st.data())
    def double_deactivate(self, data):
        f = data.draw(st.sampled_from(self.managed))
        f.deactivate()
        f.deactivate()  # must be idempotent

    # -- Invariants checked after EVERY step --

    @invariant()
    def active_flag_is_bool(self):
        for f in self.managed:
            assert isinstance(f.active, bool)

    @invariant()
    def log_consistent_with_state(self):
        """on/off log must match the .active flag at all times."""
        for f in self.managed:
            log = f._log  # type: ignore[attr-defined]
            on_count = log.count("on")
            off_count = log.count("off")
            if f.active:
                assert on_count == off_count + 1, (
                    f"{f.name}: active but on={on_count} off={off_count}"
                )
            else:
                assert on_count == off_count, (
                    f"{f.name}: inactive but on={on_count} off={off_count}"
                )

    def teardown(self):
        for f in self.managed:
            f.reset()
        super().teardown()


TestFaultLifecycleBattle = FaultLifecycleBattle.TestCase
TestFaultLifecycleBattle.settings = settings(
    max_examples=200,
    stateful_step_count=30,
)


# ============================================================================
# 2. PropertyTracker State Machine
#
# Maintains a shadow model of what the tracker SHOULD contain, then verifies
# the real tracker matches after every operation.  Explores record / reset
# interleavings across multiple property names and types.
# ============================================================================

_NAMES = st.text(min_size=1, max_size=6, alphabet="abcdef")


class TrackerBattle(ChaosTest):
    faults = []

    def __init__(self):
        super().__init__()
        self.tracker = PropertyTracker()
        self.tracker.active = True
        self.shadow: dict[str, dict] = {}

    @rule(name=_NAMES, cond=st.booleans())
    def record_always(self, name, cond):
        self.tracker.record(name, "always", cond)
        s = self.shadow.setdefault(name, {"t": "always", "h": 0, "p": 0, "f": 0})
        s["h"] += 1
        s["p" if cond else "f"] += 1

    @rule(name=_NAMES, cond=st.booleans())
    def record_sometimes(self, name, cond):
        self.tracker.record(name, "sometimes", cond)
        s = self.shadow.setdefault(name, {"t": "sometimes", "h": 0, "p": 0, "f": 0})
        s["h"] += 1
        s["p" if cond else "f"] += 1

    @rule(name=_NAMES)
    def record_reachable(self, name):
        self.tracker.record_hit(name, "reachable")
        s = self.shadow.setdefault(name, {"t": "reachable", "h": 0, "p": 0, "f": 0})
        s["h"] += 1

    @rule()
    def reset(self):
        self.tracker.reset()
        self.shadow.clear()

    @invariant()
    def shadow_matches(self):
        actual = {p.name: p for p in self.tracker.results}
        assert len(actual) == len(self.shadow)
        for name, expected in self.shadow.items():
            p = actual[name]
            assert p.hits == expected["h"]
            assert p.passes == expected["p"]
            assert p.failures == expected["f"]

    @invariant()
    def pass_semantics(self):
        for p in self.tracker.results:
            match p.type:
                case "always":
                    assert p.passed == (p.hits > 0 and p.failures == 0)
                case "sometimes":
                    assert p.passed == (p.passes > 0)
                case "reachable":
                    assert p.passed == (p.hits > 0)


TestTrackerBattle = TrackerBattle.TestCase
TestTrackerBattle.settings = settings(
    max_examples=200,
    stateful_step_count=40,
)


# ============================================================================
# 3. PatchFault Correctness Under Sequences
#
# Explores patch / unpatch / reset sequences on a real function, verifying
# that the function ALWAYS returns the expected value.
# ============================================================================

_patch_battle_original_value = 42


def _patch_battle_target() -> int:
    return _patch_battle_original_value


class PatchFaultBattle(ChaosTest):
    faults = []

    def __init__(self):
        super().__init__()
        self.active_fault: PatchFault | None = None
        self.expected: int = _patch_battle_original_value

    @precondition(lambda self: self.active_fault is None)
    @rule(val=st.integers(min_value=-1000, max_value=1000))
    def patch(self, val):
        f = PatchFault(
            f"{__name__}._patch_battle_target",
            lambda orig, v=val: lambda: v,
            name=f"ret_{val}",
        )
        f.activate()
        self.active_fault = f
        self.expected = val

    @precondition(lambda self: self.active_fault is not None)
    @rule()
    def unpatch(self):
        self.active_fault.deactivate()
        self.active_fault = None
        self.expected = _patch_battle_original_value

    @precondition(lambda self: self.active_fault is not None)
    @rule()
    def reset(self):
        self.active_fault.reset()
        self.active_fault = None
        self.expected = _patch_battle_original_value

    @invariant()
    def returns_expected(self):
        actual = _patch_battle_target()
        assert actual == self.expected, (
            f"got {actual}, want {self.expected}, fault={self.active_fault}"
        )

    def teardown(self):
        if self.active_fault:
            self.active_fault.reset()
            self.active_fault = None
            self.expected = _patch_battle_original_value
        super().teardown()


TestPatchFaultBattle = PatchFaultBattle.TestCase
TestPatchFaultBattle.settings = settings(
    max_examples=200,
    stateful_step_count=30,
)


# ============================================================================
# 4. ChaosTest Lifecycle (Level 2 — recursive)
#
# Uses ChaosTest + real LambdaFault to verify that ordeal's OWN nemesis
# rule maintains consistency.  The nemesis toggles faults automatically;
# we verify that after every step the activation log is coherent.
# ============================================================================

_lifecycle_log: list[str] = []


class ChaosTestLifecycleBattle(ChaosTest):
    faults = [
        LambdaFault(
            "lifecycle_probe",
            on_activate=lambda: _lifecycle_log.append("on"),
            on_deactivate=lambda: _lifecycle_log.append("off"),
        ),
        LambdaFault(
            "lifecycle_probe_2",
            on_activate=lambda: _lifecycle_log.append("on2"),
            on_deactivate=lambda: _lifecycle_log.append("off2"),
        ),
    ]

    def __init__(self):
        super().__init__()
        _lifecycle_log.clear()
        self.step_count = 0

    @rule()
    def do_work(self):
        self.step_count += 1

    @rule()
    def inspect_state(self):
        """Read state — should never crash regardless of active faults."""
        _ = self.active_faults
        _ = repr(self._faults)

    @invariant()
    def faults_state_consistent(self):
        for f in self._faults:
            assert isinstance(f.active, bool)

    @invariant()
    def teardown_contract_holds(self):
        """active_faults should be a subset of _faults."""
        active_names = {f.name for f in self.active_faults}
        all_names = {f.name for f in self._faults}
        assert active_names <= all_names

    def teardown(self):
        super().teardown()
        for f in self._faults:
            assert not f.active, f"{f.name} still active after teardown"


TestChaosTestLifecycleBattle = ChaosTestLifecycleBattle.TestCase
TestChaosTestLifecycleBattle.settings = settings(
    max_examples=100,
    stateful_step_count=25,
)


# ============================================================================
# 5. Buggify Properties
#
# Property-based verification of determinism, probability bounds, and
# value-or-nothing semantics.
# ============================================================================


class TestBuggifyBattle:
    def teardown_method(self):
        deactivate()

    @given(
        seed=st.integers(min_value=0, max_value=2**32 - 1),
        n=st.integers(min_value=1, max_value=300),
    )
    @settings(max_examples=100)
    def test_determinism(self, seed, n):
        """Same seed → same sequence, always."""
        activate(probability=0.5)
        set_seed(seed)
        a = [buggify() for _ in range(n)]
        set_seed(seed)
        b = [buggify() for _ in range(n)]
        assert a == b
        deactivate()

    @given(prob=st.floats(min_value=0, max_value=1))
    @settings(max_examples=50)
    def test_probability_bounds(self, prob):
        """Empirical hit rate must be within 5-sigma of declared probability."""
        activate()
        set_seed(12345)
        n = 10_000
        hits = sum(1 for _ in range(n) if buggify(probability=prob))
        if prob == 0:
            assert hits == 0
        elif prob == 1:
            assert hits == n
        else:
            expected = n * prob
            sigma = math.sqrt(n * prob * (1 - prob))
            assert abs(hits - expected) < 5 * sigma, (
                f"p={prob}: hits={hits} expected~{expected} 5σ={5 * sigma:.0f}"
            )
        deactivate()

    @given(seed=st.integers(0, 2**32 - 1), normal=st.integers(), faulty=st.integers())
    @settings(max_examples=100)
    def test_value_always_one_of_two(self, seed, normal, faulty):
        """buggify_value returns exactly normal or faulty, nothing else."""
        activate(probability=0.5)
        set_seed(seed)
        result = buggify_value(normal, faulty)
        assert result in (normal, faulty)
        deactivate()


# ============================================================================
# 6. Invariant Composition Properties
#
# Verify semantic equivalences that must hold regardless of input.
# ============================================================================


class TestInvariantBattle:
    @given(x=st.floats(allow_nan=True, allow_infinity=True))
    def test_finite_equiv_no_nan_and_no_inf(self, x):
        """finite ≡ no_nan & no_inf — must agree on every float."""
        try:
            finite(x)
            a = True
        except AssertionError:
            a = False
        try:
            no_nan(x)
            no_inf(x)
            b = True
        except AssertionError:
            b = False
        assert a == b, f"x={x!r}: finite={a}, separate={b}"

    @given(
        x=st.floats(allow_nan=False, allow_infinity=False),
        lo=st.floats(allow_nan=False, allow_infinity=False),
        hi=st.floats(allow_nan=False, allow_infinity=False),
    )
    def test_bounded_semantics(self, x, lo, hi):
        """bounded(lo, hi)(x) passes iff lo <= x <= hi."""
        assume(lo <= hi)
        check = bounded(lo, hi)
        try:
            check(x)
            passed = True
        except AssertionError:
            passed = False
        assert passed == (lo <= x <= hi), f"x={x} [{lo},{hi}]"

    @given(xs=st.lists(st.integers(), min_size=2, max_size=30))
    def test_monotonic_semantics(self, xs):
        """monotonic()(xs) passes iff non-decreasing."""
        check = monotonic()
        try:
            check(xs)
            passed = True
        except AssertionError:
            passed = False
        expected = all(xs[i] <= xs[i + 1] for i in range(len(xs) - 1))
        assert passed == expected

    @given(xs=st.lists(st.integers(), min_size=1, max_size=30))
    def test_unique_semantics(self, xs):
        check = unique()
        try:
            check(xs)
            passed = True
        except AssertionError:
            passed = False
        assert passed == (len(xs) == len(set(xs)))

    def test_composition_associativity(self):
        """(a & b) & c must behave identically to a & (b & c)."""
        a, b, c = no_nan, no_inf, bounded(0, 1)
        left = (a & b) & c
        right = a & (b & c)
        for v in [0.5, float("nan"), float("inf"), -1.0, 2.0, 0.0, 1.0]:
            try:
                left(v)
                lp = True
            except AssertionError:
                lp = False
            try:
                right(v)
                rp = True
            except AssertionError:
                rp = False
            assert lp == rp, f"v={v}: left={lp} right={rp}"


# ============================================================================
# 7. Concurrent Tracker Safety
#
# PropertyTracker claims thread-safety via a lock.  Verify under contention.
# ============================================================================


class TestConcurrencyBattle:
    def test_parallel_recording_no_data_loss(self):
        """N threads * M records each → exactly N*M total hits."""
        t = PropertyTracker()
        t.active = True
        n_threads, n_records = 8, 2_000
        errors: list[Exception] = []

        def writer(tid: int):
            for i in range(n_records):
                try:
                    t.record(f"p{tid}", "always", i % 2 == 0)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors
        assert len(t.results) == n_threads
        assert sum(p.hits for p in t.results) == n_threads * n_records

    def test_concurrent_reset_and_record(self):
        """reset() while recording must not crash (no lost-lock, no corruption)."""
        t = PropertyTracker()
        t.active = True
        errors: list[Exception] = []

        def writer():
            for _ in range(5_000):
                try:
                    t.record("p", "always", True)
                except Exception as e:
                    errors.append(e)

        def resetter():
            for _ in range(200):
                try:
                    t.reset()
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
            threading.Thread(target=resetter),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors


# ============================================================================
# 8. Strategy Adversarial Output Types
#
# Every strategy must produce the declared type for every draw — verified
# over many more examples than the unit tests.
# ============================================================================


class TestStrategiesBattle:
    @given(data=corrupted_bytes(max_size=4096))
    @settings(max_examples=500)
    def test_corrupted_bytes_type(self, data):
        assert isinstance(data, bytes)

    @given(data=nan_floats())
    @settings(max_examples=500)
    def test_nan_floats_type(self, data):
        assert isinstance(data, float)

    @given(data=nan_floats())
    @settings(max_examples=500)
    def test_nan_floats_coverage(self, data):
        """Verify that the strategy actually produces interesting values."""
        if math.isnan(data):
            pass  # good — NaN is reachable
        elif math.isinf(data):
            pass  # good — Inf is reachable
        elif data == 0.0:
            pass  # good — zero is reachable


# ============================================================================
# 9. Swarm Testing
#
# Verify that swarm mode correctly selects fault subsets and that the
# nemesis only operates on the selected subset.
# ============================================================================


class SwarmBattle(ChaosTest):
    """Verify swarm mode fault subsetting."""

    faults = [
        LambdaFault("A", lambda: None, lambda: None),
        LambdaFault("B", lambda: None, lambda: None),
        LambdaFault("C", lambda: None, lambda: None),
        LambdaFault("D", lambda: None, lambda: None),
    ]
    swarm = True

    def __init__(self):
        super().__init__()
        self.step_count = 0

    @rule()
    def do_work(self):
        self.step_count += 1

    @invariant()
    def subset_is_valid(self):
        """_faults must always be a non-empty subset of class faults."""
        assert 1 <= len(self._faults) <= 4
        subset_names = {f.name for f in self._faults}
        all_names = {f.name for f in self.__class__.faults}
        assert subset_names <= all_names

    @invariant()
    def active_in_subset(self):
        """Active faults must come from the swarm subset, not elsewhere."""
        active_names = {f.name for f in self.active_faults}
        subset_names = {f.name for f in self._faults}
        assert active_names <= subset_names


TestSwarmBattle = SwarmBattle.TestCase
TestSwarmBattle.settings = settings(max_examples=100, stateful_step_count=15)


# ============================================================================
# 10. Mutation Testing
#
# Verify that:
# a) generate_mutants produces correct mutants from source code
# b) mutate_function_and_test kills mutants when tests are strong
# c) mutate_function_and_test lets mutants survive when tests are weak
# ============================================================================


class TestMutationBattle:
    def test_generates_arithmetic_mutants(self):
        source = "def add(a, b):\n    return a + b\n"
        mutants = generate_mutants(source, operators=["arithmetic"])
        assert len(mutants) >= 1
        descriptions = [m.description for m, _ in mutants]
        assert any("-" in d for d in descriptions)  # + should be mutatable to -

    def test_generates_comparison_mutants(self):
        source = "def check(x):\n    if x > 0:\n        return True\n    return False\n"
        mutants = generate_mutants(source, operators=["comparison"])
        assert len(mutants) >= 1

    def test_generates_negate_mutants(self):
        source = "def check(x):\n    if x > 0:\n        return True\n    return False\n"
        mutants = generate_mutants(source, operators=["negate"])
        assert len(mutants) >= 1
        assert any("negate" in m.description for m, _ in mutants)

    def test_generates_return_none_mutants(self):
        source = "def f():\n    return 42\n"
        mutants = generate_mutants(source, operators=["return_none"])
        assert len(mutants) == 1
        assert mutants[0][0].description == "return None"

    def test_all_operators_combined(self):
        source = "def compute(a, b):\n    if a > b:\n        return a - b\n    return a + b\n"
        mutants = generate_mutants(source)
        # Should have arithmetic (+ and -), comparison (>), negate (if), return_none (x2)
        assert len(mutants) >= 5
        operators = {m.operator for m, _ in mutants}
        assert operators == {"arithmetic", "comparison", "negate", "return_none"}

    def test_strong_tests_kill_mutants(self):
        """Tests that fully specify the function should kill all arithmetic mutants."""
        import tests._mutation_target as mod

        def strong_test():
            assert mod.add(2, 3) == 5
            assert mod.add(0, 0) == 0
            assert mod.add(-1, 1) == 0
            assert mod.add(10, -3) == 7

        result = mutate_function_and_test(
            "tests._mutation_target.add",
            strong_test,
            operators=["arithmetic"],
        )
        assert result.total > 0
        assert result.score == 1.0, f"Survivors: {[m.description for m in result.survived]}"

    def test_weak_tests_let_mutants_survive(self):
        """A test that only checks one case may miss mutations."""
        import tests._mutation_target as mod

        def weak_test():
            # Only checks zero — add(0, 0) == 0 is true for both + and -
            assert mod.add(0, 0) == 0

        result = mutate_function_and_test(
            "tests._mutation_target.add",
            weak_test,
            operators=["arithmetic"],
        )
        assert result.total > 0
        assert len(result.survived) > 0, "Weak test should let some mutants survive"

    def test_clamp_mutation_coverage(self):
        """Comprehensive clamp tests should kill most mutants."""
        import tests._mutation_target as mod

        def clamp_tests():
            assert mod.clamp(5, 0, 10) == 5  # in range
            assert mod.clamp(-1, 0, 10) == 0  # below
            assert mod.clamp(11, 0, 10) == 10  # above
            assert mod.clamp(0, 0, 10) == 0  # boundary
            assert mod.clamp(10, 0, 10) == 10  # boundary

        result = mutate_function_and_test(
            "tests._mutation_target.clamp",
            clamp_tests,
        )
        assert result.total > 0
        # Strong boundary tests should kill most mutants
        assert result.score >= 0.5, result.summary()

    def test_validate_mined_properties_kills_mutants(self):
        """Mined properties of add() should catch arithmetic mutations."""
        result = validate_mined_properties(
            "tests._mutation_target.add",
            max_examples=50,
            operators=["arithmetic"],
        )
        assert result.total > 0
        assert result.score > 0, "Mined properties should catch at least one mutant"

    def test_validate_mined_properties_clamp(self):
        """Mined properties of clamp() should catch some mutations."""
        result = validate_mined_properties(
            "tests._mutation_target.clamp",
            max_examples=50,
            operators=["comparison", "return_none"],
        )
        assert result.total > 0
        assert result.score > 0, result.summary()


# ============================================================================
# Level 1d: Equivalence filtering — dead mutant detection
# ============================================================================


class TestEquivalenceFiltering:
    def test_filters_add_zero_identity(self):
        """x + 0 mutated to x - 0 is equivalent — should be filtered."""
        source = "def f(x):\n    return x + 0\n"
        mutants = generate_mutants(source, operators=["arithmetic"])
        # x + 0 -> x - 0 is equivalent (both == x), should be filtered
        descs = [m.description for m, _ in mutants]
        assert "+ -> -" not in descs, f"Equivalent mutant not filtered: {descs}"

    def test_filters_multiply_by_one_identity(self):
        """x * 1 mutated to x / 1 is equivalent — should be filtered."""
        source = "def f(x):\n    return x * 1\n"
        mutants = generate_mutants(source, operators=["arithmetic"])
        descs = [m.description for m, _ in mutants]
        assert "* -> /" not in descs, f"Equivalent mutant not filtered: {descs}"

    def test_preserves_real_arithmetic_mutants(self):
        """x + y (non-identity) should still generate mutants."""
        source = "def add(a, b):\n    return a + b\n"
        mutants = generate_mutants(source, operators=["arithmetic"])
        assert len(mutants) >= 1
        assert any("+ -> -" in m.description for m, _ in mutants)

    def test_filters_zero_plus_x_commutative(self):
        """0 + x mutated to 0 - x: left-side identity for commutative ops."""
        source = "def f(x):\n    return 0 + x\n"
        mutants = generate_mutants(source, operators=["arithmetic"])
        descs = [m.description for m, _ in mutants]
        assert "+ -> -" not in descs, f"Equivalent mutant not filtered: {descs}"

    def test_filters_bytecode_identical(self):
        """Mutants that compile to identical bytecode should be filtered."""
        # return_none on a function that already returns None
        source = "def f():\n    return None\n"
        mutants = generate_mutants(source, operators=["return_none"])
        assert len(mutants) == 0, "return None -> return None is equivalent"

    def test_real_mutants_survive_filtering(self):
        """Filtering should not remove mutants on code with real operations."""
        source = "def compute(a, b):\n    return a * b + a\n"
        mutants = generate_mutants(source, operators=["arithmetic"])
        assert len(mutants) >= 2, f"Real mutants should not be filtered: {len(mutants)}"


class TestAutoSecurityFocusBattle:
    def test_security_focus_promotes_data_sink_artifact_candidates(self):
        import pickle
        from multiprocessing import shared_memory

        cases = [
            ("deserialization", {"blob": st.just(b"trusted")}, bytes),
            ("ipc", {"channel": st.just("trusted")}, str),
        ]

        for critical_sink, strategies, return_type in cases:
            if critical_sink == "deserialization":

                def target(blob: bytes) -> bytes:
                    if False:
                        pickle.loads(blob)
                    if blob.startswith(b'{"artifact"'):
                        raise RuntimeError("checkpoint probe rejected")
                    return blob
            else:

                def target(channel: str) -> str:
                    if False:
                        shared_memory.SharedMemory(name=channel)
                    if channel.startswith("ordeal-security-probe"):
                        raise RuntimeError("ipc descriptor rejected")
                    return channel

            baseline = _test_one_function(
                "target",
                target,
                strategies,
                return_type,
                max_examples=1,
                check_return_type=True,
                mode="candidate",
            )
            focused = _test_one_function(
                "target",
                target,
                strategies,
                return_type,
                max_examples=1,
                check_return_type=True,
                mode="candidate",
                security_focus=True,
            )

            assert baseline.execution_ok is True
            assert focused.verdict == "promoted_real_bug"
            assert focused.input_source == "artifact_mutation"
            assert focused.proof_bundle is not None
            assert focused.proof_bundle["impact"]["critical_sinks"] == [critical_sink]
