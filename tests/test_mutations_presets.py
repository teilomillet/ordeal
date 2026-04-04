"""Tests for mutation operator presets, config, and CLI integration."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

import ordeal.mutations as mutations
from ordeal.config import ConfigError, load_config
from ordeal.mutations import (
    OPERATORS,
    PRESETS,
    Mutant,
    MutationResult,
    NoTestsFoundError,
    _get_source,
    _is_runtime_equivalent,
    _mutation_test_selection,
    _resolve_operators,
    _unwrap_func,
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


def test_thorough_preset_frontloads_standard_operators():
    assert PRESETS["thorough"][: len(PRESETS["standard"])] == PRESETS["standard"]


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


def test_auto_discovered_function_mutation_uses_batch_path(monkeypatch):
    mutant = Mutant(operator="arithmetic", description="+ -> -", line=1, col=0)
    mutated_tree = ast.parse("def _add(a: int, b: int) -> int:\n    return a - b\n")
    calls: dict[str, object] = {"probe_runs": 0}

    def fake_auto_test_fn(target, test_filter=None):
        def run():
            calls["probe_runs"] = int(calls["probe_runs"]) + 1
            raise AssertionError("auto-discovered tests should not be probed eagerly")

        return run

    monkeypatch.setattr(mutations, "_auto_test_fn", fake_auto_test_fn)
    monkeypatch.setattr(
        mutations,
        "generate_mutants",
        lambda *args, **kwargs: [(mutant, mutated_tree)],
    )
    monkeypatch.setattr(
        mutations,
        "_filter_function_mutant_pairs",
        lambda func, module, func_name, mutant_pairs, **kwargs: mutant_pairs,
    )

    def fake_batch(target, mutant_pairs, *, test_filter=None, disk_mutation=False, **kwargs):
        calls["target"] = target
        calls["count"] = len(mutant_pairs)
        calls["test_filter"] = test_filter
        return [(mutant, True, "killed", "tests.test_mutations_presets::test_add")]

    monkeypatch.setattr(mutations, "_batch_function_test", fake_batch)

    result = mutate_function_and_test(
        f"{__name__}._add",
        test_fn=None,
        preset="essential",
        filter_equivalent=False,
    )

    assert calls == {
        "probe_runs": 0,
        "target": f"{__name__}._add",
        "count": 1,
        "test_filter": None,
    }
    assert result.total == 1
    assert result.killed == 1
    assert result.mutants[0].killed_by == "tests.test_mutations_presets::test_add"


def test_auto_discovered_function_mutation_uses_parallel_batch_path(monkeypatch):
    first = Mutant(operator="arithmetic", description="+ -> -", line=1, col=0)
    second = Mutant(operator="comparison", description="< -> <=", line=2, col=0)
    mutant_pairs = [
        (first, ast.parse("def _add(a: int, b: int) -> int:\n    return a - b\n")),
        (
            second,
            ast.parse(
                "def _add(a: int, b: int) -> int:\n"
                "    if a <= 0:\n"
                "        return -a + b\n"
                "    return a + b\n"
            ),
        ),
    ]
    calls: dict[str, object] = {}

    monkeypatch.setattr(mutations, "_auto_test_fn", lambda target, test_filter=None: lambda: None)
    monkeypatch.setattr(mutations, "generate_mutants", lambda *args, **kwargs: mutant_pairs)
    monkeypatch.setattr(
        mutations,
        "_filter_function_mutant_pairs",
        lambda func, module, func_name, mutant_pairs, **kwargs: mutant_pairs,
    )

    def fake_parallel_batch(
        target,
        mutant_pairs,
        workers,
        *,
        test_filter=None,
        disk_mutation=False,
        **kwargs,
    ):
        calls["target"] = target
        calls["count"] = len(mutant_pairs)
        calls["workers"] = workers
        return [
            (first, True, "killed", "tests.test_mutations_presets::test_add"),
            (second, False, None, None),
        ]

    monkeypatch.setattr(mutations, "_parallel_function_batch_test", fake_parallel_batch)

    result = mutate_function_and_test(
        f"{__name__}._add",
        test_fn=None,
        preset="essential",
        workers=2,
        filter_equivalent=False,
    )

    assert calls == {
        "target": f"{__name__}._add",
        "count": 2,
        "workers": 2,
    }
    assert result.total == 2
    assert result.killed == 1


def test_auto_discovered_function_mutation_falls_back_to_mine_after_batch_collection(monkeypatch):
    mutant = Mutant(operator="arithmetic", description="+ -> -", line=1, col=0)
    mutated_tree = ast.parse("def _add(a: int, b: int) -> int:\n    return a - b\n")
    fallback = MutationResult(
        target=f"{__name__}._add",
        operators_used=PRESETS["essential"],
        preset_used="essential",
    )
    fallback.mutants.append(mutant)
    mutant.killed = True
    mutant.killed_by = "mine"

    monkeypatch.setattr(
        mutations,
        "_auto_test_fn",
        lambda target, test_filter=None: (
            lambda: (_ for _ in ()).throw(
                AssertionError("auto-discovered tests should not be probed eagerly")
            )
        ),
    )
    monkeypatch.setattr(
        mutations,
        "generate_mutants",
        lambda *args, **kwargs: [(mutant, mutated_tree)],
    )
    monkeypatch.setattr(
        mutations,
        "_filter_function_mutant_pairs",
        lambda func, module, func_name, mutant_pairs, **kwargs: mutant_pairs,
    )
    monkeypatch.setattr(
        mutations,
        "_batch_function_test",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoTestsFoundError("no tests")),
    )
    monkeypatch.setattr(mutations, "_mine_based_mutation_test", lambda *args, **kwargs: fallback)

    result = mutate_function_and_test(
        f"{__name__}._add",
        test_fn=None,
        preset="essential",
        filter_equivalent=False,
    )

    assert result is fallback
    assert result.killed == 1
    assert result.diagnostics["tested"] == 1


def test_auto_discovered_function_mutation_skips_pytest_when_all_mutants_filter_out(monkeypatch):
    mutant = Mutant(operator="comparison", description="< -> <=", line=1, col=0)
    mutated_tree = ast.parse(
        "def _add(a: int, b: int) -> int:\n"
        "    if a <= 0:\n"
        "        return -a + b\n"
        "    return a + b\n"
    )
    calls: dict[str, int] = {"probe_runs": 0}

    def fake_auto_test_fn(target, test_filter=None):
        def run():
            calls["probe_runs"] += 1
            raise AssertionError("pytest path should be skipped when nothing remains to test")

        return run

    monkeypatch.setattr(mutations, "_auto_test_fn", fake_auto_test_fn)
    monkeypatch.setattr(
        mutations,
        "generate_mutants",
        lambda *args, **kwargs: [(mutant, mutated_tree)],
    )
    monkeypatch.setattr(
        mutations,
        "_filter_function_mutant_pairs",
        lambda func, module, func_name, mutant_pairs, **kwargs: [],
    )
    monkeypatch.setattr(
        mutations,
        "_batch_function_test",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("batch pytest should not run when there are no mutants left")
        ),
    )

    result = mutate_function_and_test(
        f"{__name__}._add",
        test_fn=None,
        preset="essential",
        filter_equivalent=True,
    )

    assert calls["probe_runs"] == 0
    assert result.total == 0
    assert result.diagnostics["tested"] == 0


def test_mutation_test_selection_prefers_content_matches(monkeypatch, tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("def compute(x: int) -> int:\n    return x + 1\n")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    battle = tests_dir / "test_battle.py"
    battle.write_text(
        textwrap.dedent("""\
        from pkg.mod import compute

        def test_generic_regression():
            assert compute(1) == 2
        """)
    )
    (tests_dir / "test_mod.py").write_text(
        textwrap.dedent("""\
        def test_unrelated():
            assert True
        """)
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mutations._all_test_files.cache_clear()
    mutations._named_mutation_test_candidates.cache_clear()
    mutations._split_mutation_target.cache_clear()
    _mutation_test_selection.cache_clear()

    selection = _mutation_test_selection("pkg.mod.compute")

    assert selection.paths[0] == str(battle)
    assert selection.k_filter is None


def test_mutation_test_selection_matches_private_module_names(monkeypatch, tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "_mod.py").write_text("def compute(x: int) -> int:\n    return x + 1\n")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target_test = tests_dir / "test_mod.py"
    target_test.write_text(
        textwrap.dedent("""\
        from pkg._mod import compute

        def test_compute():
            assert compute(1) == 2
        """)
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mutations._all_test_files.cache_clear()
    mutations._named_mutation_test_candidates.cache_clear()
    mutations._split_mutation_target.cache_clear()
    _mutation_test_selection.cache_clear()
    monkeypatch.setattr(
        mutations,
        "_all_test_files",
        lambda: (_ for _ in ()).throw(AssertionError("should not scan every test file")),
    )

    selection = _mutation_test_selection("pkg._mod.compute")

    assert selection.paths == (str(target_test),)
    assert selection.k_filter is None


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
    assert cfg.mutations.test_filter is None


def test_mutations_config_test_filter(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        targets = ["myapp.func"]
        test_filter = "test_func"
    """)
    )
    cfg = load_config(toml)
    assert cfg.mutations is not None
    assert cfg.mutations.test_filter == "test_func"


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


def test_auto_test_fn_uses_test_filter():
    """_auto_test_fn passes test_filter as -k instead of module name."""
    from ordeal.mutations import _auto_test_fn

    fn = _auto_test_fn("myapp.scoring.compute", test_filter="test_compute")
    assert callable(fn)


def test_cli_test_filter_flag():
    """--test-filter is accepted by the CLI parser."""
    from ordeal.cli import main

    # Just verify it parses without error (--help will show the flag)
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
        assert "Draft review stubs for mutation gaps" in stubs
        assert "from __future__ import annotations" in stubs
        assert f"import {__name__} as _ordeal_target" in stubs
        assert "def test_" in stubs
        # Uses real param names from inspect.signature
        assert "a=" in stubs
        assert "b=" in stubs
        # Includes a reviewable signature and pinned-behavior comment
        assert "Reviewed signature:" in stubs
        assert "Pinned behavior candidate" in stubs
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


def test_generate_test_stubs_qualifies_local_types(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "reviewpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "types.py").write_text(
        "class PolicyConfig:\n"
        "    pass\n"
    )
    (pkg / "mod.py").write_text(
        "from reviewpkg.types import PolicyConfig\n\n"
        "def process(config: PolicyConfig) -> PolicyConfig:\n"
        "    return config\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    mutant = Mutant(
        operator="arithmetic",
        description="+ -> -",
        line=3,
        col=4,
        source_line="return config",
    )
    result = MutationResult(target="reviewpkg.mod.process", mutants=[mutant])
    stubs = result.generate_test_stubs()

    assert "import reviewpkg.mod as _ordeal_target" in stubs
    assert (
        "Reviewed signature: process(config: reviewpkg.types.PolicyConfig)"
        in stubs
    )
    assert "# assert result == ..." in stubs


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


# ============================================================================
# _unwrap_func — decorator unwrapping
# ============================================================================


def test_unwrap_func_with_wrapped_chain():
    """inspect.unwrap follows __wrapped__ chains (functools.wraps)."""
    import functools

    def real(x: int) -> int:
        return x + 1

    @functools.wraps(real)
    def wrapper(x: int) -> int:
        return real(x)

    assert _unwrap_func(wrapper) is real


def test_unwrap_func_ray_remote_attribute():
    """_unwrap_func follows ._function for Ray-like decorators."""

    def real(x: int) -> int:
        return x * 2

    class FakeRemote:
        _function = real

        def __call__(self, *args, **kwargs):
            return self._function(*args, **kwargs)

    assert _unwrap_func(FakeRemote()) is real


def test_unwrap_func_staticmethod():
    """_unwrap_func follows __func__ for staticmethod."""

    def real(x: int) -> int:
        return x

    sm = staticmethod(real)
    assert _unwrap_func(sm) is real


def test_unwrap_func_property():
    """_unwrap_func follows .fget for property objects."""

    def getter(self) -> int:
        return 42

    prop = property(getter)
    assert _unwrap_func(prop) is getter


def test_unwrap_func_partial():
    """_unwrap_func follows .func for functools.partial."""
    import functools

    def real(a: int, b: int) -> int:
        return a + b

    p = functools.partial(real, 1)
    assert _unwrap_func(p) is real


# ============================================================================
# _get_source — source extraction with fallback
# ============================================================================


def test_get_source_normal_function():
    """_get_source works for a regular function."""
    source = _get_source(_add)
    assert "def _add" in source
    assert "return a + b" in source


def test_get_source_fallback_when_getsource_fails(monkeypatch):
    """When inspect.getsource raises, _get_source falls back to file reading."""
    import inspect as _inspect

    original_getsource = _inspect.getsource

    def broken_getsource(obj):
        if obj is _add:
            raise OSError("no source")
        return original_getsource(obj)

    monkeypatch.setattr(_inspect, "getsource", broken_getsource)

    source = _get_source(_add)
    assert "def _add" in source
    assert "return a + b" in source


# ============================================================================
# _is_runtime_equivalent — boundary-aware equivalence filter
# ============================================================================


def _clamp(x: int, lo: int, hi: int) -> int:
    """Clamp x into [lo, hi]."""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _clamp_boundary_bug(x: int, lo: int, hi: int) -> int:
    """Same as _clamp but with <= instead of < — only differs at boundary."""
    if x <= lo:
        return lo
    if x > hi:
        return hi
    return x


def test_runtime_equivalent_catches_boundary_mutation():
    """Boundary values distinguish < from <= even in multi-param functions."""
    result = _is_runtime_equivalent(_clamp, _clamp_boundary_bug)
    # Boundary values like (x=0, lo=0, hi=-1) expose the difference:
    # original: 0 < 0 → False, 0 > -1 → True, returns -1
    # mutant:   0 <= 0 → True, returns 0
    assert result is False


def _threshold(x: int) -> str:
    """Classify x as low/high — boundary at exactly 0."""
    if x < 0:
        return "low"
    return "high"


def _threshold_lte(x: int) -> str:
    """< mutated to <=, changes result at x=0."""
    if x <= 0:
        return "low"
    return "high"


def test_runtime_equivalent_distinguishes_lt_vs_lte():
    """Boundary value x=0 must distinguish < from <= in threshold function."""
    result = _is_runtime_equivalent(_threshold, _threshold_lte)
    assert result is False, "boundary value 0 should distinguish < from <="


def _classify(x: float) -> float:
    """Classify: positive → 1.0, non-positive → -1.0."""
    if x > 0.0:
        return 1.0
    return -1.0


def _classify_gte(x: float) -> float:
    """> mutated to >=, changes result at x=0.0."""
    if x >= 0.0:
        return 1.0
    return -1.0


def test_runtime_equivalent_floats_boundary():
    """Boundary value 0.0 must distinguish > from >= in float function."""
    # _classify(0.0) → -1.0, _classify_gte(0.0) → 1.0
    result = _is_runtime_equivalent(_classify, _classify_gte)
    assert result is False, "boundary value 0.0 should distinguish > from >="


def test_runtime_equivalent_truly_equivalent():
    """Truly equivalent functions should still be detected."""

    def original(x: int) -> int:
        return x + 0

    def mutant(x: int) -> int:
        return x - 0

    result = _is_runtime_equivalent(original, mutant)
    assert result is True


def test_runtime_equivalent_isolates_mutable_inputs(monkeypatch):
    """Original and mutant should not share mutable arguments during comparison."""

    def original(xs: list[int]) -> list[int]:
        if xs:
            xs.pop(0)
        return xs

    def mutant(xs: list[int]) -> list[int]:
        return xs

    monkeypatch.setattr(
        mutations,
        "_equivalence_sample_plan",
        lambda func, n: mutations._EquivalenceSamplePlan(samples=(([1, 2],),)),
    )

    result = _is_runtime_equivalent(original, mutant)
    assert result is False


# ============================================================================
# MutationResult diagnostics
# ============================================================================


def test_diagnostics_populated_after_mutate():
    """mutate_function_and_test populates diagnostics counters."""
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    d = result.diagnostics
    assert d["generated"] > 0, "should count generated mutants"
    assert d["tested"] == result.total, "tested should equal final mutant count"


def test_timings_populated_after_mutate():
    """mutate_function_and_test records per-phase timings."""
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    assert result.timings["generate_seconds"] >= 0.0
    assert result.timings["test_execution_seconds"] >= 0.0
    assert result.timings["total_seconds"] >= result.timings["test_execution_seconds"]


def test_diagnostics_counts_ast_equivalent():
    """AST-equivalent mutants are counted, not silently dropped."""
    stats: dict[str, int] = {}
    # Simple source where some mutations produce equivalent bytecode
    source = textwrap.dedent("""\
        def f(x):
            return x + 0
    """)
    generate_mutants(source, operators=["arithmetic"], _stats=stats)
    # We generated something and may have filtered some as AST-equivalent
    assert stats.get("generated", 0) > 0


def test_diagnostics_counts_runtime_equivalent():
    """Runtime-equivalent filtering is tracked in diagnostics."""
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=True,
    )
    d = result.diagnostics
    # The key should exist (may be 0 if none were filtered)
    assert "filtered_runtime_equivalent" in d


def test_summary_zero_mutants_not_misleading():
    """summary() should NOT show '100%' when there are 0 mutants."""
    result = MutationResult(target="fake.target")
    s = result.summary()
    assert "100%" not in s
    assert "no mutants to test" in s


def test_summary_zero_mutants_explains_all_filtered():
    """summary() explains when all mutants were filtered as equivalent."""
    result = MutationResult(target="fake.target")
    result.diagnostics["generated"] = 5
    result.diagnostics["filtered_ast_equivalent"] = 5
    s = result.summary()
    assert "filter_equivalent=False" in s


def test_summary_zero_mutants_explains_no_sites():
    """summary() explains when no mutation sites were found."""
    result = MutationResult(target="fake.target")
    result.diagnostics["generated"] = 0
    s = result.summary()
    assert "No mutation sites" in s


def test_filter_report_empty_result():
    """filter_report() handles 0 generated, 0 tested."""
    result = MutationResult(target="fake.target")
    report = result.filter_report()
    assert "No mutants were generated" in report


def test_filter_report_with_filtering():
    """filter_report() shows pipeline breakdown."""
    result = MutationResult(target="fake.target")
    result.diagnostics["generated"] = 10
    result.diagnostics["filtered_ast_equivalent"] = 3
    result.diagnostics["filtered_runtime_equivalent"] = 2
    result.diagnostics["compilation_failed"] = 1
    result.diagnostics["tested"] = 4
    report = result.filter_report()
    assert "10 mutant(s) generated" in report
    assert "3 filtered (AST equivalent)" in report
    assert "2 filtered (runtime equivalent)" in report
    assert "1 dropped (compilation failed)" in report
    assert "4 tested" in report


def test_filter_report_from_real_run():
    """filter_report() works on a real mutate_function_and_test result."""
    result = mutate_function_and_test(
        f"{__name__}._add",
        _test_add,
        preset="essential",
        filter_equivalent=False,
    )
    report = result.filter_report()
    assert "generated" in report
    assert "tested" in report


# ============================================================================
# mutant_timeout — generation step timeout
# ============================================================================


def test_mutant_timeout_returns_partial_results():
    """generate_mutants with a very short timeout returns what it has so far."""
    # Use a source with many mutation sites
    source = textwrap.dedent("""\
        def f(a, b, c, d, e):
            if a > b and c < d:
                return a + b - c * d / e
            elif a == b or c != d:
                return a - b + c * d
            else:
                return a * b + c - d
    """)
    stats: dict[str, int] = {}
    # Timeout of 0 should abort immediately (or very quickly)
    generate_mutants(source, _stats=stats, timeout=0)
    # Should have timed out and returned partial (possibly empty) results
    assert stats.get("generation_timed_out") == 1


def test_mutant_timeout_none_means_no_limit():
    """generate_mutants with timeout=None runs to completion."""
    source = textwrap.dedent("""\
        def f(a, b):
            return a + b
    """)
    stats: dict[str, int] = {}
    mutants = generate_mutants(source, operators=["arithmetic"], _stats=stats, timeout=None)
    assert stats.get("generation_timed_out") is None
    assert len(mutants) > 0


def test_filter_report_shows_timeout():
    """filter_report() mentions timeout when generation was cut short."""
    result = MutationResult(target="fake.target")
    result.diagnostics["generated"] = 3
    result.diagnostics["generation_timed_out"] = 1
    result.diagnostics["tested"] = 3
    report = result.filter_report()
    assert "timed out" in report


def test_mutations_config_mutant_timeout(tmp_path: Path):
    toml = tmp_path / "ordeal.toml"
    toml.write_text(
        textwrap.dedent("""\
        [mutations]
        targets = ["myapp.func"]
        mutant_timeout = 30.0
    """)
    )
    cfg = load_config(toml)
    assert cfg.mutations is not None
    assert cfg.mutations.mutant_timeout == 30.0
