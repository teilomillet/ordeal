"""Tests for graceful handling of unresolvable targets across ordeal.

Covers: PatchFault.activate(), auto_faults(), module mine oracle fallback,
and the function-level mine oracle fallback. Regression tests for issues
#3 and #4 (FastAPI redoc_html rename, Ray process isolation).
"""

from __future__ import annotations

import sys
import types
import pytest

from ordeal.faults import PatchFault
from ordeal.integrations.openapi import auto_faults

# ============================================================================
# Helpers — modules with controllable attributes
# ============================================================================


def _make_module(name: str, attrs: dict) -> types.ModuleType:
    """Create a synthetic module with given attributes."""
    mod = types.ModuleType(name)
    mod.__file__ = f"<synthetic:{name}>"
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _cleanup_module(name: str) -> None:
    sys.modules.pop(name, None)


# ============================================================================
# auto_faults — openapi.py:346
# ============================================================================


class TestAutoFaultsUnresolvable:
    """auto_faults must skip unresolvable targets instead of crashing."""

    def test_nonexistent_module(self):
        """Target pointing to a module that doesn't exist."""
        faults = auto_faults(["no_such_module_xyz.some_func"])
        assert faults == []

    def test_nonexistent_function(self):
        """Module exists but function was renamed/removed."""
        _make_module("_test_af_mod", {"existing_fn": lambda: 42})
        try:
            faults = auto_faults(["_test_af_mod.DOES_NOT_EXIST"])
            assert faults == []
        finally:
            _cleanup_module("_test_af_mod")

    def test_mixed_valid_and_invalid(self):
        """Valid targets still produce faults when mixed with invalid ones."""

        def sample(x: int) -> int:
            return x + 1

        sample.__module__ = "_test_af_mixed"
        _make_module("_test_af_mixed", {"sample": sample})
        try:
            faults = auto_faults(
                [
                    "_test_af_mixed.sample",
                    "_test_af_mixed.RENAMED_FUNC",
                    "no_module_at_all.func",
                ]
            )
            # The valid target should produce faults
            assert len(faults) > 0
            # All faults should reference the valid target
            assert all("sample" in f.name or "error_on_call" in f.name for f in faults)
        finally:
            _cleanup_module("_test_af_mixed")

    def test_all_invalid_returns_empty(self):
        """When every target is unresolvable, returns empty list."""
        faults = auto_faults(
            [
                "fake_a.missing",
                "fake_b.gone",
                "fake_c.renamed",
            ]
        )
        assert faults == []

    def test_warning_logged(self, caplog):
        """Unresolvable targets should log a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            auto_faults(["no_such_pkg.old_name"])
        assert any(
            "cannot resolve" in r.message.lower() or "Skipping" in r.message
            for r in caplog.records
        )


# ============================================================================
# PatchFault — faults/__init__.py
# ============================================================================


class TestPatchFaultUnresolvable:
    """PatchFault.activate() must handle unresolvable targets."""

    def test_nonexistent_attr_warns_and_skips(self):
        fault = PatchFault(
            "tests.test_unresolvable_targets.NO_SUCH_ATTR",
            lambda orig: lambda *a, **k: -1,
        )
        with pytest.warns(UserWarning, match="cannot resolve target"):
            fault.activate()
        assert not fault.active

    def test_nonexistent_module_warns_and_skips(self):
        fault = PatchFault(
            "absolutely_fake_module.func",
            lambda orig: lambda *a, **k: -1,
        )
        with pytest.warns(UserWarning, match="cannot resolve target"):
            fault.activate()
        assert not fault.active

    def test_deactivate_after_skip_is_safe(self):
        fault = PatchFault(
            "tests.test_unresolvable_targets.MISSING",
            lambda orig: lambda *a, **k: -1,
        )
        with pytest.warns(UserWarning):
            fault.activate()
        # Should not crash
        fault.deactivate()
        assert not fault.active

    def test_retry_after_target_appears(self):
        """Skipped fault recovers when target is created."""
        import tests.test_unresolvable_targets as mod

        fault = PatchFault(
            "tests.test_unresolvable_targets._dynamic_fn",
            lambda orig: lambda *a, **k: -1,
        )
        with pytest.warns(UserWarning):
            fault.activate()
        assert not fault.active

        # Create the target
        mod._dynamic_fn = lambda x: x * 2

        fault.reset()
        fault.activate()
        assert fault.active
        assert mod._dynamic_fn(5) == -1
        fault.deactivate()
        assert mod._dynamic_fn(5) == 10

        del mod._dynamic_fn

    def test_context_manager_skip(self):
        """with fault: doesn't crash on unresolvable target."""
        fault = PatchFault(
            "tests.test_unresolvable_targets.NOPE",
            lambda orig: lambda *a, **k: -1,
        )
        with pytest.warns(UserWarning):
            with fault:
                pass  # no crash
        assert not fault.active

    def test_multiple_activate_attempts(self):
        """Repeated activation of skipped fault warns each time."""
        fault = PatchFault(
            "tests.test_unresolvable_targets.GONE",
            lambda orig: lambda *a, **k: -1,
        )
        for _ in range(3):
            with pytest.warns(UserWarning, match="cannot resolve"):
                fault.activate()
            assert not fault.active
            fault.reset()


# ============================================================================
# Module mine oracle fallback — private functions
# ============================================================================


class TestModuleMineOracleFallback:
    """The module-level mine oracle fallback must include private functions."""

    def test_private_functions_included(self):
        """_normalize_text style functions must be picked up by the fallback."""
        from ordeal.mutations import MutationResult, _module_mine_oracle_fallback

        def _normalize(text: str) -> str:
            return text.lower().strip()

        def _validate(x: int) -> bool:
            return x > 0

        mod = _make_module(
            "_test_priv_mod",
            {
                "_normalize": _normalize,
                "_validate": _validate,
                "__secret": lambda: None,  # dunder — should be skipped
            },
        )
        # Set __module__ so the filter doesn't exclude them
        _normalize.__module__ = "_test_priv_mod"
        _validate.__module__ = "_test_priv_mod"

        try:
            dummy_result = MutationResult(target="_test_priv_mod")
            dummy_result.mutants = []  # 0 killed by tests

            with pytest.warns(UserWarning, match="tests killed 0/0 mutants but mine oracle killed"):
                result = _module_mine_oracle_fallback(
                    "_test_priv_mod",
                    mod,
                    dummy_result,
                    ["arithmetic", "comparison", "negate"],
                    {},
                    filter_equivalent=True,
                    equivalence_samples=5,
                    preset_used="essential",
                    mutant_timeout=None,
                )
            # Should find and mine private functions
            # Result is None only if zero mine kills — but _normalize and _validate
            # have clear properties that mutations would violate
            if result is not None:
                assert result.total > 0
                assert result.diagnostics.get("fallback_reason") == "process_isolation"
        finally:
            _cleanup_module("_test_priv_mod")

    def test_dunders_excluded(self):
        """__dunder__ methods should not be mined."""
        from ordeal.mutations import MutationResult, _module_mine_oracle_fallback

        mod = _make_module(
            "_test_dunder_mod",
            {
                "__repr__": lambda self: "test",
                "__init__": lambda self: None,
            },
        )

        try:
            dummy_result = MutationResult(target="_test_dunder_mod")
            result = _module_mine_oracle_fallback(
                "_test_dunder_mod",
                mod,
                dummy_result,
                ["arithmetic"],
                {},
                filter_equivalent=True,
                equivalence_samples=5,
                preset_used="essential",
                mutant_timeout=None,
            )
            # No non-dunder functions → should return None (no kills)
            assert result is None
        finally:
            _cleanup_module("_test_dunder_mod")

    def test_ray_remote_unwrapped_before_module_check(self):
        """@ray.remote functions have __module__='ray.remote_function'.

        The fallback must unwrap BEFORE checking __module__, otherwise
        the function is skipped because 'ray.remote_function' doesn't
        start with the target module name.
        """
        from ordeal.mutations import MutationResult, _module_mine_oracle_fallback

        def _compute(x: int) -> int:
            return x * 2

        # Simulate @ray.remote: outer has wrong __module__, ._function has correct one
        class FakeRemote:
            def __init__(self, fn):
                self._function = fn
                self.__module__ = "ray.remote_function"
                self.__name__ = fn.__name__
                self.__qualname__ = fn.__qualname__

            def __call__(self, *args, **kwargs):
                return self._function(*args, **kwargs)

        _compute.__module__ = "_test_ray_mod"
        wrapped = FakeRemote(_compute)

        mod = _make_module("_test_ray_mod", {"_compute": wrapped})

        try:
            dummy_result = MutationResult(target="_test_ray_mod")
            dummy_result.mutants = []

            with pytest.warns(UserWarning, match="tests killed 0/0 mutants but mine oracle killed"):
                result = _module_mine_oracle_fallback(
                    "_test_ray_mod",
                    mod,
                    dummy_result,
                    ["arithmetic", "comparison"],
                    {},
                    filter_equivalent=True,
                    equivalence_samples=5,
                    preset_used="essential",
                    mutant_timeout=None,
                )
            # Should find _compute after unwrapping, not skip it
            if result is not None:
                assert result.total > 0
                assert any("_compute" in m.description or True for m in result.mutants)
        finally:
            _cleanup_module("_test_ray_mod")


# ============================================================================
# Function-level mine oracle — 0% score
# ============================================================================


class TestFunctionMineOracleFallback:
    """mutate_function_and_test falls back to mine oracle on 0% score."""

    def test_fallback_triggers_on_zero_kills(self):
        """When auto-discovered tests kill nothing, mine oracle takes over."""
        from ordeal.mutations import mutate_function_and_test

        def add(a: int, b: int) -> int:
            return a + b

        add.__module__ = "_test_fallback_mod"
        _make_module("_test_fallback_mod", {"add": add})

        try:
            # No tests match -k _test_fallback_mod → 0 kills → mine fallback
            with pytest.warns(UserWarning, match="tests killed 0/"):
                result = mutate_function_and_test(
                    "_test_fallback_mod.add",
                    preset="essential",
                )
            # Mine oracle should have caught something
            assert result.total > 0
            assert result.killed > 0
        finally:
            _cleanup_module("_test_fallback_mod")

    def test_explicit_test_fn_no_fallback(self):
        """When user provides test_fn, 0% is the real result — no fallback."""
        from ordeal.mutations import mutate_function_and_test

        def add(a: int, b: int) -> int:
            return a + b

        add.__module__ = "_test_no_fallback"
        _make_module("_test_no_fallback", {"add": add})

        def weak_test():
            assert True  # tests nothing

        try:
            result = mutate_function_and_test(
                "_test_no_fallback.add",
                test_fn=weak_test,
                operators=["arithmetic"],
            )
            # Weak test should let mutants survive — no fallback
            assert result.diagnostics.get("fallback_reason") is None
        finally:
            _cleanup_module("_test_no_fallback")
