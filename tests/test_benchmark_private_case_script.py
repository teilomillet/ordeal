"""Smoke tests for the private benchmark case harvest helper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_harvest_private_case_prefers_non_test_python_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    tests_dir = repo / "tests"
    pkg.mkdir(parents=True)
    tests_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    (pkg / "mod.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
    (tests_dir / "test_mod.py").write_text(
        "def test_run() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    (pkg / "mod.py").write_text("def run() -> int:\n    return 2\n", encoding="utf-8")
    (tests_dir / "test_mod.py").write_text(
        "def test_run() -> None:\n    assert 2 == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fix bug"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    fix_commit = _git(repo, "rev-parse", "HEAD").strip()
    script = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "harvest_private_bug_benchmark_case.py"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(repo),
            "--name",
            "demo_case",
            "--workspace",
            "../private/demo_case",
            "--module",
            "pkg.mod",
            "--fixed-commit",
            "HEAD",
            "--oracle-url",
            f"https://example.com/project/commit/{fix_commit}",
            "--failure-command",
            "pytest tests/test_mod.py -q",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert 'name = "demo_case"' in proc.stdout
    assert 'expected_files = ["pkg/mod.py"]' in proc.stdout
    assert 'expected_files = ["tests/test_mod.py"]' not in proc.stdout
    assert f'fix_commit = "{fix_commit}"' in proc.stdout
    assert 'expected_outcome = "bug"' in proc.stdout
    assert 'pair_id = "demo_case"' in proc.stdout
    assert f'oracle_url = "https://example.com/project/commit/{fix_commit}"' in proc.stdout
