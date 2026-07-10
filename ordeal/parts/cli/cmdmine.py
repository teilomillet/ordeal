from __future__ import annotations
# ruff: noqa
def _cmd_mine(args: argparse.Namespace) -> int:
    """Discover properties of a function or all public functions in a module."""
    from importlib import import_module

    from ordeal.mine import _is_suspicious_property, mine

    target = args.target
    max_examples = args.max_examples

    # Split target into module path and optional function name
    parts = target.rsplit(".", 1)
    if len(parts) < 2:
        if getattr(args, "json", False):
            print(
                _build_blocked_agent_envelope(
                    tool="mine",
                    target=target,
                    summary=f"cannot resolve target {target}",
                    blocking_reason="target must be a dotted path like mymod.func",
                    suggested_commands=(f"ordeal mine {target}.func",),
                    raw_details={"target": target},
                ).to_json()
            )
            return 1
        _stderr(f"Target must be dotted path (e.g. mymod.func): {target}\n")
        return 1

    mod_path, attr = parts
    try:
        mod = import_module(mod_path)
    except ImportError:
        # Maybe the whole target is a module (no function specified)
        try:
            mod = import_module(target)
            attr = None
        except ImportError:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mine",
                        target=target,
                        summary=f"cannot import {target}",
                        blocking_reason=f"cannot import target: {target}",
                        suggested_commands=(f"ordeal scan {mod_path}",),
                        raw_details={"target": target, "module_path": mod_path},
                    ).to_json()
                )
                return 1
            _stderr(f"Cannot import: {target}\n")
            return 1

    if attr and hasattr(mod, attr) and callable(getattr(mod, attr)):
        # Single function — unwrap decorators (@ray.remote, functools.wraps)
        from ordeal.auto import _unwrap

        funcs = [(attr, _unwrap(getattr(mod, attr)))]
        report_target = target
        report_namespace = mod.__name__
        report_is_function = True
    else:
        # Maybe the full target is a module (e.g. "ordeal.demo")
        from ordeal.auto import _get_public_functions

        try:
            mod = import_module(target)
        except ImportError:
            mod = import_module(mod_path)
        inc_private = getattr(args, "include_private", False)
        funcs = _get_public_functions(mod, include_private=inc_private)
        if not funcs:
            hint = " (try --include-private for _prefixed functions)" if not inc_private else ""
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mine",
                        target=target,
                        summary=f"no testable functions found in {target}",
                        blocking_reason="module has no discoverable callable targets",
                        suggested_commands=(
                            (f"ordeal mine {target} --include-private",) if not inc_private else ()
                        ),
                        raw_details={"target": target, "include_private": inc_private},
                    ).to_json()
                )
                return 1
            _stderr(f"No testable functions found in {target}{hint}\n")
            return 1
        report_target = getattr(mod, "__name__", target)
        report_namespace = getattr(mod, "__name__", target)
        report_is_function = False

    skipped: list[tuple[str, str]] = []
    mined_results: list[tuple[str, Any]] = []
    suspicious = 0
    for name, func in funcs:
        try:
            result = mine(
                func,
                max_examples=max_examples,
                minimize_findings=bool(getattr(args, "write_regression", None)),
            )
        except (ValueError, TypeError) as e:
            reason = str(e).split(".")[0]
            skipped.append((name, reason))
            continue

        mined_results.append((name, result))
        suspicious += sum(1 for prop in result.properties if _is_suspicious_property(prop))
        if not getattr(args, "json", False):
            print(result.summary())
            if getattr(args, "verbose", False) and result.not_applicable:
                print(f"    n/a: {', '.join(result.not_applicable)}")
            print()

    if skipped and not getattr(args, "json", False):
        print(f"Skipped {len(skipped)} function(s):")
        for name, reason in skipped:
            print(f"  {name}: {reason}")

    written_report_path: Path | None = None
    if getattr(args, "report_file", None):
        written_report_path = _write_mine_report(
            target=report_target,
            module=report_namespace,
            results=mined_results,
            skipped=skipped,
            path_str=args.report_file,
            include_scan_hint=not report_is_function,
            suspicious_count=suspicious,
        )
    written_regression_path: Path | None = None
    if getattr(args, "write_regression", None):
        written_regression_path = _write_mine_regressions(
            target=report_target,
            module=report_namespace,
            results=mined_results,
            skipped=skipped,
            path_str=args.write_regression,
            suspicious_count=suspicious,
        )
    elif (
        suspicious and not getattr(args, "report_file", None) and not getattr(args, "json", False)
    ):
        print(
            f"tip: add --report-file report.md or --write-regression ({_DEFAULT_REGRESSION_PATH})"
        )

    if getattr(args, "json", False):
        print(
            _build_mine_agent_envelope(
                target=report_target,
                module=report_namespace,
                results=mined_results,
                skipped=skipped,
                include_scan_hint=not report_is_function,
                suspicious_count=suspicious,
                report_path=written_report_path,
                regression_path=written_regression_path,
            ).to_json()
        )
    return 0
def _cmd_mine_pair(args: argparse.Namespace) -> int:
    """Discover relational properties between two functions."""
    from importlib import import_module

    from ordeal.mine import mine_pair

    def _resolve_func(path: str):
        from ordeal.auto import _unwrap

        parts = path.rsplit(".", 1)
        if len(parts) < 2:
            return None
        mod = import_module(parts[0])
        obj = getattr(mod, parts[1], None)
        return _unwrap(obj) if obj is not None else None

    f = _resolve_func(args.f)
    g = _resolve_func(args.g)
    if f is None:
        _stderr(f"Cannot resolve: {args.f}\n")
        return 1
    if g is None:
        _stderr(f"Cannot resolve: {args.g}\n")
        return 1

    try:
        result = mine_pair(f, g, max_examples=args.max_examples)
    except (ValueError, TypeError) as e:
        _stderr(f"Error: {e}\n")
        return 1

    print(result.summary())
    if getattr(args, "verbose", False) and result.not_applicable:
        print(f"    n/a: {', '.join(result.not_applicable)}")
    return 0
def _cmd_migrate(args: argparse.Namespace) -> int:
    """Run the ordered base-to-candidate correctness and protection workflow."""
    from ordeal.migration import migrate

    try:
        cfg = _load_optional_config(getattr(args, "config", None))
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 2
    except ConfigError as exc:
        _stderr(f"Config error: {exc}\n")
        return 2

    invariants: dict[str, list[Any]] = {}
    if cfg is not None:
        try:
            invariants = _config_contract_checks_for_module(cfg, args.candidate)
        except Exception as exc:
            _stderr(f"Cannot resolve candidate contracts: {exc}\n")
            return 2

    try:
        result = migrate(
            args.base,
            args.candidate,
            intended_changes=args.intended_changes or [],
            invariants=invariants,
            test_dir=args.test_dir,
            audit_max_examples=args.audit_examples,
            mine_max_examples=args.mine_examples,
            diff_max_examples=args.diff_examples,
            scan_max_examples=args.scan_examples,
            mutation_preset=args.preset,
            mutation_workers=args.workers,
            mutation_threshold=args.threshold,
            evidence_path=args.evidence_path,
            regression_path=args.regression_path,
            manifest_path=args.manifest,
        )
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "tool": "ordeal migrate",
                        "base": args.base,
                        "candidate": args.candidate,
                        "status": "inconclusive",
                        "reason": str(exc),
                    }
                )
            )
        else:
            _stderr(f"Migration workflow unavailable: {exc}\n")
        return 2

    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.summary())
        if not invariants:
            _stderr(
                "No explicit candidate invariants were configured; add matching "
                "[[contracts]] entries in ordeal.toml before claiming correctness.\n"
            )
        _stderr(f"Evidence: {result.artifacts.evidence_path}\n")
        if result.artifacts.regression_cases:
            _stderr(f"Regressions: {result.artifacts.regression_path}\n")
    return 0 if result.protected_within_measured_scope else 1
def _diff_artifact_paths(artifact_dir: Path, target: str) -> tuple[Path, Path]:
    """Return stable JSON and Markdown paths for one revision diff target."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", target.replace(":", ".")).strip(".-")
    slug = slug or "target"
    return artifact_dir / f"{slug}.json", artifact_dir / f"{slug}.md"
def _write_diff_artifacts(result: Any, artifact_dir: Path) -> tuple[Path, Path]:
    """Persist machine-readable and review-readable revision diff evidence."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = _diff_artifact_paths(artifact_dir, result.target)
    payload = result.to_dict()
    payload["generated_at"] = datetime.now(UTC).isoformat()
    rerun = [
        "ordeal",
        "diff",
        result.target,
        "--base-ref",
        result.base.ref,
        "--candidate-ref",
        result.candidate.ref,
        "--max-examples",
        str(result.max_examples),
        "--seed",
        str(result.seed),
        "--replay-attempts",
        str(result.replay_attempts),
    ]
    if result.rtol is not None:
        rerun.extend(("--rtol", str(result.rtol)))
    if result.atol is not None:
        rerun.extend(("--atol", str(result.atol)))
    if result.mode == "system":
        sequence_path = json_path.with_name(f"{json_path.stem}.sequence.json")
        sequence_path.write_text(
            json.dumps(list(result.system_sequence), indent=2) + "\n",
            encoding="utf-8",
        )
        rerun.extend(("--sequence-file", sequence_path.as_posix()))
    payload["commands"] = {"rerun": _shell_command(*rerun)}
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    markdown = (
        f"# Ordeal revision diff: `{result.target}`\n\n"
        "This report compares the recorded commits in separate detached worktrees and "
        "subprocesses. Sampled agreement is not a proof of equivalence.\n\n"
        "```text\n"
        f"{result.summary()}\n"
        "```\n"
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    return json_path, markdown_path
def _warn_if_diff_head_is_dirty(candidate_ref: str) -> None:
    """Explain when committed ``HEAD`` excludes local working-tree changes."""
    if candidate_ref != "HEAD":
        return
    import subprocess

    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    changed = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode == 0 and changed:
        _stderr(
            f"Note: candidate HEAD uses committed content; {len(changed)} working-tree "
            "change(s) are not included.\n"
        )
