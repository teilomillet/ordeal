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
        assert "pytest --chaos" in yml
        assert "ordeal mutate myapp" in yml
        assert "--preset standard" in yml
        assert "--threshold 0.8" in yml

    def test_uses_uv_when_lock_exists(self, tmp_path):
        lock = tmp_path / "uv.lock"
        lock.write_text("")
        with patch.object(Path, "exists", return_value=True):
            yml = _generate_ci_workflow("myapp")
        assert "uv sync" in yml
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
        assert "uv sync" not in yml

    def test_package_name_in_mutate(self):
        yml = _generate_ci_workflow("my_special_lib")
        assert "ordeal mutate my_special_lib" in yml

    def test_checkout_and_python_setup(self):
        yml = _generate_ci_workflow("pkg")
        assert "actions/checkout@v4" in yml
        assert "actions/setup-python@v5" in yml

    def test_triggers(self):
        yml = _generate_ci_workflow("pkg")
        assert "push:" in yml
        assert "pull_request:" in yml
