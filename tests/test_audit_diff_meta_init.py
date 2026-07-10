"""Tests for coverage gaps in audit, diff, metamorphic, and init.

Covers public APIs with zero or insufficient test coverage:
- audit_report() — multi-module aggregation
- Mismatch, DiffResult — str/summary formatting
- discover_relations() — auto-discovery from mine
- generate_starter_tests() — non-dry-run paths
- _truncate(), _approx_equal() — helpers
"""

from __future__ import annotations

import pytest

# ============================================================================
# audit_report
# ============================================================================


class TestAuditReport:
    def test_single_module(self):
        """audit_report with one module produces formatted output."""
        from ordeal.audit import audit_report

        report = audit_report(["ordeal.demo"])
        assert "ordeal audit" in report
        assert "ordeal.demo" in report
        assert "current suite:" in report
        assert "generated incremental:" in report

    def test_multi_module_totals(self):
        """audit_report with multiple modules shows total summary."""
        from ordeal.audit import audit_report

        report = audit_report(["ordeal.demo", "ordeal.demo"])
        assert "total:" in report
        assert "current suite:" in report
        assert "generated incremental:" in report

    def test_empty_module_list(self):
        """audit_report with empty list produces just header."""
        from ordeal.audit import audit_report

        report = audit_report([])
        assert report == "ordeal audit"


# ============================================================================
# Mismatch and DiffResult
# ============================================================================


class TestMismatch:
    def test_diff_witness_preserves_legacy_positional_slots(self):
        """New persistence metadata does not shift the public constructor."""
        from ordeal.diff import DiffOutcome, DiffWitness

        outcome = DiffOutcome(True, 1, None, None, {}, None, {})
        artifact = {"schema": "ordeal.divergence-evidence/v1"}

        witness = DiffWitness(
            {"x": 1},
            outcome,
            outcome,
            ("return_value",),
            3,
            3,
            True,
            artifact,
            "evidence.json",
        )

        assert witness.artifact is artifact
        assert witness.artifact_path == "evidence.json"
        assert witness.replay_args_json is None

    def test_str_format(self):
        """DiffWitness.__str__ produces readable envelope output."""
        from ordeal.diff import DiffOutcome, DiffWitness

        def returned(value: object) -> DiffOutcome:
            return DiffOutcome(
                returned=True,
                return_value=value,
                exception_type=None,
                exception_message=None,
                mutated_arguments={},
                receiver_state=None,
                side_effects={},
            )

        witness = DiffWitness(
            args={"x": 1, "y": 2},
            outcome_a=returned(3),
            outcome_b=returned(4),
            differences=("return_value",),
        )
        s = str(witness)
        assert "args:" in s
        assert "outcome_a:" in s
        assert "outcome_b:" in s
        assert "3" in s
        assert "4" in s

    def test_str_truncates_long_values(self):
        """DiffWitness.__str__ truncates very long repr values."""
        from ordeal.diff import DiffOutcome, DiffWitness

        long_str = "x" * 200
        outcome = DiffOutcome(True, long_str, None, None, {}, None, {})
        witness = DiffWitness(
            args={"data": long_str},
            outcome_a=outcome,
            outcome_b=DiffOutcome(True, "short", None, None, {}, None, {}),
            differences=("return_value",),
        )
        s = str(witness)
        assert "..." in s


class TestDiffResult:
    def test_no_divergence_observed_is_explicit(self):
        from ordeal.diff import DiffResult

        result = DiffResult(
            function_a="f",
            function_b="g",
            total=10,
            status="no_divergence_observed",
        )
        assert result.status == "no_divergence_observed"
        assert result.witness is None

    def test_divergent_requires_one_witness(self):
        from ordeal.diff import DiffResult

        with pytest.raises(ValueError, match="require"):
            DiffResult(function_a="f", function_b="g", total=10, status="divergent")

    def test_summary_reports_no_divergence_observed(self):
        from ordeal.diff import DiffResult

        result = DiffResult(
            function_a="score_v1",
            function_b="score_v2",
            total=50,
            status="no_divergence_observed",
        )
        s = result.summary()
        assert "NO DIVERGENCE OBSERVED" in s
        assert "50 examples" in s


class TestDiffFunction:
    def test_identical_functions(self):
        from ordeal.diff import diff

        def add(a: int, b: int) -> int:
            return a + b

        result = diff(add, add, max_examples=20)
        assert result.status == "no_divergence_observed"
        assert result.total == 20

    def test_different_functions(self):
        from ordeal.diff import diff

        def add(a: int, b: int) -> int:
            return a + b

        def sub(a: int, b: int) -> int:
            return a - b

        result = diff(add, sub, max_examples=20)
        assert result.status == "divergent"
        assert result.witness is not None

    def test_float_tolerance(self):
        from ordeal.diff import diff

        # Use bounded floats to avoid overflow at float_max
        def f(x: float) -> float:
            return max(-1e100, min(1e100, x)) * 1.0

        def g(x: float) -> float:
            return max(-1e100, min(1e100, x)) * 1.0000001

        result = diff(f, g, rtol=1e-5, max_examples=20)
        assert result.status == "no_divergence_observed"

    def test_custom_comparator(self):
        from ordeal.diff import diff

        def f(x: int) -> dict:
            return {"value": x, "extra": "a"}

        def g(x: int) -> dict:
            return {"value": x, "extra": "b"}

        result = diff(f, g, compare=lambda a, b: a["value"] == b["value"], max_examples=20)
        assert result.status == "no_divergence_observed"


# ============================================================================
# discover_relations
# ============================================================================


class TestDiscoverRelations:
    def test_commutative_function(self):
        from ordeal.metamorphic import discover_relations

        def add(x: int, y: int) -> int:
            return x + y

        relations = discover_relations(add, max_examples=30)
        names = [r.name for r in relations]
        assert "commutative" in names

    def test_deterministic_function(self):
        from ordeal.metamorphic import discover_relations

        def double(x: int) -> int:
            return x * 2

        relations = discover_relations(double, max_examples=30)
        names = [r.name for r in relations]
        assert "deterministic" in names

    def test_returns_list(self):
        """discover_relations always returns a list, even if empty."""
        from ordeal.metamorphic import discover_relations

        call_count = 0

        def nondeterministic(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return call_count

        relations = discover_relations(nondeterministic, max_examples=10)
        assert isinstance(relations, list)

    def test_relations_are_usable_with_metamorphic(self):
        """Discovered relations have the fields @metamorphic needs."""
        from ordeal.metamorphic import discover_relations

        def add(x: int, y: int) -> int:
            return x + y

        relations = discover_relations(add, max_examples=20)
        assert len(relations) > 0
        for r in relations:
            assert r.name
            assert r.transform is not None
            assert r.check is not None


# ============================================================================
# generate_starter_tests (non-dry-run)
# ============================================================================


class TestGenerateStarterTests:
    def test_module_target(self):
        from ordeal.mutations import generate_starter_tests

        content = generate_starter_tests("ordeal.demo")
        assert "import ordeal.demo" in content
        assert "def test_" in content
        assert "score" in content or "clamp" in content or "encode" in content

    def test_function_target(self):
        from ordeal.mutations import generate_starter_tests

        content = generate_starter_tests("ordeal.demo.score")
        assert "score" in content
        assert "def test_" in content

    def test_invalid_target_returns_empty(self):
        from ordeal.mutations import generate_starter_tests

        content = generate_starter_tests("nonexistent.module.func")
        assert content == ""

    def test_dry_run_vs_normal_both_produce_tests(self):
        from ordeal.mutations import generate_starter_tests

        dry = generate_starter_tests("ordeal.demo", dry_run=True)
        normal = generate_starter_tests("ordeal.demo", dry_run=False)
        assert "def test_" in dry
        assert "def test_" in normal

    def test_starter_tests_import_module_types_in_property_checks(self, tmp_path, monkeypatch):
        from ordeal.mutations import generate_starter_tests

        pkg = tmp_path / "genstarter"
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

        content = generate_starter_tests("genstarter.mod.parse")

        assert "import genstarter.mod" in content
        assert "config: genstarter.mod.PolicyConfig" in content
        assert "data: dict[str, Any]" in content


# ============================================================================
# _truncate helper
# ============================================================================


class TestTruncate:
    def test_short_string_unchanged(self):
        from ordeal.diff import _truncate

        assert _truncate("hello") == "'hello'"

    def test_long_string_truncated(self):
        from ordeal.diff import _truncate

        result = _truncate("x" * 200)
        assert result.endswith("...")

    def test_custom_limit(self):
        from ordeal.diff import _truncate

        result = _truncate("x" * 100, limit=20)
        assert result.endswith("...")


# ============================================================================
# _approx_equal edge cases
# ============================================================================


class TestApproxEqual:
    def test_nan_equals_nan(self):
        from ordeal.diff import _approx_equal

        assert _approx_equal(float("nan"), float("nan"), 1e-9, 0.0) is True

    def test_inf_equals_inf(self):
        from ordeal.diff import _approx_equal

        assert _approx_equal(float("inf"), float("inf"), 1e-9, 0.0) is True
        assert _approx_equal(float("-inf"), float("-inf"), 1e-9, 0.0) is True

    def test_inf_not_equals_neg_inf(self):
        from ordeal.diff import _approx_equal

        assert _approx_equal(float("inf"), float("-inf"), 1e-9, 0.0) is False

    def test_list_comparison(self):
        from ordeal.diff import _approx_equal

        assert _approx_equal([1.0, 2.0], [1.0, 2.0], 1e-9, 0.0) is True
        assert _approx_equal([1.0, 2.0], [1.0, 3.0], 1e-9, 0.0) is False

    def test_dict_comparison(self):
        from ordeal.diff import _approx_equal

        assert _approx_equal({"a": 1.0}, {"a": 1.0}, 1e-9, 0.0) is True
        assert _approx_equal({"a": 1.0}, {"a": 2.0}, 1e-9, 0.0) is False
        assert _approx_equal({"a": 1.0}, {"b": 1.0}, 1e-9, 0.0) is False

    def test_different_length_lists(self):
        from ordeal.diff import _approx_equal

        assert _approx_equal([1.0], [1.0, 2.0], 1e-9, 0.0) is False
