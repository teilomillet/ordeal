"""Tests for the --ci flag in ordeal init."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from ordeal.cli import _generate_ci_workflow


class TestGenerateCIWorkflow:
    def test_basic_structure(self):
        yml = _generate_ci_workflow("myapp")
        assert "name: ordeal" in yml
        assert "\njobs:\n  ordeal:\n    runs-on: ubuntu-latest\n    steps:\n" in yml
        assert "pytest --chaos" in yml
        assert "ordeal mutate myapp" in yml
        assert "--preset standard" in yml
        assert "--threshold 0.8" in yml

    def test_uses_uv_when_lock_exists(self, tmp_path):
        lock = tmp_path / "uv.lock"
        lock.write_text("")
        with patch.object(Path, "exists", return_value=True):
            yml = _generate_ci_workflow("myapp")
        assert "uv lock --check" in yml
        assert "uv sync --locked --extra dev" in yml
        assert "uv run pytest" in yml
        assert "uv run ordeal" in yml

    def test_uses_pip_when_no_lock(self, tmp_path):
        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            yml = _generate_ci_workflow("myapp")
        finally:
            os.chdir(orig_cwd)
        assert "pip install" in yml
        assert "uv sync --locked" not in yml

    def test_package_name_in_mutate(self):
        yml = _generate_ci_workflow("my_special_lib")
        assert "ordeal mutate my_special_lib" in yml

    def test_checkout_and_python_setup(self):
        yml = _generate_ci_workflow("pkg")
        assert "actions/checkout@v6" in yml
        assert "actions/setup-python@v6" in yml
        assert "astral-sh/setup-uv@v7" in yml

    def test_triggers(self):
        yml = _generate_ci_workflow("pkg")
        assert "push:" in yml
        assert "pull_request:" in yml


class TestCheckedInCIWorkflow:
    def test_checked_in_workflows_use_node24_safe_artifact_actions(self):
        workflows = Path(".github/workflows").glob("*.yml")
        for path in workflows:
            yml = path.read_text(encoding="utf-8")
            assert "actions/upload-artifact@v4" not in yml, path

    def test_bump_and_publish_refreshes_lockfile(self):
        yml = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        assert "bump-and-publish:" in yml
        assert "uses: astral-sh/setup-uv@v7" in yml
        assert "- name: Refresh lockfile" in yml
        assert "run: uv lock" in yml
        assert "git add pyproject.toml uv.lock CHANGELOG.md" in yml

    def test_bump_and_publish_refreshes_lockfile_before_commit(self):
        yml = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        refresh_idx = yml.index("- name: Refresh lockfile")
        changelog_idx = yml.index("- name: Update changelog")
        commit_idx = yml.index("- name: Commit and tag")
        assert refresh_idx < changelog_idx < commit_idx
        assert yml.index("run: uv lock", refresh_idx) < commit_idx
