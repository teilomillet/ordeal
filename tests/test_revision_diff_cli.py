"""Integration coverage for the revision-isolated ``ordeal diff`` CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ordeal._revision_diff import run_revision_diff
from ordeal.cli import main
from ordeal.config import load_config


def _git(repo: Path, *args: str) -> str:
    """Run Git in one temporary test repository."""
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo_with_two_versions(tmp_path: Path, *, candidate_expression: str) -> tuple[Path, str]:
    """Create two committed versions of a small typed scoring module."""
    repo = tmp_path / "project"
    package = repo / "samplepkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "scoring.py").write_text(
        "def score(x: int) -> int:\n    return x + 1\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Ordeal Test")
    _git(repo, "config", "user.email", "ordeal@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_commit = _git(repo, "rev-parse", "HEAD")

    (package / "scoring.py").write_text(
        f"def score(x: int) -> int:\n    return {candidate_expression}\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")
    return repo, base_commit


def test_revision_diff_replays_same_inputs_in_distinct_worktrees(tmp_path: Path) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="x + 2")

    result = run_revision_diff(
        "samplepkg.scoring.score",
        repo=repo,
        base_ref=base_commit,
        candidate_ref="HEAD",
        max_examples=20,
        seed=7,
    )

    assert result.status == "divergent"
    assert result.mismatch_count > 0
    assert result.base.commit == base_commit
    assert result.base.worktree != result.candidate.worktree
    assert result.base.pid != result.candidate.pid
    assert not Path(result.base.worktree).exists()
    assert not Path(result.candidate.worktree).exists()
    function = result.functions[0]
    assert function.name == "score"
    assert (
        function.mismatches[0].base["return_value"]
        != function.mismatches[0].candidate["return_value"]
    )
    artifact = function.mismatches[0].artifact
    assert artifact["schema"] == "ordeal.divergence-evidence/v1"
    assert artifact["status"] == "supported"
    assert artifact["source_binding"] == {"status": "complete", "missing": []}
    assert artifact["revisions"]["a"]["commit"] == base_commit
    assert artifact["revisions"]["b"]["commit"] == result.candidate.commit
    assert len(artifact["revisions"]["a"]["source_sha256"]) == 64
    assert len(artifact["revisions"]["b"]["source_sha256"]) == 64
    assert artifact["comparison"]["comparator"]["kind"] == "exact"
    assert artifact["comparison"]["normalizer"]["kind"] == "identity"
    assert artifact["witness"]["original_input"] == artifact["witness"]["input"]
    assert artifact["observations"]["a"] == function.mismatches[0].base
    assert artifact["observations"]["b"] == function.mismatches[0].candidate
    assert artifact["replay"]["attempts"] == 2
    assert artifact["replay"]["exact_matches"] == 2
    assert "general equivalence" in " ".join(artifact["boundaries"]["does_not_establish"])
    assert result.artifacts[0] == artifact
    assert len(result.artifacts) == result.mismatch_count


def test_revision_diff_uses_bounded_no_divergence_status(tmp_path: Path) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="1 + x")

    result = run_revision_diff(
        "samplepkg.scoring",
        repo=repo,
        base_ref=base_commit,
        max_examples=12,
    )

    assert result.status == "no_divergence_observed"
    assert result.no_divergence_found
    assert "not a proof of equivalence" in result.to_dict()["claim"].lower()


def test_revision_diff_treats_signature_changes_as_surface_divergence(tmp_path: Path) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="1 + x")
    scoring = repo / "samplepkg/scoring.py"
    scoring.write_text(
        "def score(x: int, offset: int = 1) -> int:\n    return x + offset\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "signature change")

    result = run_revision_diff(
        "samplepkg.scoring",
        repo=repo,
        base_ref=base_commit,
        max_examples=8,
    )

    assert result.status == "divergent"
    assert result.mismatch_count == 0
    assert result.functions[0].signature_changed


def test_revision_diff_reports_removed_module_functions_as_divergence(tmp_path: Path) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="1 + x")
    scoring = repo / "samplepkg/scoring.py"
    scoring.write_text("def _internal() -> None:\n    return None\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "remove public scoring surface")

    result = run_revision_diff(
        "samplepkg.scoring",
        repo=repo,
        base_ref=base_commit,
        max_examples=4,
    )

    assert result.status == "divergent"
    assert result.removed_functions == ("score",)
    assert result.candidate_resolution_error is None


def test_revision_diff_fails_closed_for_unbound_instance_methods(tmp_path: Path) -> None:
    repo = tmp_path / "method-project"
    package = repo / "samplepkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    scoring = package / "scoring.py"
    scoring.write_text(
        "class Scorer:\n    def score(self, x: int) -> int:\n        return x + 1\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Ordeal Test")
    _git(repo, "config", "user.email", "ordeal@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base method")
    base_commit = _git(repo, "rev-parse", "HEAD")
    scoring.write_text(scoring.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate method")

    result = run_revision_diff(
        "samplepkg.scoring:Scorer.score",
        repo=repo,
        base_ref=base_commit,
        max_examples=4,
    )

    assert result.status == "inconclusive"
    assert "object factory/harness" in str(result.functions[0].blocked_reason)


def test_revision_diff_is_inconclusive_when_mismatch_replay_is_unstable(tmp_path: Path) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="1 + x")
    scoring = repo / "samplepkg/scoring.py"
    scoring.write_text(
        (
            "_calls = 0\n\n"
            "def score(x: int) -> int:\n"
            "    global _calls\n"
            "    _calls += 1\n"
            "    return x + _calls\n"
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "unstable candidate")

    result = run_revision_diff(
        "samplepkg.scoring.score",
        repo=repo,
        base_ref=base_commit,
        max_examples=6,
    )

    assert result.mismatch_count > 0
    assert result.supported_mismatch_count == 0
    assert result.status == "inconclusive"
    assert {artifact["status"] for artifact in result.artifacts} == {"exploratory"}


def test_diff_cli_saves_json_and_markdown_artifacts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="x + 2")
    monkeypatch.chdir(repo)

    return_code = main(
        [
            "diff",
            "samplepkg.scoring.score",
            "--base-ref",
            base_commit,
            "--candidate-ref",
            "HEAD",
            "--max-examples",
            "8",
            "--save-artifacts",
            "--json",
        ]
    )

    assert return_code == 1
    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["status"] == "divergent"
    assert stdout_payload["artifacts"][0]["status"] == "supported"
    assert stdout_payload["saved_artifacts"]["json"].endswith(
        ".ordeal/diff/samplepkg.scoring.score.json"
    )
    json_path = repo / ".ordeal/diff/samplepkg.scoring.score.json"
    markdown_path = repo / ".ordeal/diff/samplepkg.scoring.score.md"
    assert json_path.exists()
    assert markdown_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "divergent"
    assert payload["isolated"] is True
    assert payload["artifacts"][0]["schema"] == "ordeal.divergence-evidence/v1"
    assert payload["artifacts"][0]["replay"]["exact_matches"] == 2
    assert payload["commands"]["rerun"].startswith("ordeal diff")


def test_diff_cli_can_run_entirely_from_toml(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, base_commit = _repo_with_two_versions(tmp_path, candidate_expression="1 + x")
    (repo / "ordeal.toml").write_text(
        (
            "[diff]\n"
            'target = "samplepkg.scoring"\n'
            f'base_ref = "{base_commit}"\n'
            'candidate_ref = "HEAD"\n'
            "max_examples = 6\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    return_code = main(["diff"])

    assert return_code == 0
    captured = capsys.readouterr()
    assert "NO DIVERGENCE OBSERVED" in captured.out
    assert "candidate HEAD uses committed content" in captured.err


def test_diff_toml_section_loads_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "ordeal.toml"
    config_path.write_text(
        """
[diff]
target = "samplepkg.scoring"
base_ref = "origin/main"
candidate_ref = "HEAD"
max_examples = 25
seed = 9
rtol = 1e-6
include_private = true
fixture_registries = ["tests.fixtures"]
replay_attempts = 3
save_artifacts = true
artifact_dir = "artifacts/diff"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path).diff

    assert config.target == "samplepkg.scoring"
    assert config.base_ref == "origin/main"
    assert config.max_examples == 25
    assert config.seed == 9
    assert config.rtol == 1e-6
    assert config.include_private is True
    assert config.fixture_registries == ["tests.fixtures"]
    assert config.replay_attempts == 3
    assert config.save_artifacts is True
    assert config.artifact_dir == "artifacts/diff"
