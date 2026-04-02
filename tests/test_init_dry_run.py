"""Tests for ordeal init --dry-run — zero side effects guarantee.

Verifies that --dry-run never imports target modules, never executes
target functions, never writes files, and still produces useful stubs
via AST-only discovery.  Regression tests for GitHub issue #3.
"""

from __future__ import annotations

import sys

import pytest

from ordeal.mutations import (
    _discover_callables_static,
    _discover_modules_static,
    generate_starter_tests,
    init_project,
)

# ============================================================================
# Fixtures: temporary packages with side effects
# ============================================================================


@pytest.fixture()
def side_effect_package(tmp_path):
    """Create a package that prints on import and has public functions."""
    pkg = tmp_path / "sideeffect"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'print("!!! SIDE EFFECT: __init__.py imported")\n'
        "def top_fn(x: int) -> int:\n"
        "    return x + 1\n"
    )
    (pkg / "danger.py").write_text(
        'print("!!! SIDE EFFECT: danger.py imported")\n'
        "def create_branch(name: str) -> bool:\n"
        '    """Create a git branch."""\n'
        "    return True\n\n"
        "def delete_files(path: str, force: bool = False) -> None:\n"
        '    """Delete files at path."""\n'
        "    pass\n"
    )
    sub = pkg / "nested"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "deep.py").write_text(
        'print("!!! SIDE EFFECT: nested/deep.py imported")\n'
        "def compute(a: float, b: float) -> float:\n"
        "    return a * b\n"
    )
    sys.path.insert(0, str(tmp_path))
    yield "sideeffect", tmp_path
    sys.path.remove(str(tmp_path))
    # Clean up any accidental imports
    for key in list(sys.modules):
        if key.startswith("sideeffect"):
            del sys.modules[key]


# ============================================================================
# init_project dry_run: no imports, no execution, no files
# ============================================================================


class TestInitDryRun:
    def test_no_imports_triggered(self, side_effect_package, capsys):
        """--dry-run must not import the target package."""
        pkg_name, tmp_path = side_effect_package
        init_project(pkg_name, dry_run=True, output_dir=str(tmp_path / "tests"))
        captured = capsys.readouterr()
        assert "!!! SIDE EFFECT" not in captured.out
        assert "!!! SIDE EFFECT" not in captured.err
        assert pkg_name not in sys.modules

    def test_no_files_written(self, side_effect_package):
        """--dry-run must not create any files or directories."""
        pkg_name, tmp_path = side_effect_package
        test_dir = tmp_path / "tests"
        init_project(pkg_name, dry_run=True, output_dir=str(test_dir))
        assert not test_dir.exists()

    def test_discovers_all_modules(self, side_effect_package):
        """--dry-run must discover modules via filesystem walk."""
        pkg_name, tmp_path = side_effect_package
        results = init_project(pkg_name, dry_run=True, output_dir=str(tmp_path / "tests"))
        modules = [r["module"] for r in results]
        assert "sideeffect" in modules
        assert "sideeffect.danger" in modules
        assert "sideeffect.nested.deep" in modules

    def test_generates_content(self, side_effect_package):
        """--dry-run must produce stub test content."""
        pkg_name, tmp_path = side_effect_package
        results = init_project(pkg_name, dry_run=True, output_dir=str(tmp_path / "tests"))
        generated = [r for r in results if r["status"] == "generated"]
        assert len(generated) > 0
        for r in generated:
            assert r["content"], f"Empty content for {r['module']}"

    def test_stubs_contain_function_names(self, side_effect_package):
        """--dry-run stubs must reference discovered functions."""
        pkg_name, tmp_path = side_effect_package
        results = init_project(pkg_name, dry_run=True, output_dir=str(tmp_path / "tests"))
        danger = next(r for r in results if r["module"] == "sideeffect.danger")
        assert "create_branch" in danger["content"]
        assert "delete_files" in danger["content"]

    def test_stubs_contain_signatures(self, side_effect_package):
        """--dry-run stubs must include AST-extracted signatures."""
        pkg_name, tmp_path = side_effect_package
        results = init_project(pkg_name, dry_run=True, output_dir=str(tmp_path / "tests"))
        danger = next(r for r in results if r["module"] == "sideeffect.danger")
        # Should contain type annotations from AST
        assert "name: str" in danger["content"]

    def test_dry_run_vs_normal_same_modules(self, side_effect_package):
        """--dry-run and normal mode must discover the same modules."""
        pkg_name, tmp_path = side_effect_package
        dry_results = init_project(pkg_name, dry_run=True, output_dir=str(tmp_path / "tests_dry"))
        normal_results = init_project(
            pkg_name, dry_run=False, output_dir=str(tmp_path / "tests_normal")
        )
        dry_modules = sorted(r["module"] for r in dry_results)
        normal_modules = sorted(r["module"] for r in normal_results)
        assert dry_modules == normal_modules


# ============================================================================
# generate_starter_tests: dry_run flag
# ============================================================================


class TestGenerateStarterDryRun:
    def test_dry_run_no_import(self, side_effect_package, capsys):
        """generate_starter_tests(dry_run=True) must not import."""
        pkg_name, _ = side_effect_package
        content = generate_starter_tests(f"{pkg_name}.danger", dry_run=True)
        captured = capsys.readouterr()
        assert "!!! SIDE EFFECT" not in captured.out
        assert content  # should still produce output

    def test_normal_mode_does_import(self, side_effect_package, capsys):
        """generate_starter_tests(dry_run=False) DOES import (baseline)."""
        pkg_name, _ = side_effect_package
        generate_starter_tests(f"{pkg_name}.danger", dry_run=False)
        captured = capsys.readouterr()
        assert "!!! SIDE EFFECT" in captured.out


# ============================================================================
# Static discovery helpers
# ============================================================================


class TestStaticDiscovery:
    def test_discover_modules_static(self, side_effect_package):
        """Filesystem walk finds all modules without importing."""
        pkg_name, _ = side_effect_package
        modules = _discover_modules_static(pkg_name)
        assert pkg_name in modules
        assert f"{pkg_name}.danger" in modules
        assert f"{pkg_name}.nested.deep" in modules

    def test_discover_callables_static(self, side_effect_package):
        """AST parsing finds public callables without importing."""
        _, tmp_path = side_effect_package
        source_file = str(tmp_path / "sideeffect" / "danger.py")
        callables = _discover_callables_static(source_file)
        names = [name for name, sig in callables]
        assert "create_branch" in names
        assert "delete_files" in names

    def test_signature_extraction(self, side_effect_package):
        """AST signature extraction captures types and defaults."""
        _, tmp_path = side_effect_package
        source_file = str(tmp_path / "sideeffect" / "danger.py")
        callables = _discover_callables_static(source_file)
        sigs = {name: sig for name, sig in callables}
        assert "name: str" in sigs["create_branch"]
        assert "-> bool" in sigs["create_branch"]
        assert "force: bool = False" in sigs["delete_files"]

    def test_private_functions_excluded(self, tmp_path):
        """AST discovery skips _private functions."""
        source = tmp_path / "mod.py"
        source.write_text(
            "def public(x: int) -> int:\n    return x\n\n"
            "def _private(x: int) -> int:\n    return x\n"
        )
        callables = _discover_callables_static(str(source))
        names = [name for name, sig in callables]
        assert "public" in names
        assert "_private" not in names

    def test_empty_module(self, tmp_path):
        """AST discovery returns empty list for module with no callables."""
        source = tmp_path / "empty.py"
        source.write_text("X = 42\n_Y = 'hello'\n")
        callables = _discover_callables_static(str(source))
        assert callables == []
