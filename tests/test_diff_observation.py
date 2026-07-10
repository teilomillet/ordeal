"""Cross-mode contracts for canonical differential observations and replay."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import ordeal._diff_worker as revision_worker
import ordeal._observation as observation
import ordeal.diff as diff_module
import ordeal.system_diff as system_diff_module
from ordeal.diff import Operation, diff


class HostileResult:
    """Domain value whose equality and repr must never become evidence."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        raise AssertionError("candidate equality was called")

    def __repr__(self) -> str:
        raise AssertionError("candidate repr was called")


class AliasingClone:
    """Deepcopy-shaped value that secretly keeps one mutable child shared."""

    def __init__(self) -> None:
        self.values: list[int] = []

    def __deepcopy__(self, memo: dict[int, object]) -> AliasingClone:
        clone = object.__new__(AliasingClone)
        clone.values = self.values
        return clone


def test_all_diff_modes_load_the_canonical_observation_layer() -> None:
    assert diff_module.observe is observation.observe
    assert system_diff_module.observe is observation.observe
    assert (
        Path(revision_worker._OBSERVATION.__file__).resolve()
        == Path(observation.__file__).resolve()
    )


def test_exact_replay_rejects_a_corrupted_recorded_signature() -> None:
    expected = observation.observe({"result": HostileResult(1)})
    replayed = observation.observe({"result": HostileResult(1)})

    assert observation.exact_replay_match(
        expected,
        replayed,
        recorded_expected_signature=expected.signature,
    )
    assert not observation.exact_replay_match(
        expected,
        replayed,
        recorded_expected_signature="0" * 64,
    )


def test_function_diff_structurally_compares_hostile_domain_objects() -> None:
    def left() -> HostileResult:
        return HostileResult(1)

    def right() -> HostileResult:
        return HostileResult(2)

    result = diff(left, right, max_examples=1)

    assert result.status == "divergent"
    assert result.witness is not None
    assert result.witness.replay_verified


def test_function_artifact_never_calls_hostile_input_repr() -> None:
    def left(_value: HostileResult) -> int:
        return 1

    def right(_value: HostileResult) -> int:
        return 2

    result = diff(left, right, _value=HostileResult(3), max_examples=1)

    assert result.status == "divergent"
    assert result.artifacts[0]["witness"]["canonical_input"]["schema"] == (
        "ordeal.canonical-observation/v1"
    )


def test_function_artifact_hash_distinguishes_alias_topology() -> None:
    def left(_first: list[int], _second: list[int]) -> int:
        return 1

    def right(_first: list[int], _second: list[int]) -> int:
        return 2

    shared: list[int] = []
    aliased = diff(left, right, _first=shared, _second=shared, max_examples=1)
    disjoint = diff(left, right, _first=[], _second=[], max_examples=1)

    assert aliased.artifacts[0]["witness"]["input"] == disjoint.artifacts[0]["witness"]["input"]
    assert aliased.artifacts[0]["witness"]["sha256"] != disjoint.artifacts[0]["witness"]["sha256"]


def test_function_diff_displays_ordered_dict_contents_without_target_repr() -> None:
    def left() -> OrderedDict[str, int]:
        return OrderedDict((("first", 1), ("second", 2)))

    def right() -> OrderedDict[str, int]:
        return OrderedDict((("first", 1), ("second", 3)))

    result = diff(left, right, max_examples=1)

    assert result.status == "divergent"
    assert result.mismatches[0].output_a == {"first": 1, "second": 2}
    assert result.mismatches[0].output_b == {"first": 1, "second": 3}


def test_function_diff_rejects_nested_mutable_aliases_from_deepcopy() -> None:
    original = AliasingClone()

    def use(value: AliasingClone) -> int:
        value.values.append(1)
        return len(value.values)

    result = diff(use, use, value=original, max_examples=1)

    assert result.status == "inconclusive"
    assert "mutable alias" in (result.reason or "")
    assert original.values == []


def test_function_diff_is_inconclusive_for_opaque_observations() -> None:
    def opaque() -> object:
        return object()

    result = diff(opaque, opaque, max_examples=1)

    assert result.status == "inconclusive"
    assert "losslessly" in (result.reason or "")


def test_system_diff_structurally_compares_hostile_domain_objects() -> None:
    class Left:
        def read(self) -> HostileResult:
            return HostileResult(1)

    class Right:
        def read(self) -> HostileResult:
            return HostileResult(2)

    result = diff(Left, Right, sequence=[Operation("read")], replay_attempts=2)

    assert result.status == "divergent"
    assert result.replay_matches == 2
    assert result.expected_signature is not None
    assert result.observed_signatures == (result.expected_signature,) * 2


def test_system_diff_rejects_aliasing_event_clones_as_inconclusive() -> None:
    original = AliasingClone()

    class System:
        def use(self, value: AliasingClone) -> int:
            value.values.append(1)
            return len(value.values)

    result = diff(System, System, sequence=[Operation("use", args=(original,))])

    assert result.status == "inconclusive"
    assert "mutable alias" in (result.reason or "")
    assert original.values == []


def test_system_diff_is_inconclusive_for_opaque_observations() -> None:
    class System:
        def opaque(self) -> object:
            return object()

    result = diff(System, System, sequence=[Operation("opaque")])

    assert result.status == "inconclusive"
    assert "losslessly" in (result.reason or "")
