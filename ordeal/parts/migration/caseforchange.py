from __future__ import annotations
# ruff: noqa
def _case_for_change(
    change: MigrationChange,
    *,
    candidate: str,
) -> dict[str, object]:
    """Build one executable regression case or raise when it cannot be replayed."""
    payload: dict[str, object] = {
        "change_id": change.id,
        "candidate": candidate,
        "function": change.function,
        "kind": change.kind,
    }
    if change.kind == "behavior":
        if change.mismatch is None or change.witness is None:
            raise TypeError("behavior change has no mismatch witness")
        unsupported = {"receiver_state", "side_effects"} & set(change.witness.differences)
        if unsupported:
            joined = ", ".join(sorted(unsupported))
            raise TypeError(f"cannot persist selected state channel(s): {joined}")
        if change.regression_options.get("custom_compare"):
            raise TypeError("cannot persist a custom diff comparator")
        if change.regression_options.get("custom_normalize"):
            raise TypeError("cannot persist a custom diff normalizer")
        artifact_observations = change.witness.artifact.get("observations", {})
        baseline_observation = (
            artifact_observations.get("a", {})
            if isinstance(artifact_observations, Mapping)
            else {}
        )
        if change.witness.replay_args_json is not None:
            replay_args = json.loads(change.witness.replay_args_json)
            if not isinstance(replay_args, Mapping):
                raise TypeError("behavior witness arguments are not a replayable mapping")
            payload["kwargs"] = replay_args
        elif change.witness.artifact.get("schema") == "ordeal.divergence-evidence/v1":
            raise TypeError("behavior witness arguments are not exactly replayable")
        else:
            payload["kwargs"] = _encode_value(change.mismatch.args)
        canonical_mutated_arguments = (
            baseline_observation.get("canonical_mutated_arguments")
            if isinstance(baseline_observation, Mapping)
            else None
        )
        if isinstance(canonical_mutated_arguments, Mapping):
            payload["expected_mutated_arguments_observation"] = _json_ready(
                canonical_mutated_arguments
            )
        else:
            payload["expected_mutated_arguments"] = _encode_value(
                change.witness.outcome_a.mutated_arguments
            )
        comparison = {
            key: change.regression_options[key]
            for key in ("rtol", "atol")
            if change.regression_options.get(key) is not None
        }
        if comparison:
            payload["comparison"] = comparison
        base_outcome = change.witness.outcome_a
        if not base_outcome.returned:
            if base_outcome.exception_type is None:
                raise TypeError("baseline exception witness has no exception type")
            payload["expected"] = {
                "kind": "exception",
                "type_module": base_outcome.exception_type.__module__,
                "type_qualname": base_outcome.exception_type.__qualname__,
                "message": str(base_outcome.exception_message or ""),
            }
        else:
            canonical_value = (
                baseline_observation.get("canonical_value")
                if isinstance(baseline_observation, Mapping)
                else None
            )
            if isinstance(canonical_value, Mapping) and not comparison:
                payload["expected"] = {
                    "kind": "canonical_return",
                    "observation": _json_ready(canonical_value),
                }
            else:
                payload["expected"] = {
                    "kind": "return",
                    "value": _encode_value(base_outcome.return_value),
                }
    elif change.kind == "signature":
        if change.base_signature is None:
            raise TypeError("signature change has no base signature")
        payload["expected_signature"] = change.base_signature
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    payload["id"] = f"{change.id}:{digest}"
    return payload
def _load_existing_cases(
    path: Path,
    *,
    base: str,
    candidate: str,
) -> list[dict[str, object]]:
    """Load prior cases for the same module pair."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"unsupported migration evidence schema in {path}")
    if data.get("base") != base or data.get("candidate") != candidate:
        raise ValueError(f"migration evidence at {path} belongs to another module pair")
    cases = data.get("regression_cases", [])
    if not isinstance(cases, list):
        raise ValueError(f"invalid regression_cases in {path}")
    return [dict(case) for case in cases if isinstance(case, dict)]
def _render_regression_file(cases: Sequence[Mapping[str, object]]) -> str:
    """Render a standalone pytest module backed by replay helpers."""
    return "\n".join(
        [
            _GENERATED_HEADER,
            "",
            "Review and commit this file with its migration evidence artifact.",
            '"""',
            "",
            "import pytest",
            "",
            "from ordeal.migration import replay_migration_case",
            "",
            f"CASES = {list(cases)!r}",
            "",
            "",
            '@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])',
            "def test_ordeal_migration_regression(case: dict[str, object]) -> None:",
            '    """Keep one previously unexpected candidate divergence fixed."""',
            "    replay_migration_case(case)",
            "",
        ]
    )
def _selector_matches(selector: str, *, change_id: str, function: str) -> bool:
    """Return whether an intended-change selector names a change."""
    return selector in {change_id, function, f"*:{function}"}
def _classify_change(
    change: MigrationChange,
    *,
    intended: Mapping[str, str],
    classifier: ChangeClassifier | None,
) -> None:
    """Apply explicit classification without inferring intent from parity."""
    if classifier is not None:
        decision = classifier(change)
        if isinstance(decision, tuple):
            classification, reason = decision
        else:
            classification, reason = decision, "classified by callback"
        if classification not in ("intended", "unexpected"):
            raise ValueError(f"invalid change classification: {classification!r}")
        change.classification = classification
        change.reason = reason
        return
    for selector, reason in intended.items():
        if _selector_matches(selector, change_id=change.id, function=change.function):
            change.classification = "intended"
            change.reason = reason
            return
def _normalize_intended_changes(
    intended_changes: Collection[str] | Mapping[str, str],
) -> dict[str, str]:
    """Normalize simple selectors and selector-to-reason mappings."""
    if isinstance(intended_changes, Mapping):
        return {str(selector): str(reason) for selector, reason in intended_changes.items()}
    return {str(selector): "declared as intended" for selector in intended_changes}
def _candidate_contracts(result: MineModuleResult) -> list[CandidateContract]:
    """Flatten universal mined observations while labeling their weak status."""
    contracts: list[CandidateContract] = []
    for function, mine_result in sorted(result.per_function.items()):
        for prop in mine_result.universal:
            contracts.append(
                CandidateContract(
                    function=function,
                    property=prop.name,
                    holds=prop.holds,
                    total=prop.total,
                    confidence=prop.confidence,
                )
            )
    for prop in result.cross_function:
        if prop.total > 0 and prop.holds == prop.total:
            contracts.append(
                CandidateContract(
                    function=f"{prop.function_a}<->{prop.function_b}",
                    property=prop.relation,
                    holds=prop.holds,
                    total=prop.total,
                    confidence=prop.confidence,
                )
            )
    return contracts
def _run_explicit_invariants(
    candidate: str,
    invariants: Mapping[str, Sequence[ContractCheck]],
) -> None:
    """Execute explicit contracts against the live candidate module."""
    module = importlib.import_module(candidate)
    for function, checks in invariants.items():
        func: object = module
        for part in function.split("."):
            func = getattr(func, part)
        for check in checks:
            invocation_kwargs = copy.deepcopy(check.kwargs)
            error: BaseException | None = None
            try:
                call_args, call_kwargs = _bind_named_arguments(func, invocation_kwargs)
                value = _call_sync(func, *call_args, **call_kwargs)
            except BaseException as exc:
                error = exc
                value = None
            passed = _call_contract_predicate(
                check.predicate,
                value,
                func=func,
                call_context=getattr(func, "__ordeal_last_call_context__", None),
                kwargs=invocation_kwargs,
                error=error,
            )
            if not passed:
                detail = f" ({type(error).__name__}: {error})" if error is not None else ""
                raise AssertionError(f"explicit invariant failed: {function}:{check.name}{detail}")
def _save_artifacts(
    *,
    base: str,
    candidate: str,
    changes: Sequence[MigrationChange],
    intended: Mapping[str, str],
    evidence_path: Path,
    regression_path: Path,
) -> RegressionArtifacts:
    """Persist unexpected changes and merge replayable witnesses across reruns."""
    unsupported: list[str] = []
    try:
        existing = _load_existing_cases(
            evidence_path,
            base=base,
            candidate=candidate,
        )
        intended_ids = {change.id for change in changes if change.classification == "intended"}
        existing = [
            case
            for case in existing
            if str(case.get("change_id")) not in intended_ids
            and not any(
                _selector_matches(
                    selector,
                    change_id=str(case.get("change_id", "")),
                    function=str(case.get("function", "")),
                )
                for selector in intended
            )
        ]
        current_cases: list[dict[str, object]] = []
        for change in changes:
            if change.classification != "unexpected":
                continue
            try:
                current_cases.append(_case_for_change(change, candidate=candidate))
            except (TypeError, ValueError):
                unsupported.append(change.id)

        by_id = {str(case["id"]): case for case in [*existing, *current_cases]}
        cases = list(by_id.values())
        evidence = {
            "schema_version": _SCHEMA_VERSION,
            "base": base,
            "candidate": candidate,
            "changes": [change.to_dict() for change in changes],
            "change_evidence": [
                _json_ready(change.witness.artifact)
                for change in changes
                if change.witness is not None
                and change.witness.artifact.get("schema") == "ordeal.divergence-evidence/v1"
            ],
            "regression_cases": cases,
            "unsupported_change_ids": unsupported,
            "mining_limitations": list(STRUCTURAL_LIMITATIONS),
            "regression_path": regression_path.as_posix(),
        }
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if cases or regression_path.exists():
            if regression_path.exists() and not regression_path.read_text(
                encoding="utf-8"
            ).startswith(_GENERATED_HEADER):
                raise ValueError(
                    f"refusing to overwrite non-generated regression file: {regression_path}"
                )
            regression_path.parent.mkdir(parents=True, exist_ok=True)
            regression_path.write_text(_render_regression_file(cases), encoding="utf-8")
        return RegressionArtifacts(
            evidence_path=evidence_path,
            regression_path=regression_path,
            regression_cases=tuple(cases),
            unsupported_change_ids=tuple(unsupported),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return RegressionArtifacts(
            evidence_path=evidence_path,
            regression_path=regression_path,
            unsupported_change_ids=tuple(unsupported),
            error=str(exc),
        )
def _persist_final_result(result: MigrationResult) -> str | None:
    """Append mutation, scan, and the final verdict to the migration artifact."""
    path = result.artifacts.evidence_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("migration evidence must contain a JSON object")
        final = result.to_dict()
        payload.update(
            {
                "protected_within_measured_scope": final["protected_within_measured_scope"],
                "stages": final["stages"],
                "candidate_contracts": final["candidate_contracts"],
                "diff_errors": final["diff_errors"],
                "explicit_invariant_count": final["explicit_invariant_count"],
                "explicit_invariant_functions": final["explicit_invariant_functions"],
                "measured_regression_callables": final["measured_regression_callables"],
                "unprotected_changed_callables": final["unprotected_changed_callables"],
                "mutation": final["mutation"],
                "candidate_scan": final["candidate_scan"],
                "final_verdict": (
                    "protective_within_measured_scope"
                    if result.protected_within_measured_scope
                    else "incomplete"
                ),
            }
        )
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return str(exc)
    return None
def _register_migration_regression(
    *,
    base: str,
    candidate: str,
    artifacts: RegressionArtifacts,
    manifest_path: Path,
) -> tuple[Path | None, str | None, str | None]:
    """Register the generated parity regression in the shared CI manifest."""
    from ordeal.regression_evidence import _register_python_regression

    finding_id = (
        "fnd_migration_" + hashlib.sha256(f"{base}\0{candidate}".encode("utf-8")).hexdigest()[:16]
    )
    try:
        evidence_payload = json.loads(artifacts.evidence_path.read_text(encoding="utf-8"))
        change_evidence = evidence_payload.get("change_evidence", [])
        artifact_ids = [
            str(item.get("artifact_id"))
            for item in change_evidence
            if isinstance(item, Mapping) and item.get("artifact_id")
        ]
        registered, error = _register_python_regression(
            manifest_path=manifest_path,
            finding_id=finding_id,
            change_kind="migration",
            target=f"{base} -> {candidate}",
            test_path=artifacts.regression_path,
            test_name="test_ordeal_migration_regression",
            evidence_path=artifacts.evidence_path,
            change_artifact_ids=artifact_ids,
            test_basis="generated_parity_and_explicit_contracts",
            active=bool(artifacts.regression_cases),
        )
        if error is not None:
            return None, None, error
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, None, str(exc)
    return registered, finding_id if artifacts.regression_cases else None, None
