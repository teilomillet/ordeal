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
