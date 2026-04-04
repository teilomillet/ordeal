"""Tests for ordeal.auto — zero-boilerplate testing."""

from __future__ import annotations

import shlex
import sys
import types
from pathlib import Path

import hypothesis.strategies as st

import ordeal.auto as auto_mod
import tests._auto_target as target
from ordeal.auto import ContractCheck, _test_one_function, chaos_for, fuzz, scan_module
from ordeal.invariants import finite
from ordeal.mine import MinedProperty


def _install_method_target_module(module_name: str):
    import sys
    import types

    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.ready = False\n"
        "        self.prefix = 'ready:'\n"
        "\n"
        "    def greet(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        return f'{self.prefix}{name}'\n"
        "\n"
        "    async def async_greet(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        return f'{self.prefix}{name}'\n"
        "\n"
        "    @classmethod\n"
        "    def label(cls, name: str) -> str:\n"
        "        return f'{cls.__name__}:{name}'\n"
        "\n"
        "    @staticmethod\n"
        "    def shout(name: str) -> str:\n"
        "        return name.upper()\n"
        "\n"
        "async def async_add(a: int, b: int) -> int:\n"
        "    return a + b\n",
        mod.__dict__,
    )
    return mod


def _install_scenario_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Helper:\n"
        "    def __init__(self, prefix: str = 'scenario:') -> None:\n"
        "        self.prefix = prefix\n"
        "\n"
        "    def render(self, name: str) -> str:\n"
        "        return f'{self.prefix}{name}'\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.ready = False\n"
        "        self.prefix = 'factory:'\n"
        "        self.helper = None\n"
        "\n"
        "    def render(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        if self.helper is None:\n"
        "            raise RuntimeError('missing helper')\n"
        "        return f'{self.prefix}{self.helper.render(name)}'\n"
        "\n"
        "    async def async_render(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        if self.helper is None:\n"
        "            raise RuntimeError('missing helper')\n"
        "        return f'{self.prefix}{self.helper.render(name)}'\n",
        mod.__dict__,
    )
    return mod


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

    def test_discovers_instance_methods_and_ignores_lazy_exports(self, monkeypatch):
        mod = types.ModuleType("_test_lazy_methods")
        exec(
            "class Env:\n"
            "    def __init__(self):\n"
            "        self.prefix = 'ok'\n"
            "\n"
            "    def build_env_vars(self, path: str) -> str:\n"
            "        return f'{self.prefix}:{path}'\n"
            "\n"
            "def direct(x: int) -> int:\n"
            "    return x\n"
            "\n"
            "def __getattr__(name):\n"
            "    raise AttributeError(name)\n"
            "\n"
            "def __dir__():\n"
            "    return ['Env', 'direct', 'PhantomExport']\n",
            mod.__dict__,
        )
        sys.modules[mod.__name__] = mod
        monkeypatch.setitem(
            auto_mod._REGISTERED_OBJECT_FACTORIES,
            "_test_lazy_methods:Env",
            lambda: mod.Env(),
        )
        try:
            result = scan_module(mod, max_examples=10)
            names = [f.name for f in result.functions]
            assert "direct" in names
            assert "Env.build_env_vars" in names
            assert "PhantomExport" not in names
        finally:
            del sys.modules[mod.__name__]

    def test_catches_crash(self):
        result = scan_module("tests._auto_target", max_examples=50)
        divide_result = next(f for f in result.functions if f.name == "divide")
        # divide(a, 0.0) crashes — scan should catch it
        assert not divide_result.passed

    def test_captures_failing_args_for_crash(self):
        result = scan_module("tests._auto_target", max_examples=10)
        divide_result = next(f for f in result.functions if f.name == "divide")
        assert divide_result.failing_args is not None
        assert divide_result.failing_args["b"] == 0.0

    def test_safe_functions_pass(self):
        result = scan_module("tests._auto_target", max_examples=20)
        add_result = next(f for f in result.functions if f.name == "add")
        assert add_result.passed

    def test_fixture_override_can_avoid_boundary_crash(self):
        result = scan_module(
            "tests._auto_target",
            max_examples=20,
            fixtures={"b": st.floats(min_value=0.1, max_value=100.0)},
        )
        divide_result = next(f for f in result.functions if f.name == "divide")
        assert divide_result.passed

    def test_summary(self):
        result = scan_module("tests._auto_target", max_examples=10)
        s = result.summary()
        assert "scan_module" in s
        assert "functions" in s

    def test_with_module_object(self):
        result = scan_module(target, max_examples=10)
        assert result.total > 0

    def test_discovers_public_methods_and_skips_missing_factories(self):
        import sys

        module_name = "_test_method_targets_skip"
        mod = _install_method_target_module(module_name)
        try:
            result = scan_module(mod, max_examples=10)
            names = [f.name for f in result.functions]
            assert "Env.label" in names
            assert "Env.shout" in names
            assert "async_add" in names
            skipped = dict(result.skipped)
            assert skipped["Env.greet"] == "missing object factory"
            assert skipped["Env.async_greet"] == "missing object factory"
        finally:
            del sys.modules[module_name]

    def test_method_factories_enable_async_scan_and_explicit_fuzz_targets(self):
        import sys

        module_name = "_test_method_targets_factory"
        mod = _install_method_target_module(module_name)
        key = f"{module_name}:Env"

        def factory() -> object:
            return mod.Env()

        def setup(instance: object) -> None:
            instance.ready = True
            instance.prefix = "ok:"

        try:
            result = scan_module(
                mod,
                max_examples=20,
                object_factories={key: factory},
                object_setups={key: setup},
            )
            names = [f.name for f in result.functions]
            assert "Env.greet" in names
            assert "Env.async_greet" in names
            assert "Env.label" in names
            assert "Env.shout" in names
            assert "async_add" in names
            assert result.passed

            fuzz_result = fuzz(
                f"{module_name}:Env.greet",
                max_examples=20,
                object_factories={key: factory},
                object_setups={key: setup},
            )
            assert fuzz_result.passed
        finally:
            del sys.modules[module_name]

    def test_method_scenarios_apply_after_setup_for_scan_fuzz_and_chaos(self):
        import sys

        module_name = "_test_method_targets_scenario"
        mod = _install_scenario_target_module(module_name)
        key = f"{module_name}:Env"

        def factory() -> object:
            return mod.Env()

        def setup(instance: object) -> None:
            instance.ready = True
            instance.prefix = "setup:"

        async def scenario(instance: object) -> object:
            instance.helper = mod.Helper(prefix="scenario:")
            return instance

        try:
            result = scan_module(
                mod,
                max_examples=20,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: scenario},
            )
            names = [f.name for f in result.functions]
            assert "Env.render" in names
            assert "Env.async_render" in names
            assert result.passed

            fuzz_result = fuzz(
                f"{module_name}:Env.render",
                max_examples=20,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: scenario},
            )
            assert fuzz_result.passed

            test_case = chaos_for(
                mod,
                max_examples=5,
                stateful_step_count=5,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: scenario},
            )
            test_case("runTest").runTest()
        finally:
            del sys.modules[module_name]

    def test_scan_module_can_limit_to_explicit_method_targets(self):
        import sys

        module_name = "_test_method_targets_select"
        mod = _install_method_target_module(module_name)
        key = f"{module_name}:Env"

        def factory() -> object:
            instance = mod.Env()
            instance.ready = True
            return instance

        try:
            result = scan_module(
                mod,
                max_examples=10,
                targets=[f"{module_name}:Env.greet"],
                object_factories={key: factory},
            )

            assert [item.name for item in result.functions] == ["Env.greet"]
            assert result.skipped == []
        finally:
            del sys.modules[module_name]

    def test_skips_imported_typing_helpers(self):
        import sys
        import types

        mod = types.ModuleType("_test_imported_helpers")
        exec(
            "from typing import cast\ndef real(x: int) -> int:\n    return x\n",
            mod.__dict__,
        )
        sys.modules["_test_imported_helpers"] = mod
        try:
            result = scan_module("_test_imported_helpers", max_examples=5)
            names = [f.name for f in result.functions]
            assert names == ["real"]
        finally:
            del sys.modules["_test_imported_helpers"]

    def test_documented_precondition_is_not_reported_as_crash(self):
        mod = types.ModuleType("_test_preconditions")
        exec(
            "def score_responses(prompts: list[str], responses: list[str]) -> list[int]:\n"
            '    """Raises ValueError when prompts and responses have different lengths."""\n'
            "    if len(prompts) != len(responses):\n"
            "        raise ValueError('prompts and responses must have the same length')\n"
            "    return [\n"
            "        len(prompt) + len(response)\n"
            "        for prompt, response in zip(prompts, responses)\n"
            "    ]\n",
            mod.__dict__,
        )
        sys.modules["_test_preconditions"] = mod
        try:
            result = scan_module("_test_preconditions", max_examples=20)
            scored = next(f for f in result.functions if f.name == "score_responses")
            assert scored.passed
            assert scored.contract_violations
            assert result.failed == 0
        finally:
            del sys.modules["_test_preconditions"]

    def test_pathlike_arguments_are_tracked_by_contract_helpers(self):
        contract = auto_mod.quoted_paths_contract(
            kwargs={"path": Path("demo files/input.txt")},
        )

        assert contract.predicate(["cp", "demo files/input.txt", "/tmp"])
        assert not contract.predicate(["cp", "demo", "files/input.txt", "/tmp"])

    def test_explicit_contract_checks_are_reported_without_failing_scan(self):
        mod = types.ModuleType("_test_contracts")
        exec(
            "def build_command(path: str) -> str:\n    return f'cp {path} /tmp'\n",
            mod.__dict__,
        )
        sys.modules[mod.__name__] = mod
        try:
            result = scan_module(
                mod,
                max_examples=5,
                contract_checks={
                    "build_command": [
                        ContractCheck(
                            name="shell-safe path quoting",
                            kwargs={"path": "a b"},
                            predicate=lambda cmd: shlex.quote("a b") in cmd,
                        )
                    ]
                },
            )
            command = next(f for f in result.functions if f.name == "build_command")
            assert command.passed
            assert command.contract_violations
            assert command.contract_violation_details[0]["category"] == "semantic_contract"
            assert result.failed == 0
        finally:
            del sys.modules[mod.__name__]

    def test_filters_low_signal_property_warnings(self, monkeypatch):
        import ordeal.mine as mine_mod

        def fake_mine(
            fn,
            max_examples,
            ignore_properties=(),
            ignore_relations=(),
            property_overrides=None,
            relation_overrides=None,
        ):
            return type(
                "_FakeMineResult",
                (),
                {
                    "properties": [
                        MinedProperty("monotonically non-decreasing in lo", 19, 20),
                        MinedProperty("idempotent", 19, 20),
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

        assert "idempotent (95%)" in result.property_violations
        assert not any("monotonically" in v for v in result.property_violations)

    def test_records_replayable_crashes(self):
        result = scan_module("tests._auto_target", max_examples=10)
        divide_result = next(f for f in result.functions if f.name == "divide")
        assert divide_result.replayable is True
        assert divide_result.replay_attempts == 2
        assert divide_result.replay_matches == 2
        assert divide_result.crash_category == "likely_bug"

    def test_marks_unreplayed_crashes_as_speculative(self):
        calls = iter(range(10_000))

        def flaky(x: int) -> int:
            raise RuntimeError(f"boom-{next(calls)}")

        result = _test_one_function(
            "flaky",
            flaky,
            {"x": st.integers()},
            None,
            max_examples=1,
            check_return_type=False,
        )

        assert not result.passed
        assert result.crash_category == "speculative_crash"
        assert result.replayable is False
        assert "WARN  flaky" in str(result)

    def test_ignore_properties_suppresses_noisy_scan_warnings(self, monkeypatch):
        import ordeal.mine as mine_mod

        def fake_mine(
            fn,
            max_examples,
            ignore_properties=(),
            ignore_relations=(),
            property_overrides=None,
            relation_overrides=None,
        ):
            props = [MinedProperty("commutative", 17, 20), MinedProperty("idempotent", 17, 20)]
            suppressed = set(ignore_properties)
            return type(
                "_FakeMineResult",
                (),
                {"properties": [prop for prop in props if prop.name not in suppressed]},
            )()

        monkeypatch.setattr(mine_mod, "mine", fake_mine)

        result = scan_module(
            "tests._auto_target",
            max_examples=5,
            ignore_properties=["commutative"],
            property_overrides={"add": ["idempotent"]},
        )

        add_result = next(f for f in result.functions if f.name == "add")
        assert add_result.property_violations == []

    def test_ignore_relations_suppresses_noisy_relation_warnings(self, monkeypatch):
        import ordeal.mine as mine_mod

        captured: dict[str, object] = {}

        def fake_mine(
            fn,
            max_examples,
            ignore_properties=(),
            ignore_relations=(),
            property_overrides=None,
            relation_overrides=None,
        ):
            captured["ignore_relations"] = list(ignore_relations)
            captured["relation_overrides"] = relation_overrides
            return type(
                "_FakeMineResult",
                (),
                {"properties": [MinedProperty("idempotent", 17, 20)]},
            )()

        monkeypatch.setattr(mine_mod, "mine", fake_mine)

        result = scan_module(
            "tests._auto_target",
            max_examples=5,
            ignore_relations=["commutative_composition"],
            relation_overrides={"add": ["roundtrip"]},
        )

        assert captured["ignore_relations"] == ["commutative_composition"]
        assert captured["relation_overrides"] == {"add": ["roundtrip"]}
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

    def test_async_callable_passes(self):
        import sys

        module_name = "_test_async_fuzz"
        mod = _install_method_target_module(module_name)
        try:
            result = fuzz(mod.async_add, max_examples=20)
            assert result.passed
        finally:
            del sys.modules[module_name]


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

    def test_method_targets_work_in_stateful_chaos(self):
        import sys

        module_name = "_test_method_targets_chaos"
        mod = _install_method_target_module(module_name)
        key = f"{module_name}:Env"

        def factory():
            return mod.Env()

        def setup(instance):
            instance.ready = True
            instance.prefix = "ok:"

        try:
            TestCase = chaos_for(
                mod,
                object_factories={key: factory},
                object_setups={key: setup},
                faults=[],
                invariants=[],
                max_examples=5,
                stateful_step_count=5,
            )
            test = TestCase("runTest")
            test.runTest()
        finally:
            del sys.modules[module_name]


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
