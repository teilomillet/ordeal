"""Tests for ordeal.audit — epistemic soundness of the audit module.

Tests verify that the audit:
1. Parses coverage JSON correctly (known-good fixtures)
2. Fails visibly on bad data (never silent zeros)
3. Computes Wilson CI correctly (against known values)
4. Self-verifies internal consistency
5. Reports known unknowns
6. Produces correct output for the test target module
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ordeal.audit as audit_mod
from ordeal.audit import (
    CoverageMeasurement,
    CoverageResult,
    FunctionAudit,
    MineResult,
    ModuleAudit,
    Status,
    _count_lines_in_file,
    _count_tests_in_file,
    _find_test_files,
    _format_change_summary,
    _func_sig_for_codegen,
    _group_mined_properties,
    _suggest_tests,
    _verify_consistency,
    wilson_lower,
)
from ordeal.auto import ContractCheck
from ordeal.mine import MinedProperty

# ============================================================================
# Wilson score interval
# ============================================================================


class TestWilsonLower:
    """Verify Wilson CI against known values from statistics tables."""

    def test_zero_total(self):
        assert wilson_lower(0, 0) == 0.0

    def test_perfect_small_sample(self):
        """10/10 at 95% CI → lower bound ~0.72."""
        lower = wilson_lower(10, 10)
        assert 0.70 < lower < 0.75, f"10/10: {lower:.3f}"

    def test_perfect_large_sample(self):
        """500/500 at 95% CI → lower bound ~0.993."""
        lower = wilson_lower(500, 500)
        assert 0.990 < lower < 0.996, f"500/500: {lower:.4f}"

    def test_half_rate(self):
        """50/100 at 95% CI → lower bound ~0.40."""
        lower = wilson_lower(50, 100)
        assert 0.39 < lower < 0.42, f"50/100: {lower:.3f}"

    def test_zero_successes(self):
        """0/100 at 95% CI → lower bound ~0.0."""
        lower = wilson_lower(0, 100)
        assert lower < 0.01, f"0/100: {lower:.4f}"

    def test_monotonic_in_successes(self):
        """More successes → higher lower bound."""
        bounds = [wilson_lower(k, 100) for k in range(0, 101, 10)]
        for i in range(1, len(bounds)):
            assert bounds[i] >= bounds[i - 1]

    def test_monotonic_in_sample_size(self):
        """Larger sample → tighter bound (higher lower for same rate)."""
        bounds = [wilson_lower(n, n) for n in [10, 50, 100, 500]]
        for i in range(1, len(bounds)):
            assert bounds[i] >= bounds[i - 1]


# ============================================================================
# File counting
# ============================================================================


class TestCountTests:
    def test_counts_test_functions(self, tmp_path: Path):
        f = tmp_path / "test_example.py"
        f.write_text("def test_a(): pass\ndef test_b(): pass\ndef helper(): pass\n")
        count, err = _count_tests_in_file(f)
        assert count == 2
        assert err is None

    def test_returns_error_on_missing_file(self, tmp_path: Path):
        f = tmp_path / "nonexistent.py"
        count, err = _count_tests_in_file(f)
        assert count == 0
        assert err is not None
        assert "cannot read" in err

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.py"
        f.write_text("")
        count, err = _count_tests_in_file(f)
        assert count == 0
        assert err is None


class TestCountLines:
    def test_counts_non_empty(self, tmp_path: Path):
        f = tmp_path / "code.py"
        f.write_text("line1\n\nline3\n  \nline5\n")
        count, err = _count_lines_in_file(f)
        assert count == 3  # line1, line3, line5
        assert err is None

    def test_returns_error_on_missing(self, tmp_path: Path):
        f = tmp_path / "missing.py"
        count, err = _count_lines_in_file(f)
        assert count == 0
        assert err is not None


# ============================================================================
# Test file discovery
# ============================================================================


class TestFindTestFiles:
    def test_finds_by_name(self, tmp_path: Path):
        (tmp_path / "test_scoring.py").write_text("def test_a(): pass")
        (tmp_path / "test_scoring_props.py").write_text("def test_b(): pass")
        (tmp_path / "test_other.py").write_text("def test_c(): pass")
        files = _find_test_files("myapp.scoring", tmp_path)
        names = {f.name for f in files}
        assert names == {"test_scoring.py", "test_scoring_props.py"}

    def test_ignores_substring_match(self, tmp_path: Path):
        """test_scoring_props matches, but test_prescoring does not."""
        (tmp_path / "test_scoring.py").write_text("")
        (tmp_path / "test_prescoring.py").write_text("")
        files = _find_test_files("myapp.scoring", tmp_path)
        assert len(files) == 1
        assert files[0].name == "test_scoring.py"

    def test_empty_dir(self, tmp_path: Path):
        assert _find_test_files("myapp.scoring", tmp_path) == []

    def test_nonexistent_dir(self, tmp_path: Path):
        assert _find_test_files("myapp.scoring", tmp_path / "nope") == []

    def test_finds_suffix_named_tests(self, tmp_path: Path):
        (tmp_path / "scoring_test.py").write_text("def test_a(): pass")
        files = _find_test_files("myapp.scoring", tmp_path)
        assert [file.name for file in files] == ["scoring_test.py"]

    def test_falls_back_to_import_matching(self, tmp_path: Path):
        (tmp_path / "pipeline_behavior.py").write_text(
            "from myapp.scoring import normalize\n"
            "def test_normalize(): assert normalize([1]) == [1]\n"
        )
        files = _find_test_files("myapp.scoring", tmp_path)
        assert [file.name for file in files] == ["pipeline_behavior.py"]

    def test_ignores_non_test_helpers_during_import_matching(self, tmp_path: Path):
        (tmp_path / "_helper.py").write_text(
            "from myapp.scoring import normalize\nVALUE = normalize([1])\n"
        )
        (tmp_path / "behavior_check.py").write_text(
            "from myapp.scoring import normalize\n"
            "def test_normalize(): assert normalize([1]) == [1]\n"
        )

        files = _find_test_files("myapp.scoring", tmp_path)
        assert [file.name for file in files] == ["behavior_check.py"]

    def test_falls_back_to_pytest_collection_for_custom_collectors(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        collected = tmp_path / "suite" / "behavior_check.py"
        collected.parent.mkdir()
        collected.write_text("from myapp.scoring import normalize\nVALUE = normalize([1])\n")
        monkeypatch.setattr(audit_mod, "_pytest_collected_test_files", lambda _path: [collected])
        files = _find_test_files("myapp.scoring", tmp_path)
        assert files == [collected]


# ============================================================================
# Coverage measurement types
# ============================================================================


class TestCoverageMeasurement:
    def test_verified_has_result(self):
        m = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(
                percent=98.0,
                total_statements=50,
                missing_count=1,
                missing_lines=frozenset({42}),
                source="test",
            ),
        )
        assert m.status == Status.VERIFIED
        assert m.percent == 98.0
        assert m.missing_lines == frozenset({42})

    def test_failed_has_error(self):
        m = CoverageMeasurement(Status.FAILED, error="timed out")
        assert m.status == Status.FAILED
        assert m.percent == 0.0
        assert m.missing_lines == frozenset()
        assert m.error == "timed out"

    def test_failed_default(self):
        m = CoverageMeasurement(Status.FAILED, error="not measured yet")
        assert m.percent == 0.0


class TestCoverageBackendSelection:
    def test_prefers_coverage_module_when_available(self, monkeypatch):
        sentinel = CoverageMeasurement(Status.FAILED, error="coverage backend used")

        def fake_find_spec(name: str):
            if name == "coverage":
                return object()
            return None

        monkeypatch.setattr(audit_mod.importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            audit_mod,
            "_measure_coverage_with_coverage_py",
            lambda *args: sentinel,
        )
        monkeypatch.setattr(
            audit_mod,
            "_measure_coverage_with_pytest_cov",
            lambda *args: pytest.fail("pytest-cov backend should not run"),
        )
        monkeypatch.setattr(
            audit_mod,
            "_measure_coverage_with_trace",
            lambda *args: pytest.fail("trace backend should not run"),
        )

        result = audit_mod._measure_coverage([Path("tests/test_demo.py")], "ordeal.demo")
        assert result is sentinel

    def test_generated_files_use_direct_coverage_runner_when_available(self, monkeypatch):
        sentinel = CoverageMeasurement(Status.FAILED, error="direct coverage backend used")

        def fake_find_spec(name: str):
            if name == "coverage":
                return object()
            return None

        monkeypatch.setattr(audit_mod.importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            audit_mod,
            "_measure_generated_coverage_with_coverage_py",
            lambda *args: sentinel,
        )
        monkeypatch.setattr(
            audit_mod,
            "_measure_coverage_with_coverage_py",
            lambda *args: pytest.fail("generated coverage should bypass pytest coverage runner"),
        )

        result = audit_mod._measure_coverage([Path(".ordeal/test_generated.py")], "ordeal.demo")
        assert result is sentinel

    def test_generated_relative_path_uses_trace_fallback_without_coverage(self, monkeypatch):
        sentinel = CoverageMeasurement(Status.FAILED, error="direct trace backend used")

        monkeypatch.setattr(audit_mod.importlib.util, "find_spec", lambda _name: None)
        monkeypatch.setattr(
            audit_mod,
            "_measure_generated_coverage_with_trace",
            lambda *args: sentinel,
        )

        result = audit_mod._measure_coverage([Path(".ordeal/test_generated.py")], "ordeal.demo")
        assert result is sentinel

    def test_audit_coverages_share_single_coverage_runner_when_available(self, monkeypatch):
        current = CoverageMeasurement(Status.VERIFIED)
        migrated = CoverageMeasurement(Status.FAILED, error="generated")

        def fake_find_spec(name: str):
            if name == "coverage":
                return object()
            return None

        monkeypatch.setattr(audit_mod.importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            audit_mod,
            "_measure_audit_coverages_with_coverage_py",
            lambda *args: (current, migrated),
        )
        monkeypatch.setattr(
            audit_mod,
            "_measure_coverage",
            lambda *args: pytest.fail("shared audit coverage runner should be used"),
        )

        result = audit_mod._measure_audit_coverages(
            [Path("tests/test_demo.py")],
            [Path(".ordeal/test_demo_migrated.py")],
            "ordeal.demo",
        )

        assert result == (current, migrated)

    def test_audit_coverages_fall_back_to_individual_measurements(self, monkeypatch):
        calls: list[tuple[list[Path], str]] = []

        monkeypatch.setattr(audit_mod.importlib.util, "find_spec", lambda _name: None)

        def fake_measure(test_files: list[Path], module: str) -> CoverageMeasurement:
            calls.append((test_files, module))
            return CoverageMeasurement(Status.FAILED, error=str(test_files[0]))

        monkeypatch.setattr(audit_mod, "_measure_coverage", fake_measure)

        current, migrated = audit_mod._measure_audit_coverages(
            [Path("tests/test_demo.py")],
            [Path(".ordeal/test_demo_migrated.py")],
            "ordeal.demo",
        )

        assert calls == [
            ([Path("tests/test_demo.py")], "ordeal.demo"),
            ([Path(".ordeal/test_demo_migrated.py")], "ordeal.demo"),
        ]
        assert "test_demo.py" in (current.error or "")
        assert "test_demo_migrated.py" in (migrated.error or "")


# ============================================================================
# Self-verification
# ============================================================================


class TestVerifyConsistency:
    def _make_coverage(
        self,
        percent: float,
        stmts: int,
        missing: frozenset[int],
    ) -> CoverageMeasurement:
        return CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(
                percent=percent,
                total_statements=stmts,
                missing_count=len(missing),
                missing_lines=missing,
                source="test",
            ),
        )

    def test_consistent_passes(self):
        cur = self._make_coverage(98.0, 50, frozenset({42}))
        mig = self._make_coverage(96.0, 50, frozenset({42, 43}))
        warnings: list[str] = []
        _verify_consistency(cur, mig, "def test_a(): pass\n", 1, warnings)
        assert warnings == []

    def test_statement_count_mismatch(self):
        cur = self._make_coverage(98.0, 50, frozenset())
        mig = self._make_coverage(96.0, 45, frozenset())
        warnings: list[str] = []
        _verify_consistency(cur, mig, "def test_a(): pass\n", 1, warnings)
        assert any("statement count mismatch" in w for w in warnings)

    def test_test_count_mismatch(self):
        cur = self._make_coverage(98.0, 50, frozenset())
        mig = self._make_coverage(96.0, 50, frozenset())
        warnings: list[str] = []
        # generated has 2 test defs, but we claim 1
        _verify_consistency(
            cur,
            mig,
            "def test_a(): pass\ndef test_b(): pass\n",
            1,
            warnings,
        )
        assert any("test count mismatch" in w for w in warnings)

    def test_skips_when_failed(self):
        cur = CoverageMeasurement(Status.FAILED, error="timeout")
        mig = CoverageMeasurement(Status.FAILED, error="timeout")
        warnings: list[str] = []
        _verify_consistency(cur, mig, "", 0, warnings)
        # Should not crash, should not produce statement mismatch
        assert not any("statement count" in w for w in warnings)


# ============================================================================
# ModuleAudit summary formatting
# ============================================================================


class TestModuleAuditSummary:
    def test_verified_shows_label(self):
        a = ModuleAudit(module="myapp.scoring")
        a.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(98.0, 50, 1, frozenset({42}), "test"),
        )
        s = a.summary()
        assert "[verified]" in s
        assert "98%" in s

    def test_failed_shows_reason(self):
        a = ModuleAudit(module="myapp.scoring")
        a.current_coverage = CoverageMeasurement(
            Status.FAILED,
            error="timed out after 120s",
        )
        s = a.summary()
        assert "FAILED" in s
        assert "timed out" in s

    def test_not_checked_shown(self):
        a = ModuleAudit(module="myapp.scoring")
        a.not_checked = ["output value correctness"]
        s = a.summary()
        assert "NOT verified" in s
        assert "output value correctness" in s

    def test_warnings_counted(self):
        a = ModuleAudit(module="myapp.scoring")
        a.warnings = ["something failed", "another issue"]
        s = a.summary()
        assert "warnings: 2" in s

    def test_coverage_preserved_when_both_verified(self):
        a = ModuleAudit(module="x")
        a.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(98.0, 50, 1, frozenset(), "test"),
        )
        a.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(97.0, 50, 1, frozenset(), "test"),
        )
        assert a.coverage_preserved  # 97 >= 98 - 2

    def test_coverage_not_preserved_when_gap_too_large(self):
        a = ModuleAudit(module="x")
        a.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(98.0, 50, 1, frozenset(), "test"),
        )
        a.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(90.0, 50, 5, frozenset(), "test"),
        )
        assert not a.coverage_preserved  # 90 < 98 - 2

    def test_coverage_not_preserved_when_failed(self):
        a = ModuleAudit(module="x")
        a.current_coverage = CoverageMeasurement(
            Status.FAILED,
            error="timeout",
        )
        assert not a.coverage_preserved

    def test_mutation_score_in_summary(self):
        a = ModuleAudit(module="myapp.scoring")
        a.mutation_score = "8/10 (80%)"
        s = a.summary()
        assert "mutation: 8/10 (80%)" in s

    def test_summary_separates_current_generated_and_combined_views(self):
        a = ModuleAudit(module="myapp.scoring")
        a.current_test_count = 5
        a.current_test_lines = 20
        a.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(45.0, 50, 10, frozenset({1}), "test"),
        )
        a.migrated_test_count = 8
        a.migrated_lines = 35
        a.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(42.0, 50, 11, frozenset({1, 2}), "test"),
        )
        a.mutation_score = "3/4 (75%)"

        s = a.summary()

        assert "current suite:" in s
        assert "generated incremental:" in s
        assert "combined view:" in s
        assert "mutation: 3/4 (75%)" in s

    def test_mutation_score_fraction_parses_exact_counts(self):
        a = ModuleAudit(module="myapp.scoring")
        a.mutation_score = "8/10 (80%)"
        assert a.mutation_score_counts == (8, 10)
        assert a.mutation_score_fraction == pytest.approx(0.8)

    def test_mutation_score_absent_when_empty(self):
        a = ModuleAudit(module="myapp.scoring")
        s = a.summary()
        assert "mutation:" not in s

    def test_mined_grouped_in_summary(self):
        a = ModuleAudit(module="myapp.scoring")
        a.mined_properties = [
            "add: commutative (30/30, >=89% CI)",
            "mul: commutative (30/30, >=89% CI)",
            "add: deterministic (30/30, >=89% CI)",
        ]
        s = a.summary()
        assert "commutative(add, mul)" in s
        assert "deterministic(add)" in s

    def test_growth_uses_change_label(self):
        a = ModuleAudit(module="myapp.scoring")
        a.current_test_count = 5
        a.current_test_lines = 20
        a.migrated_test_count = 8
        a.migrated_lines = 35
        a.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(45.0, 50, 10, frozenset({1}), "test"),
        )
        a.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            result=CoverageResult(42.0, 50, 11, frozenset({1, 2}), "test"),
        )

        s = a.summary()
        assert "change:" in s
        assert "more tests" in s
        assert "more code" in s
        assert "coverage -3%" in s

    def test_summary_shows_mutation_gap_details(self):
        a = ModuleAudit(module="myapp.scoring")
        a.mutation_gaps = [
            {
                "target": "myapp.scoring.normalize",
                "location": "L10:4",
                "description": "+ -> -",
                "source_line": "return a + b",
                "remediation": "add a regression test",
            }
        ]
        a.weakest_tests = [{"test": "tests/test_scoring.py::test_smoke", "kills": 1}]
        a.mutation_gap_stubs = [
            {
                "target": "myapp.scoring.normalize",
                "content": "def test_gap(): ...",
            }
        ]

        s = a.summary()
        assert "surviving mutants:" in s
        assert "weakest killers:" in s
        assert "draft review stub file" in s

    def test_summary_shows_function_audit_breakdown(self):
        a = ModuleAudit(module="myapp.scoring")
        a.function_audits = [
            FunctionAudit(
                name="normalize",
                status="exercised",
                epistemic="verified",
                covered_body_lines=3,
                total_body_lines=3,
                evidence=[
                    {
                        "kind": "coverage_lines",
                        "epistemic": "verified",
                        "detail": "coverage hits 3/3 body line(s)",
                    }
                ],
            ),
            FunctionAudit(
                name="parse",
                status="exploratory",
                epistemic="inferred",
                evidence=[
                    {
                        "kind": "import",
                        "epistemic": "inferred",
                        "detail": "/tmp/tests/test_parse.py",
                    }
                ],
            ),
            FunctionAudit(
                name="render",
                status="uncovered",
                epistemic="none",
                evidence=[
                    {
                        "kind": "no_tests",
                        "epistemic": "none",
                        "detail": "no matching pytest files or collected nodeids",
                    }
                ],
            ),
        ]

        s = a.summary()
        assert (
            "functions: 1 exercised [verified], 1 exploratory [inferred],"
            " 1 no effective tests [none]"
        ) in s
        assert "- exercised [verified]: normalize" in s
        assert "evidence: coverage_lines" in s
        assert "- exploratory [inferred]: parse" in s
        assert "- uncovered [none]: render" in s

    def test_direct_test_gap_helpers_count_non_exercised_functions(self):
        a = ModuleAudit(module="myapp.scoring")
        a.function_audits = [
            FunctionAudit(name="normalize", status="exercised", epistemic="verified"),
            FunctionAudit(name="parse", status="exploratory", epistemic="inferred"),
            FunctionAudit(name="render", status="uncovered", epistemic="none"),
        ]

        assert a.direct_test_gap_counts == {"exploratory": 1, "uncovered": 1}
        assert [item.name for item in a.direct_test_gaps] == ["parse", "render"]
        assert a.has_direct_test_gaps is True


class TestGroupMinedProperties:
    def test_groups_by_property(self):
        raw = [
            "add: commutative (30/30, >=89% CI)",
            "mul: commutative (30/30, >=89% CI)",
            "add: deterministic (30/30, >=89% CI)",
        ]
        result = _group_mined_properties(raw)
        assert "commutative(add, mul)" in result
        assert "deterministic(add)" in result

    def test_empty(self):
        assert _group_mined_properties([]) == ""

    def test_single(self):
        raw = ["bounded: never None (50/50, >=93% CI)"]
        result = _group_mined_properties(raw)
        assert "never None(bounded)" in result


class TestChangeSummary:
    def test_savings_summary(self):
        label, summary = _format_change_summary(10, 4, 100, 40)
        assert label == "saving"
        assert "fewer tests" in summary
        assert "less code" in summary

    def test_growth_summary(self):
        label, summary = _format_change_summary(10, 20, 100, 120)
        assert label == "change"
        assert "more tests" in summary
        assert "more code" in summary


class TestPytestNodeidCollection:
    def test_skips_nodeid_collection_when_verified_coverage_already_exercises_all_functions(self):
        from tests._auto_target import add

        covered_lines = audit_mod._function_body_line_numbers(add)
        assert covered_lines is not None
        current = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=100.0,
                total_statements=len(covered_lines),
                missing_count=0,
                missing_lines=frozenset(),
                source="coverage.py",
            ),
        )
        evidence = [
            audit_mod.TestFileEvidence(
                path="tests/test__auto_target.py",
                basis="filename",
                epistemic="inferred",
            )
        ]

        assert (
            audit_mod._should_collect_pytest_nodeids(
                [("add", add)],
                current_coverage=current,
                test_file_evidence=evidence,
            )
            is False
        )

    def test_keeps_nodeid_collection_for_functions_without_verified_coverage(self):
        from tests._auto_target import divide

        body_lines = audit_mod._function_body_line_numbers(divide)
        assert body_lines is not None
        current = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=0.0,
                total_statements=len(body_lines),
                missing_count=len(body_lines),
                missing_lines=frozenset(body_lines),
                source="coverage.py",
            ),
        )
        evidence = [
            audit_mod.TestFileEvidence(
                path="tests/test__auto_target.py",
                basis="filename",
                epistemic="inferred",
            )
        ]

        assert audit_mod._should_collect_pytest_nodeids(
            [("divide", divide)],
            current_coverage=current,
            test_file_evidence=evidence,
        )


# ============================================================================
# Suggestion generation
# ============================================================================


class TestSuggestTests:
    def test_no_gap_no_suggestions(self):
        # Same missing lines → no gap
        result = _suggest_tests("tests._auto_target", frozenset({5}), frozenset({5}))
        assert result == []

    def test_suggests_for_gap_lines(self):
        # Migrated misses line 17 that current covers
        result = _suggest_tests(
            "tests._auto_target",
            frozenset(),  # current covers everything
            frozenset({17}),  # migrated misses line 17
        )
        assert len(result) >= 1
        assert "L17" in result[0]

    def test_handles_module_not_found(self):
        result = _suggest_tests(
            "nonexistent.module.xyz",
            frozenset(),
            frozenset({10}),
        )
        assert len(result) >= 1
        assert "cannot suggest" in result[0]


class TestGeneratedTypeSignatures:
    def test_codegen_qualifies_module_types_and_typing_imports(self, tmp_path: Path, monkeypatch):
        pkg = tmp_path / "genpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "mod.py").write_text(
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "from typing import Any\n"
            "\n"
            "TomlDict = dict[str, Any]\n"
            "\n"
            "@dataclass\n"
            "class PolicyConfig:\n"
            "    enabled: bool\n"
            "\n"
            "def parse(config: PolicyConfig, data: TomlDict) -> int:\n"
            "    return len(data)\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        from genpkg.mod import parse

        sig = _func_sig_for_codegen(parse)
        assert sig is not None
        _names, decls, _call_args, imports = sig
        assert "config: genpkg.mod.PolicyConfig" in decls
        assert "data: dict[str, Any]" in decls
        assert "from typing import Any" in imports
        assert "import genpkg.mod" in imports

    def test_codegen_preserves_unresolved_lazy_export_annotations(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        pkg = tmp_path / "lazyann"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "def __getattr__(name):\n"
            "    if name == 'Item':\n"
            "        from .models import Item\n"
            "        return Item\n"
            "    raise AttributeError(name)\n"
        )
        (pkg / "models.py").write_text("class Item:\n    pass\n")
        (pkg / "api.py").write_text(
            "from __future__ import annotations\n"
            "\n"
            "def parse(item: Item) -> Item:\n"
            "    return item\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        from lazyann.api import parse

        sig = _func_sig_for_codegen(parse)
        assert sig is not None
        _names, decls, _call_args, imports = sig
        assert "item: Item" in decls
        assert "import lazyann" not in imports


# ============================================================================
# Integration: audit on the test target module
# ============================================================================


class TestAuditIntegration:
    """Run a real audit on tests._auto_target and verify output."""

    def test_audit_produces_result(self, tmp_path: Path):
        """Audit a module with known test files → verified coverage."""
        # _find_test_files matches test_<short>.py where short = "_auto_target"
        # so the file must be named test__auto_target.py (double underscore)
        test_file = tmp_path / "test__auto_target.py"
        test_file.write_text(
            "from tests._auto_target import add, greet\n"
            "def test_add(): assert add(1, 2) == 3\n"
            "def test_greet(): assert greet('world') == 'hello world'\n"
        )

        from ordeal.audit import audit

        result = audit(
            "tests._auto_target",
            test_dir=str(tmp_path),
            max_examples=5,
        )

        # Should have found and measured the test file
        assert result.current_test_count == 2, (
            f"expected 2 tests, got {result.current_test_count}. warnings: {result.warnings}"
        )
        assert result.module == "tests._auto_target"

        # Should have generated a migrated test
        assert result.migrated_test_count > 0
        assert len(result.generated_test) > 0

        # Should list known unknowns
        assert len(result.not_checked) > 0
        assert any("correctness" in item for item in result.not_checked)

    def test_audit_no_tests_fails_visibly(self, tmp_path: Path):
        """Audit with empty test dir → FAILED, not silent 0%."""
        from ordeal.audit import audit

        result = audit(
            "tests._auto_target",
            test_dir=str(tmp_path),
            max_examples=5,
        )

        assert result.current_coverage.status == Status.FAILED
        assert "no test files" in (result.current_coverage.error or "")

    def test_audit_reports_function_level_epistemic_status(self, tmp_path: Path):
        test_file = tmp_path / "test__auto_target.py"
        test_file.write_text(
            "from tests._auto_target import add, greet\n"
            "def test_add(): assert add(1, 2) == 3\n"
            "def test_greet(): assert greet('world') == 'hello world'\n"
        )

        from ordeal.audit import audit

        result = audit(
            "tests._auto_target",
            test_dir=str(tmp_path),
            max_examples=5,
        )

        audits = {item.name: item for item in result.function_audits}

        assert audits["add"].status in {"exercised", "exploratory"}
        assert audits["add"].epistemic in {"verified", "inferred"}
        assert audits["add"].covered_body_lines >= 0
        assert audits["divide"].status in {"exploratory", "uncovered"}
        assert audits["divide"].epistemic in {"inferred", "none"}
        assert (
            result.function_audit_counts["exercised"] + result.function_audit_counts["exploratory"]
            >= 1
        )

    def test_audit_discovers_public_class_methods(self, tmp_path: Path, monkeypatch):
        pkg = tmp_path / "demo_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "envs.py").write_text(
            """
class Env:
    def build_env_vars(self, name: str) -> str:
        return name.upper()

    def post_sandbox_setup(self) -> str:
        return "done"
""",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_envs.py").write_text(
            "from demo_pkg.envs import Env\n"
            "def test_build_env_vars():\n"
            "    assert Env().build_env_vars('x') == 'X'\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        from ordeal.audit import audit

        result = audit("demo_pkg.envs", test_dir=str(tests_dir), max_examples=5)

        audits = {item.name: item for item in result.function_audits}
        assert "Env.build_env_vars" in audits
        assert audits["Env.build_env_vars"].status in {"exercised", "exploratory"}
        assert result.total_functions >= 1

    def test_audit_uses_configured_factory_for_instance_methods(self, tmp_path: Path, monkeypatch):
        pkg = tmp_path / "factory_pkg"
        support = tmp_path / "factory_support"
        pkg.mkdir()
        support.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (support / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            """
class Env:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def build_env_vars(self, name: str) -> str:
        return f"{self.prefix}:{name}"
""",
            encoding="utf-8",
        )
        (support / "factories.py").write_text(
            """
from factory_pkg.models import Env

def make_env() -> Env:
    return Env("demo")
""",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_models.py").write_text(
            "from factory_pkg.models import Env\n"
            "def test_build_env_vars():\n"
            "    assert Env('demo').build_env_vars('x') == 'demo:x'\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        from ordeal.audit import audit

        target = SimpleNamespace(
            target="factory_pkg.models:Env",
            factory="factory_support.factories:make_env",
            methods=["build_env_vars"],
        )
        result = audit(
            "factory_pkg.models",
            targets=[target],
            test_dir=str(tests_dir),
            max_examples=5,
        )

        audits = {item.name: item for item in result.function_audits}
        assert "Env.build_env_vars" in audits
        assert result.total_functions >= 1
        assert "Env.build_env_vars" not in result.gap_functions

    def test_audit_applies_setup_and_scenarios_for_instance_methods(
        self, tmp_path: Path, monkeypatch
    ):
        pkg = tmp_path / "scenario_pkg"
        support = tmp_path / "scenario_support"
        pkg.mkdir()
        support.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (support / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            """
class Helper:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def render(self, name: str) -> str:
        return f"{self.prefix}:{name}"

class Env:
    def __init__(self) -> None:
        self.ready = False
        self.helper = None

    def build_env_vars(self, name: str) -> str:
        if not self.ready:
            raise RuntimeError("not ready")
        if self.helper is None:
            raise RuntimeError("missing helper")
        return self.helper.render(name)
""",
            encoding="utf-8",
        )
        (support / "factories.py").write_text(
            """
from scenario_pkg.models import Env, Helper

def make_env() -> Env:
    return Env()

def prime_env(instance: Env) -> None:
    instance.ready = True

def attach_helper(instance: Env) -> Env:
    instance.helper = Helper("scenario")
    return instance
""",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_models.py").write_text(
            "from scenario_pkg.models import Env, Helper\n"
            "def test_build_env_vars():\n"
            "    env = Env()\n"
            "    env.ready = True\n"
            "    env.helper = Helper('scenario')\n"
            "    assert env.build_env_vars('x') == 'scenario:x'\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        from ordeal.audit import audit

        target = SimpleNamespace(
            target="scenario_pkg.models:Env",
            factory="scenario_support.factories:make_env",
            setup="scenario_support.factories:prime_env",
            scenarios=["scenario_support.factories:attach_helper"],
            methods=["build_env_vars"],
        )
        result = audit(
            "scenario_pkg.models",
            targets=[target],
            test_dir=str(tests_dir),
            max_examples=5,
        )

        audits = {item.name: item for item in result.function_audits}
        assert "Env.build_env_vars" in audits
        assert result.total_functions >= 1
        assert "Env.build_env_vars" not in result.gap_functions

    def test_audit_blocks_when_discovered_methods_need_harness(self, tmp_path: Path, monkeypatch):
        pkg = tmp_path / "blocked_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            """
class Env:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def rollout(self, state: dict[str, str], prompt: str) -> str:
        return f"{self.prefix}:{state['seed']}:{prompt}"
""",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        monkeypatch.syspath_prepend(str(tmp_path))

        from ordeal.audit import audit

        result = audit(
            "blocked_pkg.models",
            test_dir=str(tests_dir),
            max_examples=5,
            min_fixture_completeness=0.5,
        )

        assert result.blocking_reason
        assert "harness" in result.blocking_reason or "factory" in result.blocking_reason
        assert result.total_functions >= 1
        assert "Env.rollout" in result.gap_functions
        assert result.migrated_test_count == 0

    def test_audit_runs_lifecycle_contract_checks_with_state_factory(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        pkg = tmp_path / "contract_pkg"
        support = tmp_path / "contract_support"
        pkg.mkdir()
        support.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (support / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            """
class Env:
    def __init__(self) -> None:
        self.cleaned = False

    def cleanup(self, state: dict[str, str], marker: str) -> str:
        state["marker"] = marker
        return marker
""",
            encoding="utf-8",
        )
        (support / "factories.py").write_text(
            """
from contract_pkg.models import Env

def make_env() -> Env:
    return Env()

def make_state(instance: Env) -> dict[str, str]:
    return {"existing": "value"}
""",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        monkeypatch.syspath_prepend(str(tmp_path))

        from ordeal.audit import audit

        target = SimpleNamespace(
            target="contract_pkg.models:Env",
            factory="contract_support.factories:make_env",
            state_factory="contract_support.factories:make_state",
            methods=["cleanup"],
        )

        def cleanup_marks_instance(*, instance: object, value: object) -> bool:
            del value
            return bool(getattr(instance, "cleaned", False))

        result = audit(
            "contract_pkg.models",
            targets=[target],
            test_dir=str(tests_dir),
            max_examples=5,
            contract_checks={
                "Env.cleanup": [
                    ContractCheck(
                        name="cleanup_marks_instance",
                        kwargs={"marker": "done"},
                        predicate=cleanup_marks_instance,
                        summary="cleanup should mark the instance as cleaned",
                    )
                ]
            },
        )

        assert result.blocking_reason is None
        assert len(result.contract_findings) == 1
        assert result.contract_findings[0]["function"] == "Env.cleanup"

    def test_audit_preserves_lifecycle_context_when_async_setup_faults(
        self,
        tmp_path,
        monkeypatch,
    ):
        pkg = tmp_path / "async_contract_pkg"
        support = tmp_path / "async_contract_support"
        pkg.mkdir()
        support.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (support / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            """
class Env:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def rollout(self, marker: str) -> str:
        self.events.append(f"rollout:{marker}")
        return marker
""",
            encoding="utf-8",
        )
        (support / "factories.py").write_text(
            """
from async_contract_pkg.models import Env

def make_env() -> Env:
    return Env()

async def setup_env(instance: Env) -> Env:
    instance.events.append("setup")
    return instance

async def teardown_env(instance: Env) -> None:
    instance.events.append("teardown")
""",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        monkeypatch.syspath_prepend(str(tmp_path))

        from ordeal.audit import audit

        target = SimpleNamespace(
            target="async_contract_pkg.models:Env",
            factory="async_contract_support.factories:make_env",
            setup="async_contract_support.factories:setup_env",
            teardown="async_contract_support.factories:teardown_env",
            methods=["rollout"],
        )

        def force_detail(
            value: object,
            *,
            lifecycle_probe: dict[str, object] | None = None,
            teardown_called: bool | None = None,
            **_extra: object,
        ) -> bool:
            del value, lifecycle_probe, teardown_called
            return False

        result = audit(
            "async_contract_pkg.models",
            targets=[target],
            test_dir=str(tests_dir),
            max_examples=5,
            contract_checks={
                "Env.rollout": [
                    ContractCheck(
                        name="setup_failure_triggers_teardown",
                        kwargs={"marker": "demo"},
                        predicate=force_detail,
                        summary="teardown should run even when setup fails",
                        metadata={
                            "kind": "lifecycle",
                            "phase": "setup",
                            "fault": "raise_setup_hook",
                            "followup_phases": ["teardown"],
                            "runtime_faults": ["raise_setup_hook"],
                        },
                    )
                ]
            },
        )

        assert result.blocking_reason is None
        finding = result.contract_findings[0]
        assert finding["function"] == "Env.rollout"
        assert finding["lifecycle_probe"]["phase"] == "setup"
        assert finding["teardown_called"] is True


class TestModuleAuditSummaryValidation:
    def test_includes_fast_validation_mode_with_mutation_score(self):
        result = ModuleAudit(
            module="demo.module",
            mutation_score="1/1 (100%)",
        )

        summary = result.summary()

        assert "mutation: 1/1 (100%)" in summary
        assert "validation: fast replay" in summary

    def test_includes_deep_validation_mode_when_selected(self):
        result = ModuleAudit(
            module="demo.module",
            validation_mode="deep",
        )

        summary = result.summary()

        assert "validation: deep replay + re-mine" in summary


class TestAuditReportWorkers:
    def test_forwards_workers_to_module_audits(self, monkeypatch):
        calls: list[tuple[str, int]] = []

        def fake_audit(
            module: str,
            *,
            test_dir: str = "tests",
            max_examples: int = 20,
            workers: int = 1,
            validation_mode: str = "fast",
        ) -> ModuleAudit:
            calls.append((module, workers))
            return ModuleAudit(module=module, validation_mode=validation_mode)

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        report = audit_mod.audit_report(
            ["tests._auto_target", "tests._auto_target"],
            workers=3,
        )

        assert "ordeal audit" in report
        assert calls == [
            ("tests._auto_target", 3),
            ("tests._auto_target", 3),
        ]

    def test_forwards_validation_mode_to_module_audits(self, monkeypatch):
        calls: list[tuple[str, str]] = []

        def fake_audit(
            module: str,
            *,
            test_dir: str = "tests",
            max_examples: int = 20,
            workers: int = 1,
            validation_mode: str = "fast",
        ) -> ModuleAudit:
            calls.append((module, validation_mode))
            return ModuleAudit(module=module, validation_mode=validation_mode)

        monkeypatch.setattr(audit_mod, "audit", fake_audit)

        report = audit_mod.audit_report(
            ["tests._auto_target", "tests._auto_target"],
            validation_mode="deep",
        )

        assert "ordeal audit" in report
        assert calls == [
            ("tests._auto_target", "deep"),
            ("tests._auto_target", "deep"),
        ]


class TestMutationValidationSelection:
    def test_skips_validation_without_confident_universal_properties(self):
        mine_result = MineResult(
            function="demo.func",
            examples=5,
            properties=[
                MinedProperty("non_universal", holds=4, total=5),
                MinedProperty("tiny_sample", holds=3, total=3),
            ],
        )
        assert not audit_mod._should_validate_mined_properties(mine_result)

    def test_runs_validation_with_confident_universal_property(self):
        mine_result = MineResult(
            function="demo.func",
            examples=10,
            properties=[
                MinedProperty("universal", holds=10, total=10),
            ],
        )
        assert audit_mod._should_validate_mined_properties(mine_result)


class TestAuditCache:
    def test_cache_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = ModuleAudit(module="demo.module", validation_mode="deep")
        result.current_test_count = 2
        result.current_test_lines = 10
        result.current_coverage = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=100.0,
                total_statements=5,
                missing_count=0,
                missing_lines=frozenset(),
                source="coverage.py JSON",
            ),
        )
        result.migrated_test_count = 1
        result.migrated_lines = 6
        result.migrated_coverage = CoverageMeasurement(
            Status.VERIFIED,
            CoverageResult(
                percent=100.0,
                total_statements=5,
                missing_count=0,
                missing_lines=frozenset(),
                source="coverage.py JSON",
            ),
        )
        result.generated_test = "def test_cached():\n    assert True\n"
        result.mutation_score = "1/1 (100%)"
        result.mined_properties = ["demo: deterministic (5/5, >=57% CI)"]

        audit_mod._save_audit_cache("demo.module", "hash123", result)
        loaded = audit_mod._load_audit_cache("demo.module", "hash123")

        assert loaded == result

    def test_cache_serializes_evidence_views(self):
        result = ModuleAudit(module="demo.module", validation_mode="deep")
        result.current_test_count = 2
        result.current_test_lines = 10
        result.migrated_test_count = 1
        result.migrated_lines = 6
        result.mutation_score = "1/1 (100%)"

        payload = audit_mod._module_audit_to_dict(result)

        assert payload["evidence_views"]["current_suite"]["label"] == "current suite"
        assert payload["evidence_views"]["generated_suite"]["label"] == "generated incremental"
        assert payload["evidence_views"]["combined_view"]["label"] in {
            "change",
            "saving",
            "coverage delta",
        }

    def test_state_hash_changes_with_validation_mode(self, tmp_path: Path):
        test_file = tmp_path / "test__auto_target.py"
        test_file.write_text("def test_placeholder():\n    assert True\n")

        fast_hash = audit_mod._audit_state_hash(
            "tests._auto_target",
            test_dir=str(tmp_path),
            max_examples=5,
            validation_mode="fast",
        )
        deep_hash = audit_mod._audit_state_hash(
            "tests._auto_target",
            test_dir=str(tmp_path),
            max_examples=5,
            validation_mode="deep",
        )

        assert fast_hash != deep_hash

    def test_audit_uses_cache_and_rewrites_generated_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.chdir(tmp_path)
        cached = ModuleAudit(
            module="tests._auto_target",
            generated_test="def test_cached():\n    assert True\n",
        )

        monkeypatch.setattr(audit_mod, "_audit_state_hash", lambda *args, **kwargs: "hash123")
        monkeypatch.setattr(audit_mod, "_load_audit_cache", lambda module, state_hash: cached)
        monkeypatch.setattr(
            audit_mod,
            "_measure_coverage",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("cache hit should skip fresh coverage measurement")
            ),
        )

        result = audit_mod.audit("tests._auto_target", test_dir=str(tmp_path))

        assert result is cached
        generated = tmp_path / ".ordeal" / "test__auto_target_migrated.py"
        assert generated.read_text(encoding="utf-8") == cached.generated_test
