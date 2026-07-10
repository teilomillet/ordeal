from __future__ import annotations
# ruff: noqa
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
