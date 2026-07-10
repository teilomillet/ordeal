from __future__ import annotations
# ruff: noqa
def _replay_compose_manifest_record(
    record: Mapping[str, Any],
    *,
    workspace: Path,
) -> tuple[Any | None, str | None]:
    """Validate and replay one workspace-bound Compose manifest record."""
    from dataclasses import replace

    from ordeal.compose import ComposeTrace, replay_compose_trace
    from ordeal.regression_evidence import (
        _compose_regression_binding,
        _compose_regression_binding_matches,
    )

    finding_id = str(record.get("finding_id") or "?")
    trace_file = str(record.get("trace_file") or "").strip()
    expected = record.get("binding")
    if not trace_file or not isinstance(expected, Mapping):
        return None, f"incomplete Compose record for {finding_id}"
    trace_path = _resolve_artifact_path(trace_file, workspace=str(workspace))
    if trace_path is None or not _path_is_within(trace_path, workspace):
        return None, f"Compose trace outside workspace for {finding_id}"
    if not trace_path.is_file():
        return None, f"Compose trace is missing for {finding_id}: {trace_file}"
    try:
        trace = ComposeTrace.load(trace_path)
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
        return None, f"could not load Compose trace for {finding_id}: {exc}"
    observed = _compose_regression_binding(trace.to_dict())
    if not _compose_regression_binding_matches(expected, observed):
        return None, f"Compose binding failed for {finding_id}"
    compose_file_value = str(trace.compose.get("file") or "").strip()
    compose_file = Path(compose_file_value)
    if not compose_file.is_absolute():
        compose_file = workspace / compose_file
    if not compose_file_value or not _path_is_within(compose_file, workspace):
        return None, f"Compose file outside workspace for {finding_id}"
    policy = record.get("replay_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    if policy.get("expected") not in {None, "clean"}:
        return None, f"unsupported Compose replay policy for {finding_id}"
    if int(policy.get("maximum_failures", 0)) != 0:
        return None, f"unsupported Compose failure allowance for {finding_id}"
    attempts = max(1, int(policy.get("attempts", 1)))
    replay_trace = replace(
        trace,
        compose={**trace.compose, "file": str(compose_file.resolve())},
    )
    try:
        return replay_compose_trace(replay_trace, attempts=attempts), None
    except (OSError, TypeError, ValueError) as exc:
        return None, f"could not replay Compose trace for {finding_id}: {exc}"
def _compose_replay_is_clean(report: Any) -> tuple[bool, int]:
    """Return whether every Compose replay attempt completed without failure."""
    clean_replays = sum(signature is None for signature in report.observed_signatures)
    return clean_replays == report.attempted, clean_replays
def _compose_fixed_state_control(
    record: Mapping[str, Any],
    *,
    workspace: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run the recorded Compose workload against the fixed service state."""
    from dataclasses import replace

    from ordeal.compose import ComposeTrace, _config_from_payload, run_compose_exploration

    finding_id = str(record.get("finding_id") or "?")
    trace_file = str(record.get("trace_file") or "").strip()
    trace_path = _resolve_artifact_path(trace_file, workspace=str(workspace))
    if trace_path is None or not _path_is_within(trace_path, workspace):
        return None, f"Compose trace outside workspace for {finding_id}"
    try:
        source = ComposeTrace.load(trace_path)
        config = _config_from_payload(source.compose)
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
        return None, f"could not load Compose fixed-state control for {finding_id}: {exc}"

    compose_file = Path(config.file)
    if not compose_file.is_absolute():
        compose_file = workspace / compose_file
    if not _path_is_within(compose_file, workspace):
        return None, f"Compose file outside workspace for {finding_id}"
    trace_dir = Path(config.trace_dir)
    if not trace_dir.is_absolute():
        trace_dir = workspace / trace_dir
    if not _path_is_within(trace_dir, workspace):
        return None, f"Compose trace directory outside workspace for {finding_id}"

    effective = replace(
        config,
        file=str(compose_file.resolve()),
        trace_dir=str(trace_dir.resolve()),
        seed=source.seed,
        keep_running=False,
    )
    try:
        fixed = run_compose_exploration(effective)
    except (OSError, TypeError, ValueError) as exc:
        return None, f"could not run Compose fixed-state control for {finding_id}: {exc}"

    summary = fixed.coverage.get("summary", {})
    coverage_complete = bool(
        isinstance(summary, Mapping)
        and int(summary.get("fail", 0)) == 0
        and int(summary.get("not_exercised", 0)) == 0
    )
    workload_status = str(fixed.protection.get("status") or "inconclusive")
    complete = bool(
        fixed.trace.failure is None
        and coverage_complete
        and (
            effective.workload_mutations == 0
            or workload_status == "protective_within_measured_scope"
        )
    )
    try:
        trace_display = fixed.trace_path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        trace_display = fixed.trace_path.as_posix()
    return (
        {
            "status": "complete" if complete else "incomplete",
            "trace": trace_display,
            "failure": fixed.trace.to_dict().get("failure"),
            "reliability_coverage": fixed.coverage,
            "workload_protection": fixed.protection,
            "workload_mutation_budget": effective.workload_mutations,
        },
        None,
    )
def _persist_compose_post_fix_control(
    manifest_path: Path,
    *,
    finding_id: str,
    workspace: Path,
    replay_report: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Persist clean replay plus fixed-state coverage in one manifest record."""
    payload = _read_json_file(manifest_path)
    records = payload.get("regressions")
    if not isinstance(records, list):
        return None, "regression manifest has no regressions list"
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and item.get("runner") == "compose"
            and item.get("finding_id") == finding_id
        ),
        None,
    )
    if record is None:
        return None, f"Compose record disappeared for {finding_id}"
    fixed_state, error = _compose_fixed_state_control(record, workspace=workspace)
    if error is not None:
        return None, error
    assert fixed_state is not None

    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    clean_replays = sum(signature is None for signature in replay_report.observed_signatures)
    fixed_state_complete = fixed_state.get("status") == "complete"
    control_status = "passed" if fixed_state_complete else "failed"
    evidence = record.get("evidence")
    copied_evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    copied_evidence["post_fix_control"] = {
        "status": control_status,
        "checked_at": checked_at,
        "method": "same_trace_then_fixed_state_rescan",
        "acceptance": (
            "Every exact trace replay is clean; the fixed-state rescan records coverage "
            "and configured workload-strength evidence."
        ),
        "exact_replay": {
            "attempted": replay_report.attempted,
            "clean": clean_replays,
            "observed_signatures": list(replay_report.observed_signatures),
        },
        "fixed_state": fixed_state,
        "fixed_state_sha256": _sha256_json(fixed_state),
    }
    workflow = copied_evidence.get("workflow")
    if isinstance(workflow, Mapping):
        copied_evidence["workflow"] = {
            **dict(workflow),
            "verify_fix": control_status,
        }
    record["evidence"] = copied_evidence
    record["verification"] = {
        "status": "verified" if fixed_state_complete else "failed",
        "checked_at": checked_at,
        "clean_replays": clean_replays,
        "attempted_replays": replay_report.attempted,
        "fixed_state_status": fixed_state["status"],
    }
    _write_json_file(manifest_path, payload)
    return copied_evidence["post_fix_control"], None
def _compose_persisted_control_error(record: Mapping[str, Any]) -> str | None:
    """Return why committed fixed-state Compose evidence is not CI-ready."""
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        return "has no evidence card"
    control = evidence.get("post_fix_control")
    if not isinstance(control, Mapping) or control.get("status") != "passed":
        return "post-fix control is not passed"
    fixed_state = control.get("fixed_state")
    if not isinstance(fixed_state, Mapping) or fixed_state.get("status") != "complete":
        return "fixed-state evidence is not complete"
    expected_digest = str(control.get("fixed_state_sha256") or "")
    if not expected_digest or expected_digest != _sha256_json(fixed_state):
        return "fixed-state evidence binding does not match"
    coverage = fixed_state.get("reliability_coverage")
    summary = coverage.get("summary") if isinstance(coverage, Mapping) else None
    if not isinstance(summary, Mapping):
        return "fixed-state reliability coverage is missing"
    if int(summary.get("fail", 0)) or int(summary.get("not_exercised", 0)):
        return "fixed-state reliability coverage is incomplete"
    budget = int(fixed_state.get("workload_mutation_budget", 0))
    protection = fixed_state.get("workload_protection")
    if budget > 0 and (
        not isinstance(protection, Mapping)
        or protection.get("status") != "protective_within_measured_scope"
    ):
        return "fixed-state workload protection is incomplete"
    return None
def _locate_compose_manifest_record(
    finding_id: str,
    manifest_path: Path,
) -> tuple[Path, Mapping[str, Any]] | None:
    """Locate one Compose regression record in the portable manifest."""
    if not manifest_path.is_file():
        return None
    payload = _read_json_file(manifest_path)
    if payload.get("schema") != "ordeal.regression-manifest/v1":
        return None
    records = payload.get("regressions")
    if not isinstance(records, list):
        return None
    resolved = manifest_path.resolve()
    workspace = resolved.parent.parent if resolved.parent.name == "tests" else Path.cwd().resolve()
    for record in records:
        if (
            isinstance(record, Mapping)
            and record.get("runner") == "compose"
            and record.get("finding_id") == finding_id
        ):
            return workspace, record
    return None
def _cmd_verify_ci(args: argparse.Namespace) -> int:
    """Fail closed when any saved regression binding or test fails in CI."""
    import subprocess

    from ordeal.regression_evidence import _regression_binding, _regression_binding_matches

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        _stderr(f"Regression manifest not found: {manifest_path}\n")
        return 2
    try:
        payload = _read_json_file(manifest_path)
    except json.JSONDecodeError as exc:
        _stderr(f"Regression manifest is not valid JSON: {exc}\n")
        return 2
    if payload.get("schema") != "ordeal.regression-manifest/v1":
        _stderr("Regression manifest schema must be ordeal.regression-manifest/v1.\n")
        return 2
    records = payload.get("regressions")
    if not isinstance(records, list):
        _stderr("Regression manifest has no regressions list.\n")
        return 2

    resolved_manifest = manifest_path.resolve()
    workspace = (
        resolved_manifest.parent.parent
        if resolved_manifest.parent.name == "tests"
        else Path.cwd().resolve()
    )
    if not _path_is_within(resolved_manifest, workspace):
        _stderr(f"CI guard refused manifest outside workspace: {manifest_path}\n")
        return 2
    passed = 0
    failed = 0
    errors = 0
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            _stderr("CI guard found an invalid regression manifest entry.\n")
            errors += 1
            continue
        finding_id = str(record.get("finding_id") or "").strip()
        if not finding_id or finding_id in seen:
            _stderr(f"CI guard found a missing or duplicate finding ID: {finding_id or '?'}\n")
            errors += 1
            continue
        seen.add(finding_id)
        if record.get("runner") == "compose":
            report, replay_error = _replay_compose_manifest_record(
                record,
                workspace=workspace,
            )
            if replay_error is not None:
                _stderr(f"CI guard {replay_error}.\n")
                errors += 1
                continue
            assert report is not None
            clean, clean_replays = _compose_replay_is_clean(report)
            if clean:
                control_error = _compose_persisted_control_error(record)
                if control_error is not None:
                    errors += 1
                    _stderr(
                        f"CI guard Compose evidence is incomplete for {finding_id}: "
                        f"{control_error}. Run `ordeal verify {finding_id} "
                        "--allow-unsafe-artifacts` after the fix.\n"
                    )
                    continue
                passed += 1
                print(
                    f"  passed: {finding_id} "
                    f"(Compose clean replays {clean_replays}/{report.attempted})"
                )
            else:
                failed += 1
                _stderr(
                    f"CI guard Compose regression failed for {finding_id}: clean replays "
                    f"{clean_replays}/{report.attempted}; every replay must be clean.\n"
                )
            continue
        test_file = str(record.get("test_file") or "").strip()
        test_name = str(record.get("test_name") or "").strip()
        expected = record.get("binding")
        if not test_file or not test_name or not isinstance(expected, Mapping):
            _stderr(f"CI guard found an incomplete regression record for {finding_id}.\n")
            errors += 1
            continue
        regression_path = _resolve_artifact_path(test_file, workspace=str(workspace))
        if regression_path is None or not _path_is_within(regression_path, workspace):
            _stderr(f"CI guard refused regression outside workspace for {finding_id}.\n")
            errors += 1
            continue
        if not regression_path.is_file():
            _stderr(f"CI guard regression file is missing for {finding_id}: {test_file}\n")
            errors += 1
            continue
        observed = _regression_binding(regression_path.read_text(encoding="utf-8"), test_name)
        if observed is None or not _regression_binding_matches(expected, observed):
            _stderr(f"CI guard binding failed for {finding_id}.\n")
            errors += 1
            continue
        evidence_error = _python_change_evidence_error(record, workspace=workspace)
        if evidence_error is not None:
            _stderr(f"CI guard evidence failed for {finding_id}: {evidence_error}.\n")
            errors += 1
            continue
        nodeid = f"{test_file}::{test_name}"
        run_args = [sys.executable, "-m", "pytest", nodeid, "-q"]
        display_command = _shell_command("uv", "run", "pytest", nodeid, "-q")
        proc = subprocess.run(
            run_args,
            cwd=str(workspace),
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            passed += 1
            print(f"  passed: {finding_id} ({display_command})")
        elif proc.returncode == 1:
            failed += 1
            _stderr(f"CI guard regression failed for {finding_id}: {display_command}\n")
        else:
            errors += 1
            _stderr(
                f"CI guard could not run {finding_id} (exit {proc.returncode}): "
                f"{display_command}\n"
            )

    print(f"verify --ci: {passed} passed, {failed} failed, {errors} error(s)")
    if errors:
        return 2
    return 1 if failed else 0
def _python_change_evidence_error(
    record: Mapping[str, Any],
    *,
    workspace: Path,
) -> str | None:
    """Validate shared change-artifact IDs and immutable source guards."""
    expected_ids = {str(value) for value in record.get("change_artifact_ids", []) if value}
    if expected_ids:
        evidence_file = str(record.get("evidence_file") or "").strip()
        evidence_path = _resolve_artifact_path(evidence_file, workspace=str(workspace))
        if (
            not evidence_file
            or evidence_path is None
            or not _path_is_within(evidence_path, workspace)
            or not evidence_path.is_file()
        ):
            return "source-bound evidence file is missing or outside the workspace"
        try:
            payload = _read_json_file(evidence_path)
        except json.JSONDecodeError as exc:
            return f"source-bound evidence is invalid JSON: {exc}"
        artifacts: list[Mapping[str, Any]] = []
        if payload.get("schema") == "ordeal.divergence-evidence/v1":
            artifacts.append(payload)
        for key in ("artifacts", "change_evidence"):
            values = payload.get(key, [])
            if isinstance(values, list):
                artifacts.extend(item for item in values if isinstance(item, Mapping))
        by_id = {
            str(artifact.get("artifact_id")): artifact
            for artifact in artifacts
            if artifact.get("artifact_id")
        }
        if not expected_ids <= by_id.keys():
            missing = ", ".join(sorted(expected_ids - by_id.keys()))
            return f"change artifact ID(s) missing: {missing}"
        from ordeal.finding_evidence import _sha256_json

        for artifact_id in sorted(expected_ids):
            artifact = by_id[artifact_id]
            if artifact.get("schema") != "ordeal.divergence-evidence/v1":
                return f"change artifact schema is invalid: {artifact_id}"
            unhashed = dict(artifact)
            unhashed.pop("artifact_id", None)
            computed_id = f"div_{_sha256_json(unhashed)[:16]}"
            if artifact_id != computed_id:
                return f"change artifact self-hash is invalid: {artifact_id}"
            source_binding = artifact.get("source_binding")
            if (
                not isinstance(source_binding, Mapping)
                or source_binding.get("status") != "complete"
            ):
                return f"change artifact source binding is incomplete: {artifact_id}"
            replay = artifact.get("replay")
            if not isinstance(replay, Mapping) or replay.get("status") != "verified":
                return f"change artifact replay is not verified: {artifact_id}"
            minimization = artifact.get("minimization")
            if not isinstance(minimization, Mapping) or minimization.get("status") != "verified":
                return f"change artifact minimization is not verified: {artifact_id}"
            if artifact.get("status") != "supported":
                return f"change artifact is not supported: {artifact_id}"

    guards = record.get("source_guards", [])
    if not isinstance(guards, list):
        return "source_guards must be a list"
    if guards:
        from ordeal.diff import _callable_binding, _resolve_replay_callable

        for guard in guards:
            if not isinstance(guard, Mapping):
                return "source guard is not an object"
            path = str(guard.get("callable") or "")
            expected_sha256 = str(guard.get("source_sha256") or "")
            try:
                target = _resolve_replay_callable(path)
            except (AttributeError, ImportError, TypeError, ValueError) as exc:
                return f"could not resolve guarded callable {path}: {exc}"
            if target is None:
                return f"guarded callable is missing: {path}"
            observed_binding = _callable_binding(target)
            if observed_binding.get("source_sha256") != expected_sha256:
                return f"guarded callable source changed: {path}"
    return None
