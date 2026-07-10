"""Release-blocking counterexamples for differential and migration soundness."""

from __future__ import annotations

import functools
import json
import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

import pytest

import ordeal.migration as migration
from ordeal.audit import ModuleAudit
from ordeal.auto import ContractCheck, ScanResult
from ordeal.diff import Operation, diff
from ordeal.mine import MineModuleResult
from ordeal.mutations import Mutant, MutationResult
from scripts import verify_revision_change_corpus as revision_corpus

REVISION_CHANGE_CASES = revision_corpus.CASES

pytestmark = pytest.mark.release_eval


def test_real_revision_change_corpus_stays_within_the_release_acceptance_range() -> None:
    assert 10 <= len(REVISION_CHANGE_CASES) <= 20
    assert len({case.commit for case in REVISION_CHANGE_CASES}) == len(REVISION_CHANGE_CASES)
    assert [case.expected_status for case in REVISION_CHANGE_CASES].count("divergent") == 7
    assert [case.expected_status for case in REVISION_CHANGE_CASES].count(
        "no_divergence_observed"
    ) == 5


def test_real_revision_corpus_rejects_an_all_control_false_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    cases = tuple(
        revision_corpus.ChangeCase(f"{index:040x}", "ordeal.catalog", "divergent")
        for index in range(10)
    )
    monkeypatch.setattr(revision_corpus, "CASES", cases)

    def fake_git(*arguments: str) -> str:
        if arguments[:2] == ("rev-parse", "--verify"):
            return arguments[2].removesuffix("^{commit}")
        if arguments[0] == "rev-parse":
            return "base-commit"
        if arguments[0] == "diff":
            return "ordeal/diff.py"
        if arguments[0] == "show":
            return "pinned subject"
        raise AssertionError(arguments)

    monkeypatch.setattr(revision_corpus, "_git", fake_git)
    monkeypatch.setattr(
        revision_corpus,
        "run_revision_diff",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="no_divergence_observed",
            supported_mismatch_count=0,
            isolated=True,
            total=1,
            mismatch_count=0,
            artifacts=(),
        ),
    )

    with pytest.raises(AssertionError, match="expected divergent"):
        revision_corpus.run_corpus(tmp_path / "report.json")


class _DeceptiveValue:
    """Value whose equality and representation deliberately hide its payload."""

    def __init__(self, payload: int) -> None:
        self.payload = payload

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DeceptiveValue)

    def __repr__(self) -> str:
        return "DeceptiveValue(<hidden>)"


def _aliasing_counterexample() -> str:
    shared: list[int] = []

    def base(left: list[int], right: list[int]) -> int:
        left.append(1)
        return len(right)

    def candidate(left: list[int], right: list[int]) -> int:
        left.append(1)
        return 0

    return str(diff(base, candidate, left=shared, right=shared, max_examples=1).status)


def _wrapper_counterexample() -> str:
    def base(value: int) -> int:
        return value

    def wrapped_target(value: int) -> int:
        return value

    @functools.wraps(wrapped_target)
    def candidate(*args: object, **kwargs: object) -> int:
        return wrapped_target(*args, **kwargs) + 1  # type: ignore[arg-type]

    return str(diff(base, candidate, value=0, max_examples=1).status)


def _positional_only_counterexample() -> str:
    def base(value: int, /) -> int:
        return value

    def candidate(value: int, /) -> int:
        return value + 1

    result = diff(base, candidate, value=0, max_examples=1)

    assert result.witness is not None
    assert result.witness.outcome_a.returned
    assert result.witness.outcome_b.returned
    assert result.witness.outcome_a.return_value == 0
    assert result.witness.outcome_b.return_value == 1
    return str(result.status)


def _custom_equality_repr_counterexample() -> str:
    def base() -> _DeceptiveValue:
        return _DeceptiveValue(1)

    def candidate() -> _DeceptiveValue:
        return _DeceptiveValue(2)

    return str(diff(base, candidate, max_examples=1).status)


def _invalid_operation_counterexample() -> str:
    class Base:
        pass

    class Candidate:
        pass

    result = diff(Base, Candidate, sequence=[Operation("missing")])

    assert result.harness_errors
    return str(result.status)


def _unstable_replay_counterexample() -> str:
    calls = 0

    def base(value: int) -> int:
        return 0

    def candidate(value: int) -> int:
        nonlocal calls
        calls += 1
        return int(calls == 1)

    return str(diff(base, candidate, value=0, max_examples=1).status)


_FUNCTION_COUNTEREXAMPLES: tuple[
    tuple[str, Callable[[], str], str],
    ...,
] = (
    ("aliasing", _aliasing_counterexample, "divergent"),
    ("wrappers", _wrapper_counterexample, "divergent"),
    ("positional-only functions", _positional_only_counterexample, "divergent"),
    ("custom equality/repr", _custom_equality_repr_counterexample, "divergent"),
    ("invalid operations", _invalid_operation_counterexample, "inconclusive"),
    ("unstable replay", _unstable_replay_counterexample, "inconclusive"),
)


@pytest.mark.parametrize(
    ("_name", "evaluate", "expected"),
    _FUNCTION_COUNTEREXAMPLES,
    ids=[case[0] for case in _FUNCTION_COUNTEREXAMPLES],
)
def test_function_counterexamples_never_produce_false_no_divergence(
    _name: str,
    evaluate: Callable[[], str],
    expected: str,
) -> None:
    """Require a decisive divergence or an honest non-success verdict."""
    observed = evaluate()

    assert observed == expected
    assert observed != "no_divergence_observed"


def _module(name: str, **functions: Callable[..., object]) -> ModuleType:
    module = ModuleType(name)
    for function_name, function in functions.items():
        function.__module__ = name
        setattr(module, function_name, function)
    return module


def _install_migration_evidence_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        migration,
        "_audit",
        lambda module, **_kwargs: ModuleAudit(module=module),
    )
    monkeypatch.setattr(
        migration,
        "_mine_module",
        lambda module, **_kwargs: MineModuleResult(module=module),
    )
    monkeypatch.setattr(
        migration,
        "_mutate",
        lambda module, **_kwargs: MutationResult(
            target=module,
            mutants=[Mutant("return_none", "return None", 1, 0, killed=True)],
        ),
    )
    monkeypatch.setattr(
        migration,
        "_scan_module",
        lambda module, **_kwargs: ScanResult(module=module),
    )


def test_migration_executes_wrappers_instead_of_unwrapping_away_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Keep decorator behavior inside the release migration comparison."""

    def base_compute(value: int) -> int:
        return value

    def wrapped_target(value: int) -> int:
        return value

    @functools.wraps(wrapped_target)
    def candidate_compute(*args: object, **kwargs: object) -> int:
        return wrapped_target(*args, **kwargs) + 1  # type: ignore[arg-type]

    monkeypatch.setitem(
        sys.modules,
        "release_eval_wrapper_base",
        _module("release_eval_wrapper_base", compute=base_compute),
    )
    monkeypatch.setitem(
        sys.modules,
        "release_eval_wrapper_candidate",
        _module("release_eval_wrapper_candidate", compute=candidate_compute),
    )
    _install_migration_evidence_fakes(monkeypatch)

    result = migration.migrate(
        "release_eval_wrapper_base",
        "release_eval_wrapper_candidate",
        invariants={
            "compute": [ContractCheck("candidate rule", lambda value: value == 2, {"value": 1})]
        },
        diff_max_examples=1,
        evidence_path=tmp_path / "wrapper-evidence.json",
        regression_path=tmp_path / "test_wrapper_regression.py",
    )

    assert [change.id for change in result.unexpected_changes] == ["behavior:compute"]
    assert not result.protected_within_measured_scope
    persisted = json.loads((tmp_path / "wrapper-evidence.json").read_text(encoding="utf-8"))
    assert persisted["change_evidence"][0]["schema"] == ("ordeal.divergence-evidence/v1")
    assert persisted["change_evidence"][0]["source_binding"]["status"] == "complete"


def test_migration_rejects_vacuous_differential_coverage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Do not call a migration protected when no public callable was compared."""

    def old_api(value: int) -> int:
        return value

    def new_api(value: int) -> int:
        return value + 1

    monkeypatch.setitem(
        sys.modules,
        "release_eval_vacuous_base",
        _module("release_eval_vacuous_base", old_api=old_api),
    )
    monkeypatch.setitem(
        sys.modules,
        "release_eval_vacuous_candidate",
        _module("release_eval_vacuous_candidate", new_api=new_api),
    )
    _install_migration_evidence_fakes(monkeypatch)

    result = migration.migrate(
        "release_eval_vacuous_base",
        "release_eval_vacuous_candidate",
        intended_changes=("removed:old_api", "added:new_api"),
        invariants={
            "new_api": [ContractCheck("candidate rule", lambda value: value == 2, {"value": 1})]
        },
        evidence_path=tmp_path / "vacuous-evidence.json",
        regression_path=tmp_path / "test_vacuous_regression.py",
    )

    assert result.diff_errors
    assert not result.protected_within_measured_scope
    assert result.stages[2].status == "failed"
