"""Tests for the evidence-first base-to-candidate migration workflow."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import ordeal.migration as migration
from ordeal.audit import ModuleAudit
from ordeal.auto import ContractCheck, FunctionResult, ScanResult
from ordeal.cli import main
from ordeal.diff import DiffOutcome, DiffResult, DiffWitness, Mismatch
from ordeal.diff import diff as run_diff
from ordeal.mine import MinedProperty, MineModuleResult, MineResult
from ordeal.mutations import Mutant, MutationResult


def _module(name: str, **functions: Callable[..., object]) -> ModuleType:
    module = ModuleType(name)
    for function_name, function in functions.items():
        function.__module__ = name
        setattr(module, function_name, function)
    return module


def _audit_result(module: str) -> ModuleAudit:
    return ModuleAudit(module=module)


def _mine_result(module: str) -> MineModuleResult:
    return MineModuleResult(
        module=module,
        per_function={
            "compute": MineResult(
                function="compute",
                examples=3,
                properties=[MinedProperty("output type consistent", 3, 3)],
            )
        },
    )


def _mutation_result(
    module: str,
    *,
    killed: bool = True,
    qualname: str | None = None,
) -> MutationResult:
    return MutationResult(
        target=module,
        mutants=[
            Mutant(
                "return_none",
                "return None",
                1,
                0,
                killed=killed,
                qualname=qualname,
            )
        ],
    )


def _scan_result(module: str) -> ScanResult:
    return ScanResult(
        module=module,
        functions=[FunctionResult(name="compute", passed=True)],
    )


def _divergent_result(
    mismatch: Mismatch,
    *,
    differences: tuple[str, ...] = ("return_value",),
    mutated_a: dict[str, object] | None = None,
    mutated_b: dict[str, object] | None = None,
    side_effects_a: dict[str, object] | None = None,
    side_effects_b: dict[str, object] | None = None,
) -> DiffResult:
    def outcome(
        value: object,
        *,
        mutated: dict[str, object] | None,
        side_effects: dict[str, object] | None,
    ) -> DiffOutcome:
        if isinstance(value, Exception):
            return DiffOutcome(
                returned=False,
                return_value=None,
                exception_type=type(value),
                exception_message=str(value),
                mutated_arguments=mismatch.args if mutated is None else mutated,
                receiver_state=None,
                side_effects=side_effects or {},
            )
        return DiffOutcome(
            returned=True,
            return_value=value,
            exception_type=None,
            exception_message=None,
            mutated_arguments=mismatch.args if mutated is None else mutated,
            receiver_state=None,
            side_effects=side_effects or {},
        )

    witness = DiffWitness(
        args=mismatch.args,
        outcome_a=outcome(
            mismatch.output_a,
            mutated=mutated_a,
            side_effects=side_effects_a,
        ),
        outcome_b=outcome(
            mismatch.output_b,
            mutated=mutated_b,
            side_effects=side_effects_b,
        ),
        differences=differences,
        replay_attempts=2,
        replay_matches=2,
        replay_verified=True,
    )
    return DiffResult(
        "compute",
        "compute",
        1,
        [mismatch],
        status="divergent",
        witness=witness,
    )


def _install_common_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mine_result: MineModuleResult,
    diff_result: DiffResult,
    calls: list[str],
) -> None:
    monkeypatch.setattr(
        migration,
        "_audit",
        lambda module, **kwargs: calls.append("audit") or _audit_result(module),
    )
    monkeypatch.setattr(
        migration,
        "_mine_module",
        lambda module, **kwargs: calls.append("mine") or mine_result,
    )
    monkeypatch.setattr(
        migration,
        "_diff",
        lambda base, candidate, **kwargs: calls.append("diff") or diff_result,
    )
    monkeypatch.setattr(
        migration,
        "_mutate",
        lambda module, **kwargs: calls.append("mutate") or _mutation_result(module),
    )
    monkeypatch.setattr(
        migration,
        "_scan_module",
        lambda module, **kwargs: calls.append("scan") or _scan_result(module),
    )


def test_workflow_runs_in_order_and_keeps_mined_contracts_epistemic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_compute(x: int) -> int:
        return x

    def candidate_compute(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules, "migration_base", _module("migration_base", compute=base_compute)
    )
    monkeypatch.setitem(
        sys.modules,
        "migration_candidate",
        _module("migration_candidate", compute=candidate_compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("migration_candidate"),
        diff_result=DiffResult("compute", "compute", 3),
        calls=calls,
    )
    invariant = ContractCheck(
        "identity",
        predicate=lambda value: value == 2,
        kwargs={"x": 2},
    )

    result = migration.migrate(
        "migration_base",
        "migration_candidate",
        invariants={"compute": [invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert calls == ["audit", "mine", "diff", "mutate", "scan"]
    assert [stage.name for stage in result.stages] == [
        "audit base",
        "mine candidate contracts",
        "diff base/candidate",
        "classify intended changes",
        "save unexpected divergences",
        "mutate resulting tests",
        "scan candidate",
    ]
    assert result.candidate_contracts[0].source == "mined_hypothesis"
    assert result.mining_limitations
    assert result.protected_within_measured_scope
    assert "PROTECTIVE_WITHIN_MEASURED_SCOPE" in result.summary()
    persisted = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert persisted["final_verdict"] == "protective_within_measured_scope"
    assert persisted["protected_within_measured_scope"] is True
    assert persisted["mutation"]["status"] == "passed"
    assert persisted["mutation"]["killed"] == persisted["mutation"]["total"] == 1
    assert persisted["candidate_scan"]["passed"] is True
    assert persisted["candidate_scan"]["total"] == 1
    assert persisted["stages"][-1]["name"] == "scan candidate"


def test_full_stack_fixture_runs_every_real_migration_engine(tmp_path: Path) -> None:
    """Exercise audit, mine, diff, mutate, and scan without substituting engines."""
    invariant = ContractCheck(
        "increments",
        predicate=lambda value: value == 3,
        kwargs={"value": 2},
    )

    result = migration.migrate(
        "tests.migration_fixture_base",
        "tests.migration_fixture_candidate",
        invariants={"increment": [invariant]},
        test_dir="tests/fixtures/migration_full_stack",
        audit_max_examples=3,
        mine_max_examples=5,
        diff_max_examples=5,
        scan_max_examples=5,
        mutation_preset="essential",
        mutation_workers=1,
        manifest_path=tmp_path / "tests" / "ordeal-regressions.json",
        evidence_path=tmp_path / "full-stack-evidence.json",
        regression_path=tmp_path / "tests" / "test_full_stack_regression.py",
    )

    assert [stage.name for stage in result.stages] == [
        "audit base",
        "mine candidate contracts",
        "diff base/candidate",
        "classify intended changes",
        "save unexpected divergences",
        "mutate resulting tests",
        "scan candidate",
    ]
    assert all(stage.status == "passed" for stage in result.stages)
    assert result.mutation.result is not None
    assert result.mutation.result.total > 0
    assert result.mutation.result.killed == result.mutation.result.total
    assert result.mutation.result.contract_context["test_basis"] == (
        "generated_parity_and_explicit_contracts"
    )
    assert result.candidate_scan.total == 1
    assert result.protected_within_measured_scope
    persisted = json.loads((tmp_path / "full-stack-evidence.json").read_text())
    assert persisted["final_verdict"] == "protective_within_measured_scope"
    assert persisted["mutation"]["total"] > 0
    assert persisted["mutation"]["test_basis"] == ("generated_parity_and_explicit_contracts")
    assert persisted["candidate_scan"]["total"] == 1
    manifest = json.loads(
        (tmp_path / "tests" / "ordeal-regressions.json").read_text(encoding="utf-8")
    )
    assert manifest["regressions"] == []


def test_unexpected_divergence_is_saved_and_blocks_mutation_until_fixed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_compute(x: int) -> int:
        return x

    def candidate_compute(x: int) -> int:
        return x + 1

    candidate_module = _module("candidate_buggy", compute=candidate_compute)
    monkeypatch.setitem(sys.modules, "base_stable", _module("base_stable", compute=base_compute))
    monkeypatch.setitem(sys.modules, "candidate_buggy", candidate_module)
    calls: list[str] = []
    mismatch = Mismatch(args={"x": 1}, output_a=1, output_b=2)
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_buggy"),
        diff_result=_divergent_result(mismatch),
        calls=calls,
    )
    evidence_path = tmp_path / "evidence.json"
    regression_path = tmp_path / "tests" / "test_regression.py"
    manifest_path = tmp_path / "tests" / "ordeal-regressions.json"
    invariant = ContractCheck("positive", lambda value: value > 0, {"x": 1})

    result = migration.migrate(
        "base_stable",
        "candidate_buggy",
        invariants={"compute": [invariant]},
        evidence_path=evidence_path,
        regression_path=regression_path,
        manifest_path=manifest_path,
    )

    assert calls == ["audit", "mine", "diff", "scan"]
    assert result.mutation.status == "blocked"
    assert "resulting test baseline fails" in str(result.mutation.reason)
    assert len(result.unexpected_changes) == 1
    assert len(result.artifacts.regression_cases) == 1
    assert evidence_path.is_file()
    assert regression_path.is_file()
    compile(regression_path.read_text(encoding="utf-8"), str(regression_path), "exec")
    with pytest.raises(AssertionError, match="expected preserved base outcome"):
        migration.replay_migration_case(result.artifacts.regression_cases[0])
    assert result.stages[-1].name == "scan candidate"


def test_saved_regression_preserves_mutated_argument_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_mutate(values: list[int]) -> None:
        values.append(1)

    def candidate_mutate(values: list[int]) -> None:
        values.append(2)

    monkeypatch.setitem(
        sys.modules,
        "base_mutation_state",
        _module("base_mutation_state", mutate=base_mutate),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_mutation_state",
        _module("candidate_mutation_state", mutate=candidate_mutate),
    )
    calls: list[str] = []
    mismatch = Mismatch(args={"values": []}, output_a=None, output_b=None)
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_mutation_state"),
        diff_result=_divergent_result(
            mismatch,
            differences=("mutated_arguments",),
            mutated_a={"values": [1]},
            mutated_b={"values": [2]},
        ),
        calls=calls,
    )
    invariant = ContractCheck("returns none", lambda value: value is None, {"values": []})

    result = migration.migrate(
        "base_mutation_state",
        "candidate_mutation_state",
        invariants={"mutate": [invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.artifacts.unsupported_change_ids == ()
    assert len(result.artifacts.regression_cases) == 1
    with pytest.raises(AssertionError, match="mutated arguments"):
        migration.replay_migration_case(result.artifacts.regression_cases[0])
    assert result.mutation.status == "blocked"


def test_selected_side_effect_divergence_stays_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_compute(x: int) -> int:
        return x

    def candidate_compute(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules,
        "base_side_effect",
        _module("base_side_effect", compute=base_compute),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_side_effect",
        _module("candidate_side_effect", compute=candidate_compute),
    )
    calls: list[str] = []
    mismatch = Mismatch(args={"x": 1}, output_a=1, output_b=1)
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_side_effect"),
        diff_result=_divergent_result(
            mismatch,
            differences=("side_effects",),
            side_effects_a={"events": ["base"]},
            side_effects_b={"events": ["candidate"]},
        ),
        calls=calls,
    )
    invariant = ContractCheck("identity", lambda value: value == 1, {"x": 1})

    result = migration.migrate(
        "base_side_effect",
        "candidate_side_effect",
        invariants={"compute": [invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.artifacts.unsupported_change_ids == ("behavior:compute",)
    assert result.artifacts.regression_cases == ()
    assert result.mutation.status == "blocked"
    assert result.stages[-1].name == "scan candidate"


def test_exception_regression_matches_type_name_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raises_value_error(x: int) -> int:
        raise ValueError(f"invalid: {x}")

    module = _module("candidate_exception", compute=raises_value_error)
    monkeypatch.setitem(sys.modules, "candidate_exception", module)
    case = {
        "id": "behavior:compute:test",
        "change_id": "behavior:compute",
        "candidate": "candidate_exception",
        "function": "compute",
        "kind": "behavior",
        "kwargs": {
            "__ordeal_type__": "dict",
            "items": [["x", 3]],
        },
        "expected": {
            "kind": "exception",
            "type_module": "builtins",
            "type_qualname": "ValueError",
            "message": "invalid: 3",
        },
    }

    migration.replay_migration_case(case)

    def raises_type_error(x: int) -> int:
        raise TypeError(f"invalid: {x}")

    raises_type_error.__module__ = "candidate_exception"
    module.compute = raises_type_error
    with pytest.raises(AssertionError, match="different exception"):
        migration.replay_migration_case(case)


def test_real_exception_divergence_persists_the_witness_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def base_compute(x: int) -> int:
        raise ValueError(f"invalid: {x}")

    def candidate_compute(x: int) -> int:
        return x

    base_module = _module("real_exception_base", compute=base_compute)
    candidate_module = _module("real_exception_candidate", compute=candidate_compute)
    monkeypatch.setitem(sys.modules, "real_exception_base", base_module)
    monkeypatch.setitem(sys.modules, "real_exception_candidate", candidate_module)
    result = run_diff(base_compute, candidate_compute, x=3, max_examples=1)
    assert result.witness is not None
    case = migration._case_for_change(
        migration.MigrationChange(
            id="behavior:compute",
            function="compute",
            kind="behavior",
            mismatch=result.mismatches[0],
            witness=result.witness,
        ),
        candidate="real_exception_candidate",
    )

    assert case["expected"] == {
        "kind": "exception",
        "type_module": "builtins",
        "type_qualname": "ValueError",
        "message": "invalid: 3",
    }
    with pytest.raises(AssertionError, match="did not raise"):
        migration.replay_migration_case(case)

    def fixed_compute(x: int) -> int:
        raise ValueError(f"invalid: {x}")

    fixed_compute.__module__ = "real_exception_candidate"
    candidate_module.compute = fixed_compute
    migration.replay_migration_case(case)


class _MigrationDomainValue:
    """Structurally replayable value with deliberately unhelpful equality."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _MigrationDomainValue)


def test_real_custom_object_regression_matches_canonical_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def base_compute(x: int) -> _MigrationDomainValue:
        return _MigrationDomainValue(x)

    def candidate_compute(x: int) -> _MigrationDomainValue:
        return _MigrationDomainValue(x + 1)

    base_module = _module("real_object_base", compute=base_compute)
    candidate_module = _module("real_object_candidate", compute=candidate_compute)
    monkeypatch.setitem(sys.modules, "real_object_base", base_module)
    monkeypatch.setitem(sys.modules, "real_object_candidate", candidate_module)
    result = run_diff(base_compute, candidate_compute, x=3, max_examples=1)
    assert result.witness is not None
    case = migration._case_for_change(
        migration.MigrationChange(
            id="behavior:compute",
            function="compute",
            kind="behavior",
            mismatch=result.mismatches[0],
            witness=result.witness,
        ),
        candidate="real_object_candidate",
    )

    expected = case["expected"]
    assert isinstance(expected, dict)
    assert expected["kind"] == "canonical_return"
    with pytest.raises(AssertionError, match="structurally different"):
        migration.replay_migration_case(case)

    def fixed_compute(x: int) -> _MigrationDomainValue:
        return _MigrationDomainValue(x)

    fixed_compute.__module__ = "real_object_candidate"
    candidate_module.compute = fixed_compute
    migration.replay_migration_case(case)


def test_real_migration_regression_preserves_aliased_witness_arguments() -> None:
    def base_compute(left: list[int], right: list[int]) -> int:
        return int(left is right)

    def candidate_compute(left: list[int], right: list[int]) -> int:
        return 0

    shared: list[int] = []
    result = run_diff(
        base_compute,
        candidate_compute,
        left=shared,
        right=shared,
        max_examples=1,
    )
    assert result.witness is not None
    case = migration._case_for_change(
        migration.MigrationChange(
            id="behavior:compute",
            function="compute",
            kind="behavior",
            mismatch=result.mismatches[0],
            witness=result.witness,
        ),
        candidate="alias_candidate",
    )

    decoded = migration._decode_value(case["kwargs"])
    assert isinstance(decoded, dict)
    assert decoded["left"] is decoded["right"]


def test_saved_regression_is_replayed_then_mutated_after_candidate_fix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_compute(x: int) -> int:
        return x

    def candidate_compute(x: int) -> int:
        return x + 1

    base_module = _module("base_resume", compute=base_compute)
    candidate_module = _module("candidate_resume", compute=candidate_compute)
    monkeypatch.setitem(sys.modules, "base_resume", base_module)
    monkeypatch.setitem(sys.modules, "candidate_resume", candidate_module)
    calls: list[str] = []
    results = [
        _divergent_result(Mismatch(args={"x": 1}, output_a=1, output_b=2)),
        DiffResult("compute", "compute", 1),
    ]
    monkeypatch.setattr(
        migration,
        "_audit",
        lambda module, **kwargs: calls.append("audit") or _audit_result(module),
    )
    monkeypatch.setattr(
        migration,
        "_mine_module",
        lambda module, **kwargs: calls.append("mine") or _mine_result(module),
    )
    monkeypatch.setattr(
        migration,
        "_diff",
        lambda base, candidate, **kwargs: calls.append("diff") or results.pop(0),
    )
    monkeypatch.setattr(
        migration,
        "_mutate",
        lambda module, **kwargs: calls.append("mutate") or _mutation_result(module),
    )
    monkeypatch.setattr(
        migration,
        "_scan_module",
        lambda module, **kwargs: calls.append("scan") or _scan_result(module),
    )
    evidence_path = tmp_path / "evidence.json"
    regression_path = tmp_path / "tests" / "test_regression.py"
    manifest_path = tmp_path / "tests" / "ordeal-regressions.json"
    invariant = ContractCheck("identity", lambda value: value == 1, {"x": 1})

    first = migration.migrate(
        "base_resume",
        "candidate_resume",
        invariants={"compute": [invariant]},
        evidence_path=evidence_path,
        regression_path=regression_path,
        manifest_path=manifest_path,
    )
    assert first.mutation.status == "blocked"

    def fixed_compute(x: int) -> int:
        return x

    fixed_compute.__module__ = "candidate_resume"
    candidate_module.compute = fixed_compute
    second = migration.migrate(
        "base_resume",
        "candidate_resume",
        invariants={"compute": [invariant]},
        evidence_path=evidence_path,
        regression_path=regression_path,
        manifest_path=manifest_path,
    )

    assert second.unexpected_changes == []
    assert len(second.artifacts.regression_cases) == 1
    assert second.mutation.status == "passed"
    assert second.protected_within_measured_scope
    assert calls[-2:] == ["mutate", "scan"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = manifest["regressions"][0]
    assert record["change_kind"] == "migration"
    assert record["test_basis"] == "generated_parity_and_explicit_contracts"
    assert record["test_file"] == "tests/test_regression.py"
    assert record["binding"]["test_name"] == "test_ordeal_migration_regression"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert main(["verify", "--ci", "--manifest", str(manifest_path)]) == 0


def test_intended_change_needs_invariant_but_does_not_create_parity_regression(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_compute(x: int) -> int:
        return x

    def candidate_compute(x: int) -> int:
        return x + 1

    monkeypatch.setitem(sys.modules, "base_intent", _module("base_intent", compute=base_compute))
    monkeypatch.setitem(
        sys.modules,
        "candidate_intent",
        _module("candidate_intent", compute=candidate_compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_intent"),
        diff_result=_divergent_result(Mismatch(args={"x": 1}, output_a=1, output_b=2)),
        calls=calls,
    )
    invariant = ContractCheck("new rule", lambda value: value == 2, {"x": 1})
    evidence_path = tmp_path / "evidence.json"
    regression_path = tmp_path / "test_regression.py"

    first = migration.migrate(
        "base_intent",
        "candidate_intent",
        invariants={"compute": [invariant]},
        evidence_path=evidence_path,
        regression_path=regression_path,
    )
    assert len(first.artifacts.regression_cases) == 1

    result = migration.migrate(
        "base_intent",
        "candidate_intent",
        intended_changes={"behavior:compute": "documented offset change"},
        invariants={"compute": [invariant]},
        evidence_path=evidence_path,
        regression_path=regression_path,
    )

    assert result.unexpected_changes == []
    assert result.intended_changes[0].reason == "documented offset change"
    assert result.artifacts.regression_cases == ()
    assert "CASES = []" in regression_path.read_text(encoding="utf-8")
    assert calls[-2:] == ["mutate", "scan"]
    assert result.protected_within_measured_scope


def test_each_changed_candidate_callable_requires_its_own_invariant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_critical(x: int) -> int:
        return x

    def candidate_critical(x: int) -> int:
        return x + 1

    def base_helper(x: int) -> int:
        return x

    def candidate_helper(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules,
        "base_per_callable",
        _module("base_per_callable", critical=base_critical, helper=base_helper),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_per_callable",
        _module(
            "candidate_per_callable",
            critical=candidate_critical,
            helper=candidate_helper,
        ),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_per_callable"),
        diff_result=DiffResult("helper", "helper", 1),
        calls=calls,
    )
    monkeypatch.setattr(
        migration,
        "_diff",
        lambda base, candidate, **kwargs: (
            _divergent_result(Mismatch(args={"x": 1}, output_a=1, output_b=2))
            if base is base_critical
            else DiffResult("helper", "helper", 1)
        ),
    )
    helper_invariant = ContractCheck("helper identity", lambda value: value == 1, {"x": 1})

    result = migration.migrate(
        "base_per_callable",
        "candidate_per_callable",
        intended_changes={"behavior:critical": "documented critical change"},
        invariants={"helper": [helper_invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.unexpected_changes == []
    assert result.unprotected_changed_callables == ("critical",)
    assert not result.protected_within_measured_scope


def test_killed_mutants_measure_regression_protection_for_changed_callable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_critical(x: int) -> int:
        return x

    def candidate_critical(x: int) -> int:
        return x + 1

    def base_helper(x: int) -> int:
        return x

    def candidate_helper(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules,
        "base_measured",
        _module("base_measured", critical=base_critical, helper=base_helper),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_measured",
        _module("candidate_measured", critical=candidate_critical, helper=candidate_helper),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_measured"),
        diff_result=DiffResult("helper", "helper", 1),
        calls=calls,
    )
    monkeypatch.setattr(
        migration,
        "_diff",
        lambda base, candidate, **kwargs: (
            _divergent_result(Mismatch(args={"x": 1}, output_a=1, output_b=2))
            if base is base_critical
            else DiffResult("helper", "helper", 1)
        ),
    )
    monkeypatch.setattr(
        migration,
        "_mutate",
        lambda module, **kwargs: _mutation_result(module, qualname="critical"),
    )
    helper_invariant = ContractCheck("helper identity", lambda value: value == 1, {"x": 1})

    result = migration.migrate(
        "base_measured",
        "candidate_measured",
        intended_changes={"behavior:critical": "documented critical change"},
        invariants={"helper": [helper_invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.measured_regression_callables == ("critical",)
    assert result.unprotected_changed_callables == ()
    assert result.protected_within_measured_scope


def test_explicit_invariant_inputs_are_cloned_for_every_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[int]] = []

    def mutate(values: list[int]) -> int:
        observed.append(list(values))
        values.append(2)
        return len(values)

    monkeypatch.setitem(
        sys.modules,
        "candidate_invariant_inputs",
        _module("candidate_invariant_inputs", mutate=mutate),
    )
    check = ContractCheck("appends once", lambda value: value == 2, {"values": [1]})

    migration._run_explicit_invariants(
        "candidate_invariant_inputs",
        {"mutate": [check]},
    )
    migration._run_explicit_invariants(
        "candidate_invariant_inputs",
        {"mutate": [check]},
    )

    assert observed == [[1], [1]]
    assert check.kwargs == {"values": [1]}


def test_module_migration_records_public_signature_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def base_compute(x: int) -> int:
        return x

    def candidate_compute(x: int, offset: int = 0) -> int:
        return x + offset

    monkeypatch.setitem(
        sys.modules,
        "base_signature",
        _module("base_signature", compute=base_compute),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_signature",
        _module("candidate_signature", compute=candidate_compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_signature"),
        diff_result=DiffResult("compute", "compute", 1),
        calls=calls,
    )
    invariant = ContractCheck("identity", lambda value: value == 1, {"x": 1})

    result = migration.migrate(
        "base_signature",
        "candidate_signature",
        invariants={"compute": [invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert [change.id for change in result.changes] == ["signature:compute"]
    change = result.changes[0]
    assert change.base_signature != change.candidate_signature
    assert result.artifacts.regression_cases[0]["kind"] == "signature"
    with pytest.raises(AssertionError, match="public signature"):
        migration.replay_migration_case(result.artifacts.regression_cases[0])
    assert result.mutation.status == "blocked"


def test_added_and_removed_public_callables_are_explicitly_classified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def old_api(x: int) -> int:
        return x

    def new_api(x: int) -> int:
        return x + 1

    monkeypatch.setitem(
        sys.modules,
        "base_surface",
        _module("base_surface", old_api=old_api),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_surface",
        _module("candidate_surface", new_api=new_api),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_surface"),
        diff_result=DiffResult("unused", "unused", 0),
        calls=calls,
    )
    invariant = ContractCheck("new rule", lambda value: value == 2, {"x": 1})

    result = migration.migrate(
        "base_surface",
        "candidate_surface",
        intended_changes={
            "removed:old_api": "old entrypoint retired",
            "added:new_api": "replacement entrypoint",
        },
        invariants={"new_api": [invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert {change.id for change in result.intended_changes} == {
        "removed:old_api",
        "added:new_api",
    }
    assert result.unexpected_changes == []
    assert "diff" not in calls
    assert result.diff_errors == {
        "<migration>": "no shared public callables were differentially evaluated"
    }
    assert not result.protected_within_measured_scope


def test_non_replayable_witness_is_saved_as_evidence_and_blocks_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DomainObject:
        pass

    def base_compute(x: int) -> DomainObject:
        return DomainObject()

    def candidate_compute(x: int) -> int:
        return x

    monkeypatch.setitem(sys.modules, "base_object", _module("base_object", compute=base_compute))
    monkeypatch.setitem(
        sys.modules,
        "candidate_object",
        _module("candidate_object", compute=candidate_compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_object"),
        diff_result=_divergent_result(
            Mismatch(args={"x": 1}, output_a=DomainObject(), output_b=1)
        ),
        calls=calls,
    )
    evidence_path = tmp_path / "evidence.json"
    invariant = ContractCheck("integer", lambda value: isinstance(value, int), {"x": 1})

    result = migration.migrate(
        "base_object",
        "candidate_object",
        invariants={"compute": [invariant]},
        evidence_path=evidence_path,
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.artifacts.unsupported_change_ids == ("behavior:compute",)
    assert result.mutation.status == "blocked"
    assert "non-replayable" in str(result.mutation.reason)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["changes"][0]["classification"] == "unexpected"
    assert payload["unsupported_change_ids"] == ["behavior:compute"]
    assert calls[-1] == "scan"


def test_no_explicit_invariants_cannot_claim_correct_and_protected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def compute(x: int) -> int:
        return x

    monkeypatch.setitem(sys.modules, "base_plain", _module("base_plain", compute=compute))

    def candidate_compute(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules,
        "candidate_plain",
        _module("candidate_plain", compute=candidate_compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_plain"),
        diff_result=DiffResult("compute", "compute", 1),
        calls=calls,
    )

    result = migration.migrate(
        "base_plain",
        "candidate_plain",
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.mutation.status == "blocked"
    assert result.explicit_invariant_count == 0
    assert not result.protected_within_measured_scope
    assert calls == ["audit", "mine", "diff", "scan"]


def test_empty_candidate_scan_cannot_claim_protection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def compute(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules,
        "base_empty_scan",
        _module("base_empty_scan", compute=compute),
    )
    monkeypatch.setitem(
        sys.modules,
        "candidate_empty_scan",
        _module("candidate_empty_scan", compute=compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_empty_scan"),
        diff_result=DiffResult("compute", "compute", 1),
        calls=calls,
    )
    monkeypatch.setattr(
        migration,
        "_scan_module",
        lambda module, **kwargs: calls.append("scan") or ScanResult(module=module),
    )
    invariant = ContractCheck("identity", lambda value: value == 1, {"x": 1})

    result = migration.migrate(
        "base_empty_scan",
        "candidate_empty_scan",
        invariants={"compute": [invariant]},
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.candidate_scan.passed
    assert result.candidate_scan.total == 0
    assert result.stages[-1].status == "failed"
    assert "no callables" in result.stages[-1].summary
    assert not result.protected_within_measured_scope


def test_lower_policy_threshold_cannot_upgrade_partial_mutation_score(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def compute(x: int) -> int:
        return x

    monkeypatch.setitem(sys.modules, "base_threshold", _module("base_threshold", compute=compute))

    def candidate_compute(x: int) -> int:
        return x

    monkeypatch.setitem(
        sys.modules,
        "candidate_threshold",
        _module("candidate_threshold", compute=candidate_compute),
    )
    calls: list[str] = []
    _install_common_fakes(
        monkeypatch,
        mine_result=_mine_result("candidate_threshold"),
        diff_result=DiffResult("compute", "compute", 1),
        calls=calls,
    )
    monkeypatch.setattr(
        migration,
        "_mutate",
        lambda module, **kwargs: calls.append("mutate") or _mutation_result(module, killed=False),
    )
    invariant = ContractCheck("identity", lambda value: value == 1, {"x": 1})

    result = migration.migrate(
        "base_threshold",
        "candidate_threshold",
        invariants={"compute": [invariant]},
        mutation_threshold=0.0,
        evidence_path=tmp_path / "evidence.json",
        regression_path=tmp_path / "test_regression.py",
    )

    assert result.mutation.status == "passed"
    assert result.mutation.result is not None
    assert result.mutation.result.score == 0.0
    assert not result.protected_within_measured_scope
    assert "RESULT  INCOMPLETE" in result.summary()


def test_cli_exposes_ordered_migration_workflow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeResult:
        protected_within_measured_scope = True
        artifacts = SimpleNamespace(
            evidence_path=tmp_path / "evidence.json",
            regression_path=tmp_path / "test_regression.py",
            regression_cases=(),
        )

        def summary(self) -> str:
            return "ordered migration complete"

        def to_dict(self) -> dict[str, object]:
            return {"protected_within_measured_scope": True}

    def fake_migrate(base: str, candidate: str, **kwargs: object) -> FakeResult:
        captured.update({"base": base, "candidate": candidate, **kwargs})
        return FakeResult()

    monkeypatch.setattr(migration, "migrate", fake_migrate)

    assert (
        main(
            [
                "migrate",
                "old.module",
                "new.module",
                "--intended-change",
                "behavior:compute",
                "--threshold",
                "0.9",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert "ordered migration complete" in output.out
    assert captured["base"] == "old.module"
    assert captured["candidate"] == "new.module"
    assert captured["intended_changes"] == ["behavior:compute"]
    assert captured["mutation_threshold"] == 0.9
