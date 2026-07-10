"""Focused tests for method-aware mutation target resolution."""

from __future__ import annotations

import ast
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

import ordeal.mutations as mutations
import tests._mutation_bench_target as mutation_bench_target


def _clear_module_family(prefix: str) -> None:
    """Remove a temporary test package from ``sys.modules``."""
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            sys.modules.pop(name, None)


def _write_method_target_module(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "class First:\n"
        "    def render(self) -> str:\n"
        "        return 'first'\n"
        "\n"
        "class Second:\n"
        "    def render(self) -> str:\n"
        "        return 'second'\n",
        encoding="utf-8",
    )


def test_resolve_mutation_target_handles_class_methods(tmp_path: Path, monkeypatch):
    _write_method_target_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        spec = mutations._resolve_mutation_target("pkg.mod.First.render")

        assert spec.module_name == "pkg.mod"
        assert spec.leaf_name == "render"
        assert spec.qualname_parts == ("First",)
        assert not spec.is_module
    finally:
        _clear_module_family("pkg")


def test_resolve_mutation_target_replaces_stale_parent_module(
    tmp_path: Path,
    monkeypatch,
):
    """A synthetic cached parent must not hide the current sys.path package."""
    _write_method_target_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    stale = types.ModuleType("pkg")
    monkeypatch.setitem(sys.modules, "pkg", stale)

    try:
        spec = mutations._resolve_mutation_target("pkg.mod.First.render")

        assert spec.module_name == "pkg.mod"
        assert sys.modules["pkg"] is not stale
    finally:
        _clear_module_family("pkg")


def test_function_mutated_on_disk_replaces_exact_class_method(tmp_path: Path, monkeypatch):
    _write_method_target_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_module_family("pkg")

    import pkg.mod as mod

    try:
        spec = mutations._resolve_mutation_target("pkg.mod.First.render")
        mutated_tree = ast.parse("def render(self) -> str:\n    return 'mutated'\n")

        with mutations._function_mutated_on_disk(spec, mutated_tree):
            reloaded = importlib.reload(mod)
            assert reloaded.First().render() == "mutated"
            assert reloaded.Second().render() == "second"

        restored = importlib.reload(mod)
        assert restored.First().render() == "first"
        assert restored.Second().render() == "second"
    finally:
        _clear_module_family("pkg")


def test_review_signature_and_summary_keep_method_targets_explicit(
    tmp_path: Path,
    monkeypatch,
):
    pkg = tmp_path / "reviewpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "class Service:\n"
        "    def build(self, config: Config) -> Config:\n"
        "        return config\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        signature = mutations._review_signature("reviewpkg.mod.Service.build")
        assert signature == "reviewpkg.mod.Service.build(config: Config) -> Config"

        result = mutations.MutationResult(target="reviewpkg.mod.Service.build")
        summary = result.summary()
        assert "method target: reviewpkg.mod.Service.build" in summary
    finally:
        _clear_module_family("reviewpkg")


def test_mutant_report_label_includes_exact_source_site():
    mutant = mutations.Mutant(
        operator="comparison",
        description="<= -> <",
        line=12,
        col=4,
        source_line="if timeout <= limit:",
        qualname="Service.build_env_vars",
    )

    assert mutant.site_summary == "L12:4 | if timeout <= limit:"
    assert mutant.report_label == "L12:4 | if timeout <= limit: [comparison] <= -> <"
    result = mutations.MutationResult(target="pkg.mod.Service.build", mutants=[mutant])
    assert "GAP L12:4 | if timeout <= limit: [comparison] <= -> <" in result.summary()


def test_mutant_semantic_tags_do_not_infer_transport_semantics_from_source_text():
    mutant = mutations.Mutant(
        operator="delete_statement",
        description="remove return",
        line=1,
        col=0,
        source_line="return response",
        qualname="Service.process",
    )

    tags = mutations._mutant_semantic_tags(mutant, target="reviewpkg.mod.Service.process")

    assert "json" not in tags
    assert "http" not in tags
    assert "shell" not in tags


def test_mutant_semantic_tags_do_not_infer_transport_semantics_from_generic_target_text():
    mutant = mutations.Mutant(
        operator="delete_statement",
        description="remove return",
        line=1,
        col=0,
        source_line="return result",
        qualname="Service.normalize",
    )

    tags = mutations._mutant_semantic_tags(
        mutant,
        target="reviewpkg.mod.Service.normalize_payload",
    )

    assert "json" not in tags
    assert "http" not in tags
    assert "shell" not in tags


def test_create_pytest_test_fn_disables_seed_replay(monkeypatch):
    class _Selection:
        def pytest_args(self) -> list[str]:
            return ["tests/test_mutation_bench_target.py"]

    observed: dict[str, str | None] = {}

    def fake_pytest_main(args: list[str]) -> int:
        observed["env"] = os.environ.get("ORDEAL_DISABLE_SEED_REPLAY")
        observed["args"] = " ".join(args)
        return 0

    monkeypatch.delenv("ORDEAL_DISABLE_SEED_REPLAY", raising=False)
    monkeypatch.setattr(
        mutations,
        "_mutation_test_selection",
        lambda target, test_filter=None: _Selection(),
    )
    monkeypatch.setattr("pytest.main", fake_pytest_main)

    run_tests = mutations._auto_test_fn("pkg.mod.fn")
    run_tests()

    assert observed["env"] == "1"
    assert observed["args"] is not None
    assert os.environ.get("ORDEAL_DISABLE_SEED_REPLAY") is None


def test_adaptive_mutation_workers_keep_small_workloads_serial(monkeypatch):
    monkeypatch.setattr(mutations.os, "cpu_count", lambda: 12)

    assert (
        mutations._resolve_mutation_worker_count(
            0,
            mutant_count=22,
            selected_test_count=22,
            profile=None,
        )
        == 1
    )
    assert (
        mutations._resolve_mutation_worker_count(
            8,
            mutant_count=22,
            selected_test_count=22,
            profile=None,
        )
        == 8
    )


def test_adaptive_mutation_workers_parallelize_broader_workloads(monkeypatch):
    monkeypatch.setattr(mutations.os, "cpu_count", lambda: 12)

    assert (
        mutations._resolve_mutation_worker_count(
            0,
            mutant_count=30,
            selected_test_count=22,
            profile=None,
        )
        == 4
    )


def test_adaptive_mutation_workers_keep_disk_mutation_serial(monkeypatch):
    monkeypatch.setattr(mutations.os, "cpu_count", lambda: 12)

    assert (
        mutations._resolve_mutation_worker_count(
            8,
            mutant_count=30,
            selected_test_count=22,
            profile=None,
            disk_mutation=True,
        )
        == 1
    )


def test_adaptive_mutation_workers_use_observed_calibration(monkeypatch):
    monkeypatch.setattr(mutations.os, "cpu_count", lambda: 12)
    cheap_profile = mutations._MutationExecutionProfile(
        collected_tests=2,
        mutant_count=30,
        pytest_seconds=0.08,
        workers=1,
    )

    assert (
        mutations._resolve_mutation_worker_count(
            0,
            mutant_count=30,
            selected_test_count=2,
            profile=cheap_profile,
        )
        == 1
    )
    expensive_profile = mutations._MutationExecutionProfile(
        collected_tests=22,
        mutant_count=30,
        pytest_seconds=0.40,
        workers=1,
    )
    assert (
        mutations._resolve_mutation_worker_count(
            0,
            mutant_count=30,
            selected_test_count=22,
            profile=expensive_profile,
        )
        == 4
    )
    short_cheap_calibration = mutations._MutationExecutionProfile(
        collected_tests=22,
        mutant_count=1,
        pytest_seconds=0.001,
        workers=1,
    )
    assert (
        mutations._resolve_mutation_worker_count(
            0,
            mutant_count=30,
            selected_test_count=22,
            profile=short_cheap_calibration,
        )
        == 1
    )


def test_auto_parallel_preflight_requires_uncalibrated_timing_and_coverage():
    assert mutations._needs_mutation_worker_preflight(
        0,
        preliminary_workers=4,
        profile=None,
        disk_mutation=False,
    )
    assert not mutations._needs_mutation_worker_preflight(
        4,
        preliminary_workers=4,
        profile=None,
        disk_mutation=False,
    )
    assert not mutations._needs_mutation_worker_preflight(
        0,
        preliminary_workers=4,
        profile=mutations._MutationExecutionProfile(coverage_calibrated=True),
        disk_mutation=False,
    )


def test_broad_fallback_keeps_indirect_killer(
    tmp_path: Path,
    monkeypatch,
):
    package = tmp_path / "fallbackpkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "calc.py").write_text(
        "def compute(x: int) -> int:\n    return x + 1\n\n"
        "def public_wrapper() -> int:\n    return compute(2)\n",
        encoding="utf-8",
    )
    (package / "service.py").write_text(
        "from . import calc\n\ndef value() -> int:\n    return calc.compute(2)\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    fallback_marker = tests_dir / "fallback-marker.txt"
    fallback_marker.write_text("pass", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(
        "import fallbackpkg.calc as calc\n\n"
        "def test_direct_but_weak():\n"
        "    assert calc.compute(2) in {1, 3}\n",
        encoding="utf-8",
    )
    (tests_dir / "test_contract.py").write_text(
        "from pathlib import Path\n\n"
        "from fallbackpkg.service import value\n\n"
        "def test_indirect_contract():\n"
        "    assert value() in {1, 3}\n"
        "    assert Path(__file__).with_name('fallback-marker.txt').read_text() == 'pass'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_mixed.py").write_text(
        "import fallbackpkg.calc as calc\n"
        "from fallbackpkg.service import value\n\n"
        "def test_weak_direct():\n"
        "    assert calc.compute(2) in {1, 3}\n\n"
        "def test_weak_wrapper():\n"
        "    assert calc.public_wrapper() in {1, 3}\n\n"
        "def test_service_contract():\n"
        "    assert value() == 3\n",
        encoding="utf-8",
    )
    mutant = mutations.Mutant(
        operator="arithmetic",
        description="+ -> -",
        line=2,
        col=13,
    )
    mutated_tree = ast.parse("def compute(x: int) -> int:\n    return x - 1\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mutations._all_test_files.cache_clear()
    mutations._attributed_mutation_test_candidates.cache_clear()
    mutations._named_mutation_test_candidates.cache_clear()
    mutations._split_mutation_target.cache_clear()
    mutations._mutation_test_selection.cache_clear()
    mutations._save_mutation_execution_profile(
        "fallbackpkg.calc.compute",
        mutations._MutationExecutionProfile(
            coverage_hits=("tests/test_removed.py::test_old_target",),
            coverage_calibrated=True,
            collected_tests=1,
        ),
    )
    narrow_selection = mutations._mutation_test_selection("fallbackpkg.calc.compute")
    broad_selection = mutations._broad_mutation_test_selection(
        "fallbackpkg.calc.compute",
        narrow_selection,
    )
    assert broad_selection is not None
    assert [path.split("tests/", 1)[-1] for path in broad_selection.paths] == [
        "test_contract.py::test_indirect_contract",
        "test_mixed.py::test_weak_direct",
        "test_mixed.py::test_weak_wrapper",
        "test_mixed.py::test_service_contract",
    ]
    try:
        results = mutations._batch_function_test(
            "fallbackpkg.calc.compute",
            [(mutant, mutated_tree)],
        )
    finally:
        _clear_module_family("fallbackpkg")
        mutations._all_test_files.cache_clear()
        mutations._attributed_mutation_test_candidates.cache_clear()
        mutations._named_mutation_test_candidates.cache_clear()
        mutations._split_mutation_target.cache_clear()
        mutations._mutation_test_selection.cache_clear()

    assert results[0][1] is True
    assert results[0][3] is not None
    assert "test_mixed.py::test_service_contract" in results[0][3]
    refreshed_profile = mutations._load_mutation_execution_profile(
        "fallbackpkg.calc.compute"
    )
    assert refreshed_profile is not None
    assert refreshed_profile.coverage_hits == (
        "tests/test_calc.py::test_direct_but_weak",
    )

    mutations._mutation_test_selection.cache_clear()
    learned_selection = mutations._mutation_test_selection("fallbackpkg.calc.compute")
    assert learned_selection.paths[0].endswith("test_mixed.py")

    mutations._mutation_profile_path("fallbackpkg.calc.compute").unlink()
    fallback_marker.write_text("fail", encoding="utf-8")
    mutations._mutation_test_selection.cache_clear()
    with pytest.raises(RuntimeError, match="fail before mutation"):
        mutations._batch_function_test(
            "fallbackpkg.calc.compute",
            [(mutant, mutated_tree)],
        )


def test_mutation_batch_rejects_failing_baseline(tmp_path: Path, monkeypatch):
    package = tmp_path / "baselinepkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target_source = package / "baselinecalc.py"
    target_source.write_text(
        "def compute(x: int) -> int:\n    return x + 1\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    baseline_test = tests_dir / "test_baselinecalc.py"
    baseline_test.write_text(
        "import baselinepkg.baselinecalc as calc\n\n"
        "def test_same_node_baseline():\n"
        "    assert calc.compute(2) in {1, 3}\n",
        encoding="utf-8",
    )
    mutant = mutations.Mutant(
        operator="arithmetic",
        description="+ -> -",
        line=2,
        col=13,
    )
    mutated_tree = ast.parse("def compute(x: int) -> int:\n    return x - 1\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mutations._all_test_files.cache_clear()
    mutations._attributed_mutation_test_candidates.cache_clear()
    mutations._named_mutation_test_candidates.cache_clear()
    mutations._split_mutation_target.cache_clear()
    mutations._mutation_test_selection.cache_clear()
    try:
        first_results = mutations._batch_function_test(
            "baselinepkg.baselinecalc.compute",
            [(mutant, mutated_tree)],
        )
        assert first_results[0][1] is False
        target_source.write_text(
            "def compute(x: int) -> int:\n    return x + 200\n",
            encoding="utf-8",
        )
        _clear_module_family("baselinepkg")
        sys.modules.pop("test_baselinecalc", None)
        with pytest.raises(RuntimeError, match="fail before mutation"):
            mutations._batch_function_test(
                "baselinepkg.baselinecalc.compute",
                [(mutant, mutated_tree)],
            )
        target_source.write_text(
            "def compute(x: int) -> int:\n    return x + 1\n",
            encoding="utf-8",
        )
        baseline_test.write_text(
            "import baselinepkg.baselinecalc as calc\n\n"
            "def test_same_node_baseline():\n"
            "    assert calc.compute(2) == 999\n",
            encoding="utf-8",
        )
        _clear_module_family("baselinepkg")
        sys.modules.pop("test_baselinecalc", None)
        with pytest.raises(RuntimeError, match="fail before mutation"):
            mutations._batch_function_test(
                "baselinepkg.baselinecalc.compute",
                [(mutant, mutated_tree)],
            )
    finally:
        _clear_module_family("baselinepkg")
        mutations._all_test_files.cache_clear()
        mutations._attributed_mutation_test_candidates.cache_clear()
        mutations._named_mutation_test_candidates.cache_clear()
        mutations._split_mutation_target.cache_clear()
        mutations._mutation_test_selection.cache_clear()


def test_mutation_execution_profile_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = mutations._MutationExecutionProfile(
        kill_counts={"tests/test_calc.py::test_add": 2},
        mutant_killers={"arithmetic|+ -> -|4|11|add": "tests/test_calc.py::test_add"},
        coverage_hits=("tests/test_calc.py::test_add",),
        coverage_calibrated=True,
        baseline_fingerprint="baseline-v1",
        collected_tests=3,
        mutant_count=4,
        pytest_seconds=0.25,
        workers=2,
    )

    mutations._save_mutation_execution_profile("pkg.calc.add", profile)
    loaded = mutations._load_mutation_execution_profile("pkg.calc.add")

    assert loaded == profile


def test_empty_path_baseline_fingerprint_tracks_discovered_tests_and_target(
    tmp_path: Path,
    monkeypatch,
):
    package = tmp_path / "fingerprintpkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target_source = package / "calc.py"
    target_source.write_text("def compute(x):\n    return x + 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    helper_source = tests_dir / "helpers.py"
    helper_source.write_text("EXPECTED = True\n", encoding="utf-8")
    test_source = tests_dir / "test_contract.py"
    test_source.write_text(
        "from helpers import EXPECTED\n\ndef test_same_node():\n    assert EXPECTED\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mutations._all_test_files.cache_clear()
    selection = mutations._MutationTestSelection(paths=(), k_filter="same_node")
    first = mutations._mutation_test_baseline_fingerprint(
        "fingerprintpkg.calc.compute",
        selection,
    )
    test_source.write_text("def test_same_node():\n    assert False\n", encoding="utf-8")
    test_changed = mutations._mutation_test_baseline_fingerprint(
        "fingerprintpkg.calc.compute",
        selection,
    )
    helper_source.write_text("EXPECTED = False\n", encoding="utf-8")
    helper_changed = mutations._mutation_test_baseline_fingerprint(
        "fingerprintpkg.calc.compute",
        selection,
    )
    target_source.write_text("def compute(x):\n    return x + 200\n", encoding="utf-8")
    target_changed = mutations._mutation_test_baseline_fingerprint(
        "fingerprintpkg.calc.compute",
        selection,
    )

    assert len({first, test_changed, helper_changed, target_changed}) == 4
    _clear_module_family("fingerprintpkg")
    mutations._all_test_files.cache_clear()


def test_mutation_test_order_combines_prior_kills_coverage_ast_and_fallback():
    class _Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    mutant = mutations.Mutant(
        operator="arithmetic",
        description="+ -> -",
        line=4,
        col=11,
    )
    items = [
        _Item("tests/test_calc.py::test_fallback"),
        _Item("tests/test_calc.py::test_ast"),
        _Item("tests/test_calc.py::test_covered"),
        _Item("tests/test_calc.py::test_previous"),
    ]
    selection = mutations._MutationTestSelection(
        paths=("tests/test_calc.py",),
        k_filter=None,
        ast_scores=(("tests/test_calc.py::test_ast", 12),),
    )
    profile = mutations._MutationExecutionProfile(
        kill_counts={"tests/test_calc.py::test_previous": 3},
        mutant_killers={
            mutations._mutant_profile_key(mutant): "tests/test_calc.py::test_previous"
        },
    )

    ordered = mutations._order_mutation_test_items(
        items,
        mutant=mutant,
        selection=selection,
        coverage_hits={"tests/test_calc.py::test_covered"},
        profile=profile,
    )

    assert [item.nodeid for item in ordered] == [
        "tests/test_calc.py::test_previous",
        "tests/test_calc.py::test_covered",
        "tests/test_calc.py::test_ast",
        "tests/test_calc.py::test_fallback",
    ]


def test_mutation_coverage_calibration_observes_target_entry_only():
    class _Hook:
        def pytest_runtest_protocol(self, *, item, nextitem) -> None:
            del nextitem
            item.callback()

    class _Item:
        def __init__(self, nodeid: str, callback) -> None:
            self.nodeid = nodeid
            self.callback = callback
            self.config = types.SimpleNamespace(hook=_Hook())

    session = types.SimpleNamespace(
        items=[
            _Item("tests/test_calc.py::test_unrelated", lambda: None),
            _Item(
                "tests/test_calc.py::test_tiny_add",
                lambda: mutation_bench_target.tiny_add(1, 2),
            ),
        ],
        testsfailed=0,
    )

    hits = mutations._calibrate_mutation_test_coverage(
        session,
        "tests._mutation_bench_target.tiny_add",
    )

    assert hits == {"tests/test_calc.py::test_tiny_add"}


def test_review_signature_refreshes_shadowed_local_modules(tmp_path_factory, monkeypatch):
    first_root = tmp_path_factory.mktemp("reviewpkg_first")
    first_pkg = first_root / "reviewpkg"
    first_pkg.mkdir()
    (first_pkg / "__init__.py").write_text("", encoding="utf-8")
    (first_pkg / "mod.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "class Service:\n"
        "    def build(self, config: Config) -> Config:\n"
        "        return config\n",
        encoding="utf-8",
    )

    second_root = tmp_path_factory.mktemp("reviewpkg_second")
    second_pkg = second_root / "reviewpkg"
    second_pkg.mkdir()
    (second_pkg / "__init__.py").write_text("", encoding="utf-8")
    (second_pkg / "types.py").write_text("class PolicyConfig:\n    pass\n", encoding="utf-8")
    (second_pkg / "mod.py").write_text(
        "from reviewpkg.types import PolicyConfig\n\n"
        "def process(config: PolicyConfig) -> PolicyConfig:\n"
        "    return config\n",
        encoding="utf-8",
    )

    try:
        monkeypatch.syspath_prepend(str(first_root))
        assert (
            mutations._review_signature("reviewpkg.mod.Service.build")
            == "reviewpkg.mod.Service.build(config: Config) -> Config"
        )

        monkeypatch.syspath_prepend(str(second_root))
        assert (
            mutations._review_signature("reviewpkg.mod.process")
            == "process(config: reviewpkg.types.PolicyConfig) -> "
            "reviewpkg.types.PolicyConfig"
        )
    finally:
        _clear_module_family("reviewpkg")


def test_mutation_result_promotes_only_clustered_survivors_by_default():
    result = mutations.MutationResult(
        target="pkg.mod.build",
        mutants=[
            mutations.Mutant(
                operator="comparison",
                description="<= -> <",
                line=10,
                col=4,
                source_line="if timeout <= limit:",
                qualname="Service.build_env_vars",
            ),
            mutations.Mutant(
                operator="boundary",
                description="10 -> 11",
                line=12,
                col=8,
                source_line="timeout = min(timeout, 10)",
                qualname="Service.build_env_vars",
            ),
            mutations.Mutant(
                operator="delete_statement",
                description="remove assignment",
                line=20,
                col=2,
                source_line="result = build_shell_command(path)",
                qualname="Service.cleanup",
            ),
        ],
    )

    promoted = result.promoted_survivor_clusters()
    assert len(promoted) == 2
    assert promoted[0]["owner"] == "Service.build_env_vars"
    assert promoted[0]["tag"] == "env"
    assert promoted[1]["owner"] == "Service.cleanup"
    assert promoted[1]["tag"] == "lifecycle"
    summary = result.summary()
    assert "cluster: Service.build_env_vars -> environment shaping (2 survivor(s)" in summary
    assert "cluster: Service.cleanup -> lifecycle contract boundary (1 survivor(s)" in summary
    assert "exploratory survivor(s) remain" not in summary


def test_mutation_result_can_expose_single_survivors_when_cluster_gate_disabled():
    result = mutations.MutationResult(
        target="pkg.mod.build",
        promote_clusters_only=False,
        mutants=[
            mutations.Mutant(
                operator="delete_statement",
                description="remove assignment",
                line=20,
                col=2,
                source_line="result = build_shell_command(path)",
            )
        ],
    )

    promoted = result.promoted_survivor_clusters()
    assert len(promoted) == 1
    assert promoted[0]["tag"] in {"shell", "path", "behavior"}


def test_mutation_result_clusters_lifecycle_survivors_by_owner():
    result = mutations.MutationResult(
        target="pkg.mod",
        promote_clusters_only=False,
        mutants=[
            mutations.Mutant(
                operator="delete_statement",
                description="remove cleanup call",
                line=40,
                col=2,
                source_line="self.cleanup_handlers.append(handler)",
                qualname="Env.cleanup",
            ),
            mutations.Mutant(
                operator="logical",
                description="and -> or",
                line=44,
                col=6,
                source_line="if teardown and cleanup:",
                qualname="Env.cleanup",
            ),
        ],
    )

    clusters = result.semantic_survivor_clusters()
    assert clusters[0]["owner"] == "Env.cleanup"
    assert clusters[0]["tag"] == "lifecycle"
    summary = result.summary()
    assert "Env.cleanup -> lifecycle contract boundary" in summary


def test_mutation_result_prefers_explicit_contract_metadata_over_heuristics():
    result = mutations.MutationResult(
        target="pkg.mod.Service.run",
        contract_context={
            "contract_name": "cleanup_after_cancellation",
            "contract_kind": "lifecycle",
            "harness": "stateful",
        },
        mutants=[
            mutations.Mutant(
                operator="delete_statement",
                description="remove cleanup callback",
                line=40,
                col=2,
                source_line="return result",
                qualname="Service.cleanup",
                metadata={
                    "contract_name": "cleanup_after_cancellation",
                    "contract_kind": "lifecycle",
                    "harness": "stateful",
                },
            )
        ],
    )

    clusters = result.promoted_survivor_clusters()
    assert clusters[0]["owner"] == "Service.cleanup"
    assert clusters[0]["tag"] == "lifecycle"
    assert clusters[0]["contract_name"] == "cleanup_after_cancellation"
    assert clusters[0]["harness"] == "stateful"
    summary = result.summary()
    assert "contract: cleanup_after_cancellation, lifecycle, harness=stateful" in summary
    assert "Service.cleanup -> lifecycle contract boundary" in summary


def test_mutation_contract_context_extracts_names_tags_and_harness():
    from types import SimpleNamespace

    context = mutations.mutation_contract_context(
        [
            SimpleNamespace(
                name="cleanup_after_cancellation",
                metadata={
                    "kind": "lifecycle",
                    "phase": "rollout",
                    "followup_phases": ["cleanup", "teardown"],
                    "fault": "cancel_rollout",
                },
            )
        ],
        harness="stateful",
    )

    assert context["contract_name"] == "cleanup_after_cancellation"
    assert context["contract_kind"] == "lifecycle"
    assert "cleanup_after_cancellation" in context["contract_tags"]
    assert context["phase"] == "rollout"
    assert context["followup_phases"] == ["cleanup", "teardown"]
    assert context["fault"] == "cancel_rollout"
    assert context["harness"] == "stateful"


def test_mutation_result_uses_explicit_shell_path_env_tags():
    result = mutations.MutationResult(
        target="pkg.mod.Service.build",
        mutants=[
            mutations.Mutant(
                operator="delete_statement",
                description="remove command argument quoting",
                line=12,
                col=4,
                source_line="return rendered",
                qualname="Service.build_env_vars",
                metadata={
                    "contract_tags": ["shell", "path", "env"],
                    "contract_name": "quoted_paths",
                },
            )
        ],
    )

    cluster = result.promoted_survivor_clusters()[0]
    assert cluster["owner"] == "Service.build_env_vars"
    assert cluster["tag"] == "shell"
    assert cluster["contract_name"] == "quoted_paths"
    assert cluster["coherent_boundary"] is True
    summary = result.summary()
    assert "Service.build_env_vars -> shell/argv construction" in summary


def test_mutant_semantic_tags_still_honor_explicit_contract_metadata():
    mutant = mutations.Mutant(
        operator="delete_statement",
        description="remove return",
        line=1,
        col=0,
        source_line="return response",
        qualname="Service.process",
        metadata={"contract_tags": ["json", "http"]},
    )

    tags = mutations._mutant_semantic_tags(mutant, target="reviewpkg.mod.Service.process")

    assert "json" in tags
    assert "http" in tags


def test_mutation_metadata_and_contract_context_round_trip_through_cache(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    result = mutations.MutationResult(
        target="pkg.mod.Service.run",
        contract_context={
            "contract_name": "cleanup_after_cancellation",
            "contract_kind": "lifecycle",
            "harness": "stateful",
        },
        mutants=[
            mutations.Mutant(
                operator="delete_statement",
                description="remove cleanup callback",
                line=40,
                col=2,
                source_line="return result",
                qualname="Service.cleanup",
                metadata={"contract_kind": "lifecycle", "harness": "stateful"},
            )
        ],
    )

    mutations._save_cache("pkg.mod.Service.run", result, "module-hash", "config-hash")
    loaded = mutations._load_cache(
        "pkg.mod.Service.run",
        "module-hash",
        None,
        None,
        "config-hash",
    )

    assert loaded is not None
    assert loaded.contract_context == result.contract_context
    assert loaded.mutants[0].metadata == result.mutants[0].metadata


def test_mutation_epistemic_view_keeps_contract_metadata_compact():
    result = mutations.MutationResult(
        target="pkg.mod.Service.cleanup",
        contract_context={
            "contract_name": "cleanup_after_cancellation",
            "contract_kind": "lifecycle",
            "harness": "stateful",
        },
        mutants=[
            mutations.Mutant(
                operator="delete_statement",
                description="remove cleanup callback",
                line=40,
                col=2,
                source_line="await hook(state)",
                qualname="Service.cleanup",
                metadata={"contract_kind": "lifecycle", "harness": "stateful"},
            ),
            mutations.Mutant(
                operator="delete_statement",
                description="remove second cleanup callback",
                line=44,
                col=2,
                source_line="await hook(state)",
                qualname="Service.cleanup",
                metadata={"contract_kind": "lifecycle", "harness": "stateful"},
            ),
        ],
    )

    view = result.epistemic_view()

    assert view["status"] == "promoted_gaps"
    assert view["score"] == "0/2 (0%)"
    assert view["contract"] == "cleanup_after_cancellation, lifecycle, harness=stateful"
    assert view["promoted_boundary_count"] == 1
    assert view["exploratory_survivors"] == 0
    assert view["promoted_boundaries"] == [
        {
            "owner": "Service.cleanup",
            "tag": "lifecycle",
            "label": "lifecycle contract boundary",
            "size": 2,
            "operators": ["delete_statement"],
            "contract": "cleanup_after_cancellation, lifecycle, harness=stateful",
        }
    ]


def test_mutation_epistemic_view_reports_exploratory_survivors_and_weakest_killers():
    result = mutations.MutationResult(
        target="pkg.mod.normalize",
        mutants=[
            mutations.Mutant(
                operator="comparison",
                description="<= -> <",
                line=10,
                col=4,
                source_line="if value <= limit:",
                qualname="normalize",
            ),
            mutations.Mutant(
                operator="arithmetic",
                description="+ -> -",
                line=12,
                col=8,
                killed=True,
                killed_by="tests/test_normalize.py::test_limit",
            ),
            mutations.Mutant(
                operator="boundary",
                description="10 -> 11",
                line=15,
                col=8,
                killed=True,
                killed_by="tests/test_normalize.py::test_smoke",
            ),
            mutations.Mutant(
                operator="logical",
                description="and -> or",
                line=18,
                col=8,
                killed=True,
                killed_by="tests/test_normalize.py::test_smoke",
            ),
        ],
    )

    view = result.epistemic_view()

    assert view["status"] == "exploratory_gaps"
    assert view["score"] == "3/4 (75%)"
    assert view["exploratory_survivors"] == 1
    assert view["promoted_boundary_count"] == 0
    assert view["weakest_killers"] == [
        {
            "test": "tests/test_normalize.py::test_limit",
            "kills": 1,
            "operators": ["arithmetic"],
        },
        {
            "test": "tests/test_normalize.py::test_smoke",
            "kills": 2,
            "operators": ["boundary", "logical"],
        },
    ]
