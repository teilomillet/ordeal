"""Tests for ordeal.auto — zero-boilerplate testing."""

from __future__ import annotations

import tests._auto_target as target
from ordeal.auto import chaos_for, fuzz, scan_module
from ordeal.invariants import finite


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
            max_examples=10,
            stateful_step_count=5,
        )
        assert TestCase is not None
        test = TestCase("runTest")
        test.runTest()

    def test_with_invariants(self):
        import hypothesis.strategies as st

        # Exclude divide (crashes on b=0) by providing safe fixtures
        TestCase = chaos_for(
            "tests._auto_target",
            fixtures={"b": st.floats(min_value=0.1, max_value=10.0)},
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
            max_examples=10,
            stateful_step_count=5,
        )
        test = TestCase("runTest")
        test.runTest()
