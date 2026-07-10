from __future__ import annotations
# ruff: noqa
def _cmd_explore_compose(args: argparse.Namespace, cfg: OrdealConfig) -> int:
    """Run the long-lived Docker Compose service harness."""
    from ordeal.compose import REPLAY_BOUNDARY, run_compose_exploration, save_compose_regression

    if cfg.compose is None:
        _stderr("No [compose] section in config.\n")
        return 1
    unsupported = [
        option
        for option, value in (
            ("--resume", args.resume),
            ("--save-state", args.save_state),
            ("--generate-tests", args.generate_tests),
        )
        if value
    ]
    if unsupported:
        _stderr(f"Compose runner does not support: {', '.join(unsupported)}\n")
        return 2
    if args.workers not in {None, 1}:
        _stderr("Compose runner owns one long-lived topology; --workers must be 1.\n")
        return 2

    _stderr(f"Exploring Compose services from {cfg.compose.file}...\n")
    try:
        result = run_compose_exploration(
            cfg.compose,
            seed=args.seed,
            max_time=args.max_time,
            replay_attempts=args.replay_attempts,
        )
    except (OSError, ValueError) as exc:
        _stderr(f"Compose runner error: {exc}\n")
        return 2

    _stderr(
        f"  Actions: {len(result.trace.actions)} exact, "
        f"requests={result.requests}, faults={result.faults}\n"
    )
    _stderr(f"  Trace: {result.trace_path}\n")
    coverage_rows = result.coverage.get("rows", [])
    if coverage_rows:
        _stderr("  Reliability coverage (operation × fault × property):\n")
        for row in coverage_rows:
            _stderr(
                f"    {row['operation']} × {row['fault']} × {row['property']}: "
                f"{row['status']} ({row['passes']}/{row['hits']} passed)\n"
            )
    if result.protection:
        _stderr(
            "  Workload protection: "
            f"{str(result.protection.get('status', 'inconclusive')).upper()}: "
            f"{result.protection.get('summary', 'not measured')}\n"
        )
    if result.trace.failure is None:
        _stderr("  No failure recorded; replay not attempted.\n")
        _stderr(f"  Boundary: {REPLAY_BOUNDARY}\n")
        if args.save_artifacts:
            evidence_path = _save_compose_run_payload(result)
            _stderr(f"  Complete run evidence: {evidence_path}\n")
            _stderr("  No durable regression saved because no failure was recorded.\n")
        if args.json:
            print(json.dumps(_compose_run_payload(result), indent=2))
        return 0

    failure = result.trace.failure
    _stderr(f"  Failure: {failure.kind}: {failure.message}\n")
    assert result.replay is not None
    _stderr(
        f"  Replay attempted {result.replay.attempted} times, "
        f"reproduced {result.replay.reproduced} times.\n"
    )
    if result.evidence is not None:
        _stderr("  Evidence card:\n")
        for label, value in _evidence_card_fields(result.evidence):
            _stderr(f"    {label}: {value}\n")
    if args.save_artifacts:
        evidence_path = _save_compose_run_payload(result)
        _stderr(f"  Complete run evidence: {evidence_path}\n")
        try:
            artifacts = save_compose_regression(result)
        except (OSError, ValueError) as exc:
            _stderr(f"  Durable regression error: {exc}\n")
            _stderr(f"  Boundary: {result.replay.boundary}\n")
            return 2
        if artifacts is None:
            _stderr(
                "  Durable regression not saved: no replay-backed service-contract "
                "failure was observed.\n"
            )
        else:
            try:
                durable_display = (
                    artifacts.trace_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
                )
                manifest_display = (
                    artifacts.manifest_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
                )
            except ValueError:
                durable_display = artifacts.trace_path.as_posix()
                manifest_display = artifacts.manifest_path.as_posix()
            _stderr(f"  Durable trace: {durable_display}\n")
            _stderr(f"  Regression manifest: {manifest_display}\n")
            verify_command = _shell_command(
                "uv",
                "run",
                "ordeal",
                "verify",
                artifacts.finding_id,
                "--allow-unsafe-artifacts",
            )
            _stderr(f"  Verify fix: {verify_command}\n")
            _stderr(f"  CI guard: {_shell_command('uv', 'run', 'ordeal', 'verify', '--ci')}\n")
    _stderr(f"  Boundary: {result.replay.boundary}\n")
    if args.json:
        print(json.dumps(_compose_run_payload(result), indent=2))
    return 1
def _cmd_explore(args: argparse.Namespace) -> int:
    """Run coverage-guided or long-lived service exploration from ordeal.toml."""
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    if getattr(args, "runner", "python") == "compose":
        return _cmd_explore_compose(args, cfg)

    explorer_cls = Explorer
    if explorer_cls is None:
        from ordeal.explore import Explorer as explorer_cls

    # CLI overrides
    if args.seed is not None:
        cfg.explorer.seed = args.seed
    if args.max_time is not None:
        cfg.explorer.max_time = args.max_time
    if args.workers is not None:
        cfg.explorer.workers = args.workers
    verbose = args.verbose or cfg.report.verbose
    allow_unsafe_resume = bool(getattr(args, "allow_unsafe_resume", False))

    if args.resume and not allow_unsafe_resume:
        _stderr(
            "Refusing to load --resume without --allow-unsafe-resume. "
            "Explorer state files use pickle and may execute arbitrary code.\n"
        )
        return 2

    if not cfg.tests:
        if cfg.scan:
            return _run_configured_scans(
                cfg.scan,
                cfg=cfg,
                shared_fixture_registries=cfg.fixtures.registries,
                verbose=verbose,
            )
        _stderr("No [[tests]] entries in config.\n")
        return 1

    all_results: list[tuple[str, ExplorationResult]] = []
    exit_code = 0

    for test_cfg in cfg.tests:
        try:
            test_class = test_cfg.resolve()
        except (ImportError, AttributeError) as e:
            _stderr(f"Cannot import {test_cfg.class_path}: {e}\n")
            exit_code = 1
            continue

        _stderr(f"Exploring {test_cfg.class_path}...\n")

        corpus_dir = None if args.no_seeds else cfg.report.corpus_dir
        explorer = explorer_cls(
            test_class,
            target_modules=cfg.explorer.target_modules,
            seed=cfg.explorer.seed,
            max_checkpoints=cfg.explorer.max_checkpoints,
            checkpoint_prob=cfg.explorer.checkpoint_prob,
            checkpoint_strategy=cfg.explorer.checkpoint_strategy,
            fault_toggle_prob=cfg.explorer.fault_toggle_prob,
            record_traces=cfg.report.traces or bool(args.generate_tests),
            workers=cfg.explorer.workers,
            seed_mutation_respect_strategies=getattr(
                cfg.explorer, "seed_mutation_respect_strategies", False
            ),
            ngram=cfg.explorer.ngram,
            corpus_dir=corpus_dir,
            rule_swarm=cfg.explorer.rule_swarm,
        )

        result = explorer.run(
            max_time=cfg.explorer.max_time,
            max_runs=cfg.explorer.max_runs,
            steps_per_run=getattr(test_cfg, "steps_per_run", None) or cfg.explorer.steps_per_run,
            shrink=not args.no_shrink,
            progress=_ProgressPrinter() if verbose else None,
            resume_from=args.resume,
            save_state_to=args.save_state,
            allow_unsafe_resume=allow_unsafe_resume,
        )

        if verbose:
            _stderr("\n")  # newline after progress

        if result.seed_replays:
            reproduced = sum(1 for sr in result.seed_replays if sr["reproduced"])
            fixed = len(result.seed_replays) - reproduced
            if verbose:
                for sr in result.seed_replays:
                    if sr["reproduced"]:
                        _stderr(f"  REGRESSION  {sr['seed_name']}: {sr['error']}\n")
                    else:
                        _stderr(f"  fixed       {sr['seed_name']}: no longer reproduces\n")
            else:
                _stderr(f"  Seed replay: {fixed} fixed, {reproduced} reproduced\n")
            if args.prune_fixed_seeds and fixed:
                pruned = 0
                for sr in result.seed_replays:
                    if sr["reproduced"]:
                        continue
                    try:
                        Path(sr["path"]).unlink()
                    except FileNotFoundError:
                        continue
                    else:
                        pruned += 1
                if pruned:
                    _stderr(f"  Pruned {pruned} fixed seed(s).\n")

        all_results.append((test_cfg.class_path, result))

        if result.failures:
            exit_code = 1
            # Report saved seeds
            if not args.no_seeds:
                seed_dir = Path(cfg.report.corpus_dir)
                seed_files = list(seed_dir.rglob("seed-*.json")) if seed_dir.exists() else []
                if seed_files:
                    _stderr(
                        f"  Seeds saved: {len(seed_files)} in {seed_dir}/"
                        f" (auto-replay on next run)\n"
                    )

        # Save traces
        if cfg.report.traces:
            traces_dir = Path(cfg.report.traces_dir)
            for trace in result.traces:
                if trace.failure:
                    trace.save(traces_dir / f"fail-run-{trace.run_id}.json")

        # Generate tests from traces
        if args.generate_tests and result.traces:
            from ordeal.trace import generate_tests

            test_src = generate_tests(result.traces, class_path=test_cfg.class_path)
            if test_src:
                out = Path(args.generate_tests)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(test_src, encoding="utf-8")
                _stderr(f"Generated tests: {out}\n")

    # -- Report --
    _print_report(all_results, cfg)

    # JSON report
    if cfg.report.format in ("json", "both"):
        _write_json_report(all_results, cfg)

    return exit_code
def _cmd_replay_compose(args: argparse.Namespace) -> int:
    """Replay a Compose trace repeatedly and report the honest boundary."""
    from ordeal.compose import ComposeTrace, replay_compose_trace

    incompatible = [
        option
        for option, enabled in (
            ("--shrink", args.shrink),
            ("--ablate", args.ablate),
            ("--output", bool(args.output)),
        )
        if enabled
    ]
    if incompatible:
        _stderr(f"Compose traces do not support: {', '.join(incompatible)}\n")
        return 2
    try:
        trace = ComposeTrace.load(args.trace_file)
        report = replay_compose_trace(trace, attempts=args.attempts)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "runner": "compose",
                        "status": "error",
                        "trace_file": args.trace_file,
                        "error": str(exc),
                    },
                    sort_keys=True,
                )
            )
        else:
            _stderr(f"Cannot replay Compose trace: {exc}\n")
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "runner": "compose",
                    "status": "reproduced" if report.reproduced else "not_reproduced",
                    "trace_file": args.trace_file,
                    "actions": len(trace.actions),
                    "failure_signature": trace.failure_signature,
                    "replay": report.to_dict(),
                },
                sort_keys=True,
            )
        )
    else:
        _stderr(
            f"Replaying Compose trace ({len(trace.actions)} exact actions, "
            f"signature={trace.failure_signature or 'none'})...\n"
        )
        _stderr(
            f"Replay attempted {report.attempted} times, reproduced {report.reproduced} times.\n"
        )
        _stderr(f"Boundary: {report.boundary}\n")
    return 1 if report.reproduced else 0
def _cmd_replay(args: argparse.Namespace) -> int:
    """Replay a saved trace."""
    from ordeal.compose import ComposeTrace

    if ComposeTrace.is_trace_file(args.trace_file):
        return _cmd_replay_compose(args)

    from ordeal.trace import Trace, ablate_faults, replay, shrink

    try:
        trace = Trace.load(args.trace_file)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        if getattr(args, "json", False):
            print(
                _build_replay_agent_envelope(
                    trace_file=args.trace_file,
                    trace=None,
                    reproduced_error=None,
                    blocking_reason=str(e),
                ).to_json()
            )
            return 1
        _stderr(f"Cannot load trace: {e}\n")
        return 1

    if not getattr(args, "json", False):
        msg = f"Replaying {trace.test_class} (run {trace.run_id}, {len(trace.steps)} steps)..."
        _stderr(f"{msg}\n")

    error = replay(trace)
    shrunk = None
    faults = None
    if error is not None:
        if not getattr(args, "json", False):
            _stderr(f"Failure reproduced: {type(error).__name__}: {error}\n")
        if args.shrink:
            if not getattr(args, "json", False):
                _stderr("Shrinking...\n")
            shrunk = shrink(trace)
            if not getattr(args, "json", False):
                _stderr(f"Shrunk to {len(shrunk.steps)} steps (from {len(trace.steps)})\n")
            if args.output:
                shrunk.save(args.output)
                if not getattr(args, "json", False):
                    _stderr(f"Saved: {args.output}\n")
            trace = shrunk  # use shrunk trace for ablation
        if args.ablate:
            if not getattr(args, "json", False):
                _stderr("Ablating faults...\n")
            faults = ablate_faults(trace)
            if faults and not getattr(args, "json", False):
                needed = [f for f, necessary in faults.items() if necessary]
                unneeded = [f for f, necessary in faults.items() if not necessary]
                if needed:
                    _stderr(f"Necessary faults: {', '.join(needed)}\n")
                if unneeded:
                    _stderr(f"Unnecessary faults: {', '.join(unneeded)}\n")
                if not needed:
                    _stderr("Bug reproduces without any faults.\n")
            elif not getattr(args, "json", False):
                _stderr("No fault toggles in trace.\n")
        if getattr(args, "json", False):
            print(
                _build_replay_agent_envelope(
                    trace_file=args.trace_file,
                    trace=trace,
                    reproduced_error=error,
                    shrunk_trace=shrunk,
                    ablation=faults,
                    output_path=Path(args.output) if args.output else None,
                ).to_json()
            )
        return 1
    else:
        if getattr(args, "json", False):
            print(
                _build_replay_agent_envelope(
                    trace_file=args.trace_file,
                    trace=trace,
                    reproduced_error=None,
                ).to_json()
            )
        else:
            _stderr("Failure did not reproduce.\n")
        return 0
def _cmd_seeds(args: argparse.Namespace) -> int:
    """List or manage the persistent seed corpus."""
    from ordeal.trace import Trace
    from ordeal.trace import replay as _replay

    corpus = Path(args.dir)
    if not corpus.exists():
        _stderr("No seed corpus found.\n")
        _stderr("  Seeds are saved automatically when ordeal explore finds failures.\n")
        _stderr(f"  Directory: {corpus}/\n")
        return 0

    seed_files = sorted(corpus.rglob("seed-*.json"))
    if not seed_files:
        _stderr("Seed corpus is empty.\n")
        return 0

    _stderr(f"Seed corpus: {len(seed_files)} seed(s) in {corpus}/\n\n")

    pruned = 0
    for sf in seed_files:
        try:
            trace = Trace.load(sf)
        except Exception:
            _stderr(f"  {sf.name}: corrupt (cannot load)\n")
            continue

        error = _replay(trace)
        class_name = trace.test_class.rsplit(":", 1)[-1] if ":" in trace.test_class else ""
        steps = len(trace.steps)

        if error is not None:
            err_short = f"{type(error).__name__}: {str(error)[:60]}"
            _stderr(f"  REPRODUCES  {sf.name}  {class_name} ({steps} steps) — {err_short}\n")
        else:
            _stderr(f"  fixed       {sf.name}  {class_name} ({steps} steps)\n")
            if args.prune_fixed:
                sf.unlink()
                pruned += 1

    if args.prune_fixed and pruned:
        _stderr(f"\nPruned {pruned} fixed seed(s).\n")

    return 0
