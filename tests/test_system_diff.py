"""System-level contracts for differential refactor reports."""

from __future__ import annotations

import time
from typing import Any

import ordeal
from ordeal.diff import FaultEvent, Operation, PerformanceBudget, diff


class CounterA:
    def __init__(self) -> None:
        self.value = 0

    def add(self, amount: int) -> int:
        self.value += amount
        return self.value


class CounterB(CounterA):
    pass


def test_system_diff_types_are_public_exports() -> None:
    assert ordeal.Operation is Operation
    assert ordeal.FaultEvent is FaultEvent
    assert ordeal.PerformanceBudget is PerformanceBudget


def test_system_diff_compares_operation_outcomes_and_public_state() -> None:
    result = diff(
        CounterA,
        CounterB,
        sequence=[Operation("add", args=(2,)), Operation("add", args=(-1,))],
    )

    assert result.no_divergence_found
    assert result.interface.matches
    assert result.state_checked
    assert not result.side_effects_checked
    assert [step.outcome_match for step in result.steps] == [True, True]
    assert result.original_length == result.minimized_length == 2


def test_system_diff_reports_public_export_and_signature_changes() -> None:
    class APIA:
        def fetch(self, key: str) -> str:
            return key

        def put(self, key: str, value: str) -> None:
            return None

    class APIB:
        def fetch(self, key: str, default: str = "") -> str:
            return key or default

    result = diff(APIA, APIB, sequence=[])

    assert result.divergent
    assert result.interface.missing_from_b == ("put",)
    assert result.interface.signature_mismatches == ("fetch",)
    assert "interface" in result.summary().lower()


def test_system_diff_reports_dynamic_public_export_changes() -> None:
    class APIA:
        def __init__(self) -> None:
            self.client = "v1"

    class APIB:
        pass

    result = diff(APIA, APIB, sequence=[])

    assert result.interface.missing_from_b == ("client",)


def test_system_diff_compares_exceptions_and_declared_side_effects() -> None:
    class ServiceA:
        def __init__(self) -> None:
            self.events: list[str] = []

        def submit(self, value: str) -> str:
            self.events.append(f"accepted:{value}")
            raise ValueError("retry")

    class ServiceB(ServiceA):
        def submit(self, value: str) -> str:
            self.events.append(f"queued:{value}")
            raise ValueError("retry")

    result = diff(
        ServiceA,
        ServiceB,
        sequence=[Operation("submit", args=("order-1",))],
        state=lambda _service: {},
        side_effects=lambda service: list(service.events),
        minimize=False,
    )

    assert result.divergent
    assert result.steps[0].outcome_match
    assert not result.steps[0].side_effects_match
    assert result.mismatches[0].kind == "side_effects"


class RecoveringStore:
    def __init__(self) -> None:
        self.timeout = False

    def noop(self) -> None:
        return None

    def read(self) -> str:
        if self.timeout:
            raise TimeoutError("backend timed out")
        return "ready"


class StickyStore(RecoveringStore):
    pass


def apply_timeout(store: RecoveringStore, event: FaultEvent) -> None:
    if event.action == "activate":
        store.timeout = True
    elif event.action == "deactivate" and not isinstance(store, StickyStore):
        store.timeout = False


def test_system_diff_replays_and_minimizes_one_fault_schedule_for_both_versions() -> None:
    activate = FaultEvent("timeout", "activate", {"after_ms": 10})
    deactivate = FaultEvent("timeout", "deactivate")
    sequence = [
        Operation("noop"),
        activate,
        Operation("read"),
        deactivate,
        Operation("read"),
    ]

    result = diff(
        RecoveringStore,
        StickyStore,
        sequence=sequence,
        apply_fault=apply_timeout,
        state=lambda _service: {},
    )

    assert result.divergent
    assert result.original_length == 5
    assert result.sequence == (activate, deactivate, Operation("read"))
    assert result.fault_schedule == (activate, deactivate)
    assert result.fault_schedule_replayed
    assert result.recovery_parity is False
    assert result.replay_attempts == 2
    assert result.replay_matches == 2
    assert result.replay_verified is True


def test_system_diff_reports_matching_recovery_behavior() -> None:
    sequence = [
        FaultEvent("timeout", "activate"),
        Operation("read"),
        FaultEvent("timeout", "deactivate"),
        Operation("read"),
    ]
    result = diff(
        RecoveringStore,
        RecoveringStore,
        sequence=sequence,
        apply_fault=apply_timeout,
        state=lambda _service: {},
        minimize=False,
    )

    assert result.no_divergence_found
    assert result.fault_schedule_replayed
    assert result.recovery_parity is True


def test_system_diff_keeps_unstable_divergence_inconclusive() -> None:
    calls = {"candidate": 0}

    class Stable:
        def read(self) -> int:
            return 0

    class Flaky:
        def read(self) -> int:
            calls["candidate"] += 1
            return 1 if calls["candidate"] == 1 else 0

    result = diff(
        Stable,
        Flaky,
        sequence=[Operation("read")],
        minimize=False,
        replay_attempts=2,
    )

    assert result.status == "inconclusive"
    assert len(result.mismatches) == 1
    assert result.replay_matches == 0
    assert result.replay_verified is False
    assert not result.no_divergence_found
    assert "INCONCLUSIVE" in result.summary()


def test_performance_budget_is_a_separate_measured_contract() -> None:
    class Fast:
        def work(self) -> str:
            return "done"

    class Slow(Fast):
        def work(self) -> str:
            time.sleep(0.003)
            return "done"

    result = diff(
        Fast,
        Slow,
        sequence=[Operation("work")],
        performance=PerformanceBudget(
            max_candidate_seconds=0.001,
            samples=2,
            warmup=0,
        ),
    )

    assert result.no_divergence_found
    assert result.performance is not None
    assert result.performance.within_budget is False
    assert result.performance.candidate_median_seconds >= 0.003
    assert "BUDGET EXCEEDED" in result.summary()


def test_operation_inputs_are_isolated_between_systems() -> None:
    class Mutator:
        def consume(self, values: list[int]) -> int:
            values.pop()
            return len(values)

    values = [1, 2]
    result = diff(
        Mutator,
        Mutator,
        sequence=[Operation("consume", args=(values,))],
    )

    assert result.no_divergence_found
    assert values == [1, 2]


def test_minimization_preserves_the_exact_first_divergence() -> None:
    class VersionA:
        def first(self) -> int:
            return 1

        def second(self) -> int:
            return 10

    class VersionB:
        def first(self) -> int:
            return 2

        def second(self) -> int:
            return 20

    result = diff(
        VersionA,
        VersionB,
        sequence=[Operation("first"), Operation("second")],
        state=lambda _system: {},
    )

    assert result.sequence == (Operation("first"),)
    assert result.mismatches[0].observed_a == 1
    assert result.mismatches[0].observed_b == 2


def test_step_results_are_frozen_before_later_operations_mutate_state() -> None:
    class Service:
        def __init__(self) -> None:
            self.values: list[int] = []

        def expose(self) -> list[int]:
            return self.values

        def add(self) -> None:
            self.values.append(1)

    result = diff(
        Service,
        Service,
        sequence=[Operation("expose"), Operation("add")],
        minimize=False,
    )

    assert result.steps[0].outcome_a == []
    assert result.steps[0].outcome_b == []


def test_fault_event_parameters_are_preserved() -> None:
    event = FaultEvent("corruption", parameters={"payload": {"bad": True}})

    assert event.parameters == {"payload": {"bad": True}}
    assert isinstance(event.parameters, dict)


def test_performance_budget_validates_thresholds() -> None:
    try:
        PerformanceBudget(max_slowdown=0.0)
    except ValueError as exc:
        assert "max_slowdown" in str(exc)
    else:
        raise AssertionError("zero slowdown budget should be rejected")


def test_system_factories_must_be_zero_argument_callables() -> None:
    def factory(required: Any) -> CounterA:
        return CounterA()

    try:
        diff(factory, CounterB, sequence=[])
    except TypeError as exc:
        assert "zero-argument factory" in str(exc)
    else:
        raise AssertionError("invalid system factory should fail closed")
