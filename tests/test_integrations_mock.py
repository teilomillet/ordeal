"""Tests for ordeal.integrations — mock-based tests for atheris and schemathesis."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from ordeal.faults import LambdaFault

# ============================================================================
# Mock atheris module
# ============================================================================


def _make_mock_atheris():
    mock = ModuleType("atheris")

    class FuzzedDataProvider:
        def __init__(self, data: bytes):
            self._data = data
            self._pos = 0

        def ConsumeFloat(self) -> float:
            return 0.5

        def ConsumeBool(self) -> bool:
            return True

        def ConsumeIntInRange(self, lo: int, hi: int) -> int:
            return lo

        def remaining_bytes(self) -> int:
            return max(0, len(self._data) - self._pos)

    mock.FuzzedDataProvider = FuzzedDataProvider
    mock.Setup = MagicMock()
    mock.Fuzz = MagicMock()
    return mock


# ============================================================================
# _AtherisBuggifyRNG
# ============================================================================


class TestAtherisBuggifyRNG:
    def test_random_delegates_to_fdp(self):
        mock_atheris = _make_mock_atheris()
        with patch.dict(sys.modules, {"atheris": mock_atheris}):
            from ordeal.integrations.atheris_engine import _AtherisBuggifyRNG

            fdp = MagicMock()
            fdp.ConsumeFloat.return_value = 0.42
            rng = _AtherisBuggifyRNG(fdp)
            assert rng.random() == 0.42
            fdp.ConsumeFloat.assert_called_once()


# ============================================================================
# fuzz()
# ============================================================================


class TestAtherisFuzz:
    def test_raises_without_atheris(self):
        with patch.dict(sys.modules, {"atheris": None}):
            if "ordeal.integrations.atheris_engine" in sys.modules:
                del sys.modules["ordeal.integrations.atheris_engine"]
            from ordeal.integrations.atheris_engine import fuzz

            with pytest.raises(ImportError, match="atheris"):
                fuzz(lambda: None, max_time=1)

    def test_calls_atheris_setup_and_fuzz(self):
        mock_atheris = _make_mock_atheris()
        with patch.dict(sys.modules, {"atheris": mock_atheris}):
            import ordeal.integrations.atheris_engine as ae

            ae.fuzz(lambda: None, max_time=5, buggify_probability=0.3)
            mock_atheris.Setup.assert_called_once()
            mock_atheris.Fuzz.assert_called_once()

    def test_custom_sys_argv(self):
        mock_atheris = _make_mock_atheris()
        with patch.dict(sys.modules, {"atheris": mock_atheris}):
            import ordeal.integrations.atheris_engine as ae

            ae.fuzz(lambda: None, sys_argv=["test", "-max_total_time=1"])
            assert mock_atheris.Setup.call_args[0][0] == ["test", "-max_total_time=1"]

    def test_test_one_input_registered(self):
        """Verify that fuzz() registers a test_one_input callback with atheris."""
        mock_atheris = _make_mock_atheris()
        with patch.dict(sys.modules, {"atheris": mock_atheris}):
            import ordeal.integrations.atheris_engine as ae

            ae.fuzz(lambda: None, max_time=1)
            # The second arg to Setup is the test_one_input callback
            test_fn = mock_atheris.Setup.call_args[0][1]
            assert callable(test_fn)


# ============================================================================
# fuzz_chaos_test()
# ============================================================================


class TestAtherisFuzzChaosTest:
    def test_raises_without_atheris(self):
        with patch.dict(sys.modules, {"atheris": None}):
            if "ordeal.integrations.atheris_engine" in sys.modules:
                del sys.modules["ordeal.integrations.atheris_engine"]
            from ordeal.integrations.atheris_engine import fuzz_chaos_test

            with pytest.raises(ImportError, match="atheris"):
                fuzz_chaos_test(MagicMock, max_time=1)

    def test_calls_atheris_setup_and_fuzz(self):
        mock_atheris = _make_mock_atheris()
        with patch.dict(sys.modules, {"atheris": mock_atheris}):
            from hypothesis.stateful import rule

            import ordeal.integrations.atheris_engine as ae
            from ordeal.chaos import ChaosTest

            class Simple(ChaosTest):
                faults = [LambdaFault("f", lambda: None, lambda: None)]

                @rule()
                def tick(self):
                    pass

            ae.fuzz_chaos_test(Simple, max_time=5)
            mock_atheris.Setup.assert_called_once()
            mock_atheris.Fuzz.assert_called_once()

    def test_test_one_input_exercises_machine(self):
        mock_atheris = _make_mock_atheris()
        with patch.dict(sys.modules, {"atheris": mock_atheris}):
            from hypothesis.stateful import rule

            import ordeal.integrations.atheris_engine as ae
            from ordeal.chaos import ChaosTest

            class Logged(ChaosTest):
                faults = [LambdaFault("f", lambda: None, lambda: None)]

                @rule()
                def tick(self):
                    pass

            ae.fuzz_chaos_test(Logged, max_time=1, max_steps=3)
            test_fn = mock_atheris.Setup.call_args[0][1]
            test_fn(b"\x00" * 100)  # should not raise


# ============================================================================
# Built-in engine has no external dependencies
# ============================================================================


class TestBuiltinEngineNoDeps:
    def test_openapi_import_works(self):
        """The built-in engine requires no optional deps."""
        from ordeal.integrations.openapi import chaos_api_test  # noqa: F811

        assert callable(chaos_api_test)

    def test_chaos_api_hook_raises_not_implemented(self):
        """ChaosAPIHook is removed — instantiation raises NotImplementedError."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from ordeal.integrations.schemathesis_ext import ChaosAPIHook

        with pytest.raises(NotImplementedError, match="removed"):
            ChaosAPIHook(faults=[])


# ============================================================================
# with_chaos — edge cases
# ============================================================================


class TestWithChaosEdgeCases:
    def test_return_value_preserved(self):
        from ordeal.integrations.openapi import with_chaos

        faults = [LambdaFault("f", lambda: None, lambda: None)]

        @with_chaos(faults, seed=42)
        def fn():
            return 42

        assert fn() == 42

    def test_passes_args(self):
        from ordeal.integrations.openapi import with_chaos

        faults = [LambdaFault("f", lambda: None, lambda: None)]

        @with_chaos(faults, seed=42)
        def fn(x, y=10):
            return x + y

        assert fn(5, y=20) == 25

    def test_empty_faults(self):
        from ordeal.integrations.openapi import with_chaos

        @with_chaos([], seed=42)
        def fn():
            return "ok"

        assert fn() == "ok"
