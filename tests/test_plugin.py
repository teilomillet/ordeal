"""Tests for ordeal.plugin — pytest plugin hooks, fixtures, scan collection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ordeal import assertions
from ordeal.buggify import _state
from ordeal.buggify import activate as _buggify_activate
from ordeal.buggify import deactivate as _buggify_deactivate
from ordeal.buggify import is_active as _buggify_is_active
from ordeal.plugin import (
    OrdealScanError,
    OrdealScanItem,
    _parse_toml_fixtures,
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_configure,
    pytest_terminal_summary,
    pytest_unconfigure,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_config(chaos: bool = False, seed: int | None = None, prob: float = 0.1):
    """Create a mock pytest.Config with ordeal options."""
    cfg = MagicMock()
    values = {"chaos": chaos, "chaos_seed": seed, "buggify_prob": prob}
    cfg.getoption = lambda key, default=None: values.get(key, default)
    cfg.addinivalue_line = MagicMock()
    return cfg


def _make_parser():
    """Create a mock pytest.Parser."""
    parser = MagicMock()
    group = MagicMock()
    parser.getgroup = MagicMock(return_value=group)
    return parser, group


# ============================================================================
# pytest_addoption
# ============================================================================


class TestPytestAddoption:
    def test_registers_group(self):
        parser, group = _make_parser()
        pytest_addoption(parser)
        parser.getgroup.assert_called_once_with("ordeal", "Chaos testing with ordeal")

    def test_registers_chaos_flag(self):
        parser, group = _make_parser()
        pytest_addoption(parser)
        calls = [str(c) for c in group.addoption.call_args_list]
        assert any("--chaos" in c for c in calls)

    def test_registers_chaos_seed(self):
        parser, group = _make_parser()
        pytest_addoption(parser)
        calls = [str(c) for c in group.addoption.call_args_list]
        assert any("--chaos-seed" in c for c in calls)

    def test_registers_buggify_prob(self):
        parser, group = _make_parser()
        pytest_addoption(parser)
        calls = [str(c) for c in group.addoption.call_args_list]
        assert any("--buggify-prob" in c for c in calls)

    def test_registers_three_options(self):
        parser, group = _make_parser()
        pytest_addoption(parser)
        assert group.addoption.call_count == 3


# ============================================================================
# pytest_configure
# ============================================================================


class TestPytestConfigure:
    def setup_method(self):
        self._prev_active = assertions.tracker.active
        self._prev_buggify = _buggify_is_active()
        assertions.tracker.active = False

    def teardown_method(self):
        assertions.tracker.active = self._prev_active
        if self._prev_buggify:
            _buggify_activate()
        else:
            _buggify_deactivate()

    def test_registers_markers(self):
        cfg = _make_config(chaos=False)
        pytest_configure(cfg)
        assert cfg.addinivalue_line.call_count == 2
        calls = [str(c) for c in cfg.addinivalue_line.call_args_list]
        assert any("chaos" in c for c in calls)
        assert any("ordeal_scan" in c for c in calls)

    def test_activates_tracker_with_chaos(self):
        cfg = _make_config(chaos=True)
        pytest_configure(cfg)
        assert assertions.tracker.active is True

    def test_activates_buggify_with_chaos(self):
        cfg = _make_config(chaos=True, prob=0.5)
        pytest_configure(cfg)
        assert _buggify_is_active() is True
        assert getattr(_state, "probability", None) == 0.5

    def test_sets_seed_when_provided(self):
        cfg = _make_config(chaos=True, seed=42)
        pytest_configure(cfg)
        assert _buggify_is_active() is True

    def test_no_activation_without_chaos(self):
        cfg = _make_config(chaos=False)
        pytest_configure(cfg)
        assert assertions.tracker.active is False


# ============================================================================
# pytest_unconfigure
# ============================================================================


class TestPytestUnconfigure:
    def test_deactivates_tracker(self):
        assertions.tracker.active = True
        cfg = _make_config()
        pytest_unconfigure(cfg)
        assert assertions.tracker.active is False

    def test_deactivates_buggify(self):
        _buggify_activate()
        cfg = _make_config()
        pytest_unconfigure(cfg)
        assert _buggify_is_active() is False


# ============================================================================
# pytest_collection_modifyitems
# ============================================================================


class TestPytestCollectionModifyItems:
    def test_skips_chaos_marked_without_flag(self):
        cfg = _make_config(chaos=False)
        item = MagicMock()
        item.keywords = {"chaos": True}
        items = [item]
        pytest_collection_modifyitems(cfg, items)
        item.add_marker.assert_called_once()

    def test_no_skip_with_chaos_flag(self):
        cfg = _make_config(chaos=True)
        item = MagicMock()
        item.keywords = {"chaos": True}
        items = [item]
        pytest_collection_modifyitems(cfg, items)
        item.add_marker.assert_not_called()

    def test_non_chaos_items_untouched(self):
        cfg = _make_config(chaos=False)
        item = MagicMock()
        item.keywords = {}
        items = [item]
        pytest_collection_modifyitems(cfg, items)
        item.add_marker.assert_not_called()

    def test_mixed_items(self):
        cfg = _make_config(chaos=False)
        chaos_item = MagicMock()
        chaos_item.keywords = {"chaos": True}
        normal_item = MagicMock()
        normal_item.keywords = {}
        items = [chaos_item, normal_item]
        pytest_collection_modifyitems(cfg, items)
        chaos_item.add_marker.assert_called_once()
        normal_item.add_marker.assert_not_called()


# ============================================================================
# chaos_enabled fixture
# ============================================================================


class TestChaosEnabledFixture:
    def test_fixture_activates_and_restores(self, chaos_enabled):
        """The chaos_enabled fixture should activate tracker + buggify."""
        assert assertions.tracker.active is True
        assert _buggify_is_active() is True


# ============================================================================
# _parse_toml_fixtures
# ============================================================================


class TestParseTomlFixtures:
    def test_empty_returns_none(self):
        assert _parse_toml_fixtures({}) is None

    def test_csv_becomes_sampled_from(self):
        result = _parse_toml_fixtures({"mode": "a,b,c"})
        assert result is not None
        assert "mode" in result

    def test_single_string_becomes_just(self):
        result = _parse_toml_fixtures({"name": "hello"})
        assert result is not None
        assert "name" in result

    def test_non_string_becomes_just(self):
        result = _parse_toml_fixtures({"count": 42})
        assert result is not None
        assert "count" in result

    def test_mixed_fixtures(self):
        result = _parse_toml_fixtures(
            {
                "mode": "fast,slow",
                "name": "test",
                "retries": 3,
            }
        )
        assert result is not None
        assert len(result) == 3


# ============================================================================
# OrdealScanItem
# ============================================================================


class TestOrdealScanItem:
    def test_runtest_fuzz_fails_raises(self):
        item = OrdealScanItem.__new__(OrdealScanItem)
        item.module_name = "tests._mutation_target"
        item.func_name = "add"
        item.max_examples = 10
        item._fixtures = None

        with patch("ordeal.auto.fuzz") as mock_fuzz:
            result = MagicMock()
            result.passed = False
            result.summary.return_value = "found crash"
            mock_fuzz.return_value = result
            with pytest.raises(OrdealScanError, match="found crash"):
                item.runtest()

    def test_runtest_fuzz_passes(self):
        item = OrdealScanItem.__new__(OrdealScanItem)
        item.module_name = "tests._mutation_target"
        item.func_name = "add"
        item.max_examples = 10
        item._fixtures = None

        with patch("ordeal.auto.fuzz") as mock_fuzz:
            result = MagicMock()
            result.passed = True
            mock_fuzz.return_value = result
            item.runtest()  # should not raise

    def test_reportinfo(self):
        item = OrdealScanItem.__new__(OrdealScanItem)
        item.module_name = "mymod"
        item.func_name = "myfunc"
        info = item.reportinfo()
        assert info[0] == "mymod"
        assert "mymod.myfunc" in info[2]

    def test_repr_failure_scan_error(self):
        item = OrdealScanItem.__new__(OrdealScanItem)
        item.config = MagicMock()
        exc = OrdealScanError("boom")
        excinfo = MagicMock()
        excinfo.value = exc
        result = item.repr_failure(excinfo)
        assert result == "boom"

    def test_repr_failure_non_scan_error(self):
        item = OrdealScanItem.__new__(OrdealScanItem)
        item.config = MagicMock()
        exc = ValueError("other")
        excinfo = MagicMock()
        excinfo.value = exc
        # Falls back to super().repr_failure
        with patch.object(pytest.Item, "repr_failure", return_value="fallback"):
            result = item.repr_failure(excinfo, style=None)
            assert result == "fallback"


# ============================================================================
# pytest_terminal_summary
# ============================================================================


class TestPytestTerminalSummary:
    def test_no_output_without_chaos(self):
        cfg = _make_config(chaos=False)
        tr = MagicMock()
        pytest_terminal_summary(tr, 0, cfg)
        tr.section.assert_not_called()

    def test_no_output_without_results(self):
        cfg = _make_config(chaos=True)
        tr = MagicMock()
        # Ensure tracker has no results
        prev = assertions.tracker.active
        assertions.tracker.reset()
        pytest_terminal_summary(tr, 0, cfg)
        tr.section.assert_not_called()
        assertions.tracker.active = prev

    def test_prints_pass_results(self):
        cfg = _make_config(chaos=True)
        tr = MagicMock()

        prop = MagicMock()
        prop.passed = True
        prop.summary = "test_prop PASS"

        with patch.object(
            type(assertions.tracker),
            "results",
            new_callable=lambda: property(lambda self: [prop]),
        ):
            pytest_terminal_summary(tr, 0, cfg)
            tr.section.assert_called_once_with("Ordeal Property Results")

    def test_prints_fail_results(self):
        cfg = _make_config(chaos=True)
        tr = MagicMock()

        prop_pass = MagicMock()
        prop_pass.passed = True
        prop_pass.summary = "ok_prop"
        prop_fail = MagicMock()
        prop_fail.passed = False
        prop_fail.summary = "fail_prop"

        with patch.object(
            type(assertions.tracker),
            "results",
            new_callable=lambda: property(lambda self: [prop_pass, prop_fail]),
        ):
            pytest_terminal_summary(tr, 0, cfg)
            tr.section.assert_called_once()
            assert tr.line.call_count >= 3
