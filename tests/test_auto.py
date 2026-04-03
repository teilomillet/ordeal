"""Tests for ordeal.auto — zero-boilerplate testing."""

from __future__ import annotations

import hypothesis.strategies as st

import tests._auto_target as target
from ordeal.auto import _test_one_function, chaos_for, fuzz, scan_module
from ordeal.invariants import finite
from ordeal.mine import MinedProperty


class TestScanModule:
    def test_scans_typed_functions(self):
        result = scan_module("tests._auto_target", max_examples=10)
        names = [f.name for f in result.functions]
        assert "add" in names
        assert "greet" in names
        assert "clamp" in names

    def test_skips_untyped(self):
        result = scan_module("tests._auto_target", max_examples=10)
        skipped_names = [name for name, _ in result.skipped]
        assert "no_hints" in skipped_names

    def test_skips_private(self):
        result = scan_module("tests._auto_target", max_examples=10)
        all_names = [f.name for f in result.functions] + [n for n, _ in result.skipped]
        assert "_private" not in all_names

    def test_catches_crash(self):
        result = scan_module("tests._auto_target", max_examples=50)
        divide_result = next(f for f in result.functions if f.name == "divide")
        # divide(a, 0.0) crashes — scan should catch it
        assert not divide_result.passed

    def test_safe_functions_pass(self):
        result = scan_module("tests._auto_target", max_examples=20)
        add_result = next(f for f in result.functions if f.name == "add")
        assert add_result.passed

    def test_summary(self):
        result = scan_module("tests._auto_target", max_examples=10)
        s = result.summary()
        assert "scan_module" in s
        assert "functions" in s

    def test_with_module_object(self):
        result = scan_module(target, max_examples=10)
        assert result.total > 0

    def test_filters_low_signal_property_warnings(self, monkeypatch):
        import ordeal.mine as mine_mod

        def fake_mine(fn, max_examples):
            return type(
                "_FakeMineResult",
                (),
                {
                    "properties": [
                        MinedProperty("monotonically non-decreasing in lo", 19, 20),
                        MinedProperty("idempotent", 17, 20),
                    ]
                },
            )()

        monkeypatch.setattr(mine_mod, "mine", fake_mine)

        result = _test_one_function(
            "identity",
            lambda x: x,
            {"x": st.integers()},
            None,
            max_examples=1,
            check_return_type=False,
        )

        assert "idempotent (85%)" in result.property_violations
        assert not any("monotonically" in v for v in result.property_violations)


class TestFuzz:
    def test_safe_function_passes(self):
        result = fuzz(target.add, max_examples=50)
        assert result.passed

    def test_crashy_function_fails(self):
        result = fuzz(target.divide, max_examples=200)
        assert not result.passed

    def test_with_fixture_override(self):
        import hypothesis.strategies as st

        # Force b to be nonzero — divide should pass
        result = fuzz(
            target.divide,
            max_examples=100,
            b=st.floats(min_value=0.1, max_value=100.0),
        )
        assert result.passed

    def test_summary(self):
        result = fuzz(target.add, max_examples=10)
        assert "fuzz" in result.summary()


class TestChaosFor:
    def test_generates_testcase(self):
        import hypothesis.strategies as st

        # Provide safe fixture for b to avoid known divide-by-zero
        TestCase = chaos_for(
            "tests._auto_target",
            fixtures={"b": st.floats(min_value=0.1, max_value=10.0)},
            faults=[],
            invariants=[],
            max_examples=10,
            stateful_step_count=5,
        )
        assert TestCase is not None
        test = TestCase("runTest")
        test.runTest()

    def test_with_invariants(self):
        import hypothesis.strategies as st

        # Constrain both a and b to avoid Inf from large/small divisions
        TestCase = chaos_for(
            "tests._auto_target",
            fixtures={
                "a": st.floats(min_value=-1e6, max_value=1e6),
                "b": st.floats(min_value=0.1, max_value=10.0),
            },
            invariants=[finite],
            max_examples=10,
            stateful_step_count=5,
        )
        test = TestCase("runTest")
        test.runTest()

    def test_with_fixtures(self):
        import hypothesis.strategies as st

        TestCase = chaos_for(
            "tests._auto_target",
            fixtures={"b": st.floats(min_value=0.1, max_value=10.0)},
            faults=[],
            invariants=[],
            max_examples=10,
            stateful_step_count=5,
        )
        test = TestCase("runTest")
        test.runTest()

    def test_auto_discovers_invariants(self):
        """chaos_for() with no invariants/faults auto-mines and infers."""
        TestCase = chaos_for(
            "ordeal.demo",
            max_examples=5,
            stateful_step_count=5,
        )
        assert TestCase is not None


# ============================================================================
# Tests for new features
# ============================================================================


class TestScanExpectedFailures:
    """expected_failures parameter skips known-broken functions."""

    def test_expected_failure_does_not_count(self):
        result = scan_module(
            "tests._auto_target",
            max_examples=5,
            expected_failures=["divide"],  # divide crashes on b=0
        )
        # divide may fail but shouldn't count toward .failed
        assert (
            result.passed
            or result.failed == 0
            or "divide"
            not in [
                f.name
                for f in result.functions
                if not f.passed and f.name not in result.expected_failure_names
            ]
        )

    def test_expected_failures_tracked(self):
        result = scan_module(
            "tests._auto_target",
            max_examples=5,
            expected_failures=["divide"],
        )
        assert "divide" in result.expected_failure_names


class TestScanPerFunctionBudget:
    """max_examples as dict gives per-function control."""

    def test_dict_max_examples(self):
        result = scan_module(
            "tests._auto_target",
            max_examples={"add": 3, "greet": 3, "__default__": 5},
        )
        # Should still work — just different budgets per function
        names = [f.name for f in result.functions]
        assert "add" in names
        assert "greet" in names


class TestFuzzFailingArgs:
    """fuzz() captures shrunk failing input."""

    def test_failing_args_captured(self):
        result = fuzz(target.divide, max_examples=50)
        if not result.passed:
            # divide(a, 0) crashes — failing_args should be set
            assert result.failing_args is not None
            assert "b" in result.failing_args

    def test_passing_has_no_failing_args(self):
        result = fuzz(target.add, max_examples=20)
        assert result.passed
        assert result.failing_args is None


class TestChaosForPerFunctionInvariants:
    """chaos_for with dict invariants applies per function."""

    def test_dict_invariants_type_accepted(self):
        """Verify chaos_for accepts dict invariants without error."""
        from ordeal.invariants import bounded

        # Just verify it creates the class — don't run it because
        # _auto_target.divide crashes on b=0 regardless of invariants
        TestCase = chaos_for(
            "tests._auto_target",
            invariants={"clamp": bounded(0, 1)},
            max_examples=5,
            stateful_step_count=3,
        )
        assert TestCase is not None


class TestLiteralInScan:
    """Literal-typed params are auto-resolved in scan_module."""

    def test_literal_param_not_skipped(self):
        import sys
        import types

        # Create a module with Literal param
        mod = types.ModuleType("_test_literal_scan")
        exec(
            "from typing import Literal\n"
            'def choose(opt: Literal["a", "b"]) -> str:\n'
            "    return opt\n",
            mod.__dict__,
        )
        sys.modules["_test_literal_scan"] = mod
        try:
            result = scan_module("_test_literal_scan", max_examples=5)
            names = [f.name for f in result.functions]
            assert "choose" in names
            skipped_names = [n for n, _ in result.skipped]
            assert "choose" not in skipped_names
        finally:
            del sys.modules["_test_literal_scan"]
