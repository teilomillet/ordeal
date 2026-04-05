"""Focused tests for method-aware mutation target resolution."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import ordeal.mutations as mutations


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
            ),
            mutations.Mutant(
                operator="boundary",
                description="10 -> 11",
                line=12,
                col=8,
                source_line="timeout = min(timeout, 10)",
            ),
            mutations.Mutant(
                operator="delete_statement",
                description="remove assignment",
                line=20,
                col=2,
                source_line="result = build_shell_command(path)",
            ),
        ],
    )

    promoted = result.promoted_survivor_clusters()
    assert len(promoted) == 1
    assert promoted[0]["tag"] == "boundary"
    summary = result.summary()
    assert "cluster: boundary handling (2 survivor(s)" in summary
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
