from __future__ import annotations
# ruff: noqa
def _record_post_fix_control(
    finding: dict[str, Any],
    *,
    verification_status: str,
    checked_at: str,
    command: str,
    exit_code: int,
    observed_binding: Mapping[str, Any],
) -> str | None:
    """Record the bound regression outcome on a finding evidence card."""
    evidence = finding.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    control = evidence.get("post_fix_control")
    if not isinstance(control, Mapping):
        return None
    control_status = {
        "verified": "passed",
        "reproduced": "failed",
        "error": "error",
    }[verification_status]
    finding["evidence"] = {
        **dict(evidence),
        "post_fix_control": {
            **dict(control),
            "status": control_status,
            "checked_at": checked_at,
            "command": command,
            "exit_code": exit_code,
            "observed_regression_binding": dict(observed_binding),
        },
    }
    workflow = finding["evidence"].get("workflow")
    if isinstance(workflow, Mapping):
        finding["evidence"]["workflow"] = {
            **dict(workflow),
            "verify_fix": control_status,
        }
    return control_status
def _cmd_verify(args: argparse.Namespace) -> int:
    """Re-run the saved regression for one finding ID."""
    import subprocess

    if bool(getattr(args, "ci", False)):
        if args.finding_id:
            _stderr(
                "Do not pass a finding ID with --ci; the CI guard checks every saved finding.\n"
            )
            return 2
        return _cmd_verify_ci(args)

    if not args.finding_id:
        _stderr("A finding ID is required unless --ci is used.\n")
        return 2
    if not bool(getattr(args, "allow_unsafe_artifacts", False)):
        _stderr(
            "Refusing to load artifact indexes or bundles without "
            "--allow-unsafe-artifacts. Saved findings may point verify at "
            "attacker-controlled repo tests.\n"
        )
        return 2

    index_path = Path(args.index)
    try:
        located = _locate_saved_finding(args.finding_id, index_path=index_path)
    except json.JSONDecodeError as exc:
        _stderr(f"Artifact data is not valid JSON: {exc}\n")
        return 2
    if located is None:
        try:
            compose_located = _locate_compose_manifest_record(
                args.finding_id,
                Path(args.manifest),
            )
        except json.JSONDecodeError as exc:
            _stderr(f"Regression manifest is not valid JSON: {exc}\n")
            return 2
        if compose_located is None:
            _stderr(f"Finding not found in artifact index or manifest: {args.finding_id}\n")
            return 2
        workspace, record = compose_located
        report, replay_error = _replay_compose_manifest_record(record, workspace=workspace)
        if replay_error is not None:
            _stderr(f"Compose regression verification error: {replay_error}.\n")
            return 2
        assert report is not None
        clean, clean_replays = _compose_replay_is_clean(report)
        if clean:
            control, control_error = _persist_compose_post_fix_control(
                Path(args.manifest),
                finding_id=args.finding_id,
                workspace=workspace,
                replay_report=report,
            )
            if control_error is not None:
                _stderr(f"Compose post-fix evidence error: {control_error}.\n")
                return 2
            assert control is not None
            print(
                f"verified: {args.finding_id} "
                f"(Compose clean replays {clean_replays}/{report.attempted})"
            )
            fixed_state = control.get("fixed_state", {})
            if isinstance(fixed_state, Mapping):
                print(
                    "  fixed-state evidence: "
                    f"{fixed_state.get('status', 'incomplete')} "
                    f"(workload protection: "
                    f"{fixed_state.get('workload_protection', {}).get('status', 'inconclusive')})"
                )
            print(f"  manifest: {_display_path(Path(args.manifest))}")
            if control.get("status") != "passed":
                _stderr(
                    f"fixed-state evidence incomplete for {args.finding_id}; "
                    "coverage and configured workload protection must both complete.\n"
                )
                return 1
            return 0
        _stderr(
            f"reproduced: {args.finding_id} (Compose clean replays "
            f"{clean_replays}/{report.attempted}; every replay must be clean)\n"
        )
        return 1

    bundle_path, bundle, finding = located
    command = _verification_command(bundle, finding)
    if command is None:
        _stderr(
            f"No runnable regression is recorded for {args.finding_id}. "
            "Re-run `ordeal scan --save-artifacts` first.\n"
        )
        return 2

    workspace = _index_workspace(index_path)
    observed_binding, binding_error = _verify_regression_binding(
        bundle,
        finding,
        workspace=workspace,
    )
    if binding_error is not None:
        _stderr(f"Regression binding check failed: {binding_error}\n")
        return 2
    assert observed_binding is not None

    run_args, display_command = command
    proc = subprocess.run(
        run_args,
        cwd=str(workspace),
        text=True,
        capture_output=True,
        check=False,
    )

    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if proc.returncode == 0:
        verification_status = "verified"
        finding["status"] = "verified"
        rc = 0
    elif proc.returncode == 1:
        verification_status = "reproduced"
        finding["status"] = "reproduced"
        rc = 1
    else:
        verification_status = "error"
        rc = 2

    bundle["verification"] = {
        "checked_at": checked_at,
        "finding_id": args.finding_id,
        "status": verification_status,
        "command": display_command,
        "exit_code": proc.returncode,
        "regression_binding": observed_binding,
    }
    control_status = _record_post_fix_control(
        finding,
        verification_status=verification_status,
        checked_at=checked_at,
        command=display_command,
        exit_code=proc.returncode,
        observed_binding=observed_binding,
    )
    _write_json_file(bundle_path, bundle)

    _append_index_entry(
        index_path,
        {
            "kind": "verification",
            "created_at": checked_at,
            "module": bundle.get("target"),
            "workspace": str(workspace),
            "finding_id": args.finding_id,
            "status": verification_status,
            "qualname": finding.get("qualname"),
            "exit_code": proc.returncode,
            "post_fix_control": control_status,
            "regression_binding": observed_binding,
            "artifacts": dict(bundle.get("artifacts", {})),
            "commands": {
                "verify": display_command,
            },
        },
    )

    print(f"verify: {args.finding_id}")
    print(f"  target: {finding.get('qualname', bundle.get('target', '?'))}")
    print(f"  status: {verification_status}")
    if control_status is not None:
        print(f"  post-fix control: {control_status}")
    print(f"  command: {display_command}")
    print(f"  bundle: {_display_path(bundle_path)}")
    print(f"  index: {_display_path(index_path)}")

    if verification_status == "error":
        if proc.stderr.strip():
            _stderr(proc.stderr)
        elif proc.stdout.strip():
            _stderr(proc.stdout)
    return rc
def _write_mine_regressions(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    path_str: str,
    suspicious_count: int,
) -> Path | None:
    """Write runnable pytest regressions for concrete mine findings."""
    stubs, skipped_findings = _mine_regression_stubs(
        target=target,
        module=module,
        results=results,
        skipped=skipped,
        suspicious_count=suspicious_count,
    )
    if not stubs:
        _stderr("No concrete regression tests could be generated from current mine findings.\n")
        if skipped_findings:
            _stderr(
                f"Skipped {len(skipped_findings)} finding(s) without replayable concrete inputs.\n"
            )
        return None
    path, added, deduped = _write_regression_file(
        path_str=path_str,
        header=[
            '"""Generated by `ordeal mine --write-regression`.',
            "",
            f"Target: {target}",
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
    if skipped_findings:
        _stderr(
            f"Skipped {len(skipped_findings)} finding(s) without replayable concrete inputs.\n"
        )
    if deduped:
        _stderr(f"Skipped {deduped} existing regression(s) already present in {path.name}.\n")
    return path
def _write_mine_report(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    path_str: str,
    include_scan_hint: bool,
    suspicious_count: int,
) -> Path:
    """Write a Markdown report for `ordeal mine`."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = _build_mine_report(
        target=target,
        module=module,
        results=results,
        skipped=skipped,
        include_scan_hint=include_scan_hint,
        suspicious_count=suspicious_count,
    )
    path.write_text(_render_findings_report_markdown(report), encoding="utf-8")
    _stderr(f"Mine report saved: {path}\n")
    return path
def _write_json_report(
    results: list[tuple[str, ExplorationResult]],
    cfg: OrdealConfig,
) -> None:
    """Write JSON report to the configured output path."""
    report: dict[str, Any] = {
        "results": [
            {
                "test_class": class_path,
                "total_runs": r.total_runs,
                "total_steps": r.total_steps,
                "unique_edges": r.unique_edges,
                "checkpoints_saved": r.checkpoints_saved,
                "duration_seconds": r.duration_seconds,
                "telemetry": _exploration_telemetry_payload(r),
                "failures": [
                    {
                        "error_type": type(f.error).__name__,
                        "error_message": str(f.error)[:500],
                        "step": f.step,
                        "run_id": f.run_id,
                        "active_faults": f.active_faults,
                        "trace_steps": len(f.trace.steps) if f.trace else 0,
                    }
                    for f in r.failures
                ],
            }
            for class_path, r in results
        ],
    }
    path = Path(cfg.report.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    _stderr(f"Report saved: {path}\n")
# ============================================================================
# Entry point
# ============================================================================


def _scan_command_description() -> str:
    """Return the long-form `ordeal scan` help description."""
    return (
        "Find replayable failures in a Python package, module, file, or callable.\n\n"
        "Start with `ordeal scan .`; Ordeal auto-detects the current package. A normal\n"
        "scan executes target code but does not write project artifacts. If it finds a\n"
        "useful failure, run the same command with `--save` to create the evidence bundle\n"
        "and durable pytest regression, then follow the printed `ordeal verify` command.\n\n"
        "Every scan also builds a source-backed reliability map of operations, fault\n"
        "seams, and candidate properties. Static candidates remain NOT EXERCISED\n"
        "hypotheses until runtime evidence supports PASS or FAIL. Use `--base-ref` to\n"
        "prioritize changed code. Use `--deepen --time-limit SECONDS` to run one cheapest\n"
        "safe follow-up inside an explicit budget. Service faults additionally require\n"
        "`--allow-service-faults` and a configured `[compose]` section.\n\n"
        "Use `--list-targets` only when Ordeal cannot construct a method or you need to\n"
        "inspect what will run. Advanced ranking, fixture, security, and report controls\n"
        "remain compatible and are documented in the full CLI reference.\n\n"
        "Core loop:\n"
        "  ordeal scan .\n"
        "  ordeal scan . --base-ref origin/main --deepen --time-limit 60\n"
        "  ordeal scan . --save\n"
        "  ordeal verify <finding-id> --allow-unsafe-artifacts\n"
        "  ordeal verify --ci\n\n"
        "Docs: https://docs.byordeal.com/guides/scan-quickstart/\n"
        "Advanced reference: https://docs.byordeal.com/guides/cli/"
    )
def _catalog_command_description() -> str:
    """Return the long-form `ordeal catalog` help description."""
    return (
        "Inspect ordeal's live capability surface.\n\n"
        "The default text view groups capabilities by subsystem.\n"
        "`--detail` adds applicability, expected inputs/outputs, usage patterns,\n"
        "and adjacent learning surfaces for each entry.\n"
        "`--json` prints the same capability map as structured data so agents and\n"
        "tools can reason over it directly without scraping help text."
    )
def _verify_command_description() -> str:
    """Return the long-form `ordeal verify` help description."""
    return (
        "Re-run a saved regression from `.ordeal/findings/index.json` or the portable"
        " manifest.\n\n"
        "Use the stable `finding_id` from a JSON bug bundle, index, or Compose record.\n"
        "For safety, verification requires `--allow-unsafe-artifacts` because "
        "artifact bundles can point pytest at repo-controlled code.\n"
        "Python verification updates its bundle and index; Compose verification reads the"
        " bound trace and requires every replay to finish cleanly. Use `ordeal verify --ci`"
        " for a read-only,"
        " provider-neutral guard over every saved regression and binding."
    )
def _audit_command_description() -> str:
    """Return the long-form `ordeal audit` help description."""
    return (
        "Compare current tests with generated checks and report whether the resulting\n"
        "checks protect measured behavior. The protection verdict combines migrated\n"
        "line coverage, mutation survival, and property exercise. A surviving mutant\n"
        "keeps the verdict WEAK even at 100% line coverage.\n\n"
        "Use `ordeal mutate` to judge the selected existing pytest tests directly;\n"
        "audit's protection verdict describes the generated/migrated checks.\n\n"
        "Validation modes:\n"
        "  fast  replay mined inputs against mutants (default, faster)\n"
        "  deep  replay mined inputs, then re-mine mutants for extra search depth\n\n"
        "Use --list-targets to inspect the callable surface that audit can see, including\n"
        " methods that need configured factories.\n"
        "Use [audit] in ordeal.toml to persist module lists, validation depth, "
        "and direct-test policy, and reuse shared `[[objects]]` for bound methods.\n"
        "Use --write-gaps PATH to emit draft review stubs for surviving mutants "
        "and function-level coverage gaps."
    )
def _mutate_command_description() -> str:
    """Return the long-form `ordeal mutate` help description."""
    return (
        "Test whether the selected existing tests notice deliberate code changes.\n\n"
        "A surviving non-equivalent mutant is direct evidence of a test gap. Mutation\n"
        "score is scoped to the target, operator preset, filters, and executed tests;\n"
        "even a 100% score is not a universal correctness proof.\n\n"
        "Use `ordeal audit` when you also want generated-check coverage, property\n"
        "strength, exact line gaps, and the combined protection verdict."
    )
def _mine_command_description() -> str:
    """Return the long-form `ordeal mine` help description."""
    return (
        "Discover properties of a function or module.\n\n"
        "Use --report-file report.md to save a shareable Markdown finding report.\n"
        f"Use --write-regression or --write-regression PATH to save runnable pytest"
        f" regressions (default: {_DEFAULT_REGRESSION_PATH})."
    )
def _diff_command_description() -> str:
    """Return the long-form ``ordeal diff`` help description."""
    return (
        "Check whether a committed refactor changed one callable or module.\n\n"
        "Ordeal checks out the base and candidate into separate temporary worktrees. "
        "The base generates test inputs; the candidate receives those exact inputs. "
        "The command reports differences but does not decide which version is correct.\n\n"
        "Example:\n"
        "  ordeal diff mypkg.scoring --base-ref origin/main --candidate-ref HEAD\n\n"
        "HEAD means committed files only. Defaults may live in [diff] in ordeal.toml. "
        "NO DIVERGENCE OBSERVED is sampled evidence, never as proven equivalence.\n"
        "Beginner model: https://docs.byordeal.com/concepts/differential-testing/\n"
        "Divergence evidence: https://docs.byordeal.com/concepts/divergence-evidence/\n"
        "Start: https://docs.byordeal.com/guides/revision-diff/\n"
        "Fix problems: https://docs.byordeal.com/guides/revision-diff-troubleshooting/\n"
        "JSON fields: https://docs.byordeal.com/reference/revision-diff-schema/"
    )
def _migrate_command_description() -> str:
    """Return the long-form ``ordeal migrate`` help description."""
    return (
        "Replace one importable module without confusing 'same as before' with "
        "'correct and protected.' A perfect parity match can preserve an old bug.\n\n"
        "The fixed order is: audit base, mine candidate contracts, diff shared callables, "
        "classify intended changes, save unexpected divergences, mutate the resulting "
        "tests, then scan only the candidate. Declare intended changes with "
        "--intended-change and define explicit candidate invariants with matching "
        "[[contracts]] entries in ordeal.toml. Mined properties remain hypotheses.\n\n"
        "Unexpected divergences create failing parity regressions. Mutation is therefore "
        "blocked until the candidate is fixed or the change is explicitly reclassified; "
        "rerun the same command to resume from the saved witnesses.\n\n"
        "Start: https://docs.byordeal.com/concepts/safe-migrations/\n"
        "Guide: https://docs.byordeal.com/guides/migration-workflow/"
    )
def _init_command_description() -> str:
    """Return the long-form `ordeal init` help description."""
    return (
        "Bootstrap starter tests and ordeal.toml. By default this writes only the "
        "starter files, validates them, and prints a lightweight read-only scan "
        "summary. Use [init] in ordeal.toml to persist bootstrap defaults, and "
        "use --install-skill / --close-gaps to opt into extra writes."
    )
def _catalog_spec() -> CommandSpec:
    return CommandSpec(
        name="catalog",
        handler=_cmd_catalog,
        help="Show all capabilities — faults, mining, mutations, exploration, ...",
        description=_catalog_command_description,
        arguments=(
            _arg("--detail", action="store_true", help="Show full signatures and docstrings"),
            _arg("--json", action="store_true", help="Emit the capability map as JSON"),
        ),
    )
