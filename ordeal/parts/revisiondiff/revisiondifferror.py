from __future__ import annotations
# ruff: noqa
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from ordeal.finding_evidence import _build_divergence_evidence
RevisionDiffStatus = Literal["divergent", "no_divergence_observed", "inconclusive"]
class RevisionDiffError(RuntimeError):
    """Raised when an isolated revision comparison cannot be executed."""
@dataclass(frozen=True)
class RevisionRuntime:
    """Execution evidence for one side of a revision diff."""

    ref: str
    commit: str
    pid: int
    worktree: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe runtime record."""
        return {
            "ref": self.ref,
            "commit": self.commit,
            "pid": self.pid,
            "worktree": self.worktree,
        }
@dataclass(frozen=True)
class RevisionMismatch:
    """One same-input behavior mismatch across two revisions."""

    args: Any
    canonical_args: dict[str, Any]
    replay_args: object
    base: dict[str, Any]
    candidate: dict[str, Any]
    artifact: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mismatch record."""
        return {
            "args": self.args,
            "canonical_args": self.canonical_args,
            "replay_args": self.replay_args,
            "base": self.base,
            "candidate": self.candidate,
            "artifact": self.artifact,
        }
@dataclass(frozen=True)
class RevisionFunctionDiff:
    """Measured comparison for one function shared by both revisions."""

    name: str
    base_signature: str
    candidate_signature: str
    total: int
    mismatch_count: int
    mismatches: tuple[RevisionMismatch, ...] = ()
    blocked_reason: str | None = None

    @property
    def signature_changed(self) -> bool:
        """Whether the callable signature changed across revisions."""
        return self.base_signature != self.candidate_signature

    @property
    def supported_mismatch_count(self) -> int:
        """Return mismatches whose full replay and source bindings verified."""
        return sum(
            1 for mismatch in self.mismatches if mismatch.artifact.get("status") == "supported"
        )

    @property
    def status(self) -> RevisionDiffStatus:
        """Return the claim-scoped status for this function."""
        if self.blocked_reason is not None:
            return "inconclusive"
        if self.signature_changed or self.supported_mismatch_count:
            return "divergent"
        if self.mismatch_count:
            return "inconclusive"
        return "no_divergence_observed"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe function result."""
        return {
            "name": self.name,
            "status": self.status,
            "base_signature": self.base_signature,
            "candidate_signature": self.candidate_signature,
            "signature_changed": self.signature_changed,
            "total": self.total,
            "mismatch_count": self.mismatch_count,
            "supported_mismatch_count": self.supported_mismatch_count,
            "mismatches": [mismatch.to_dict() for mismatch in self.mismatches],
            "blocked_reason": self.blocked_reason,
        }
@dataclass(frozen=True)
class RevisionDiffResult:
    """Result of comparing one target across two isolated Git revisions."""

    target: str
    base: RevisionRuntime
    candidate: RevisionRuntime
    functions: tuple[RevisionFunctionDiff, ...]
    added_functions: tuple[str, ...] = ()
    removed_functions: tuple[str, ...] = ()
    candidate_resolution_error: str | None = None
    max_examples: int = 100
    seed: int = 42
    rtol: float | None = None
    atol: float | None = None
    replay_attempts: int = 2
    mode: Literal["function", "system"] = "function"
    system_sequence: tuple[dict[str, Any], ...] = ()
    include_private: bool = False
    fixture_registries: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        """Return the number of same-input examples evaluated."""
        return sum(function.total for function in self.functions)

    @property
    def mismatch_count(self) -> int:
        """Return the number of observed behavior mismatches."""
        return sum(function.mismatch_count for function in self.functions)

    @property
    def supported_mismatch_count(self) -> int:
        """Return the number of source-bound, fully replayed mismatches."""
        return sum(function.supported_mismatch_count for function in self.functions)

    @property
    def isolated(self) -> bool:
        """Whether the two workers ran from distinct worktrees."""
        return self.base.worktree != self.candidate.worktree

    @property
    def status(self) -> RevisionDiffStatus:
        """Return a bounded claim; sampled agreement is never equivalence."""
        if self.candidate_resolution_error is not None or any(
            function.status == "inconclusive" for function in self.functions
        ):
            return "inconclusive"
        if (
            self.added_functions
            or self.removed_functions
            or any(function.status == "divergent" for function in self.functions)
        ):
            return "divergent"
        if not self.functions:
            return "inconclusive"
        return "no_divergence_observed"

    @property
    def no_divergence_found(self) -> bool:
        """Whether no divergence was observed within the measured scope."""
        return self.status == "no_divergence_observed"

    @property
    def artifacts(self) -> tuple[dict[str, Any], ...]:
        """Return every source-bound runtime divergence artifact."""
        return tuple(
            mismatch.artifact for function in self.functions for mismatch in function.mismatches
        )

    def summary(self) -> str:
        """Return a concise human-readable revision comparison."""
        label = self.status.replace("_", " ").upper()
        lines = [
            f"diff {self.target}: {label}",
            f"  base:      {self.base.ref} ({self.base.commit[:12]})",
            f"  candidate: {self.candidate.ref} ({self.candidate.commit[:12]})",
            (
                "  isolation: separate worktrees and subprocesses "
                f"(pids {self.base.pid}, {self.candidate.pid})"
            ),
            (
                f"  measured: {len(self.functions)} function(s), {self.total} example(s), "
                f"{self.mismatch_count} mismatch(es), "
                f"{self.supported_mismatch_count} replay-supported"
            ),
        ]
        if self.added_functions:
            lines.append(f"  added functions: {', '.join(self.added_functions)}")
        if self.removed_functions:
            lines.append(f"  removed functions: {', '.join(self.removed_functions)}")
        if self.candidate_resolution_error:
            lines.append(f"  candidate blocked: {self.candidate_resolution_error}")
        for function in self.functions:
            function_label = function.status.replace("_", " ").upper()
            if function.blocked_reason:
                lines.append(f"  {function.name}: {function_label} — {function.blocked_reason}")
                continue
            signature_note = (
                " (signature changed)"
                if function.base_signature != function.candidate_signature
                else ""
            )
            lines.append(
                f"  {function.name}: {function_label} "
                f"({function.total} examples, {function.mismatch_count} mismatches)"
                f"{signature_note}"
            )
            for mismatch in function.mismatches[:3]:
                lines.append(f"    args:      {_truncate(mismatch.args)}")
                lines.append(f"    base:      {_truncate(mismatch.base)}")
                lines.append(f"    candidate: {_truncate(mismatch.candidate)}")
            if function.mismatch_count > 3:
                lines.append(f"    ... and {function.mismatch_count - 3} more")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON artifact payload."""
        return {
            "schema_version": 1,
            "tool": "ordeal diff",
            "mode": self.mode,
            "target": self.target,
            "status": self.status,
            "claim": (
                "No divergence was observed within the generated same-input sample. "
                "This is not a proof of equivalence."
                if self.status == "no_divergence_observed"
                else "The result is bounded to the recorded target, revisions, and inputs."
            ),
            "isolated": self.isolated,
            "base": self.base.to_dict(),
            "candidate": self.candidate.to_dict(),
            "settings": {
                "max_examples": self.max_examples,
                "seed": self.seed,
                "rtol": self.rtol,
                "atol": self.atol,
                "replay_attempts": self.replay_attempts,
                "include_private": self.include_private,
                "fixture_registries": list(self.fixture_registries),
            },
            "totals": {
                "functions": len(self.functions),
                "examples": self.total,
                "mismatches": self.mismatch_count,
                "supported_mismatches": self.supported_mismatch_count,
            },
            "added_functions": list(self.added_functions),
            "removed_functions": list(self.removed_functions),
            "candidate_resolution_error": self.candidate_resolution_error,
            "functions": [function.to_dict() for function in self.functions],
            "artifacts": list(self.artifacts),
            "system_sequence": list(self.system_sequence),
        }
def _truncate(value: Any, limit: int = 160) -> str:
    """Return a bounded representation for terminal summaries."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."
def _git(
    repo: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one non-interactive Git command in *repo*."""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RevisionDiffError(f"git {' '.join(arguments)} failed: {detail}")
    return completed
def _git_root(repo: str | os.PathLike[str] | None) -> Path:
    """Resolve the containing Git worktree root."""
    start = Path(repo or Path.cwd()).resolve()
    completed = _git(start, "rev-parse", "--show-toplevel")
    return Path(completed.stdout.strip()).resolve()
def _resolve_commit(repo: Path, ref: str) -> str:
    """Resolve *ref* to one commit without accepting option-like refs."""
    cleaned = str(ref).strip()
    if not cleaned:
        raise RevisionDiffError("revision ref cannot be empty")
    completed = _git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{cleaned}^{{commit}}",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RevisionDiffError(f"cannot resolve Git ref {cleaned!r}: {detail}")
    return completed.stdout.strip()
def default_base_ref(repo: str | os.PathLike[str] | None = None) -> str:
    """Choose the remote default branch, main branch, or commit parent."""
    repo_root = _git_root(repo)
    symbolic = _git(
        repo_root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
    )
    candidates = [
        symbolic.stdout.strip(),
        "origin/main",
        "origin/master",
        "main",
        "master",
        "HEAD^",
    ]
    for candidate in dict.fromkeys(item for item in candidates if item):
        try:
            _resolve_commit(repo_root, candidate)
        except RevisionDiffError:
            continue
        return candidate
    raise RevisionDiffError("could not infer a base revision; pass --base-ref")
def _worker_command(
    *,
    mode: str,
    target: str,
    payload_path: Path,
    result_path: Path,
    max_examples: int,
    seed: int,
    include_private: bool,
    fixture_registries: Sequence[str],
    rtol: float | None = None,
    atol: float | None = None,
    replay_attempts: int = 2,
    system_sequence: Sequence[Mapping[str, Any]] | None = None,
    exact_cases: Mapping[str, Sequence[object]] | None = None,
) -> list[str]:
    """Build one private revision-worker command."""
    worker = Path(__file__).with_name("_diff_worker.py").resolve()
    command = [
        sys.executable,
        str(worker),
        mode,
        "--target",
        target,
        "--payload",
        str(payload_path),
        "--result",
        str(result_path),
        "--max-examples",
        str(max_examples),
        "--seed",
        str(seed),
        "--fixture-registries",
        json.dumps(list(fixture_registries)),
        "--replay-attempts",
        str(replay_attempts),
    ]
    if include_private:
        command.append("--include-private")
    if rtol is not None:
        command.extend(("--rtol", str(rtol)))
    if atol is not None:
        command.extend(("--atol", str(atol)))
    if system_sequence is not None:
        command.extend(("--system-sequence", json.dumps(list(system_sequence))))
    if exact_cases is not None:
        command.extend(("--exact-cases", json.dumps(exact_cases)))
    return command
def _run_worker(command: Sequence[str], *, cwd: Path, label: str) -> None:
    """Run one isolated worker and surface a bounded diagnostic on failure."""
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout).strip()
    if len(detail) > 2000:
        detail = detail[-2000:]
    raise RevisionDiffError(f"{label} worker failed: {detail or 'no diagnostic output'}")
def _runtime(
    payload: dict[str, Any],
    *,
    ref: str,
    commit: str,
) -> RevisionRuntime:
    """Build public runtime evidence from private worker output."""
    return RevisionRuntime(
        ref=ref,
        commit=commit,
        pid=int(payload["pid"]),
        worktree=str(payload["worktree"]),
    )
