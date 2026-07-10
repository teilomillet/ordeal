from __future__ import annotations
# ruff: noqa
def _scan_reliability(scope: SimpleNamespace) -> int | None:
    from ordeal.reliability import (
        _build_reliability_map,
        _default_reliability_map_path,
        _run_fault_probe,
    )

    evidence_fault = str(getattr(scope.args, "evidence_fault", None) or "").strip()
    if evidence_fault:
        if len(scope.scan_targets) != 1:
            scope.reason = "--evidence-fault requires exactly one --target selector"
            if scope.args.json:
                print(
                    _build_blocked_agent_envelope(
                        tool="scan",
                        target=scope.scan_target,
                        summary="fault-specific evidence probe is not targeted",
                        blocking_reason=scope.reason,
                        suggested_commands=(f"ordeal scan {scope.scan_target} --list-targets",),
                    ).to_json()
                )
            else:
                _stderr(f"Scan blocked: {scope.reason}.\n")
            return 2
        observation = _run_fault_probe(
            scope.module_name,
            scope.scan_targets[0],
            evidence_fault,
            max_examples=scope.scan_max_examples,
            scan_kwargs={
                "check_return_type": True,
                "include_private": scope.inc_private,
                "fixtures": scope.runtime_defaults.fixtures,
                "object_factories": scope.runtime_defaults.object_factories,
                "object_setups": scope.runtime_defaults.object_setups,
                "object_scenarios": scope.runtime_defaults.object_scenarios,
                "object_state_factories": scope.runtime_defaults.object_state_factories,
                "object_teardowns": scope.runtime_defaults.object_teardowns,
                "object_harnesses": scope.runtime_defaults.object_harnesses,
                "expected_failures": scope.runtime_defaults.expected_failures,
                "expected_preconditions": scope.runtime_defaults.expected_preconditions,
                "ignore_properties": scope.scan_ignore_properties,
                "ignore_relations": scope.scan_ignore_relations,
                "property_overrides": scope.scan_property_overrides,
                "relation_overrides": scope.scan_relation_overrides,
                "expected_properties": scope.runtime_defaults.expected_properties,
                "expected_relations": scope.runtime_defaults.expected_relations,
                "contract_checks": scope.runtime_defaults.contract_checks,
                "ignore_contracts": scope.runtime_defaults.ignore_contracts,
                "contract_overrides": scope.runtime_defaults.contract_overrides,
                "mode": scope.scan_mode,
                "seed_from_tests": scope.scan_seed_from_tests,
                "seed_from_fixtures": scope.runtime_defaults.seed_from_fixtures,
                "seed_from_docstrings": scope.runtime_defaults.seed_from_docstrings,
                "seed_from_code": scope.runtime_defaults.seed_from_code,
                "seed_from_call_sites": scope.scan_seed_from_call_sites,
                "treat_any_as_weak": scope.runtime_defaults.treat_any_as_weak,
                "proof_bundles": scope.runtime_defaults.proof_bundles,
                "auto_contracts": scope.runtime_defaults.auto_contracts,
                "require_replayable": scope.runtime_defaults.require_replayable,
                "min_contract_fit": scope.scan_min_contract_fit,
                "min_reachability": scope.scan_min_reachability,
                "min_realism": scope.scan_min_realism,
                "security_focus": scope.scan_security_focus,
                "minimize_findings": False,
            },
        )
        scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
        scope.state.supervisor_info["reliability_observations"] = [observation]
    try:
        scope.reliability_map = _build_reliability_map(
            scope.module_name,
            scope.state,
            scope.scan_target_rows,
            base_ref=scope.reliability_base_ref,
            allow_service_faults=bool(getattr(scope.args, "allow_service_faults", False)),
            previous_path=_default_reliability_map_path(scope.module_name),
        )
    except Exception as exc:
        scope.reliability_map = {
            "schema": "ordeal.reliability-map/v1",
            "module": scope.module_name,
            "status": "blocked",
            "blocking_reason": f"reliability map construction failed: {type(exc).__name__}: {exc}",
            "summary": {
                "operations": 0,
                "cells": 0,
                "pass": 0,
                "not_exercised": 0,
                "fail": 0,
                "blocked": 0,
            },
            "operations": [],
            "cells": [],
            "next_experiment": None,
        }
    scope.state.supervisor_info = dict(getattr(scope.state, "supervisor_info", {}) or {})
    scope.state.supervisor_info["reliability_map"] = scope.reliability_map
    if getattr(scope.args, "deepen", False):
        budget = float(scope.args.time_limit)
        elapsed = _time.monotonic() - scope.scan_started
        remaining = max(0.0, budget - elapsed)
        experiment = scope.reliability_map.get("next_experiment")
        deepening: dict[str, Any] = {
            "requested": True,
            "budget_seconds": budget,
            "remaining_before_seconds": round(remaining, 4),
            "service_faults_enabled": bool(getattr(scope.args, "allow_service_faults", False)),
            "service_faults_executed": False,
        }
        if remaining <= 0:
            deepening.update(
                {
                    "status": "budget_exhausted",
                    "reason": "the initial scan consumed the explicit time budget",
                }
            )
        elif not isinstance(experiment, Mapping):
            deepening.update(
                {
                    "status": "no_safe_experiment",
                    "reason": "the reliability map had no safe automatic follow-up",
                }
            )
        elif not experiment.get("auto_runnable"):
            deepening.update(
                {
                    "status": "review_required",
                    "engine": experiment.get("engine"),
                    "command": experiment.get("command"),
                    "reason": experiment.get("reason"),
                }
            )
        else:
            command = shlex.split(str(experiment["command"]))
            if command and command[0] == "ordeal":
                command = [sys.executable, "-m", "ordeal.cli", *command[1:]]
            if experiment.get("engine") in {"scan", "compose"} and "--json" not in command:
                command.append("--json")
            started = _time.monotonic()
            try:
                completed = _run_budgeted_child(command, timeout=remaining)
            except subprocess.TimeoutExpired:
                deepening.update(
                    {
                        "status": "budget_exhausted",
                        "engine": experiment.get("engine"),
                        "command": experiment.get("command"),
                        "reason": "the follow-up reached the remaining time budget",
                    }
                )
            else:
                output: dict[str, Any] | None = None
                with contextlib.suppress(json.JSONDecodeError):
                    output = json.loads(completed.stdout)
                child_details = (
                    output.get("raw_details", {}).get("report", {}).get("details", [])
                    if output
                    else []
                )
                child_observations = (
                    output.get("raw_details", {}).get("reliability_observations", [])
                    if output
                    else []
                )
                if child_observations:
                    existing_observations = list(
                        scope.state.supervisor_info.get("reliability_observations", ())
                    )
                    existing_observations.extend(
                        (item for item in child_observations if isinstance(item, Mapping))
                    )
                    scope.state.supervisor_info["reliability_observations"] = existing_observations
                    scope.reliability_map = _build_reliability_map(
                        scope.module_name,
                        scope.state,
                        scope.scan_target_rows,
                        base_ref=scope.reliability_base_ref,
                        allow_service_faults=bool(
                            getattr(scope.args, "allow_service_faults", False)
                        ),
                        previous_path=_default_reliability_map_path(scope.module_name),
                    )
                child_findings = [
                    {
                        "target": detail.get("qualname") or detail.get("function"),
                        "kind": detail.get("kind") or detail.get("category"),
                        "summary": detail.get("summary") or detail.get("error"),
                    }
                    for detail in child_details[:10]
                    if isinstance(detail, Mapping)
                ]
                deepening.update(
                    {
                        "status": "completed" if completed.returncode in {0, 1} else "error",
                        "engine": experiment.get("engine"),
                        "command": experiment.get("command"),
                        "exit_code": completed.returncode,
                        "result_status": output.get("status") if output else None,
                        "finding_count": output.get("raw_details", {}).get("finding_count")
                        if output
                        else None,
                        "findings": child_findings,
                        "findings_truncated": len(child_details) > 10,
                        "reliability_observations": child_observations[:10],
                        "stderr": completed.stderr[-500:]
                        if completed.returncode not in {0, 1}
                        else "",
                        "service_faults_executed": experiment.get("engine") == "compose",
                    }
                )
            deepening["elapsed_seconds"] = round(_time.monotonic() - started, 4)
        scope.reliability_map["deepening"] = deepening
        scope.state.supervisor_info["reliability_map"] = scope.reliability_map
    return None
def _scan_output(scope: SimpleNamespace) -> int | None:
    if not scope.args.json:
        print(_format_scan_summary(scope.state))
        if (
            (scope.state.findings or _scan_report_details(scope.state))
            and (not getattr(scope.args, "save_artifacts", False))
            and (not getattr(scope.args, "report_file", None))
            and (not getattr(scope.args, "write_regression", None))
        ):
            print(f"  next: ordeal scan {scope.scan_target} --save")
    save_artifacts = getattr(scope.args, "save_artifacts", False)
    report_path = scope.args.report_file
    regression_path = scope.args.write_regression
    written_report_path: Path | None = None
    written_regression_path: Path | None = None
    written_regression_manifest_path: Path | None = None
    written_config_path: Path | None = None
    written_support_path: Path | None = None
    written_proofs_path: Path | None = None
    written_replay_path: Path | None = None
    written_scenario_library_path: Path | None = None
    written_reliability_map_path: Path | None = None
    index_path: Path | None = None
    report_details = _scan_report_details(scope.state)
    has_regression_details = bool(
        scope.state.findings
        or any(
            (detail.get("kind") not in {"blocked", "precondition"} for detail in report_details)
        )
    )
    has_details = bool(report_details or scope.reliability_map.get("cells"))
    if save_artifacts and has_details:
        report_path = report_path or _default_scan_report_path(scope.state.module)
        if has_regression_details:
            regression_path = regression_path or _DEFAULT_REGRESSION_PATH
    if regression_path:
        written_regression_path = _write_scan_regressions(scope.state, regression_path)
    if report_path:
        written_report_path = _write_scan_report(
            scope.state, report_path, regression_path=written_regression_path
        )
    if save_artifacts and (not has_details):
        _stderr("No findings yet; no artifacts written.\n")
    if save_artifacts and has_details and (written_report_path is not None):
        report = _build_scan_report(scope.state, regression_path=written_regression_path)
        written_review_artifacts = _write_scan_review_bundle_artifacts(
            scope.state, report=report, regression_path=written_regression_path
        )
        written_config_path = written_review_artifacts.get("config")
        written_support_path = written_review_artifacts.get("support")
        written_proofs_path = written_review_artifacts.get("proofs")
        written_replay_path = written_review_artifacts.get("replay")
        written_scenario_library_path = written_review_artifacts.get("scenarios")
        written_reliability_map_path = written_review_artifacts.get("reliability_map")
        bundle_path, bundle = _write_scan_bundle(
            scope.state,
            path_str=_artifact_bundle_path(str(written_report_path)),
            report_path=written_report_path,
            regression_path=written_regression_path,
            config_path=written_config_path,
            support_path=written_support_path,
            proofs_path=written_proofs_path,
            replay_path=written_replay_path,
            scenario_library_path=written_scenario_library_path,
        )
        written_regression_manifest_path = _write_regression_manifest(bundle)
        index_path = _write_scan_artifact_index(bundle=bundle, bundle_path=bundle_path)
        if not scope.args.json:
            _print_scan_artifact_workflow(
                module=scope.state.module,
                report_path=written_report_path,
                bundle_path=bundle_path,
                finding_ids=[finding["finding_id"] for finding in bundle["findings"]],
                regression_path=written_regression_path,
                regression_manifest_path=written_regression_manifest_path,
                config_path=written_config_path,
                support_path=written_support_path,
                proofs_path=written_proofs_path,
                replay_path=written_replay_path,
                scenario_library_path=written_scenario_library_path,
                index_path=index_path,
            )
    if getattr(scope.args, "json", False):
        print(
            _build_scan_agent_envelope(
                scope.state,
                written_report_path=written_report_path,
                written_regression_path=written_regression_path,
                written_regression_manifest_path=written_regression_manifest_path,
                written_config_path=written_config_path,
                written_support_path=written_support_path,
                written_proofs_path=written_proofs_path,
                written_replay_path=written_replay_path,
                written_scenario_library_path=written_scenario_library_path,
                written_reliability_map_path=written_reliability_map_path,
                index_path=index_path,
            ).to_json()
        )
    return 1 if scope.state.findings else 0
def _cmd_scan(args: argparse.Namespace) -> int:
    """Run unified exploratory analysis over one module or explicit callable target."""
    scope = SimpleNamespace(args=args)
    exit_code = _scan_prepare(scope)
    if exit_code is not None:
        return exit_code
    exit_code = _scan_explore(scope)
    if exit_code is not None:
        return exit_code
    exit_code = _scan_reliability(scope)
    if exit_code is not None:
        return exit_code
    return _scan_output(scope)
def _terminate_child_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a budgeted child and descendants without leaking the process tree."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            text=True,
            capture_output=True,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        if process.poll() is None:
            process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)
    # The group can outlive its leader; SIGKILL any remaining descendants.
    with contextlib.suppress(PermissionError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
def _run_budgeted_child(
    command: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run one child in an isolated process group and enforce a hard tree timeout."""
    group_kwargs: dict[str, Any]
    if os.name == "nt":
        group_kwargs = {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    else:
        group_kwargs = {"start_new_session": True}
    process = subprocess.Popen(
        list(command),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **group_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_child_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            exc.cmd,
            exc.timeout,
            output=stdout,
            stderr=stderr,
        ) from None
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
def _compose_run_payload(result: Any) -> dict[str, Any]:
    """Return complete JSON evidence for one normal Compose CLI run."""
    trace_payload = result.trace.to_dict()
    return {
        "schema": "ordeal.compose-run/v1",
        "status": "failed" if result.trace.failure is not None else "clean",
        "trace_file": _display_path(result.trace_path),
        "trace_sha256": _sha256_json(trace_payload),
        "trace": trace_payload,
        "reliability_coverage": result.coverage,
        "workload_protection": result.protection,
        "requests": result.requests,
        "faults": result.faults,
        "duration_seconds": result.duration,
        "replay": result.replay.to_dict() if result.replay is not None else None,
    }
def _save_compose_run_payload(result: Any) -> Path:
    """Persist the complete normal-CLI Compose run beside its exact trace."""
    path = result.trace_path.with_suffix(".evidence.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_compose_run_payload(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
