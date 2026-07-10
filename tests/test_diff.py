"""Tests for ordeal.diff — differential testing."""

from __future__ import annotations

import json
import runpy
import subprocess
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import hypothesis.strategies as st
import pytest

from ordeal.cli import main
from ordeal.diff import SideEffect, diff
from ordeal.finding_evidence import _sha256_json


def add_v1(x: int, y: int) -> int:
    return x + y


def add_v2(x: int, y: int) -> int:
    return x + y


def add_buggy(x: int, y: int) -> int:
    if x == 0:
        return y + 1  # off-by-one when x=0
    return x + y


def scale_v1(x: float, factor: float) -> float:
    return x * factor


def scale_v2(x: float, factor: float) -> float:
    return x * factor + 1e-10  # tiny drift


_DURABLE_FUNCTION_FIXED = False


def durable_function_baseline(x: int) -> int:
    return x


def durable_function_candidate(x: int) -> int:
    return x if _DURABLE_FUNCTION_FIXED else x + 1


class TestDiff:
    def test_assertion_from_implementation_is_a_divergence(self):
        def asserting(x: int) -> int:
            raise AssertionError("implementation rejected input")

        def returning(x: int) -> int:
            return x

        result = diff(asserting, returning, max_examples=1, x=st.just(0))

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.outcome_a.exception_type is AssertionError
        assert result.witness.outcome_b.return_value == 0

    def test_identical_mutators_receive_isolated_inputs(self):
        def mutate(values: list[int]) -> int:
            values.pop()
            return len(values)

        original = [1, 2]
        result = diff(mutate, mutate, max_examples=1, values=original)

        assert result.status == "no_divergence_observed"
        assert result.witness is None
        assert original == [1, 2]

    def test_mismatch_witness_preserves_original_mutable_input(self):
        def pop_last(values: list[int]) -> int:
            values.pop()
            return 0

        def clear(values: list[int]) -> int:
            values.clear()
            return 1

        original = [1, 2]
        result = diff(pop_last, clear, max_examples=1, values=original)

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.args == {"values": (1, 2)}
        assert original == [1, 2]

    def test_matching_exceptions_are_observed_agreement(self):
        def raises_a(x: int) -> int:
            raise ValueError(f"invalid: {x}")

        def raises_b(x: int) -> int:
            raise ValueError(f"invalid: {x}")

        result = diff(raises_a, raises_b, max_examples=1, x=st.just(3))

        assert result.status == "no_divergence_observed"

    def test_different_exception_messages_are_a_divergence(self):
        def raises_a(x: int) -> int:
            raise ValueError("left")

        def raises_b(x: int) -> int:
            raise ValueError("right")

        result = diff(raises_a, raises_b, max_examples=1, x=st.just(0))

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.outcome_a.exception_type is ValueError
        assert result.witness.outcome_b.exception_type is ValueError

    def test_different_exception_types_are_a_divergence(self):
        def raises_value_error(x: int) -> int:
            raise ValueError("invalid")

        def raises_type_error(x: int) -> int:
            raise TypeError("invalid")

        result = diff(
            raises_value_error,
            raises_type_error,
            max_examples=1,
            x=st.just(0),
        )

        assert result.status == "divergent"

    def test_returned_exception_is_not_the_same_as_raising_it(self):
        def returns_error(x: int) -> Exception:
            return ValueError("invalid")

        def raises_error(x: int) -> Exception:
            raise ValueError("invalid")

        result = diff(returns_error, raises_error, max_examples=1, x=st.just(0))

        assert result.status == "divergent"

    def test_custom_comparator_assertion_is_not_swallowed(self):
        def broken_comparator(a: int, b: int) -> bool:
            raise AssertionError("comparator failed")

        with pytest.raises(AssertionError, match="comparator failed"):
            diff(
                add_v1,
                add_v2,
                max_examples=1,
                compare=broken_comparator,
                x=st.just(1),
                y=st.just(2),
            )

    def test_uncopyable_inputs_fail_instead_of_sharing_state(self):
        class Uncopyable:
            def __deepcopy__(self, memo: dict) -> "Uncopyable":
                raise RuntimeError("cannot copy")

        def identity(value: Uncopyable) -> Uncopyable:
            return value

        result = diff(identity, identity, max_examples=1, value=Uncopyable())

        assert result.status == "inconclusive"
        assert "reconstruct" in (result.reason or "")

    def test_deepcopy_that_returns_shared_mutable_input_fails_closed(self) -> None:
        class SharedDomainObject:
            def __init__(self) -> None:
                self.events: list[str] = []

            def __deepcopy__(self, memo: dict[int, object]) -> SharedDomainObject:
                return self

        def left(value: SharedDomainObject) -> None:
            value.events.append("left")

        def right(value: SharedDomainObject) -> None:
            value.events.append("right")

        original = SharedDomainObject()
        result = diff(left, right, max_examples=1, value=original)

        assert result.status == "inconclusive"
        assert "shared mutable" in (result.reason or "")
        assert original.events == []

    def test_sampled_agreement_is_not_general_equivalence(self):
        result = diff(add_v1, add_v2, max_examples=10)

        assert result.status == "no_divergence_observed"
        assert "NO DIVERGENCE OBSERVED" in result.summary()

    def test_equivalent_functions(self):
        result = diff(add_v1, add_v2, max_examples=50)
        assert result.status == "no_divergence_observed"
        assert result.total == 50

    def test_detects_mismatch(self):
        result = diff(add_v1, add_buggy, max_examples=100)
        assert result.status == "divergent"
        assert result.witness is not None

    def test_summary(self):
        result = diff(add_v1, add_v2, max_examples=10)
        s = result.summary()
        assert "NO DIVERGENCE OBSERVED" in s

    def test_summary_divergent(self):
        result = diff(add_v1, add_buggy, max_examples=100)
        s = result.summary()
        assert "DIVERGENT" in s

    def test_with_tolerance(self):
        result = diff(scale_v1, scale_v2, max_examples=50, atol=1e-8)
        assert result.status == "no_divergence_observed"

    def test_without_tolerance_catches_drift(self):
        result = diff(scale_v1, scale_v2, max_examples=50)
        assert result.status == "divergent"

    def test_custom_comparator(self):
        def loose(a: int, b: int) -> bool:
            return abs(a - b) <= 1

        result = diff(add_v1, add_buggy, max_examples=50, compare=loose)
        assert result.status == "no_divergence_observed"

    def test_with_fixture(self):
        import hypothesis.strategies as st

        result = diff(
            add_v1,
            add_v2,
            max_examples=20,
            x=st.integers(0, 10),
            y=st.integers(0, 10),
        )
        assert result.status == "no_divergence_observed"

    def test_plain_value_fixture(self):
        result = diff(add_v1, add_v2, max_examples=10, x=5, y=3)
        assert result.status == "no_divergence_observed"


class TestOutcomeEnvelope:
    def test_compares_mutated_arguments(self) -> None:
        def left(values: list[int]) -> None:
            values.append(1)

        def right(values: list[int]) -> None:
            values.append(2)

        result = diff(left, right, values=[], max_examples=5)

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.differences == ("mutated_arguments",)
        assert result.witness.outcome_a.mutated_arguments["values"] == (1,)
        assert result.witness.outcome_b.mutated_arguments["values"] == (2,)

    def test_reconstructs_and_compares_bound_receiver_state(self) -> None:
        class Counter:
            def __init__(self) -> None:
                self.value = 0

            def left(self, amount: int) -> None:
                self.value += amount

            def right(self, amount: int) -> None:
                self.value += amount

        receiver = Counter()
        result = diff(receiver.left, receiver.right, amount=2, max_examples=5)

        assert result.status == "no_divergence_observed"
        assert receiver.value == 0

    def test_detects_bound_receiver_state_divergence(self) -> None:
        class Counter:
            def __init__(self) -> None:
                self.value = 0

            def left(self, amount: int) -> None:
                self.value += amount

            def right(self, amount: int) -> None:
                self.value += amount + 1

        receiver = Counter()
        result = diff(receiver.left, receiver.right, amount=2, max_examples=5)

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.differences == ("receiver_state",)
        assert result.witness.outcome_a.receiver_state["value"] == 2
        assert result.witness.outcome_b.receiver_state["value"] == 3
        assert receiver.value == 0

    def test_selected_side_effects_are_isolated_compared_and_restored(self) -> None:
        events: list[str] = ["baseline"]

        def capture() -> list[str]:
            return list(events)

        def restore(snapshot: list[str]) -> None:
            events[:] = snapshot

        def left(value: int) -> None:
            events.append(f"event:{value}")

        def right(value: int) -> None:
            events.append(f"event:{value}")

        result = diff(
            left,
            right,
            value=3,
            max_examples=5,
            side_effects={"events": SideEffect(capture=capture, restore=restore)},
        )

        assert result.status == "no_divergence_observed"
        assert events == ["baseline"]

    def test_detects_selected_side_effect_divergence(self) -> None:
        events: list[str] = []

        def capture() -> list[str]:
            return list(events)

        def restore(snapshot: list[str]) -> None:
            events[:] = snapshot

        def left(value: int) -> None:
            events.append(f"left:{value}")

        def right(value: int) -> None:
            events.append(f"right:{value}")

        result = diff(
            left,
            right,
            value=3,
            max_examples=5,
            side_effects={"events": SideEffect(capture=capture, restore=restore)},
        )

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.differences == ("side_effects",)
        assert result.witness.outcome_a.side_effects["events"] == ("left:3",)
        assert result.witness.outcome_b.side_effects["events"] == ("right:3",)
        assert events == []

    def test_reconstruction_failure_is_inconclusive(self) -> None:
        class Uncopyable:
            def __deepcopy__(self, memo: dict[int, object]) -> Uncopyable:
                raise TypeError("cannot copy")

        def identity(value: Uncopyable) -> Uncopyable:
            return value

        result = diff(identity, identity, value=Uncopyable(), max_examples=5)

        assert result.status == "inconclusive"
        assert result.witness is None
        assert "reconstruct" in (result.reason or "")

    def test_explicit_full_envelope_proof_has_a_distinct_status(self) -> None:
        result = diff(
            add_v1,
            add_v2,
            x=1,
            y=2,
            max_examples=5,
            equivalence_proof=lambda _left, _right: True,
        )

        assert result.status == "proven_equivalent"

    def test_non_replayable_mismatch_is_inconclusive(self) -> None:
        calls = 0

        def left(value: int) -> int:
            return 0

        def unstable(value: int) -> int:
            nonlocal calls
            calls += 1
            return int(calls == 1)

        result = diff(left, unstable, value=0, max_examples=1)

        assert result.status == "inconclusive"
        assert result.witness is None
        assert "replay" in (result.reason or "").lower()

    def test_witness_is_single_immutable_minimized_and_replay_verified(self) -> None:
        def left(value: int) -> int:
            return 0

        def right(value: int) -> int:
            return int(value >= 7)

        result = diff(left, right, value=st.integers(min_value=0, max_value=100))

        assert result.status == "divergent"
        assert result.witness is not None
        assert len(result.mismatches) == 1
        assert result.witness.args == {"value": 7}
        assert result.witness.replay_verified is True
        with pytest.raises(FrozenInstanceError):
            result.witness.replay_verified = False  # type: ignore[misc]
        with pytest.raises(TypeError):
            result.witness.args["value"] = 8  # type: ignore[index]


class TestDurableDivergenceEvidence:
    def test_artifact_binds_revisions_comparison_input_observations_and_replay(
        self,
    ) -> None:
        result = diff(
            add_v1,
            add_buggy,
            x=0,
            y=3,
            max_examples=1,
            replay_attempts=3,
        )

        assert result.status == "divergent"
        assert len(result.artifacts) == 1
        artifact = result.artifacts[0]
        assert artifact["schema"] == "ordeal.divergence-evidence/v1"
        assert artifact["status"] == "supported"
        assert artifact["source_binding"] == {"status": "complete", "missing": []}
        assert len(artifact["revisions"]["a"]["source_sha256"]) == 64
        assert len(artifact["revisions"]["b"]["source_sha256"]) == 64
        assert len(artifact["comparison"]["comparator"]["source_sha256"]) == 64
        assert len(artifact["comparison"]["normalizer"]["source_sha256"]) == 64
        assert artifact["witness"]["original_input"] == {"x": 0, "y": 3}
        assert artifact["witness"]["input"] == {"x": 0, "y": 3}
        assert artifact["observations"]["a"]["value"] == 3
        assert artifact["observations"]["b"]["value"] == 4
        assert artifact["replay"]["attempts"] == 3
        assert artifact["replay"]["exact_matches"] == 3
        assert len(artifact["replay"]["observed_signatures"]) == 3
        assert all(
            signature == artifact["replay"]["expected_signature"]
            for signature in artifact["replay"]["observed_signatures"]
        )
        assert "general equivalence" in " ".join(artifact["boundaries"]["does_not_establish"])
        json.dumps(artifact)

    def test_custom_normalizer_and_comparator_are_source_bound(self) -> None:
        def left(value: int) -> dict[str, int]:
            return {"score": value}

        def right(value: int) -> dict[str, int]:
            return {"score": value + 1}

        def score(payload: dict[str, int]) -> int:
            return payload["score"]

        def exact(left_score: int, right_score: int) -> bool:
            return left_score == right_score

        result = diff(
            left,
            right,
            value=2,
            max_examples=1,
            normalize=score,
            compare=exact,
        )

        artifact = result.artifacts[0]
        assert artifact["comparison"]["normalizer"]["kind"] == "custom"
        assert artifact["comparison"]["normalizer"]["target"].endswith(".<locals>.score")
        assert artifact["comparison"]["comparator"]["kind"] == "custom"
        assert artifact["comparison"]["comparator"]["target"].endswith(".<locals>.exact")
        assert artifact["observations"]["a"]["normalized_value"] == 2
        assert artifact["observations"]["b"]["normalized_value"] == 3

    def test_normalizer_removes_nondeterministic_request_ids_during_replay(self) -> None:
        request_id = 0

        def next_id() -> str:
            nonlocal request_id
            request_id += 1
            return f"req-{request_id}"

        def left() -> dict[str, str]:
            return {"request_id": next_id(), "status": "old"}

        def right() -> dict[str, str]:
            return {"request_id": next_id(), "status": "new"}

        def stable_fields(payload: dict[str, str]) -> dict[str, str]:
            return {"status": payload["status"]}

        result = diff(
            left,
            right,
            max_examples=1,
            replay_attempts=3,
            normalize=stable_fields,
        )

        assert result.status == "divergent"
        assert result.witness is not None
        assert result.witness.replay_matches == 3
        artifact = result.artifacts[0]
        assert artifact["replay"]["exact_matches"] == 3
        assert artifact["observations"]["a"]["value"]["request_id"] == "req-1"
        assert artifact["observations"]["b"]["value"]["request_id"] == "req-2"

    def test_artifact_dir_persists_the_canonical_json(self, tmp_path) -> None:
        result = diff(
            add_v1,
            add_buggy,
            x=0,
            y=0,
            max_examples=1,
            artifact_dir=tmp_path / "divergences",
        )

        assert len(result.artifact_paths) == 1
        path = result.artifact_paths[0]
        with open(path, encoding="utf-8") as artifact_file:
            persisted = json.load(artifact_file)
        assert persisted == result.artifacts[0]
        assert path.endswith(f"{persisted['artifact_id']}.json")

    def test_function_diff_generates_a_source_bound_ci_regression(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        global _DURABLE_FUNCTION_FIXED

        _DURABLE_FUNCTION_FIXED = False
        regression_path = tmp_path / "tests" / "test_function_diff_regression.py"
        manifest_path = tmp_path / "tests" / "ordeal-regressions.json"
        result = diff(
            durable_function_baseline,
            durable_function_candidate,
            x=0,
            max_examples=1,
            artifact_dir=tmp_path / ".ordeal" / "findings",
            regression_path=regression_path,
            manifest_path=manifest_path,
        )

        assert result.divergent
        assert result.regression_error is None
        assert result.regression_path == regression_path.as_posix()
        assert result.manifest_path == manifest_path.as_posix()
        generated_test = runpy.run_path(str(regression_path))["test_ordeal_diff_regression"]
        with pytest.raises(AssertionError, match="not fixed"):
            generated_test()

        _DURABLE_FUNCTION_FIXED = True
        try:
            generated_test()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            record = manifest["regressions"][0]
            assert record["change_kind"] == "function"
            assert record["test_basis"] == "paired_minimized_witness"
            assert record["change_artifact_ids"] == [result.artifacts[0]["artifact_id"]]
            assert record["binding"]["test_name"] == "test_ordeal_diff_regression"
            monkeypatch.setattr(
                subprocess,
                "run",
                lambda *args, **kwargs: SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            )
            assert main(["verify", "--ci", "--manifest", str(manifest_path)]) == 0
            artifact_path = (
                tmp_path / ".ordeal" / "findings" / (f"{result.artifacts[0]['artifact_id']}.json")
            )
            original_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            tampered = json.loads(json.dumps(original_artifact))
            tampered["claim"] = "edited while retaining the trusted artifact ID"
            artifact_path.write_text(json.dumps(tampered), encoding="utf-8")
            assert main(["verify", "--ci", "--manifest", str(manifest_path)]) == 2

            invalid_claims = (
                ("status", "exploratory"),
                ("replay.status", "failed"),
                ("minimization.status", "not_run"),
            )
            for field, value in invalid_claims:
                invalid = json.loads(json.dumps(original_artifact))
                if "." in field:
                    section, key = field.split(".", 1)
                    invalid[section][key] = value
                else:
                    invalid[field] = value
                unhashed = dict(invalid)
                unhashed.pop("artifact_id")
                invalid_id = f"div_{_sha256_json(unhashed)[:16]}"
                invalid["artifact_id"] = invalid_id
                artifact_path.write_text(json.dumps(invalid), encoding="utf-8")
                invalid_manifest = json.loads(json.dumps(manifest))
                invalid_manifest["regressions"][0]["change_artifact_ids"] = [invalid_id]
                manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")
                assert main(["verify", "--ci", "--manifest", str(manifest_path)]) == 2
        finally:
            _DURABLE_FUNCTION_FIXED = False
