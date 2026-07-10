"""Git-revision runner behind the public ``ordeal diff`` CLI.

The in-process public API remains :func:`ordeal.diff.diff`.  This module adds
the revision/worktree orchestration needed by the CLI without creating a
second user-facing ``refactor`` concept.
"""

from __future__ import annotations

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


def _result_from_payload(
    payload: dict[str, Any],
    *,
    target: str,
    base_ref: str,
    base_commit: str,
    candidate_ref: str,
    candidate_commit: str,
    max_examples: int,
    seed: int,
    rtol: float | None,
    atol: float | None,
    replay_attempts: int,
    include_private: bool,
    fixture_registries: Sequence[str],
) -> RevisionDiffResult:
    """Convert worker JSON into public result objects."""
    comparison = dict(payload.get("comparison", {}))
    functions: list[RevisionFunctionDiff] = []
    for item in payload.get("functions", []):
        base_source = dict(item.get("base_source", {}))
        base_source.update({"role": "base", "ref": base_ref, "commit": base_commit})
        candidate_source = dict(item.get("candidate_source", {}))
        candidate_source.update(
            {"role": "candidate", "ref": candidate_ref, "commit": candidate_commit}
        )
        mismatches: list[RevisionMismatch] = []
        for mismatch in item.get("mismatches", []):
            base_observation = dict(mismatch["base"])
            candidate_observation = dict(mismatch["candidate"])
            differences: list[str] = []
            if any(
                base_observation.get(field) != candidate_observation.get(field)
                for field in ("kind", "return_value", "exception")
            ):
                differences.append("return_or_exception")
            if base_observation.get("mutated_arguments") != candidate_observation.get(
                "mutated_arguments"
            ):
                differences.append("mutated_arguments")
            replay = dict(mismatch.get("replay", {}))
            minimization = dict(mismatch.get("minimization", {}))
            original_observation_a = dict(mismatch.get("original_base", base_observation))
            original_observation_b = dict(
                mismatch.get("original_candidate", candidate_observation)
            )
            artifact = _build_divergence_evidence(
                revisions={"a": base_source, "b": candidate_source},
                comparison=comparison,
                original_input=mismatch.get("original_args", mismatch["args"]),
                minimized_input=mismatch["args"],
                original_input_canonical=mismatch.get(
                    "original_canonical_args",
                    mismatch.get("canonical_args"),
                ),
                minimized_input_canonical=mismatch.get("canonical_args"),
                original_observations={
                    "a": original_observation_a,
                    "b": original_observation_b,
                },
                observations={"a": base_observation, "b": candidate_observation},
                differences=differences or ["outcome_envelope"],
                replay_attempts=int(replay.get("attempts", 0)),
                replay_matches=int(replay.get("exact_matches", 0)),
                expected_signature=str(replay.get("expected_signature", "")),
                observed_signatures=list(replay.get("observed_signatures", [])),
                witness_source="canonical_same_input_revision_sample",
                minimization_method=str(
                    minimization.get("method", "canonical observed-case shrinking")
                ),
                minimization_boundary=str(
                    minimization.get(
                        "boundary",
                        "Canonical selection was bounded to observed generated cases.",
                    )
                ),
            )
            mismatches.append(
                RevisionMismatch(
                    args=mismatch["args"],
                    canonical_args=dict(mismatch.get("canonical_args", {})),
                    replay_args=mismatch.get("replay_args"),
                    base=base_observation,
                    candidate=candidate_observation,
                    artifact=artifact,
                )
            )
        functions.append(
            RevisionFunctionDiff(
                name=str(item["name"]),
                base_signature=str(item["base_signature"]),
                candidate_signature=str(item["candidate_signature"]),
                total=int(item["total"]),
                mismatch_count=int(item["mismatch_count"]),
                mismatches=tuple(mismatches),
                blocked_reason=item.get("blocked_reason"),
            )
        )
    return RevisionDiffResult(
        target=target,
        base=_runtime(payload["base_runtime"], ref=base_ref, commit=base_commit),
        candidate=_runtime(
            payload["candidate_runtime"],
            ref=candidate_ref,
            commit=candidate_commit,
        ),
        functions=tuple(functions),
        added_functions=tuple(str(name) for name in payload.get("added_functions", [])),
        removed_functions=tuple(str(name) for name in payload.get("removed_functions", [])),
        candidate_resolution_error=payload.get("candidate_resolution_error"),
        max_examples=max_examples,
        seed=seed,
        rtol=rtol,
        atol=atol,
        replay_attempts=replay_attempts,
        mode="system" if payload.get("execution_mode") == "system" else "function",
        system_sequence=tuple(
            dict(item) for item in payload.get("system_sequence", []) if isinstance(item, Mapping)
        ),
        include_private=include_private,
        fixture_registries=tuple(fixture_registries),
    )


_REVISION_REGRESSION_TEST = "test_ordeal_revision_diff_regression"


def replay_revision_regression_case(case: Mapping[str, Any]) -> None:
    """Re-run one pinned base-to-current revision witness in CI."""
    sequence = case.get("system_sequence")
    if sequence is not None and not isinstance(sequence, list):
        raise TypeError("revision regression system_sequence must be a list")
    exact_cases = case.get("exact_cases")
    if exact_cases is not None and not isinstance(exact_cases, Mapping):
        raise TypeError("revision regression exact_cases must be a mapping")
    result = run_revision_diff(
        str(case["target"]),
        base_ref=str(case["base_commit"]),
        candidate_ref="HEAD",
        max_examples=int(case["max_examples"]),
        seed=int(case["seed"]),
        rtol=case.get("rtol"),
        atol=case.get("atol"),
        include_private=bool(case.get("include_private", False)),
        fixture_registries=tuple(str(item) for item in case.get("fixture_registries", [])),
        replay_attempts=int(case["replay_attempts"]),
        sequence=sequence,
        exact_cases=exact_cases,
    )
    if not result.no_divergence_found:
        raise AssertionError(f"saved Git-revision divergence is not fixed: {result.status}")


def _render_revision_regression(case: Mapping[str, Any]) -> str:
    """Render one source-bindable Git-revision regression."""
    return "\n".join(
        [
            '"""Generated by `ordeal diff --write-regression`.',
            "",
            f"Target ID: {case['id']}",
            '"""',
            "",
            "from ordeal._revision_diff import replay_revision_regression_case",
            "",
            f"CASE = {dict(case)!r}",
            "",
            "",
            f"def {_REVISION_REGRESSION_TEST}() -> None:",
            '    """Keep the pinned base-to-current change witness fixed."""',
            "    replay_revision_regression_case(CASE)",
            "",
        ]
    )


def persist_revision_regression(
    result: RevisionDiffResult,
    *,
    evidence_path: Path,
    regression_path: Path,
    manifest_path: Path,
) -> tuple[Path | None, str | None, str | None]:
    """Persist and register a replay of one divergent Git-revision result."""
    from ordeal.regression_evidence import _register_python_regression

    if result.status != "divergent" or not result.artifacts:
        return None, None, "only replay-supported runtime divergences can be persisted"
    if any(artifact.get("status") != "supported" for artifact in result.artifacts):
        return None, None, "all persisted revision divergences require supported evidence"
    exact_cases: dict[str, list[object]] | None = None
    if result.mode == "function":
        exact_cases = {}
        for function in result.functions:
            replay_cases = [mismatch.replay_args for mismatch in function.mismatches]
            if any(case is None for case in replay_cases):
                return None, None, "revision witness arguments are not exactly replayable"
            if replay_cases:
                exact_cases[function.name] = replay_cases
        if not exact_cases:
            return None, None, "revision divergence has no exact function witness"
    identity = hashlib.sha256(
        f"{result.mode}\0{result.target}\0{result.base.commit}".encode("utf-8")
    ).hexdigest()[:16]
    case: dict[str, Any] = {
        "id": f"revision:{identity}",
        "target": result.target,
        "base_commit": result.base.commit,
        "max_examples": result.max_examples,
        "seed": result.seed,
        "rtol": result.rtol,
        "atol": result.atol,
        "replay_attempts": result.replay_attempts,
        "include_private": result.include_private,
        "fixture_registries": list(result.fixture_registries),
        "system_sequence": (list(result.system_sequence) if result.mode == "system" else None),
        "exact_cases": exact_cases,
    }
    try:
        if regression_path.exists():
            existing = regression_path.read_text(encoding="utf-8")
            if not existing.startswith('"""Generated by `ordeal diff --write-regression`.'):
                raise ValueError(
                    f"refusing to overwrite non-generated regression: {regression_path}"
                )
            if f"Target ID: {case['id']}" not in existing:
                raise ValueError(
                    f"regression path already belongs to another revision diff: {regression_path}"
                )
        regression_path.parent.mkdir(parents=True, exist_ok=True)
        regression_path.write_text(
            _render_revision_regression(case),
            encoding="utf-8",
        )
        finding_id = f"fnd_revision_{identity}"
        registered, error = _register_python_regression(
            manifest_path=manifest_path,
            finding_id=finding_id,
            change_kind=("system_revision" if result.mode == "system" else "revision"),
            target=result.target,
            test_path=regression_path,
            test_name=_REVISION_REGRESSION_TEST,
            evidence_path=evidence_path,
            change_artifact_ids=[
                str(artifact.get("artifact_id")) for artifact in result.artifacts
            ],
            test_basis="pinned_base_to_current_revision_witness",
            extra={"base_commit": result.base.commit},
        )
        if error is not None:
            return regression_path, None, error
        assert registered is not None
        return regression_path, finding_id, None
    except (OSError, ValueError) as exc:
        return None, None, str(exc)


def run_revision_diff(
    target: str,
    *,
    base_ref: str | None = None,
    candidate_ref: str = "HEAD",
    repo: str | os.PathLike[str] | None = None,
    max_examples: int = 100,
    seed: int = 42,
    rtol: float | None = None,
    atol: float | None = None,
    include_private: bool = False,
    fixture_registries: Sequence[str] = (),
    replay_attempts: int = 2,
    sequence: Sequence[Mapping[str, Any]] | None = None,
    exact_cases: Mapping[str, Sequence[object]] | None = None,
) -> RevisionDiffResult:
    """Compare one target across two detached worktrees and subprocesses.

    Pass a JSON-safe operation/fault ``sequence`` to treat *target* as a
    zero-argument system factory. Fault events call ``system.apply_fault(event)``.
    """
    if max_examples < 1:
        raise ValueError("max_examples must be at least 1")
    if rtol is not None and rtol < 0:
        raise ValueError("rtol must be non-negative")
    if atol is not None and atol < 0:
        raise ValueError("atol must be non-negative")
    if replay_attempts < 1:
        raise ValueError("replay_attempts must be at least 1")
    if sequence is not None and exact_cases is not None:
        raise ValueError("system sequence and exact function cases are mutually exclusive")
    if exact_cases is not None:
        if not exact_cases or not all(
            isinstance(name, str) and isinstance(cases, Sequence) and bool(cases)
            for name, cases in exact_cases.items()
        ):
            raise ValueError("exact_cases must map function names to non-empty case sequences")
    if sequence is not None:
        if rtol is not None or atol is not None:
            raise ValueError("Git-revision system mode does not accept numeric tolerances")
        for index, event in enumerate(sequence):
            if not isinstance(event, Mapping) or event.get("kind") not in {
                "operation",
                "fault",
            }:
                raise ValueError(
                    f"system sequence event {index} must be an operation or fault object"
                )

    repo_root = _git_root(repo)
    resolved_base_ref = base_ref or default_base_ref(repo_root)
    resolved_candidate_ref = candidate_ref or "HEAD"
    base_commit = _resolve_commit(repo_root, resolved_base_ref)
    candidate_commit = _resolve_commit(repo_root, resolved_candidate_ref)

    with tempfile.TemporaryDirectory(prefix="ordeal-diff-") as temporary:
        temporary_root = Path(temporary)
        base_worktree = temporary_root / "base"
        candidate_worktree = temporary_root / "candidate"
        payload_path = temporary_root / "baseline.pkl"
        baseline_meta_path = temporary_root / "baseline.json"
        comparison_path = temporary_root / "comparison.json"
        added_worktrees: list[Path] = []
        try:
            for path, commit in (
                (base_worktree, base_commit),
                (candidate_worktree, candidate_commit),
            ):
                _git(
                    repo_root,
                    "worktree",
                    "add",
                    "--detach",
                    "--quiet",
                    str(path),
                    commit,
                )
                added_worktrees.append(path)

            _run_worker(
                _worker_command(
                    mode="prepare",
                    target=target,
                    payload_path=payload_path,
                    result_path=baseline_meta_path,
                    max_examples=max_examples,
                    seed=seed,
                    include_private=include_private,
                    fixture_registries=fixture_registries,
                    replay_attempts=replay_attempts,
                    system_sequence=sequence,
                    exact_cases=exact_cases,
                ),
                cwd=base_worktree,
                label=f"base revision {resolved_base_ref}",
            )
            _run_worker(
                _worker_command(
                    mode="compare",
                    target=target,
                    payload_path=payload_path,
                    result_path=comparison_path,
                    max_examples=max_examples,
                    seed=seed,
                    include_private=include_private,
                    fixture_registries=fixture_registries,
                    rtol=rtol,
                    atol=atol,
                    replay_attempts=replay_attempts,
                    system_sequence=sequence,
                    exact_cases=exact_cases,
                ),
                cwd=candidate_worktree,
                label=f"candidate revision {resolved_candidate_ref}",
            )
            comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
            return _result_from_payload(
                comparison,
                target=target,
                base_ref=resolved_base_ref,
                base_commit=base_commit,
                candidate_ref=resolved_candidate_ref,
                candidate_commit=candidate_commit,
                max_examples=max_examples,
                seed=seed,
                rtol=rtol,
                atol=atol,
                replay_attempts=replay_attempts,
                include_private=include_private,
                fixture_registries=fixture_registries,
            )
        finally:
            for worktree in reversed(added_worktrees):
                _git(repo_root, "worktree", "remove", "--force", str(worktree), check=False)
            _git(repo_root, "worktree", "prune", check=False)
