#!/usr/bin/env python3
"""Generate one private bug benchmark case block from a git fix commit."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _resolve_commit(repo: Path, rev: str) -> str:
    """Resolve *rev* to a full commit SHA."""
    return _git(repo, "rev-parse", rev).strip()


def _changed_python_files(repo: Path, base: str, head: str) -> list[str]:
    """Return changed Python files, preferring non-test files when present."""
    stdout = _git(repo, "diff", "--name-only", base, head, "--", "*.py")
    changed = [line.strip().replace("\\", "/") for line in stdout.splitlines() if line.strip()]
    if not changed:
        raise ValueError("No changed Python files found in the selected commit range")
    preferred = [
        path
        for path in changed
        if not path.startswith("tests/")
        and "/tests/" not in path
        and not path.startswith("test_")
        and "/test_" not in path
    ]
    return preferred or changed


def _toml_string(value: str) -> str:
    """Return *value* as a TOML string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_list(values: list[str]) -> str:
    """Return *values* as a TOML list literal."""
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def build_case_block(args: argparse.Namespace) -> str:
    """Build one TOML case block from parsed CLI args."""
    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise ValueError(f"Repository does not exist: {repo}")
    fixed_commit = _resolve_commit(repo, args.fixed_commit)
    buggy_rev = args.buggy_commit or f"{fixed_commit}^"
    buggy_commit = _resolve_commit(repo, buggy_rev)
    expected_files = _changed_python_files(repo, buggy_commit, fixed_commit)

    lines = [
        "[[cases]]",
        f"name = {_toml_string(args.name)}",
        f"workspace = {_toml_string(args.workspace)}",
        f"module = {_toml_string(args.module)}",
        f"expected_outcome = {_toml_string(args.expected_outcome)}",
        f"pair_id = {_toml_string(args.pair_id or args.name)}",
        f"expected_files = {_toml_list(expected_files)}",
    ]
    if args.expected_target:
        lines.append(f"expected_targets = {_toml_list(args.expected_target)}")
    if args.pythonpath:
        lines.append(f"pythonpath = {_toml_list(args.pythonpath)}")
    lines.extend(
        [
            f"selection_reason = {_toml_string(args.selection_reason)}",
            f"harvested_at = {_toml_string(args.harvested_at)}",
            f"fix_commit = {_toml_string(fixed_commit)}",
            f"oracle_url = {_toml_string(args.oracle_url)}",
            f"failure_command = {_toml_string(args.failure_command)}",
        ]
    )
    if args.notes:
        lines.append(f"notes = {_toml_string(args.notes)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the private-case harvest CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Generate one private bug benchmark TOML block from a repository and "
            "a known fix commit."
        )
    )
    parser.add_argument("--repo", required=True, help="Path to the local git repository")
    parser.add_argument("--name", required=True, help="Benchmark case name")
    parser.add_argument("--workspace", required=True, help="Workspace path to record in TOML")
    parser.add_argument("--module", required=True, help="Importable module to scan")
    parser.add_argument("--fixed-commit", required=True, help="Fix commit SHA or rev")
    parser.add_argument(
        "--oracle-url",
        required=True,
        help="Immutable HTTPS commit URL used to verify the fix provenance",
    )
    parser.add_argument(
        "--expected-outcome",
        choices=["bug", "clean"],
        default="bug",
        help="Whether this checkout is the buggy positive or fixed negative control",
    )
    parser.add_argument(
        "--pair-id",
        default=None,
        help="Shared ID joining buggy and fixed blocks (default: --name)",
    )
    parser.add_argument(
        "--buggy-commit",
        default=None,
        help="Buggy/base commit SHA or rev (default: <fixed-commit>^)",
    )
    parser.add_argument(
        "--expected-target",
        action="append",
        default=[],
        help="Optional callable target that should count as a hit (repeatable)",
    )
    parser.add_argument(
        "--pythonpath",
        action="append",
        default=[],
        help="Optional extra PYTHONPATH entry to record in the manifest (repeatable)",
    )
    parser.add_argument(
        "--selection-reason",
        default="Holdout bug harvested after the current prompt/model stack.",
        help="Why this case belongs in the private benchmark track",
    )
    parser.add_argument(
        "--failure-command",
        default="pytest -q",
        help="Local command that reproduces the oracle",
    )
    parser.add_argument(
        "--harvested-at",
        default=date.today().isoformat(),
        help="Acquisition date to record in the case metadata",
    )
    parser.add_argument("--notes", default=None, help="Optional note to append")
    parser.add_argument(
        "--append-manifest",
        default=None,
        help="Optional TOML file to append the generated block to",
    )
    args = parser.parse_args(argv)

    try:
        block = build_case_block(args)
    except (subprocess.CalledProcessError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    if args.append_manifest:
        path = Path(args.append_manifest)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "\n" if existing and not existing.endswith("\n\n") else ""
        path.write_text(existing + separator + block, encoding="utf-8")
    else:
        sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
