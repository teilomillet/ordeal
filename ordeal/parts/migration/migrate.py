from __future__ import annotations
# ruff: noqa
def migrate(
    base: str,
    candidate: str,
    *,
    intended_changes: Collection[str] | Mapping[str, str] = (),
    classify: ChangeClassifier | None = None,
    invariants: Mapping[str, Sequence[ContractCheck]] | None = None,
    test_dir: str = "tests",
    audit_max_examples: int = 20,
    mine_max_examples: int = 200,
    diff_max_examples: int = 100,
    scan_max_examples: int = 50,
    mutation_preset: Literal["essential", "standard", "thorough"] = "standard",
    mutation_workers: int = 1,
    mutation_threshold: float = 1.0,
    mine_fixtures: Mapping[str, Any] | None = None,
    diff_options: Mapping[str, Mapping[str, Any]] | None = None,
    scan_options: Mapping[str, Any] | None = None,
    evidence_path: str | Path | None = None,
    regression_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> MigrationResult:
    """Run the ordered evidence workflow from *base* to *candidate*.

    Unexpected divergences are saved as executable parity regressions when
    their witnesses are replayable literals. A run with a still-unexpected
    divergence deliberately blocks mutation because a mutation score is not
    meaningful while the resulting regression baseline fails. Re-run after
    fixing the candidate, or classify the change as intended.

    ``invariants`` are explicit candidate contracts keyed by public callable
    name. Each intended behavior change needs an invariant for that callable
    or fully killed mutants attributed to it. Invariant inputs are cloned for
    every mutation invocation. The final candidate scan must exercise at least
    one callable. Mined properties are returned as hypotheses only.

    Args:
        base: Importable base module path.
        candidate: Importable candidate module path.
        intended_changes: Function names or stable ``kind:function`` selectors,
            optionally mapped to human-readable reasons.
        classify: Optional callback for programmatic change classification.
        invariants: Explicit semantic checks keyed by candidate callable name.
        test_dir: Existing test directory used by the base audit.
        audit_max_examples: Example budget for the base audit.
        mine_max_examples: Example budget for candidate contract mining.
        diff_max_examples: Example budget for each shared callable diff.
        scan_max_examples: Example budget for the candidate-only scan.
        mutation_preset: Mutation operator preset for resulting tests.
        mutation_workers: Mutation worker count.
        mutation_threshold: Minimum measured mutation score, default 100%.
        mine_fixtures: Global fixture overrides for candidate mining.
        diff_options: Per-function options forwarded to :func:`ordeal.diff.diff`.
        scan_options: Additional options forwarded to candidate ``scan_module``.
        evidence_path: JSON artifact path; defaults under ``.ordeal/migrations``.
        regression_path: Generated pytest path; defaults under ``tests``.

    Returns:
        A :class:`MigrationResult` with evidence for all seven ordered stages.
    """
    if not 0.0 <= mutation_threshold <= 1.0:
        raise ValueError("mutation_threshold must be between 0 and 1")
    explicit_invariants = {str(name): list(checks) for name, checks in (invariants or {}).items()}
    explicit_invariant_count = sum(len(checks) for checks in explicit_invariants.values())
    intended = _normalize_intended_changes(intended_changes)
    evidence = (
        Path(evidence_path)
        if evidence_path is not None
        else _default_evidence_path(base, candidate)
    )
    regression = (
        Path(regression_path)
        if regression_path is not None
        else _default_regression_path(candidate)
    )
    stages: list[MigrationStage] = []

    base_audit = _audit(
        base,
        test_dir=test_dir,
        max_examples=audit_max_examples,
        workers=mutation_workers,
        contract_checks=explicit_invariants,
    )
    audit_block = getattr(base_audit, "blocking_reason", None)
    stages.append(
        MigrationStage(
            "audit base",
            "blocked" if audit_block else "passed",
            str(audit_block or base_audit.test_protection_view()["summary"]),
        )
    )

    candidate_mining = _mine_module(
        candidate,
        max_examples=mine_max_examples,
        **dict(mine_fixtures or {}),
    )
    candidate_contracts = _candidate_contracts(candidate_mining)
    stages.append(
        MigrationStage(
            "mine candidate contracts",
            "passed",
            f"{len(candidate_contracts)} observed contract hypothesis/hypotheses",
        )
    )

    base_module = importlib.import_module(base)
    candidate_module = importlib.import_module(candidate)
    base_functions = dict(_get_public_functions(base_module, preserve_wrappers=True))
    candidate_functions = dict(_get_public_functions(candidate_module, preserve_wrappers=True))
    changes: list[MigrationChange] = []
    diff_errors: dict[str, str] = {}
    shared_functions = base_functions.keys() & candidate_functions.keys()
    if not shared_functions:
        diff_errors["<migration>"] = "no shared public callables were differentially evaluated"
    for function in sorted(shared_functions):
        try:
            base_signature = str(inspect.signature(base_functions[function]))
            candidate_signature = str(inspect.signature(candidate_functions[function]))
        except (TypeError, ValueError) as exc:
            diff_errors[function] = f"public signature comparison failed: {exc}"
            continue
        if base_signature != candidate_signature:
            changes.append(
                MigrationChange(
                    id=f"signature:{function}",
                    function=function,
                    kind="signature",
                    base_signature=base_signature,
                    candidate_signature=candidate_signature,
                )
            )
        options = dict((diff_options or {}).get(function, {}))
        options.pop("max_examples", None)
        try:
            result: DiffResult = _diff(
                base_functions[function],
                candidate_functions[function],
                max_examples=diff_max_examples,
                **options,
            )
        except (TypeError, ValueError) as exc:
            diff_errors[function] = str(exc)
            continue
        if result.total < 1:
            diff_errors[function] = "differential comparison executed zero examples"
            continue
        if result.divergent:
            if result.witness is None or len(result.mismatches) != 1:
                diff_errors[function] = "divergent diff did not provide one canonical witness"
                continue
            changes.append(
                MigrationChange(
                    id=f"behavior:{function}",
                    function=function,
                    kind="behavior",
                    mismatch=result.mismatches[-1],
                    witness=result.witness,
                    regression_options={
                        **({"rtol": options["rtol"]} if options.get("rtol") is not None else {}),
                        **({"atol": options["atol"]} if options.get("atol") is not None else {}),
                        **({"custom_compare": True} if options.get("compare") else {}),
                        **({"custom_normalize": True} if options.get("normalize") else {}),
                    },
                )
            )
        elif result.status == "inconclusive":
            diff_errors[function] = result.reason or "differential comparison was inconclusive"
    for function in sorted(candidate_functions.keys() - base_functions.keys()):
        changes.append(
            MigrationChange(
                id=f"added:{function}",
                function=function,
                kind="added",
            )
        )
    for function in sorted(base_functions.keys() - candidate_functions.keys()):
        changes.append(
            MigrationChange(
                id=f"removed:{function}",
                function=function,
                kind="removed",
            )
        )
    diff_status: StageStatus = "failed" if diff_errors else "passed"
    stages.append(
        MigrationStage(
            "diff base/candidate",
            diff_status,
            f"{len(changes)} divergence(s), {len(diff_errors)} untested callable(s)",
        )
    )

    for change in changes:
        _classify_change(change, intended=intended, classifier=classify)
    unexpected = [change for change in changes if change.classification == "unexpected"]
    intended_count = len(changes) - len(unexpected)
    stages.append(
        MigrationStage(
            "classify intended changes",
            "passed",
            f"{intended_count} intended, {len(unexpected)} unexpected",
        )
    )

    artifacts = _save_artifacts(
        base=base,
        candidate=candidate,
        changes=changes,
        intended=intended,
        evidence_path=evidence,
        regression_path=regression,
    )
    save_status: StageStatus = "failed" if artifacts.error else "passed"
    stages.append(
        MigrationStage(
            "save unexpected divergences",
            save_status,
            (
                artifacts.error
                or f"{len(artifacts.regression_cases)} replayable case(s), "
                f"{len(artifacts.unsupported_change_ids)} evidence-only"
            ),
        )
    )

    baseline_errors: list[str] = []

    def resulting_tests() -> None:
        for case in artifacts.regression_cases:
            replay_migration_case(case)
        _run_explicit_invariants(candidate, explicit_invariants)

    if artifacts.error:
        baseline_errors.append(artifacts.error)
    if artifacts.unsupported_change_ids:
        baseline_errors.append(
            "non-replayable unexpected divergences: " + ", ".join(artifacts.unsupported_change_ids)
        )
    if not artifacts.regression_cases and explicit_invariant_count == 0:
        baseline_errors.append("no resulting regression tests or explicit invariants")
    if not baseline_errors:
        try:
            resulting_tests()
        except Exception as exc:
            baseline_errors.append(f"resulting test baseline fails: {type(exc).__name__}: {exc}")

    if baseline_errors:
        mutation_gate = MutationGate(
            status="blocked",
            threshold=mutation_threshold,
            reason="; ".join(baseline_errors),
        )
    else:
        mutation_result = _mutate(
            candidate,
            test_fn=resulting_tests,
            preset=mutation_preset,
            workers=mutation_workers,
            contract_context={
                "source": "migration",
                "test_basis": "generated_parity_and_explicit_contracts",
                "explicit_invariants": {
                    name: [check.name for check in checks]
                    for name, checks in explicit_invariants.items()
                },
                "saved_regressions": len(artifacts.regression_cases),
            },
        )
        mutation_passed = mutation_result.total > 0 and mutation_result.score >= mutation_threshold
        mutation_gate = MutationGate(
            status="passed" if mutation_passed else "failed",
            threshold=mutation_threshold,
            result=mutation_result,
            reason=(
                None
                if mutation_passed
                else (
                    "no mutants were measured"
                    if mutation_result.total == 0
                    else f"mutation score {mutation_result.score:.0%} below "
                    f"{mutation_threshold:.0%}"
                )
            ),
        )
    stages.append(
        MigrationStage(
            "mutate resulting tests",
            mutation_gate.status,
            (
                mutation_gate.reason
                or f"{mutation_gate.result.killed}/{mutation_gate.result.total} mutants killed"
            ),
        )
    )

    effective_scan_options = dict(scan_options or {})
    if "contract_checks" in effective_scan_options:
        raise ValueError("pass explicit contract_checks through invariants, not scan_options")
    effective_scan_options.setdefault("max_examples", scan_max_examples)
    candidate_scan = _scan_module(
        candidate,
        contract_checks=explicit_invariants,
        **effective_scan_options,
    )
    candidate_scan_passed = candidate_scan.total > 0 and candidate_scan.passed
    candidate_scan_summary = (
        f"{candidate_scan.total} callable(s), {candidate_scan.failed} promoted finding(s)"
        if candidate_scan.total > 0
        else "no callables produced executable scan evidence"
    )
    stages.append(
        MigrationStage(
            "scan candidate",
            "passed" if candidate_scan_passed else "failed",
            candidate_scan_summary,
        )
    )

    result = MigrationResult(
        base=base,
        candidate=candidate,
        stages=stages,
        base_audit=base_audit,
        candidate_mining=candidate_mining,
        candidate_contracts=candidate_contracts,
        changes=changes,
        artifacts=artifacts,
        mutation=mutation_gate,
        candidate_scan=candidate_scan,
        diff_errors=diff_errors,
        explicit_invariant_count=explicit_invariant_count,
        explicit_invariant_functions=tuple(
            sorted(name for name, checks in explicit_invariants.items() if checks)
        ),
    )
    if result.artifacts.error is None and manifest_path is not None:
        registered_path, finding_id, registration_error = _register_migration_regression(
            base=base,
            candidate=candidate,
            artifacts=result.artifacts,
            manifest_path=Path(manifest_path),
        )
        result.artifacts = RegressionArtifacts(
            evidence_path=result.artifacts.evidence_path,
            regression_path=result.artifacts.regression_path,
            regression_cases=result.artifacts.regression_cases,
            unsupported_change_ids=result.artifacts.unsupported_change_ids,
            error=registration_error,
            manifest_path=registered_path,
            finding_id=finding_id,
        )
    if result.artifacts.error is None:
        finalization_error = _persist_final_result(result)
        if finalization_error is not None:
            result.artifacts = RegressionArtifacts(
                evidence_path=result.artifacts.evidence_path,
                regression_path=result.artifacts.regression_path,
                regression_cases=result.artifacts.regression_cases,
                unsupported_change_ids=result.artifacts.unsupported_change_ids,
                error=finalization_error,
                manifest_path=result.artifacts.manifest_path,
                finding_id=result.artifacts.finding_id,
            )
    return result
