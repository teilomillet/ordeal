"""Evidence that coverage alone cannot establish test protection."""

from __future__ import annotations

from coverage import Coverage

from ordeal.audit import CoverageMeasurement, CoverageResult, ModuleAudit, Status
from ordeal.mutations import (
    Mutant,
    MutationResult,
    mutate_function_and_test,
    validate_mined_properties,
)


def _coverage(percent: float, *, missing: frozenset[int] = frozenset()) -> CoverageMeasurement:
    return CoverageMeasurement(
        Status.VERIFIED,
        CoverageResult(
            percent=percent,
            total_statements=10,
            missing_count=len(missing),
            missing_lines=missing,
            source="test fixture",
        ),
    )


def test_property_strength_requires_mutation_discrimination() -> None:
    result = MutationResult(
        target="pkg.score",
        property_observations=[
            {"name": "bounded", "holds": 20, "total": 20},
            {"name": "is truthy", "holds": 20, "total": 20},
            {"name": "declared path", "holds": 0, "total": 0},
        ],
        mutants=[
            Mutant(
                "boundary",
                "10 -> 11",
                3,
                4,
                killed=True,
                killed_by="property:bounded",
                metadata={"killed_by_properties": ["bounded"]},
            ),
            Mutant("arithmetic", "+ -> -", 4, 4, killed=True, killed_by="test_exact"),
        ],
    )

    strengths = {item["name"]: item for item in result.property_strength()}

    assert strengths["bounded"]["status"] == "discriminating"
    assert strengths["bounded"]["mutants_killed"] == 1
    assert strengths["is truthy"]["status"] == "tautological_or_weak"
    assert strengths["declared path"]["status"] == "unexercised"
    assert list(result.kill_attribution()) == ["property:bounded", "test_exact"]
    view = result.test_protection_view()
    assert view["protects"] is False
    assert view["status"] == "weak"
    assert view["tautological_or_weak_properties"] == ["is truthy"]
    assert view["unexercised_properties"] == ["declared path"]


def test_full_line_coverage_with_surviving_mutant_is_weak() -> None:
    audit = ModuleAudit(
        module="pkg.score",
        migrated_coverage=_coverage(100.0),
        mutation_score="3/4 (75%)",
        weakest_tests=[{"test": "test_exact", "kills": 1}],
    )

    view = audit.test_protection_view()

    assert view["status"] == "weak"
    assert view["protects"] is False
    assert view["line_coverage_percent"] == 100.0
    assert view["surviving_mutants"] == 1
    assert view["kill_attribution"] == [{"test": "test_exact", "kills": 1}]
    assert view["summary"] == "100% line coverage but 1/4 mutation(s) survived"
    assert "protection: WEAK: 100% line coverage but" in audit.summary()


def test_real_full_line_coverage_can_have_zero_mutation_score() -> None:
    import importlib

    coverage = Coverage(source=["tests._weak_coverage_target"])
    coverage.start()
    target = importlib.reload(importlib.import_module("tests._weak_coverage_target"))

    def weak_suite() -> None:
        target.classify(1)
        target.classify(-1)

    weak_suite()
    coverage.stop()
    _, statements, _, missing, _ = coverage.analysis2(target)
    line_percent = (len(statements) - len(missing)) / len(statements) * 100

    mutation = mutate_function_and_test(
        "tests._weak_coverage_target.classify",
        weak_suite,
        operators=["comparison", "negate"],
    )

    assert line_percent == 100.0
    assert mutation.total > 0
    assert mutation.score == 0.0
    assert mutation.test_protection_view()["status"] == "weak"


def test_all_lines_and_mutants_prove_protection_within_scope() -> None:
    audit = ModuleAudit(
        module="pkg.score",
        migrated_coverage=_coverage(100.0),
        mutation_score="4/4 (100%)",
    )

    view = audit.test_protection_view()

    assert view["status"] == "protective_within_measured_scope"
    assert view["protects"] is True
    assert view["coverage_gaps"] == []


def test_uncovered_lines_keep_full_mutation_score_from_overclaiming() -> None:
    audit = ModuleAudit(
        module="pkg.score",
        migrated_coverage=_coverage(90.0, missing=frozenset({7})),
        mutation_score="4/4 (100%)",
    )

    view = audit.test_protection_view()

    assert view["status"] == "weak"
    assert view["protects"] is False
    assert view["coverage_gaps"] == [7]


def test_mined_property_kills_are_attributed_to_the_property() -> None:
    result = validate_mined_properties(
        "tests._mutation_target.add",
        max_examples=30,
        operators=["arithmetic"],
    )

    assert result.total > 0
    assert result.killed > 0
    assert any(mutant.metadata.get("killed_by_properties") for mutant in result.mutants)
    assert any(item["status"] == "discriminating" for item in result.property_strength())
