"""Tests for ordeal.auto — zero-boilerplate testing."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import types
from pathlib import Path

import hypothesis.strategies as st
import pytest

import ordeal
import ordeal.auto as auto_mod
import tests._auto_target as target
from ordeal.auto import (
    ContractCheck,
    SeedExample,
    _candidate_inputs,
    _get_public_functions,
    _test_one_function,
    chaos_for,
    fuzz,
    scan_module,
)
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


def _install_multi_scenario_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Helper:\n"
        "    def __init__(self, prefix: str) -> None:\n"
        "        self.prefix = prefix\n"
        "\n"
        "    def render(self, name: str) -> str:\n"
        "        return f'{self.prefix}{name}'\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.ready = False\n"
        "        self.helper = None\n"
        "\n"
        "    def render(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        if self.helper is None:\n"
        "            raise RuntimeError('missing helper')\n"
        "        return self.helper.render(name)\n"
        "\n"
        "    async def async_render(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        if self.helper is None:\n"
        "            raise RuntimeError('missing helper')\n"
        "        return self.helper.render(name)\n",
        mod.__dict__,
    )
    return mod


def _install_inline_scenario_target_module(module_name: str):
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
        "    async def async_render(self, name: str) -> str:\n"
        "        return f'{self.prefix}{name}'\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.ready = False\n"
        "        self.helper = None\n"
        "\n"
        "    def render(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        if self.helper is None:\n"
        "            raise RuntimeError('missing helper')\n"
        "        return self.helper.render(name)\n"
        "\n"
        "    async def async_render(self, name: str) -> str:\n"
        "        if not self.ready:\n"
        "            raise RuntimeError('not ready')\n"
        "        if self.helper is None:\n"
        "            raise RuntimeError('missing helper')\n"
        "        return await self.helper.async_render(name)\n",
        mod.__dict__,
    )
    return mod


def _install_subprocess_pack_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Runner:\n"
        "    async def run(self, command: str):\n"
        "        raise RuntimeError('needs subprocess scenario')\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.runner = Runner()\n"
        "\n"
        "    async def render(self, command: str) -> str:\n"
        "        result = await self.runner.run(command)\n"
        "        return result.stdout\n",
        mod.__dict__,
    )
    return mod


def _install_sandbox_client_pack_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class SandboxClient:\n"
        "    async def execute_command(self, command: str):\n"
        "        raise RuntimeError('needs sandbox scenario')\n"
        "\n"
        "    async def upload_content(self, path: str, content: bytes):\n"
        "        raise RuntimeError('needs sandbox scenario')\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.sandbox_client = SandboxClient()\n"
        "\n"
        "    async def render(self, command: str, path: str, content: bytes) -> str:\n"
        "        result = await self.sandbox_client.execute_command(command)\n"
        "        await self.sandbox_client.upload_content(path, content)\n"
        "        return result.stdout\n",
        mod.__dict__,
    )
    return mod


def _install_upload_download_pack_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Storage:\n"
        "    def upload(self, payload: bytes):\n"
        "        raise RuntimeError('needs upload_download scenario')\n"
        "\n"
        "    def download(self, receipt):\n"
        "        raise RuntimeError('needs upload_download scenario')\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.client = Storage()\n"
        "\n"
        "    def render(self, payload: bytes) -> int:\n"
        "        receipt = self.client.upload(payload)\n"
        "        data = self.client.download(receipt)\n"
        "        return len(data)\n",
        mod.__dict__,
    )
    return mod


def _install_http_pack_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Client:\n"
        "    async def get(self, url: str):\n"
        "        raise RuntimeError('needs http scenario')\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.session = Client()\n"
        "\n"
        "    async def render(self, url: str) -> int:\n"
        "        response = await self.session.get(url)\n"
        "        return response.status_code\n",
        mod.__dict__,
    )
    return mod


def _install_state_store_pack_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class StateStore:\n"
        "    def get(self, key: str, default=None):\n"
        "        raise RuntimeError('needs state_store scenario')\n"
        "\n"
        "    def set(self, key: str, value: str) -> None:\n"
        "        raise RuntimeError('needs state_store scenario')\n"
        "\n"
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.state_store = StateStore()\n"
        "\n"
        "    def render(self, key: str, value: str) -> str:\n"
        "        self.state_store.set(key, value)\n"
        "        return self.state_store.get(key)\n",
        mod.__dict__,
    )
    return mod


def _install_stateful_target_module(module_name: str):
    mod = types.ModuleType(module_name)
    sys.modules[module_name] = mod
    exec(
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.seen = []\n"
        "\n"
        "    def rollout(self, state: dict[str, str], prompt: str) -> str:\n"
        "        state['prompt'] = prompt\n"
        "        self.seen.append(prompt)\n"
        "        return state['prompt']\n",
        mod.__dict__,
    )
    return mod


class TestScanModule:
    def test_real_bug_prefers_observed_inputs_before_boundary_smoke(self, monkeypatch):
        def target(value: int) -> int:
            if value < 0:
                raise RuntimeError("boundary")
            return value

        monkeypatch.setattr(
            auto_mod,
            "_seed_examples_for_callable",
            lambda *args, **kwargs: [
                SeedExample(kwargs={"value": 1}, source="test", evidence="observed test"),
                SeedExample(kwargs={"value": 2}, source="fixture", evidence="observed fixture"),
            ],
        )
        monkeypatch.setattr(
            auto_mod,
            "_boundary_smoke_inputs",
            lambda *args, **kwargs: [{"value": -1}],
        )

        candidates = _candidate_inputs(target, mode="real_bug")

        assert [candidate.origin for candidate in candidates[:2]] == ["test", "fixture"]
        assert candidates[-1].origin == "boundary"

    def test_test_seeds_include_pytest_parametrize_examples(self, tmp_path, monkeypatch):
        pkg = tmp_path / "seedpkg"
        tests_dir = tmp_path / "tests"
        pkg.mkdir()
        tests_dir.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "mod.py").write_text(
            "def compute(x: int, y: int) -> int:\n    return x + y\n",
            encoding="utf-8",
        )
        (tests_dir / "test_mod.py").write_text(
            "import pytest\n"
            "from seedpkg.mod import compute\n"
            "\n"
            "@pytest.mark.parametrize(('x', 'y'), [(1, 2), (3, 4)])\n"
            "def test_compute(x, y):\n"
            "    assert compute(x, y) >= x\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        seeds = auto_mod._test_seed_examples("seedpkg.mod", "compute")

        assert {tuple(seed.kwargs.items()) for seed in seeds} == {
            (("x", 1), ("y", 2)),
            (("x", 3), ("y", 4)),
        }

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

    def test_target_selectors_filter_scan_module_by_exact_and_glob_names(self):
        import sys
        import types

        mod = types.ModuleType("_test_target_selectors")
        exec(
            "def alpha(x: int) -> int:\n"
            "    return x\n"
            "\n"
            "def beta(x: int) -> int:\n"
            "    return x\n"
            "\n"
            "def gamma(x: int) -> int:\n"
            "    return x\n",
            mod.__dict__,
        )
        sys.modules[mod.__name__] = mod
        try:
            result = scan_module(mod, max_examples=2, targets=["a*", "gamma"])
            assert [f.name for f in result.functions] == ["alpha", "gamma"]
            assert result.skipped == []
        finally:
            del sys.modules[mod.__name__]

    def test_target_selector_error_is_explicit_when_no_callables_match(self):
        import sys
        import types

        mod = types.ModuleType("_test_target_selector_error")
        exec(
            "def alpha(x: int) -> int:\n    return x\n",
            mod.__dict__,
        )
        sys.modules[mod.__name__] = mod
        try:
            with pytest.raises(
                ValueError,
                match="target selector 'omega\\*' matched no callables",
            ):
                scan_module(mod, max_examples=1, targets=["omega*"])
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

    def test_package_root_discovery_skips_nonfatal_lazy_exports(self, tmp_path, monkeypatch):
        package_dir = tmp_path / "lazy_pkg"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text(
            "import pytest\n"
            "\n"
            "def safe(value: int) -> int:\n"
            "    return value + 1\n"
            "\n"
            "def __dir__():\n"
            "    return ['safe', 'toxic']\n"
            "\n"
            "def __getattr__(name: str):\n"
            "    if name == 'toxic':\n"
            "        return pytest.importorskip('definitely_missing_optional_dep')\n"
            "    raise AttributeError(name)\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        import importlib

        mod = importlib.import_module("lazy_pkg")
        funcs = auto_mod._get_public_functions(mod)

        assert [name for name, _func in funcs] == ["safe"]

    def test_callable_seed_files_ignore_virtualenv_trees(self, tmp_path, monkeypatch):
        package_dir = tmp_path / "demo_pkg"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "feature.py").write_text(
            "def ping() -> str:\n    return 'ok'\n",
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_feature.py").write_text("def test_ping(): pass\n", encoding="utf-8")
        toxic = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "pyarrow" / "tests"
        toxic.mkdir(parents=True)
        (toxic / "test_jvm.py").write_text("def test_optional_dep(): pass\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        seed_files = auto_mod._callable_seed_files("demo_pkg.feature")

        assert tests_dir / "test_feature.py" in seed_files
        assert all(".venv" not in str(path) for path in seed_files)

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

    def test_method_scenarios_can_be_applied_as_an_ordered_sequence(self):
        import sys

        module_name = "_test_method_targets_multi_scenario"
        mod = _install_multi_scenario_target_module(module_name)
        key = f"{module_name}:Env"

        def factory() -> object:
            return mod.Env()

        def setup(instance: object) -> None:
            instance.ready = True

        def scenario_one(instance: object) -> object:
            instance.helper = mod.Helper(prefix="first:")
            return instance

        def scenario_two(instance: object) -> object:
            instance.helper.prefix += "second:"
            return instance

        try:
            result = scan_module(
                mod,
                max_examples=20,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: (scenario_one, scenario_two)},
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
                object_scenarios={key: (scenario_one, scenario_two)},
            )
            assert fuzz_result.passed

            test_case = chaos_for(
                mod,
                max_examples=5,
                stateful_step_count=5,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: (scenario_one, scenario_two)},
            )
            test_case("runTest").runTest()
        finally:
            del sys.modules[module_name]

    def test_inline_scenario_specs_apply_to_bound_methods_and_stateful_init(self):
        import sys

        module_name = "_test_inline_method_scenarios"
        mod = _install_inline_scenario_target_module(module_name)
        key = f"{module_name}:Env"

        def factory() -> object:
            return mod.Env()

        def setup(instance: object) -> None:
            instance.ready = True

        scenarios = [
            {"kind": "setattr", "path": "helper", "value": mod.Helper(prefix="scenario:")},
            {"kind": "stub_return", "path": "helper.render", "value": "stubbed-sync"},
            {"kind": "stub_return", "path": "helper.async_render", "value": "stubbed-async"},
        ]

        try:
            result = scan_module(
                mod,
                max_examples=20,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: scenarios},
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
                object_scenarios={key: scenarios},
            )
            assert fuzz_result.passed

            test_case = chaos_for(
                mod,
                max_examples=5,
                stateful_step_count=5,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: scenarios},
                object_harnesses={key: "stateful"},
            )
            test_case("runTest").runTest()
        finally:
            del sys.modules[module_name]

    def test_inline_scenario_stub_raise_is_applied_to_collaborators(self):
        import sys

        module_name = "_test_inline_method_scenarios_raise"
        mod = _install_inline_scenario_target_module(module_name)
        key = f"{module_name}:Env"

        def factory() -> object:
            return mod.Env()

        def setup(instance: object) -> None:
            instance.ready = True

        scenarios = [
            {"kind": "setattr", "path": "helper", "value": mod.Helper(prefix="scenario:")},
            {"kind": "stub_raise", "path": "helper.async_render", "error": "RuntimeError: denied"},
        ]

        try:
            result = fuzz(
                f"{module_name}:Env.async_render",
                max_examples=1,
                object_factories={key: factory},
                object_setups={key: setup},
                object_scenarios={key: scenarios},
            )
            assert not result.passed
            assert result.failures
            assert isinstance(result.failures[0], RuntimeError)
        finally:
            del sys.modules[module_name]

    @pytest.mark.parametrize(
        ("module_factory", "pack_name"),
        [
            (_install_subprocess_pack_target_module, "subprocess"),
            (_install_sandbox_client_pack_target_module, "sandbox_client"),
            (_install_upload_download_pack_target_module, "upload_download"),
            (_install_http_pack_target_module, "http"),
            (_install_state_store_pack_target_module, "state_store"),
        ],
    )
    def test_builtin_scenario_packs_are_resolved_by_name(
        self,
        module_factory,
        pack_name,
    ):
        module_name = f"_test_builtin_scenario_{pack_name}"
        mod = module_factory(module_name)
        try:
            hook = auto_mod._builtin_object_scenario_hook(pack_name)
            assert hook is not None
            instance = hook(mod.Env())

            if pack_name == "subprocess":
                assert asyncio.run(instance.render("echo hi")) == ""
            elif pack_name == "sandbox_client":
                assert asyncio.run(instance.render("echo hi", "/tmp/file", b"payload")) == ""
            elif pack_name == "upload_download":
                assert instance.render(b"payload") == 0
            elif pack_name == "http":
                assert asyncio.run(instance.render("https://example.test")) == 200
            else:
                assert instance.render("prompt", "seed") == "seed"
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

    def test_harness_hints_include_concrete_config_suggestions(self, tmp_path, monkeypatch):
        pkg = tmp_path / "hintpkg"
        tests_dir = tmp_path / "tests"
        docs_dir = tmp_path / "docs"
        pkg.mkdir()
        tests_dir.mkdir()
        docs_dir.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "envs.py").write_text(
            "class Env:\n"
            "    def __init__(self, prefix: str):\n"
            "        self.prefix = prefix\n"
            "        self.sandbox_client = None\n"
            "\n"
            "    def render(self, state: dict[str, str], prompt: str) -> str:\n"
            "        if self.sandbox_client is not None:\n"
            "            self.sandbox_client.execute_command(prompt)\n"
            "        return f'{self.prefix}:{state[\"seed\"]}:{prompt}'\n",
            encoding="utf-8",
        )
        (tests_dir / "support_factories.py").write_text(
            "from hintpkg.envs import Env\n"
            "\n"
            "class FakeSandbox:\n"
            "    def execute_command(self, prompt: str) -> None:\n"
            "        return None\n"
            "\n"
            "def make_env() -> Env:\n"
            "    env = Env('demo')\n"
            "    env.sandbox_client = FakeSandbox()\n"
            "    return env\n"
            "\n"
            "def make_env_state() -> dict[str, str]:\n"
            "    return {'seed': 'cached'}\n"
            "\n"
            "def teardown_env(env: Env) -> None:\n"
            "    env.prefix = 'closed'\n",
            encoding="utf-8",
        )
        (docs_dir / "lifecycle.md").write_text(
            "Cli lifecycle notes for Env.render.\n"
            "This target needs state setup and a teardown hook.\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        hints = auto_mod._mine_object_harness_hints("hintpkg.envs", "Env", "render")

        factory_hint = next(hint for hint in hints if hint.kind == "factory")
        state_hint = next(hint for hint in hints if hint.kind == "state_factory")
        teardown_hint = next(hint for hint in hints if hint.kind == "teardown")
        scenario_hint = next(hint for hint in hints if hint.kind == "scenario_pack")

        assert factory_hint.config["section"] == "[[objects]]"
        assert factory_hint.config["target"] == "hintpkg.envs:Env"
        assert factory_hint.config["key"] == "factory"
        assert factory_hint.config["value"].endswith("make_env")
        assert state_hint.config["key"] == "state_factory"
        assert teardown_hint.config["key"] == "teardown"
        assert scenario_hint.config["key"] == "scenarios"
        assert "sandbox_client" in scenario_hint.config["value"]

    def test_harness_hints_rank_structural_matches_above_generic_fixtures(
        self,
        tmp_path,
        monkeypatch,
    ):
        pkg = tmp_path / "hintpkg"
        tests_dir = tmp_path / "tests"
        pkg.mkdir()
        tests_dir.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "envs.py").write_text(
            "class Env:\n"
            "    def __init__(self, prefix: str):\n"
            "        self.prefix = prefix\n"
            "\n"
            "    def render(self, prompt: str) -> str:\n"
            "        return f'{self.prefix}:{prompt}'\n",
            encoding="utf-8",
        )
        (tests_dir / "support_factories.py").write_text(
            "from hintpkg.envs import Env\n"
            "\n"
            "def make_env() -> Env:\n"
            "    return Env('factory')\n",
            encoding="utf-8",
        )
        (tests_dir / "conftest.py").write_text(
            "import pytest\n"
            "from hintpkg.envs import Env\n"
            "\n"
            "@pytest.fixture\n"
            "def env() -> Env:\n"
            "    return Env('fixture')\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        auto_mod._mine_object_harness_hints.cache_clear()
        hints = auto_mod._mine_object_harness_hints("hintpkg.envs", "Env", "render")
        factory_hints = [hint for hint in hints if hint.kind == "factory"]

        assert len(factory_hints) >= 2
        assert factory_hints[0].score > factory_hints[1].score
        assert factory_hints[0].config["value"].endswith("make_env")
        assert "constructor_like" in factory_hints[0].signals
        assert factory_hints[1].config["value"].endswith("env")
        assert "pytest_fixture" in factory_hints[1].signals

    def test_method_seed_examples_capture_fixture_backed_bound_calls(self, tmp_path, monkeypatch):
        pkg = tmp_path / "hintpkg"
        tests_dir = tmp_path / "tests"
        pkg.mkdir()
        tests_dir.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "envs.py").write_text(
            "class Env:\n"
            "    def __init__(self, prefix: str):\n"
            "        self.prefix = prefix\n"
            "\n"
            "    def render(self, state: dict[str, str], prompt: str) -> str:\n"
            "        return f'{self.prefix}:{state[\"seed\"]}:{prompt}'\n",
            encoding="utf-8",
        )
        (tests_dir / "conftest.py").write_text(
            "import pytest\n"
            "from hintpkg.envs import Env\n"
            "\n"
            "@pytest.fixture\n"
            "def env() -> Env:\n"
            "    return Env('demo')\n"
            "\n"
            "@pytest.fixture\n"
            "def cached_state() -> dict[str, str]:\n"
            "    return {'seed': 'cached'}\n",
            encoding="utf-8",
        )
        test_file = tests_dir / "test_envs.py"
        test_file.write_text(
            "def test_render(env, cached_state):\n"
            "    prompt = 'hello'\n"
            "    assert env.render(cached_state, prompt) == 'demo:cached:hello'\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        import importlib

        auto_mod._mine_object_harness_hints.cache_clear()
        module = importlib.import_module("hintpkg.envs")
        funcs = dict(
            _get_public_functions(
                module,
                object_factories={"hintpkg.envs:Env": lambda: module.Env("demo")},
            )
        )
        examples = auto_mod._call_seed_examples_from_files(
            funcs["Env.render"],
            [test_file],
            source="test",
        )

        assert any(example.kwargs == {"prompt": "hello"} for example in examples)

    def test_harness_hints_detect_yield_fixtures_and_file_symbol_paths(
        self,
        tmp_path,
        monkeypatch,
    ):
        pkg = tmp_path / "hintpkg"
        tests_dir = tmp_path / "tests"
        pkg.mkdir()
        tests_dir.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "envs.py").write_text(
            "class Env:\n"
            "    def __init__(self, prefix: str):\n"
            "        self.prefix = prefix\n"
            "\n"
            "    def close(self) -> None:\n"
            "        self.prefix = 'closed'\n"
            "\n"
            "    def render(self, state: dict[str, str], prompt: str) -> str:\n"
            "        return f'{self.prefix}:{state[\"seed\"]}:{prompt}'\n",
            encoding="utf-8",
        )
        (tests_dir / "conftest.py").write_text(
            "import pytest\n"
            "from hintpkg.envs import Env\n"
            "\n"
            "@pytest.fixture\n"
            "def env() -> Env:\n"
            "    instance = Env('demo')\n"
            "    yield instance\n"
            "    instance.close()\n"
            "\n"
            "@pytest.fixture\n"
            "def cached_state() -> dict[str, str]:\n"
            "    return {'seed': 'cached'}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        auto_mod._mine_object_harness_hints.cache_clear()
        hints = auto_mod._mine_object_harness_hints("hintpkg.envs", "Env", "render")

        factory_hint = next(hint for hint in hints if hint.kind == "factory")
        state_hint = next(hint for hint in hints if hint.kind == "state_factory")
        teardown_hint = next(hint for hint in hints if hint.kind == "teardown")

        assert factory_hint.config["value"].endswith("tests/conftest.py:env")
        assert state_hint.config["value"].endswith("tests/conftest.py:cached_state")
        assert teardown_hint.config["value"].endswith("tests/conftest.py:env")

    def test_auto_mined_harness_makes_instance_method_runnable(self, tmp_path, monkeypatch):
        pkg = tmp_path / "autoharnesspkg"
        tests_dir = tmp_path / "tests"
        pkg.mkdir()
        tests_dir.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "envs.py").write_text(
            "class Env:\n"
            "    def __init__(self, prefix: str):\n"
            "        self.prefix = prefix\n"
            "\n"
            "    def render(self, state: dict[str, str], prompt: str) -> str:\n"
            "        return f'{self.prefix}:{state[\"seed\"]}:{prompt}'\n",
            encoding="utf-8",
        )
        (tests_dir / "support_factories.py").write_text(
            "from autoharnesspkg.envs import Env\n"
            "\n"
            "def make_env() -> Env:\n"
            "    return Env('demo')\n"
            "\n"
            "def make_env_state(instance: Env) -> dict[str, str]:\n"
            "    return {'seed': instance.prefix}\n",
            encoding="utf-8",
        )
        (tests_dir / "test_envs.py").write_text(
            "from autoharnesspkg.envs import Env\n"
            "\n"
            "def test_render_examples() -> None:\n"
            "    env = Env('demo')\n"
            "    assert env.render({'seed': 'demo'}, 'hello') == 'demo:demo:hello'\n",
            encoding="utf-8",
        )
        auto_mod._mine_object_harness_hints.cache_clear()
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        module = __import__("autoharnesspkg.envs", fromlist=["Env"])
        callables = dict(_get_public_functions(module))
        render = callables["Env.render"]

        assert getattr(render, "__ordeal_factory_source__", None) == "mined"
        assert getattr(render, "__ordeal_state_factory_source__", None) == "mined"
        assert getattr(render, "__ordeal_auto_harness__", False) is True

        result = scan_module("autoharnesspkg.envs", targets=["Env.render"], mode="real_bug")

        assert result.skipped == []
        assert [item.name for item in result.functions] == ["Env.render"]
        assert result.functions[0].verdict == "clean"

    def test_builtin_object_scenario_library_aliases_resolve(self):
        assert auto_mod._builtin_object_scenario_hook("subprocess_runner") is not None
        assert auto_mod._builtin_object_scenario_hook("upload_download_client") is not None
        assert auto_mod._builtin_object_scenario_hook("http_client") is not None

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
            assert command.passed is False
            assert command.verdict == "semantic_contract"
            assert command.contract_violations
            assert command.contract_violation_details[0]["category"] == "semantic_contract"
            proof = command.contract_violation_details[0]["proof_bundle"]
            assert proof["version"] == 2
            assert proof["verdict"]["promoted"] is True
            assert proof["minimal_reproduction"]["command"] == (
                "uv run ordeal check _test_contracts:build_command"
                " --contract shell-safe path quoting"
            )
            assert result.failed == 1
        finally:
            del sys.modules[mod.__name__]

    @pytest.mark.parametrize(
        ("module_name", "target", "pack_name", "expected_contracts", "needs_factory"),
        [
            (
                "_test_contract_pack_shell_path",
                "build_command",
                "shell_path_safety",
                {"shell_safe", "quoted_paths", "command_arg_stability", "subprocess_argv"},
                False,
            ),
            (
                "_test_contract_pack_env",
                "update_env",
                "protected_env_vars",
                {"protected_env_keys"},
                False,
            ),
            (
                "_test_contract_pack_cleanup",
                "Env.cleanup",
                "cleanup_teardown",
                {"lifecycle_attempts_all"},
                True,
            ),
            (
                "_test_contract_pack_cancel",
                "Env.rollout",
                "cancellation_safety",
                {"cleanup_after_cancellation", "rollout_cancellation_triggers_cleanup"},
                True,
            ),
            (
                "_test_contract_pack_json",
                "normalize",
                "json_tool_call_normalization",
                {"json_roundtrip", "http_shape"},
                False,
            ),
        ],
    )
    def test_builtin_contract_packs_are_resolved_by_name(
        self,
        module_name,
        target,
        pack_name,
        expected_contracts,
        needs_factory,
    ):
        mod = types.ModuleType(module_name)
        sys.modules[module_name] = mod
        if pack_name == "shell_path_safety":
            exec(
                "def build_command(path: str) -> str:\n    return f'cp {path} /tmp'\n",
                mod.__dict__,
            )
            contract_kwargs = {"path": "demo files/input.txt"}
        elif pack_name == "protected_env_vars":
            exec(
                "def update_env(env_vars: dict[str, str]) -> dict[str, str]:\n"
                "    updated = dict(env_vars)\n"
                "    updated.pop('PATH', None)\n"
                "    return updated\n",
                mod.__dict__,
            )
            contract_kwargs = {"env_vars": {"PATH": "/bin", "HOME": "/home/test", "PWD": "/tmp"}}
        elif pack_name == "cleanup_teardown":
            exec(
                "class Env:\n"
                "    def __init__(self) -> None:\n"
                "        self.events = []\n"
                "\n"
                "    def cleanup(self, marker: str) -> list[str]:\n"
                "        try:\n"
                "            self.cleanup_first(marker)\n"
                "        except Exception:\n"
                "            return list(self.events)\n"
                "        self.cleanup_second(marker)\n"
                "        return list(self.events)\n"
                "\n"
                "    def teardown(self, marker: str) -> list[str]:\n"
                "        self.events.append(f'teardown:{marker}')\n"
                "        return list(self.events)\n"
                "\n"
                "    def cleanup_first(self, marker: str) -> None:\n"
                "        self.events.append(f'first:{marker}')\n"
                "\n"
                "    def cleanup_second(self, marker: str) -> None:\n"
                "        self.events.append(f'second:{marker}')\n"
                "\n"
                "Env.cleanup_first.cleanup = True\n"
                "Env.cleanup_second.cleanup = True\n",
                mod.__dict__,
            )
            contract_kwargs = {"marker": "demo"}
        elif pack_name == "cancellation_safety":
            exec(
                "class Env:\n"
                "    def __init__(self) -> None:\n"
                "        self.events = []\n"
                "\n"
                "    def rollout(self, marker: str) -> list[str]:\n"
                "        try:\n"
                "            self.rollout_step(marker)\n"
                "        finally:\n"
                "            self.cleanup(marker)\n"
                "        return list(self.events)\n"
                "\n"
                "    def rollout_step(self, marker: str) -> None:\n"
                "        self.events.append(f'rollout:{marker}')\n"
                "\n"
                "    def cleanup(self, marker: str) -> None:\n"
                "        self.events.append(f'cleanup:{marker}')\n"
                "\n"
                "    def teardown(self, marker: str) -> None:\n"
                "        self.events.append(f'teardown:{marker}')\n",
                mod.__dict__,
            )
            contract_kwargs = {"marker": "demo"}
        else:
            exec(
                "def normalize(payload: dict[str, object]) -> dict[str, object]:\n"
                "    return payload\n",
                mod.__dict__,
            )
            contract_kwargs = {
                "payload": {
                    "tool_call": {
                        "args": {"alpha", "beta"},
                    }
                }
            }
        try:
            resolved = auto_mod._resolve_contract_check_entries(
                [{"pack": pack_name, "kwargs": contract_kwargs}],
                probe_kwargs=contract_kwargs,
            )
            assert {check.name for check in resolved} & expected_contracts
            if pack_name == "shell_path_safety":
                assert not resolved[0].predicate("cp demo files/input.txt /tmp")
            elif pack_name == "protected_env_vars":
                assert not resolved[0].predicate({"HOME": "/home/test", "PWD": "/tmp"})
            elif pack_name == "json_tool_call_normalization":
                assert not resolved[0].predicate({"tool_call": {"args": {"alpha", "beta"}}})
        finally:
            del sys.modules[module_name]

    def test_package_root_discovery_includes_lazy_exported_callables(self):
        discovered = {name for name, _ in _get_public_functions(ordeal)}

        assert "scan_module" in discovered
        assert "fuzz" in discovered
        assert "mutate" in discovered
        assert "auto_configure" in discovered

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
        assert divide_result.proof_bundle is not None
        assert divide_result.proof_bundle["version"] == 2
        assert divide_result.proof_bundle["verdict"]["promoted"] is True
        assert divide_result.proof_bundle["contract_basis"]["category"] == "likely_bug"
        assert divide_result.proof_bundle["confidence_breakdown"]["replayability"] == 1.0
        assert divide_result.proof_bundle["minimal_reproduction"]["direct_call_supported"] is True
        assert (
            "import_module('tests._auto_target')"
            in (divide_result.proof_bundle["minimal_reproduction"]["python_snippet"])
        )
        assert divide_result.proof_bundle["valid_input_witness"]["source"] == "boundary"
        assert divide_result.proof_bundle["reproduction"]["replay_matches"] == 2
        json.dumps(divide_result.proof_bundle)

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

        assert result.passed
        assert result.execution_ok is False
        assert result.verdict == "exploratory_crash"
        assert result.crash_category == "speculative_crash"
        assert result.replayable is False
        assert "WARN  flaky" in str(result)

    def test_real_bug_mode_demotes_invalid_input_crashes(self):
        def only_text(value: object) -> int:
            return len(value.strip())  # type: ignore[union-attr]

        result = _test_one_function(
            "only_text",
            only_text,
            {"value": st.just(None)},
            None,
            max_examples=1,
            check_return_type=False,
            mode="real_bug",
        )

        assert result.passed is True
        assert result.execution_ok is False
        assert result.verdict == "invalid_input_crash"
        assert result.crash_category == "invalid_input_crash"
        assert result.proof_bundle is not None
        assert result.proof_bundle["contract_validity"]["category"] == "invalid_input_crash"
        assert result.proof_bundle["verdict"]["demotion_reason"] is not None

    def test_real_bug_mode_skips_property_mining_for_passing_functions(self, monkeypatch):
        import ordeal.mine as mine_mod

        def fail_mine(*args, **kwargs):
            raise AssertionError("real_bug mode should not mine passing functions")

        monkeypatch.setattr(mine_mod, "mine", fail_mine)

        result = scan_module("tests._auto_target", max_examples=1, mode="real_bug")

        add_result = next(f for f in result.functions if f.name == "add")
        assert add_result.passed is True
        assert add_result.property_violations == []

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

    def test_state_factory_unlocks_stateful_method_targets(self):
        import sys

        module_name = "_test_stateful_method_targets"
        mod = _install_stateful_target_module(module_name)
        key = f"{module_name}:Env"
        teardown_calls: list[list[str]] = []

        def factory():
            return mod.Env()

        def state_factory(instance):
            return {"prior_calls": str(len(instance.seen))}

        def teardown(instance):
            teardown_calls.append(list(instance.seen))

        try:
            result = scan_module(
                mod,
                targets=[f"{module_name}:Env.rollout"],
                object_factories={key: factory},
                object_state_factories={key: state_factory},
                max_examples=5,
            )
            assert "Env.rollout" in [item.name for item in result.functions]
            assert "Env.rollout" not in [name for name, _reason in result.skipped]

            TestCase = chaos_for(
                mod,
                object_factories={key: factory},
                object_state_factories={key: state_factory},
                object_teardowns={key: teardown},
                object_harnesses={key: "stateful"},
                faults=[],
                invariants=[],
                max_examples=3,
                stateful_step_count=3,
            )
            test = TestCase("runTest")
            test.runTest()

            assert teardown_calls
        finally:
            del sys.modules[module_name]

    def test_lifecycle_attempts_all_contract_detects_short_circuit_cleanup(self):
        import sys

        module_name = "_test_lifecycle_contract_cleanup"
        mod = types.ModuleType(module_name)
        sys.modules[module_name] = mod
        exec(
            "class Env:\n"
            "    def __init__(self) -> None:\n"
            "        self.events = []\n"
            "\n"
            "    def cleanup(self, marker: str) -> list[str]:\n"
            "        try:\n"
            "            self.cleanup_first(marker)\n"
            "        except Exception:\n"
            "            return list(self.events)\n"
            "        self.cleanup_second(marker)\n"
            "        return list(self.events)\n"
            "\n"
            "    def cleanup_first(self, marker: str) -> None:\n"
            "        self.events.append(f'first:{marker}')\n"
            "\n"
            "    def cleanup_second(self, marker: str) -> None:\n"
            "        self.events.append(f'second:{marker}')\n"
            "\n"
            "Env.cleanup_first.cleanup = True\n"
            "Env.cleanup_second.cleanup = True\n",
            mod.__dict__,
        )
        key = f"{module_name}:Env"

        try:
            result = scan_module(
                mod,
                targets=[f"{module_name}:Env.cleanup"],
                object_factories={key: mod.Env},
                contract_checks={
                    "Env.cleanup": [
                        auto_mod.builtin_contract_check(
                            "lifecycle_attempts_all",
                            kwargs={"marker": "demo"},
                            phase="cleanup",
                            fault="raise_cleanup_handler",
                        )
                    ]
                },
                max_examples=5,
            )

            function = next(item for item in result.functions if item.name == "Env.cleanup")
            assert function.passed is False
            assert function.verdict == "lifecycle_contract"
            assert function.contract_violations
            detail = function.contract_violation_details[0]
            assert detail["category"] == "lifecycle_contract"
            assert detail["lifecycle_probe"]["phase"] == "cleanup"
            assert detail["lifecycle_probe"]["attempts"] == ["cleanup_first"]
        finally:
            del sys.modules[module_name]

    def test_lifecycle_followup_contract_accepts_teardown_after_rollout_cancellation(self):
        import sys

        module_name = "_test_lifecycle_contract_followup"
        mod = types.ModuleType(module_name)
        sys.modules[module_name] = mod
        exec(
            "class Env:\n"
            "    def __init__(self) -> None:\n"
            "        self.events = []\n"
            "\n"
            "    def rollout(self, marker: str) -> list[str]:\n"
            "        try:\n"
            "            self.rollout_step(marker)\n"
            "        finally:\n"
            "            self.teardown_alpha()\n"
            "            self.teardown_beta()\n"
            "        return list(self.events)\n"
            "\n"
            "    def rollout_step(self, marker: str) -> None:\n"
            "        self.events.append(f'rollout:{marker}')\n"
            "\n"
            "    def teardown_alpha(self) -> None:\n"
            "        self.events.append('alpha')\n"
            "\n"
            "    def teardown_beta(self) -> None:\n"
            "        self.events.append('beta')\n"
            "\n"
            "Env.teardown_alpha.teardown = True\n"
            "Env.teardown_beta.teardown = True\n",
            mod.__dict__,
        )
        key = f"{module_name}:Env"

        try:
            result = scan_module(
                mod,
                targets=[f"{module_name}:Env.rollout"],
                object_factories={key: mod.Env},
                contract_checks={
                    "Env.rollout": [
                        auto_mod.builtin_contract_check(
                            "lifecycle_followup",
                            kwargs={"marker": "demo"},
                            phase="rollout",
                            followup_phases=["teardown"],
                            fault="cancel_rollout",
                        )
                    ]
                },
                max_examples=5,
            )

            function = next(item for item in result.functions if item.name == "Env.rollout")
            assert function.passed
            assert function.contract_violations == []
        finally:
            del sys.modules[module_name]

    def test_async_setup_failure_contract_still_runs_async_teardown(self):
        import sys

        module_name = "_test_async_lifecycle_setup_failure"
        mod = types.ModuleType(module_name)
        sys.modules[module_name] = mod
        exec(
            "class Env:\n"
            "    def __init__(self) -> None:\n"
            "        self.events = []\n"
            "\n"
            "    async def rollout(self, marker: str) -> str:\n"
            "        self.events.append(f'rollout:{marker}')\n"
            "        return marker\n",
            mod.__dict__,
        )

        async def setup_env(instance):
            instance.events.append("setup")
            return instance

        async def teardown_env(instance):
            instance.events.append("teardown")
            return None

        key = f"{module_name}:Env"

        try:
            result = scan_module(
                mod,
                targets=[f"{module_name}:Env.rollout"],
                object_factories={key: mod.Env},
                object_setups={key: setup_env},
                object_teardowns={key: teardown_env},
                contract_checks={
                    "Env.rollout": [
                        auto_mod.builtin_contract_check(
                            "setup_failure_triggers_teardown",
                            kwargs={"marker": "demo"},
                        )
                    ]
                },
                max_examples=5,
            )

            function = next(item for item in result.functions if item.name == "Env.rollout")
            assert function.passed
            assert function.contract_violations == []
        finally:
            del sys.modules[module_name]

    def test_async_rollout_cancellation_contract_tracks_async_followups(self):
        import sys

        module_name = "_test_async_lifecycle_rollout_cancel"
        mod = types.ModuleType(module_name)
        sys.modules[module_name] = mod
        exec(
            "class Env:\n"
            "    def __init__(self) -> None:\n"
            "        self.events = []\n"
            "\n"
            "    async def rollout(self, marker: str) -> list[str]:\n"
            "        try:\n"
            "            await self.rollout_step(marker)\n"
            "        finally:\n"
            "            await self.teardown_alpha()\n"
            "            await self.teardown_beta()\n"
            "        return list(self.events)\n"
            "\n"
            "    async def rollout_step(self, marker: str) -> None:\n"
            "        self.events.append(f'rollout:{marker}')\n"
            "\n"
            "    async def teardown_alpha(self) -> None:\n"
            "        self.events.append('alpha')\n"
            "\n"
            "    async def teardown_beta(self) -> None:\n"
            "        self.events.append('beta')\n"
            "\n"
            "Env.teardown_alpha.teardown = True\n"
            "Env.teardown_beta.teardown = True\n",
            mod.__dict__,
        )
        key = f"{module_name}:Env"

        try:
            result = scan_module(
                mod,
                targets=[f"{module_name}:Env.rollout"],
                object_factories={key: mod.Env},
                contract_checks={
                    "Env.rollout": [
                        auto_mod.builtin_contract_check(
                            "cleanup_after_cancellation",
                            kwargs={"marker": "demo"},
                            followup_phases=["teardown"],
                        )
                    ]
                },
                max_examples=5,
            )

            function = next(item for item in result.functions if item.name == "Env.rollout")
            assert function.passed
            assert function.contract_violations == []
        finally:
            del sys.modules[module_name]

    def test_lifecycle_phase_honors_setup_and_rollout_decorator_markers(self):
        def setup_hook() -> None:
            return None

        def rollout_hook() -> None:
            return None

        setup_hook.setup = True  # type: ignore[attr-defined]
        rollout_hook.rollout = True  # type: ignore[attr-defined]

        assert auto_mod._lifecycle_phase("prepare_env", setup_hook) == "setup"
        assert auto_mod._lifecycle_phase("execute", rollout_hook) == "rollout"

    def test_discover_lifecycle_handlers_includes_inherited_methods(self):
        class BaseEnv:
            def cleanup_alpha(self) -> None:
                return None

        class ChildEnv(BaseEnv):
            def cleanup_beta(self) -> None:
                return None

        BaseEnv.cleanup_alpha.cleanup = True  # type: ignore[attr-defined]
        ChildEnv.cleanup_beta.cleanup = True  # type: ignore[attr-defined]

        handlers = auto_mod._discover_lifecycle_handlers(ChildEnv, "cleanup")

        assert handlers == ["cleanup_alpha", "cleanup_beta"]


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

    def test_literal_param_crash_is_ranked_without_type_error(self):
        import sys
        import types

        mod = types.ModuleType("_test_literal_scan_crash")
        exec(
            "from typing import Literal\n"
            'def choose(opt: Literal["a", "b"]) -> str:\n'
            "    raise RuntimeError('boom')\n",
            mod.__dict__,
        )
        sys.modules["_test_literal_scan_crash"] = mod
        try:
            result = scan_module("_test_literal_scan_crash", max_examples=1, mode="real_bug")
            choose = next(f for f in result.functions if f.name == "choose")
            assert choose.passed is False
            assert choose.verdict == "promoted_real_bug"
            assert choose.error_type == "RuntimeError"
            assert choose.proof_bundle is not None
        finally:
            del sys.modules["_test_literal_scan_crash"]
