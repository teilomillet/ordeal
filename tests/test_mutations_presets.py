"""Tests for mutation operator presets, config, and CLI integration."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ordeal.config import ConfigError, load_config
from ordeal.mutations import (
    OPERATORS,
    PRESETS,
    _resolve_operators,
    generate_mutants,
    mutate_function_and_test,
)

# ============================================================================
# Preset contents
# ============================================================================


def test_presets_has_three_tiers():
    assert set(PRESETS.keys()) == {"essential", "standard", "thorough"}


def test_essential_preset_operators():
    assert PRESETS["essential"] == ["arithmetic", "comparison", "negate", "return_none"]
    assert len(PRESETS["essential"]) == 4


def test_standard_preset_operators():
    assert len(PRESETS["standard"]) == 8
    # Standard is a superset of essential
    for op in PRESETS["essential"]:
        assert op in PRESETS["standard"]


def test_thorough_preset_has_all_operators():
    assert set(PRESETS["thorough"]) == set(OPERATORS.keys())
    assert len(PRESETS["thorough"]) == 14


def test_all_preset_operators_are_valid():
    for name, ops in PRESETS.items():
        for op in ops:
            assert op in OPERATORS, f"Preset {name!r} has unknown operator {op!r}"


# ============================================================================
# _resolve_operators
# ============================================================================


def test_resolve_both_none_returns_none():
    assert _resolve_operators(None, None) is None


def test_resolve_preset_only():
    result = _resolve_operators(None, "essential")
    assert result == PRESETS["essential"]


def test_resolve_operators_only():
    ops = ["arithmetic", "comparison"]
    result = _resolve_operators(ops, None)
    assert result is ops


def test_resolve_both_raises():
    with pytest.raises(ValueError, match="Cannot specify both"):
        _resolve_operators(["arithmetic"], "essential")


def test_resolve_unknown_preset_raises():
    with pytest.raises(ValueError, match="Unknown preset.*'turbo'"):
        _resolve_operators(None, "turbo")


# ============================================================================
# Preset integration with mutate_function_and_test
# ============================================================================


def _add(a: int, b: int) -> int:
    if a < 0:
        return -a + b
    return a + b


def _test_add():
    assert _add(1, 2) == 3
    assert _add(-1, 2) == 3


def test_preset_essential_limits_operators():
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    essential_ops = set(PRESETS["essential"])
    for m in result.mutants:
        assert m.operator in essential_ops, (
            f"Mutant {m.description} has operator {m.operator!r} not in essential preset"
        )


def test_result_carries_preset_and_operators_metadata():
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    assert result.preset_used == "essential"
    assert result.operators_used == PRESETS["essential"]
    # summary includes metadata for AI consumption
    s = result.summary()
    assert "preset: essential" in s
    assert f"operators: {len(PRESETS['essential'])}" in s


def test_summary_shows_gaps_with_cause_and_fix():
    """Surviving mutants appear as GAP with Cause + Fix for AI consumption."""

    def weak_test():
        # Only checks one input — unlikely to catch all mutations
        assert _add(1, 2) == 3

    result = mutate_function_and_test(
        f"{__name__}._add",
        weak_test,
        preset="essential",
        filter_equivalent=False,
    )
    s = result.summary()
    if result.survived:
        assert "test gap(s)" in s
        assert "GAP" in s
        assert "Cause:" in s
        assert "Fix:" in s


def test_test_fn_defaults_to_none():
    """test_fn is optional — auto-discovers tests when omitted."""
    from ordeal.mutations import _auto_test_fn

    # _auto_test_fn returns a callable that runs pytest in-process
    fn = _auto_test_fn("tests.test_mutations_presets._add")
    assert callable(fn)


def test_preset_thorough_produces_more_mutants():
    result_essential = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    result_thorough = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="thorough",
        filter_equivalent=False,
    )
    assert result_thorough.total >= result_essential.total


def test_preset_and_operators_raises_in_api():
    with pytest.raises(ValueError, match="Cannot specify both"):
        mutate_function_and_test(
            f"{__name__}._add",
            _test_add,
            operators=["arithmetic"],
            preset="essential",
        )


# ============================================================================
# generate_mutants respects operator lists from presets
# ============================================================================


_SAMPLE_SOURCE = textwrap.dedent("""\
    def compute(a, b):
        if a > b:
            return a + b
        return a - b
""")


def test_generate_mutants_essential_only():
    mutants = generate_mutants(_SAMPLE_SOURCE, operators=PRESETS["essential"])
    op_names = {m.operator for m, _ in mutants}
    # Should only contain essential operators
    assert op_names <= set(PRESETS["essential"])


def test_generate_mutants_all_when_none():
    # Richer source that triggers operators beyond essential
    rich = textwrap.dedent("""\
        def compute(a, b):
            limit = 10
            if a > b and a < limit:
                return a + b
            else:
                return a - b
    """)
    mutants = generate_mutants(rich)
    op_names = {m.operator for m, _ in mutants}
    # Should include operators beyond essential (e.g. boundary, logical, swap_if_else)
    assert len(op_names) > len(PRESETS["essential"])


# ============================================================================
# Config: [mutations] section
# ============================================================================


def test_mutations_config_loads(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        targets = ["myapp.scoring.compute"]
        preset = "standard"
        threshold = 0.8
        workers = 4
    """)
    )
    cfg = load_config(toml)
    assert cfg.mutations is not None
    assert cfg.mutations.targets == ["myapp.scoring.compute"]
    assert cfg.mutations.preset == "standard"
    assert cfg.mutations.threshold == 0.8
    assert cfg.mutations.workers == 4


def test_mutations_config_defaults(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text("[mutations]\n")
    cfg = load_config(toml)
    assert cfg.mutations is not None
    assert cfg.mutations.preset == "standard"
    assert cfg.mutations.threshold == 0.0
    assert cfg.mutations.workers == 1
    assert cfg.mutations.filter_equivalent is True
    assert cfg.mutations.equivalence_samples == 10


def test_mutations_config_operators_instead_of_preset(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        targets = ["myapp.func"]
        operators = ["arithmetic", "comparison"]
    """)
    )
    cfg = load_config(toml)
    assert cfg.mutations is not None
    assert cfg.mutations.operators == ["arithmetic", "comparison"]


def test_mutations_config_both_preset_and_operators_raises(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        preset = "essential"
        operators = ["arithmetic"]
    """)
    )
    with pytest.raises(ConfigError, match="Cannot specify both"):
        load_config(toml)


def test_mutations_config_unknown_preset_raises(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        preset = "turbo"
    """)
    )
    with pytest.raises(ConfigError, match="Invalid mutations preset"):
        load_config(toml)


def test_mutations_config_threshold_out_of_range(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        threshold = 1.5
    """)
    )
    with pytest.raises(ConfigError, match="threshold must be between"):
        load_config(toml)


def test_mutations_config_unknown_key_raises(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        bogus = true
    """)
    )
    with pytest.raises(ConfigError, match="Unknown key"):
        load_config(toml)


def test_no_mutations_section_returns_none(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text("[report]\nformat = 'text'\n")
    cfg = load_config(toml)
    assert cfg.mutations is None


# ============================================================================
# Top-level imports
# ============================================================================


def test_top_level_import():
    from ordeal import MutationResult, mutate_function_and_test

    assert callable(mutate_function_and_test)
    assert MutationResult is not None


# ============================================================================
# CLI parser (unit-test the arg parsing without running mutations)
# ============================================================================


def test_cli_mutate_parser():
    from ordeal.cli import main

    # --help exits with 0
    with pytest.raises(SystemExit) as exc_info:
        main(["mutate", "--help"])
    assert exc_info.value.code == 0


# ============================================================================
# generate_test_stubs
# ============================================================================


def test_generate_test_stubs_for_surviving_mutants():
    def weak_test():
        assert _add(1, 2) == 3

    result = mutate_function_and_test(
        f"{__name__}._add",
        weak_test,
        preset="essential",
        filter_equivalent=False,
    )
    stubs = result.generate_test_stubs()
    if result.survived:
        assert f"from {__name__} import _add" in stubs
        assert "def test_" in stubs
        # Uses real param names from inspect.signature
        assert "a=" in stubs
        assert "b=" in stubs
        # Includes function signature in header
        assert "Function signature:" in stubs
    else:
        assert stubs == ""


def test_generate_test_stubs_empty_when_all_killed():
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    if not result.survived:
        assert result.generate_test_stubs() == ""


# ============================================================================
# pytest marker registration
# ============================================================================


def test_mutate_marker_registered():
    """The mutate marker is registered by the plugin."""

    # The marker should be in the plugin's pytest_configure
    from ordeal.plugin import pytest_configure

    class FakeConfig:
        _ini_lines: list[str] = []

        def addinivalue_line(self, name: str, line: str) -> None:
            self._ini_lines.append(line)

        def getoption(self, name: str, default=None):
            return default

    cfg = FakeConfig()
    pytest_configure(cfg)  # type: ignore[arg-type]
    assert any("mutate" in line for line in cfg._ini_lines)
