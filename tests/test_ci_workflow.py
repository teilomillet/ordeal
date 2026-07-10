"""Tests for the --ci flag in ordeal init."""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

from ordeal.cli import _generate_ci_workflow


class TestGenerateCIWorkflow:
    def test_basic_structure(self):
        yml = _generate_ci_workflow("myapp")
        assert "name: ordeal" in yml
        assert "\njobs:\n  ordeal:\n    runs-on: ubuntu-latest\n" in yml
        assert "    steps:\n" in yml
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
        assert "actions/checkout@v7" in yml
        assert "actions/setup-python@v6" in yml
        assert "astral-sh/setup-uv@v7" in yml
        assert 'version: "0.11.28"' in yml
        assert 'UV_PYTHON: "3.12"' in yml
        assert "platform.python_version().startswith" in yml

    def test_triggers(self):
        yml = _generate_ci_workflow("pkg")
        assert "push:" in yml
        assert "pull_request:" in yml


class TestCheckedInCIWorkflow:
    def test_checked_in_workflows_use_current_checkout_action(self):
        workflows = Path(".github/workflows").glob("*.yml")
        for path in workflows:
            yml = path.read_text(encoding="utf-8")
            assert "actions/checkout@v6" not in yml, path
            if "actions/checkout@" in yml:
                assert "actions/checkout@v7" in yml, path

    def test_docs_uses_current_pages_artifact_action(self):
        yml = Path(".github/workflows/docs.yml").read_text(encoding="utf-8")

        assert "actions/upload-pages-artifact@v4" not in yml
        assert "actions/upload-pages-artifact@v5" in yml

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

    def test_lint_job_is_read_only_and_never_autofixes(self):
        yml = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        lint_job = yml.split("\n  test:", 1)[0]

        assert "contents: read" in lint_job
        assert "ruff check ." in lint_job
        assert "ruff format --check ." in lint_job
        assert "--fix" not in lint_job
        assert "git push" not in lint_job

    def test_nightly_pipe_propagates_perf_contract_failure(self):
        yml = Path(".github/workflows/nightly.yml").read_text(encoding="utf-8")

        assert "set -o pipefail" in yml
        assert "--check" in yml
        assert "--output-json perf-results.json" in yml
        assert "| tee perf-summary.txt" in yml
        assert yml.count("uv run ordeal benchmark") == 1

    def test_uv_workflows_pin_uv_and_select_the_declared_python(self):
        workflow_paths = [
            Path(".github/workflows/ci.yml"),
            Path(".github/workflows/docs.yml"),
            Path(".github/workflows/nightly.yml"),
            Path(".github/workflows/perf-nightly.yml"),
        ]
        runtime_assertion = (
            "assert platform.python_version().startswith(os.environ['UV_PYTHON'] + '.')"
        )
        for path in workflow_paths:
            yml = path.read_text(encoding="utf-8")
            assert 'version: "0.11.28"' in yml, path
            jobs_body = yml.split("\njobs:\n", 1)[1]
            job_matches = list(re.finditer(r"(?m)^  ([a-zA-Z0-9_-]+):\n", jobs_body))
            for index, match in enumerate(job_matches):
                end = (
                    job_matches[index + 1].start()
                    if index + 1 < len(job_matches)
                    else len(jobs_body)
                )
                job = jobs_body[match.start() : end]
                if "astral-sh/setup-uv@v7" not in job:
                    continue
                assert "UV_PYTHON:" in job, (path, match.group(1))
                assert 'version: "0.11.28"' in job, (path, match.group(1))
                assert runtime_assertion in job, (path, match.group(1))

        ci = workflow_paths[0].read_text(encoding="utf-8")
        perf = workflow_paths[-1].read_text(encoding="utf-8")
        assert "UV_PYTHON: ${{ matrix.python-version }}" in ci
        assert "UV_PYTHON: ${{ matrix.python-version }}" in perf

    def test_repo_pins_uv_and_tests_supported_python_range(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        assert 'requires-python = ">=3.12"' in pyproject
        assert 'required-version = "==0.11.28"' in pyproject
        assert 'python-version: ["3.12", "3.13", "3.14"]' in workflow

    def test_ci_verifies_pinned_httpie_evidence(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        assert "bug-evidence-httpie:" in workflow
        assert "--verify-evidence benchmarks/evidence/httpie-3.toml" in workflow
        assert "--online-sources" in workflow
        assert "--output-json .artifacts/httpie-3-evidence.json" in workflow
        assert "name: httpie-3-evidence" in workflow

    def test_ci_verifies_pinned_pysnooper_evidence(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        assert "bug-evidence-pysnooper:" in workflow
        assert "--verify-evidence benchmarks/evidence/pysnooper-3.toml" in workflow
        assert "--output-json .artifacts/pysnooper-3-evidence.json" in workflow
        assert "name: pysnooper-3-evidence" in workflow

    def test_ci_verifies_pinned_tornado_evidence(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        assert "bug-evidence-tornado:" in workflow
        assert "--verify-evidence benchmarks/evidence/tornado-14.toml" in workflow
        assert "--output-json .artifacts/tornado-14-evidence.json" in workflow
        assert "name: tornado-14-evidence" in workflow

    def test_real_project_evidence_closure_blocks_publication(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

        assert "evidence-closure-real-projects:" in workflow
        assert "- evidence-closure-real-projects" in workflow
        assert "scripts/verify_evidence_closure_real_projects.py" in workflow
        assert "name: evidence-closure-real-projects" in workflow
        assert "scripts/verify_evidence_closure_real_projects.py" in release
        assert "tests/test_evidence_closure_real_projects.py" in release
