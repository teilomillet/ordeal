from __future__ import annotations
# ruff: noqa
def _build_replay_agent_envelope(
    *,
    trace_file: str,
    trace: Any | None,
    reproduced_error: Exception | None,
    shrunk_trace: Any | None = None,
    ablation: Mapping[str, bool] | None = None,
    output_path: Path | None = None,
    blocking_reason: str | None = None,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal replay`."""
    details = []
    if reproduced_error is not None:
        details.append(
            {
                "kind": "reproduced_failure",
                "summary": f"{type(reproduced_error).__name__}: {reproduced_error}",
                "qualname": trace.test_class if trace is not None else trace_file,
                "details": {
                    "error_type": type(reproduced_error).__name__,
                    "error_message": str(reproduced_error),
                },
            }
        )
    artifacts = (
        [_agent_artifact("trace", output_path, "saved shrunk trace")]
        if output_path is not None and output_path.exists()
        else []
    )
    suggested_commands: list[str] = []
    if reproduced_error is not None and trace is not None:
        if shrunk_trace is None:
            suggested_commands.append(f"ordeal replay {trace_file} --shrink")
        if ablation is None:
            suggested_commands.append(f"ordeal replay {trace_file} --ablate")
    report = {
        "target": trace_file,
        "tool": "replay",
        "status": (
            "failure reproduced"
            if reproduced_error is not None
            else ("blocked" if blocking_reason else "failure did not reproduce")
        ),
        "summary": [
            f"Trace file: {trace_file}",
            (f"Steps replayed: {len(trace.steps)}" if trace is not None else "Steps replayed: 0"),
        ],
        "details": details,
        "suggested_commands": suggested_commands,
    }
    recommended = "Inspect the current code or regenerate the trace."
    if reproduced_error is not None:
        if shrunk_trace is None:
            recommended = "Shrink the trace to a minimal reproducer."
        elif ablation is None:
            recommended = "Ablate fault toggles to isolate which ones are necessary."
        else:
            recommended = "Turn the reproducing trace into a regression test."
    elif blocking_reason:
        recommended = "Regenerate or fix the trace file before replaying again."
    return _build_agent_envelope_from_report(
        {**report, "recommended_action": recommended},
        status=(
            "reproduced"
            if reproduced_error is not None
            else ("blocked" if blocking_reason else "not_reproduced")
        ),
        confidence=(1.0 if trace is not None else None),
        confidence_basis=(
            (
                f"{len(trace.steps)} recorded step(s) replayed"
                if trace is not None
                else "trace could not be loaded"
            ),
        ),
        blocking_reason=blocking_reason,
        artifacts=artifacts,
        raw_details={
            "trace_file": trace_file,
            "trace": trace.to_dict() if trace is not None else None,
            "test_class": getattr(trace, "test_class", None),
            "run_id": getattr(trace, "run_id", None),
            "step_count": len(trace.steps) if trace is not None else 0,
            "shrunk_trace": shrunk_trace.to_dict() if shrunk_trace is not None else None,
            "shrunk_steps": len(shrunk_trace.steps) if shrunk_trace is not None else None,
            "ablation": dict(ablation) if ablation is not None else None,
        },
    )
def _build_blocked_agent_envelope(
    *,
    tool: str,
    target: str,
    summary: str,
    blocking_reason: str,
    suggested_commands: Sequence[str] = (),
    suggested_test_file: str | None = None,
    raw_details: Mapping[str, Any] | None = None,
) -> Any:
    """Build a minimal blocked/error envelope for early CLI exits."""
    from ordeal.agent_schema import build_agent_envelope

    return build_agent_envelope(
        tool=tool,
        target=target,
        status="blocked",
        summary=summary,
        recommended_action=(
            f"Unblock `{tool}` by fixing the input or running `{suggested_commands[0]}`."
            if suggested_commands
            else f"Unblock `{tool}` by fixing the input or environment."
        ),
        suggested_commands=suggested_commands,
        suggested_test_file=suggested_test_file,
        confidence=None,
        confidence_basis=("command did not reach a measured execution path",),
        blocking_reason=blocking_reason,
        findings=(),
        artifacts=(),
        raw_details=dict(raw_details or {}),
    )
def _write_scan_report(
    state: Any,
    path_str: str,
    *,
    regression_path: Path | None = None,
) -> Path:
    """Write a Markdown report for `ordeal scan`."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_scan_report_markdown(state, regression_path=regression_path),
        encoding="utf-8",
    )
    _stderr(f"Scan report saved: {path}\n")
    return path
def _write_scan_bundle(
    state: Any,
    *,
    path_str: str,
    report_path: Path,
    regression_path: Path | None,
    config_path: Path | None = None,
    support_path: Path | None = None,
    proofs_path: Path | None = None,
    replay_path: Path | None = None,
    scenario_library_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write a machine-readable JSON finding bundle for `ordeal scan`."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = _build_scan_bundle(
        state,
        report_path=report_path,
        regression_path=regression_path,
        config_path=config_path,
        support_path=support_path,
        proofs_path=proofs_path,
        replay_path=replay_path,
        scenario_library_path=scenario_library_path,
    )
    bundle["artifacts"]["bundle"] = _display_path(path)
    _write_json_file(path, bundle)
    _stderr(f"Scan bundle saved: {path}\n")
    return path, bundle
def _write_scan_regressions(state: Any, path_str: str) -> Path | None:
    """Write runnable pytest regressions for concrete scan findings."""
    stubs, skipped = _scan_regression_stubs(state)
    if not stubs:
        _stderr("No concrete regression tests could be generated from current scan findings.\n")
        if skipped:
            _stderr(f"Skipped {len(skipped)} finding(s) without replayable concrete inputs.\n")
        return None
    path, added, deduped = _write_regression_file(
        path_str=path_str,
        header=[
            '"""Generated by `ordeal scan --write-regression`.',
            "",
            f"Target: {state.module}",
            '"""',
            "",
        ],
        stubs=stubs,
    )
    if added > 0:
        verb = "written" if added == len(stubs) and deduped == 0 else "updated"
        _stderr(f"Regression tests {verb}: {path}\n")
    else:
        _stderr(f"Regression tests already present: {path}\n")
    _stderr(f"Run: uv run pytest {path} -q\n")
    if skipped:
        _stderr(f"Skipped {len(skipped)} finding(s) without replayable concrete inputs.\n")
    if deduped:
        _stderr(f"Skipped {deduped} existing regression(s) already present in {path.name}.\n")
    return path
def _write_scan_artifact_index(
    *,
    bundle: dict[str, Any],
    bundle_path: Path,
) -> Path:
    """Append a `scan --save-artifacts` record to the artifact index."""
    path = Path(_default_artifact_index_path())
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {"version": 1, "entries": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
            payload = {
                "version": int(loaded.get("version", 1)),
                "entries": list(loaded["entries"]),
            }

    payload["entries"].append(
        {
            "kind": "scan",
            "created_at": bundle["saved_at"],
            "module": bundle["target"],
            "workspace": bundle.get("workspace"),
            "status": bundle["status"],
            "confidence": bundle["confidence"],
            "seed": bundle.get("seed"),
            "finding_count": bundle["finding_count"],
            "finding_ids": [finding["finding_id"] for finding in bundle["findings"]],
            "findings": [
                {
                    "finding_id": detail.get("finding_id"),
                    "fingerprint": detail.get("fingerprint"),
                    "qualname": detail.get("qualname"),
                    "kind": detail.get("kind"),
                    "name": detail.get("name"),
                    "summary": detail.get("summary"),
                }
                for detail in bundle["findings"]
            ],
            "regressions": [
                {
                    "finding_id": detail.get("finding_id"),
                    "test_name": detail.get("regression_test"),
                    "binding": detail.get("regression_binding"),
                    "path": bundle["artifacts"].get("regression"),
                }
                for detail in bundle["findings"]
                if detail.get("regression_binding") is not None
            ],
            "artifacts": {
                **bundle["artifacts"],
                "bundle": bundle["artifacts"]["bundle"] or _display_path(bundle_path),
            },
            "commands": dict(bundle["commands"]),
        }
    )
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _stderr(f"Artifact index updated: {path}\n")
    return path
def _write_regression_manifest(bundle: Mapping[str, Any]) -> Path | None:
    """Persist portable CI bindings beside the generated regression module."""
    regression_path = bundle.get("artifacts", {}).get("regression")
    if not regression_path:
        return None
    records = []
    for finding in bundle.get("findings", ()):
        binding = finding.get("regression_binding")
        if not isinstance(binding, Mapping):
            continue
        evidence = finding.get("evidence")
        witness = evidence.get("witness") if isinstance(evidence, Mapping) else None
        subject = evidence.get("subject") if isinstance(evidence, Mapping) else None
        records.append(
            {
                "finding_id": finding.get("finding_id"),
                "target": finding.get("qualname") or finding.get("function"),
                "test_file": regression_path,
                "test_name": finding.get("regression_test"),
                "binding": dict(binding),
                "witness_sha256": (
                    witness.get("sha256") if isinstance(witness, Mapping) else None
                ),
                "source_sha256": (
                    subject.get("source_sha256") if isinstance(subject, Mapping) else None
                ),
            }
        )
    if not records:
        return None

    path = Path(_DEFAULT_REGRESSION_MANIFEST)
    payload: dict[str, Any] = {
        "schema": "ordeal.regression-manifest/v1",
        "regressions": [],
    }
    if path.is_file():
        try:
            loaded = _read_json_file(path)
        except json.JSONDecodeError:
            loaded = {}
        if (
            isinstance(loaded, dict)
            and loaded.get("schema") == "ordeal.regression-manifest/v1"
            and isinstance(loaded.get("regressions"), list)
        ):
            payload["regressions"] = list(loaded["regressions"])
    by_id = {
        str(item.get("finding_id")): item
        for item in payload["regressions"]
        if isinstance(item, Mapping) and item.get("finding_id")
    }
    for record in records:
        by_id[str(record["finding_id"])] = record
    payload["regressions"] = [by_id[finding_id] for finding_id in sorted(by_id)]
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(path, payload)
    _stderr(f"Regression manifest updated: {path}\n")
    return path
def _print_scan_artifact_workflow(
    *,
    module: str,
    report_path: Path,
    bundle_path: Path,
    finding_ids: list[str],
    regression_path: Path | None,
    regression_manifest_path: Path | None,
    config_path: Path | None,
    support_path: Path | None,
    proofs_path: Path | None,
    replay_path: Path | None,
    scenario_library_path: Path | None,
    index_path: Path,
) -> None:
    """Print available artifacts and commands after saving scan artifacts."""
    print("")
    print("artifacts:")
    print(f"  report: {_display_path(report_path)}")
    print(f"  bundle: {_display_path(bundle_path)}")
    if regression_path is not None:
        print(f"  regression: {_display_path(regression_path)}")
    else:
        print("  regression: not generated from current findings")
    if regression_manifest_path is not None:
        print(f"  regression-manifest: {_display_path(regression_manifest_path)}")
    if config_path is not None:
        print(f"  config: {_display_path(config_path)}")
    if support_path is not None:
        print(f"  support: {_display_path(support_path)}")
    if proofs_path is not None:
        print(f"  proofs: {_display_path(proofs_path)}")
    if replay_path is not None:
        print(f"  replay: {_display_path(replay_path)}")
    if scenario_library_path is not None:
        print(f"  scenarios: {_display_path(scenario_library_path)}")
    print(f"  index: {_display_path(index_path)}")
    print("available:")
    if len(finding_ids) == 1 and regression_path is not None:
        verify_cmd = _shell_command(
            "uv",
            "run",
            "ordeal",
            "verify",
            finding_ids[0],
            "--allow-unsafe-artifacts",
        )
        print(f"  verify: {verify_cmd}")
    if regression_path is not None:
        run_cmd = _shell_command("uv", "run", "pytest", _display_path(regression_path), "-q")
        print(f"  pytest: {run_cmd}")
        guard_cmd = _shell_command("uv", "run", "ordeal", "verify", "--ci")
        print(f"  ci: {guard_cmd}")
    if config_path is not None:
        print(f"  review-config: {_shell_command('cat', _display_path(config_path))}")
    if support_path is not None:
        print(f"  review-support: {_shell_command('cat', _display_path(support_path))}")
    rescan = _shell_command("uv", "run", "ordeal", "scan", module, "--save-artifacts")
    print(f"  rescan: {rescan}")
def _append_index_entry(index_path: Path, entry: dict[str, Any]) -> None:
    """Append one event entry to the artifact index."""
    payload: dict[str, Any] = {"version": 1, "entries": []}
    if index_path.exists():
        try:
            loaded = _read_json_file(index_path)
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
            payload = {
                "version": int(loaded.get("version", 1)),
                "entries": list(loaded["entries"]),
            }
    payload["entries"].append(entry)
    _write_json_file(index_path, payload)
def _locate_saved_finding(
    finding_id: str,
    *,
    index_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    """Return the latest bundle and finding record for a saved finding ID."""
    if not index_path.exists():
        return None
    payload = _read_json_file(index_path)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    current_workspace = _index_workspace(index_path)

    for entry in reversed(entries):
        artifacts = entry.get("artifacts") or {}
        bundle_value = artifacts.get("bundle")
        if not bundle_value:
            continue
        raw_bundle_path = Path(str(bundle_value))
        candidates = (
            [raw_bundle_path]
            if raw_bundle_path.is_absolute()
            else [
                current_workspace / raw_bundle_path,
                Path(str(entry.get("workspace"))) / raw_bundle_path,
            ]
        )
        bundle_path = next((path for path in candidates if path.exists()), None)
        if bundle_path is None:
            continue
        bundle = _read_json_file(bundle_path)
        for finding in bundle.get("findings", []):
            if finding.get("finding_id") == finding_id:
                return bundle_path, bundle, finding
    return None
def _index_workspace(index_path: Path) -> Path:
    """Resolve the portable workspace root for a finding index."""
    resolved = index_path.resolve()
    if resolved.parent.name == "findings" and resolved.parent.parent.name == ".ordeal":
        return resolved.parent.parent.parent
    return Path.cwd().resolve()
def _verification_command(
    bundle: dict[str, Any],
    finding: dict[str, Any],
) -> tuple[list[str], str] | None:
    """Build the exact pytest command for verifying one finding."""
    regression_path = bundle.get("artifacts", {}).get("regression")
    if not regression_path:
        return None

    regression_test = finding.get("regression_test")
    if regression_test:
        nodeid = f"{regression_path}::{regression_test}"
        return (
            [sys.executable, "-m", "pytest", nodeid, "-q"],
            _shell_command("uv", "run", "pytest", nodeid, "-q"),
        )

    if bundle.get("finding_count") == 1:
        return (
            [sys.executable, "-m", "pytest", regression_path, "-q"],
            _shell_command("uv", "run", "pytest", regression_path, "-q"),
        )

    return None
def _verify_regression_binding(
    bundle: Mapping[str, Any],
    finding: Mapping[str, Any],
    *,
    workspace: str | Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify that the saved witness regression still matches its AST binding."""
    from ordeal.regression_evidence import _regression_binding, _regression_binding_matches

    expected = finding.get("regression_binding")
    if not isinstance(expected, Mapping):
        return (
            None,
            "Saved finding has no regression binding; re-run `ordeal scan --save-artifacts`.",
        )
    regression_path = _resolve_artifact_path(
        bundle.get("artifacts", {}).get("regression"),
        workspace=str(workspace or bundle.get("workspace") or ""),
    )
    if regression_path is None or not regression_path.is_file():
        return None, "The bound regression file is missing; re-run `ordeal scan --save-artifacts`."
    test_name = str(expected.get("test_name") or "")
    observed = _regression_binding(regression_path.read_text(encoding="utf-8"), test_name)
    if observed is None:
        return None, f"The bound regression test `{test_name}` is missing or invalid."
    if not _regression_binding_matches(expected, observed):
        return observed, (
            "The saved regression test or target import changed after the scan; "
            "refusing to treat it as the same-witness control."
        )
    return observed, None
def _path_is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved artifact stays inside the CI workspace."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
def _sha256_json(value: object) -> str:
    """Hash one JSON-safe value with the repository's canonical encoding."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
