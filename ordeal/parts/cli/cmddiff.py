from __future__ import annotations
# ruff: noqa
def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare one callable or module across isolated Git revisions."""
    from ordeal._revision_diff import RevisionDiffError, run_revision_diff

    try:
        cfg = _load_optional_config(getattr(args, "config", None))
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 2
    except ConfigError as exc:
        _stderr(f"Config error: {exc}\n")
        return 2

    diff_cfg = cfg.diff if cfg is not None else None
    target = _cli_or_config(args.target, diff_cfg.target if diff_cfg else None)
    if not target:
        _stderr("No diff target specified. Pass TARGET or set [diff].target.\n")
        return 2

    base_ref = _cli_or_config(args.base_ref, diff_cfg.base_ref if diff_cfg else None)
    candidate_ref = str(
        _cli_or_config(args.candidate_ref, diff_cfg.candidate_ref if diff_cfg else "HEAD")
    )
    max_examples = int(
        _cli_or_config(args.max_examples, diff_cfg.max_examples if diff_cfg else 100)
    )
    seed = int(_cli_or_config(args.seed, diff_cfg.seed if diff_cfg else 42))
    rtol = _cli_or_config(args.rtol, diff_cfg.rtol if diff_cfg else None)
    atol = _cli_or_config(args.atol, diff_cfg.atol if diff_cfg else None)
    replay_attempts = int(
        _cli_or_config(
            args.replay_attempts,
            diff_cfg.replay_attempts if diff_cfg else 2,
        )
    )
    include_private = bool(
        _cli_or_config(
            args.include_private,
            diff_cfg.include_private if diff_cfg else False,
        )
    )
    save_artifacts = bool(
        _cli_or_config(
            args.save_artifacts,
            diff_cfg.save_artifacts if diff_cfg else False,
        )
    )
    if args.write_regression is not None:
        save_artifacts = True
    configured_registries = diff_cfg.fixture_registries if diff_cfg else []
    shared_registries = cfg.fixtures.registries if cfg is not None else []
    fixture_registries = list(
        dict.fromkeys(
            [
                *shared_registries,
                *configured_registries,
                *(args.fixture_registries or []),
            ]
        )
    )

    system_sequence: list[Mapping[str, Any]] | None = None
    if args.sequence_file is not None:
        try:
            sequence_payload = json.loads(Path(args.sequence_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _stderr(f"Cannot read system sequence: {exc}\n")
            return 2
        if not isinstance(sequence_payload, list) or not all(
            isinstance(item, Mapping) for item in sequence_payload
        ):
            _stderr("System sequence must be a JSON list of event objects.\n")
            return 2
        system_sequence = [dict(item) for item in sequence_payload]

    artifact_dir: Path | None = None
    if save_artifacts:
        raw_artifact_dir = str(
            _cli_or_config(
                args.artifact_dir,
                diff_cfg.artifact_dir if diff_cfg else ".ordeal/diff",
            )
        )
        try:
            artifact_dir = _workspace_output_path(raw_artifact_dir, label="diff.artifact_dir")
        except ValueError as exc:
            _stderr(f"Invalid artifact directory: {exc}\n")
            return 2

    _warn_if_diff_head_is_dirty(candidate_ref)

    try:
        result = run_revision_diff(
            str(target),
            base_ref=base_ref,
            candidate_ref=candidate_ref,
            max_examples=max_examples,
            seed=seed,
            rtol=rtol,
            atol=atol,
            include_private=include_private,
            fixture_registries=fixture_registries,
            replay_attempts=replay_attempts,
            sequence=system_sequence,
        )
    except (RevisionDiffError, ValueError) as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "tool": "ordeal diff",
                        "target": str(target),
                        "status": "inconclusive",
                        "reason": str(exc),
                    }
                )
            )
        else:
            _stderr(f"Diff unavailable: {exc}\n")
        return 2

    written_paths: tuple[Path, Path] | None = None
    if artifact_dir is not None:
        written_paths = _write_diff_artifacts(result, artifact_dir)

    written_regression: Path | None = None
    regression_finding_id: str | None = None
    if args.write_regression is not None:
        from ordeal._revision_diff import persist_revision_regression

        assert written_paths is not None
        try:
            regression_output = _workspace_output_path(
                str(args.write_regression),
                label="diff.write_regression",
            )
            manifest_output = _workspace_output_path(
                str(args.manifest),
                label="diff.manifest",
            )
        except ValueError as exc:
            _stderr(f"Invalid diff regression path: {exc}\n")
            return 2
        written_regression, regression_finding_id, regression_error = persist_revision_regression(
            result,
            evidence_path=written_paths[0],
            regression_path=regression_output,
            manifest_path=manifest_output,
        )
        if regression_error is not None:
            _stderr(f"Diff regression unavailable: {regression_error}\n")
            return 2

    if getattr(args, "json", False):
        payload = result.to_dict()
        if written_paths is not None:
            payload["saved_artifacts"] = {
                "json": _display_path(written_paths[0]),
                "markdown": _display_path(written_paths[1]),
            }
        if written_regression is not None:
            payload["saved_regression"] = {
                "path": _display_path(written_regression),
                "manifest": _display_path(Path(args.manifest)),
                "finding_id": regression_finding_id,
            }
        print(json.dumps(payload, indent=2))
    else:
        print(result.summary())
        if written_paths is not None:
            _stderr(f"Diff artifacts saved: {written_paths[0]}, {written_paths[1]}\n")
        if written_regression is not None:
            _stderr(
                f"Diff regression saved: {written_regression}; "
                f"CI guard: ordeal verify --ci --manifest {args.manifest}\n"
            )

    if result.status == "no_divergence_observed":
        return 0
    if result.status == "divergent":
        return 1
    return 2
def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Measure scaling, mutation latency, or a checked-in perf/quality contract."""
    import os

    from ordeal.benchmarking import benchmark_bug_manifest as _benchmark_bug_manifest
    from ordeal.benchmarking import (
        verify_bug_benchmark_certificate as _verify_bug_benchmark_certificate,
    )
    from ordeal.evidence import verify_bug_evidence as _verify_bug_evidence
    from ordeal.scaling import analyze as _analyze_scaling
    from ordeal.scaling import benchmark as _benchmark
    from ordeal.scaling import benchmark_perf_contract as _benchmark_perf_contract

    explorer_cls = Explorer
    if explorer_cls is None:
        from ordeal.explore import Explorer as explorer_cls

    if (
        args.output_json
        and not args.perf_contract
        and not getattr(args, "bug_manifest", None)
        and not getattr(args, "verify_certificate", None)
        and not getattr(args, "verify_evidence", None)
    ):
        _stderr(
            "--output-json requires a perf contract, bug manifest, certificate, "
            "or evidence record\n"
        )
        return 2

    if getattr(args, "verify_evidence", None):
        try:
            result = _verify_bug_evidence(
                args.verify_evidence,
                online_sources=bool(getattr(args, "online_sources", False)),
                python_executable=sys.executable,
            )
        except (OSError, ValueError) as exc:
            _stderr(f"Evidence verification error: {exc}\n")
            return 2
        if getattr(args, "output_json", None):
            out_path = Path(args.output_json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.to_json() + "\n", encoding="utf-8")
        if getattr(args, "json", False):
            print(result.to_json())
        else:
            print(result.summary())
        return 0 if result.verified else 1

    if getattr(args, "verify_certificate", None):
        result = _verify_bug_benchmark_certificate(
            args.verify_certificate,
            manifest_path=getattr(args, "certificate_manifest", None),
        )
        if getattr(args, "output_json", None):
            out_path = Path(args.output_json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.to_json() + "\n", encoding="utf-8")
        if getattr(args, "json", False):
            print(result.to_json())
        else:
            print(result.summary())
        return 0 if result.passed else 1

    if getattr(args, "bug_manifest", None):
        suite = _benchmark_bug_manifest(
            args.bug_manifest,
            python_executable=sys.executable,
            ordeal_root=str(Path(__file__).resolve().parent.parent),
            tier=getattr(args, "benchmark_tier", None),
            bugsinpy_root=getattr(args, "bugsinpy_root", None),
            checkout_root=getattr(args, "checkout_root", None),
            online_sources=bool(getattr(args, "online_sources", False)),
        )
        if getattr(args, "output_json", None):
            out_path = Path(args.output_json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(suite.to_json() + "\n", encoding="utf-8")
        if getattr(args, "json", False):
            print(suite.to_json())
        else:
            print(suite.summary())
        if args.check and not getattr(suite, "check_passed", suite.passed):
            return 1
        return 0

    if args.perf_contract:
        suite = _benchmark_perf_contract(
            args.perf_contract,
            cwd=os.getcwd(),
            tier=getattr(args, "tier", None),
        )
        if getattr(args, "output_json", None):
            out_path = Path(args.output_json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(suite.to_json() + "\n", encoding="utf-8")
        if getattr(args, "json", False):
            print(suite.to_json())
        else:
            print(suite.summary())
        if args.check and not suite.passed:
            return 1
        return 0

    if args.mutate_targets:
        suite = _benchmark(
            mutate_targets=args.mutate_targets,
            repeats=args.repeat,
            workers=args.workers,
            preset=args.preset,
            filter_equivalent=args.filter_equivalent,
            test_filter=args.test_filter,
            cwd=os.getcwd(),
        )
        print(suite.summary())
        return 0

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    if not cfg.tests:
        _stderr("No [[tests]] entries in config.\n")
        return 1

    test_cfg = cfg.tests[0]
    try:
        test_class = test_cfg.resolve()
    except (ImportError, AttributeError) as e:
        _stderr(f"Cannot import {test_cfg.class_path}: {e}\n")
        return 1

    max_workers = args.max_workers or os.cpu_count() or 4
    time_per_trial = args.time
    metric = args.metric

    _stderr(f"Benchmarking {test_cfg.class_path}\n")
    _stderr(f"  CPUs: {os.cpu_count()}, max workers: {max_workers}\n")
    _stderr(f"  Time per trial: {time_per_trial}s, metric: {metric}\n\n")

    measurements: list[tuple[int, float]] = []
    signal_profile: list[dict[str, int | float]] = []
    n = 1
    while n <= max_workers:
        _stderr(f"  N={n:2d} ... ")

        explorer = explorer_cls(
            test_class,
            target_modules=cfg.explorer.target_modules,
            seed=cfg.explorer.seed,
            max_checkpoints=cfg.explorer.max_checkpoints,
            checkpoint_prob=cfg.explorer.checkpoint_prob,
            checkpoint_strategy=cfg.explorer.checkpoint_strategy,
            fault_toggle_prob=cfg.explorer.fault_toggle_prob,
            workers=n,
            ngram=cfg.explorer.ngram,
        )

        import time as _t

        t0 = _t.monotonic()
        progress = None
        finalize_profile = None
        if n == 1:
            progress, finalize_profile = _make_signal_profiler()
        result = explorer.run(
            max_time=time_per_trial,
            steps_per_run=cfg.explorer.steps_per_run,
            progress=progress,
        )
        wall = _t.monotonic() - t0
        if finalize_profile is not None:
            signal_profile = finalize_profile(result)

        if metric == "edges":
            throughput = result.unique_edges / max(wall, 0.001)
        elif metric == "steps":
            throughput = result.total_steps / max(wall, 0.001)
        else:
            throughput = result.total_runs / max(wall, 0.001)

        measurements.append((n, throughput))

        _stderr(
            f"{result.total_runs:5d} runs, {result.total_steps:6d} steps, "
            f"{result.unique_edges:3d} edges, "
            f"{throughput:.0f} {metric}/s\n"
        )

        n *= 2

    # Normalize and analyze
    baseline = measurements[0][1]
    if baseline <= 0:
        _stderr("Baseline throughput is zero — cannot analyze.\n")
        return 1
    normalized = [(n, t / baseline) for n, t in measurements]

    _stderr("\n")

    if len(normalized) >= 3:
        analysis = _analyze_scaling(normalized)
        print(analysis.summary())
    else:
        _stderr("Need at least 3 data points (1, 2, 4+ workers) to fit USL.\n")
        print("Raw measurements:")
        for n, t in measurements:
            c = t / baseline
            print(f"  N={n:2d}: {c:.2f}x ({c / n * 100:.1f}% efficient)")

    if signal_profile:
        print("")
        print("Anytime Signal (N=1 Baseline)")
        for sample in signal_profile:
            print(
                f"  {sample['seconds']:.0f}s: "
                f"runs={sample['runs']}, "
                f"steps={sample['steps']}, "
                f"edges={sample['edges']}, "
                f"checkpoints={sample['checkpoints']}, "
                f"failures={sample['failures']}"
            )

    return 0
def _is_function_target(target: str) -> bool:
    """Determine if a dotted path refers to a callable (vs a module)."""
    from importlib import import_module

    try:
        import_module(target)
        return False  # imported as module — not a function
    except ImportError:
        pass

    parts = target.rsplit(".", 1)
    if len(parts) < 2:
        return False
    try:
        mod = import_module(parts[0])
        attr = getattr(mod, parts[1], None)
        return callable(attr)
    except ImportError:
        return False
def _generate_ci_workflow(pkg: str) -> str:
    """Generate a GitHub Actions workflow for ordeal CI."""
    has_uv_lock = Path("uv.lock").exists()

    if has_uv_lock:
        runtime_check = (
            "      - run: uv run python -c 'import os, platform; "
            'assert platform.python_version().startswith(os.environ["UV_PYTHON"] + ".")\''
        )
        install_steps = (
            """\
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.11.28"
      - run: uv lock --check
      - run: uv sync --locked --extra dev"""
            + "\n"
            + runtime_check
        )
        run_prefix = "uv run "
    else:
        install_steps = """\
      - run: pip install -e ".[dev]" """
        run_prefix = ""

    return f"""\
name: ordeal
on:
  push:
    branches: [main, master]
  pull_request:

jobs:
  ordeal:
    runs-on: ubuntu-latest
    env:
      UV_PYTHON: "3.12"
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
{install_steps}
      - run: {run_prefix}pytest --chaos
      - run: {run_prefix}ordeal mutate {pkg} --preset standard --threshold 0.8
"""
def _cmd_skill(args: argparse.Namespace) -> int:
    """Install the ordeal skill for AI coding agents."""
    path = _install_skill(dry_run=args.dry_run)
    if path is None and args.dry_run:
        _stderr("ordeal skill — already up-to-date\n")
        return 0
    if path is None:
        _stderr("ordeal skill — already up-to-date\n")
        return 0
    if args.dry_run:
        _stderr(f"ordeal skill — would write: {path}\n")
    else:
        _stderr(f"ordeal skill — installed: {path}\n")
    return 0
