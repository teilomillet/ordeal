"""CLI entry point for ordeal commands.

$ ordeal explore                    # reads ordeal.toml
$ ordeal explore -c ci.toml         # custom config
$ ordeal explore --max-time 300     # override time
$ ordeal replay .ordeal/traces/run-42.json
$ ordeal replay --shrink trace.json
$ ordeal mine mymod.func            # discover properties
$ ordeal mine mymod.func -n 1000    # more examples
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
from pathlib import Path
from typing import Any

from ordeal.config import ConfigError, OrdealConfig, load_config
from ordeal.explore import ExplorationResult, Explorer, ProgressSnapshot
from ordeal.trace import Trace, replay, shrink


def _stderr(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()


# ============================================================================
# Progress reporter
# ============================================================================


class _ProgressPrinter:
    """Prints one-line progress to stderr at a fixed interval."""

    def __init__(self, interval: float = 2.0) -> None:
        self._interval = interval
        self._last: float = float("-inf")

    def __call__(self, snap: ProgressSnapshot) -> None:
        now = _time.monotonic()
        if now - self._last < self._interval:
            return
        self._last = now
        _stderr(
            f"\r  [{snap.elapsed:.0f}s] "
            f"runs={snap.total_runs} steps={snap.total_steps} "
            f"edges={snap.unique_edges} cps={snap.checkpoints} "
            f"fails={snap.failures} "
            f"({snap.runs_per_second:.0f} runs/s)    "
        )


# ============================================================================
# Commands
# ============================================================================


def _cmd_explore(args: argparse.Namespace) -> int:
    """Run coverage-guided exploration from ordeal.toml."""
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    # CLI overrides
    if args.seed is not None:
        cfg.explorer.seed = args.seed
    if args.max_time is not None:
        cfg.explorer.max_time = args.max_time
    if args.workers is not None:
        cfg.explorer.workers = args.workers
    verbose = args.verbose or cfg.report.verbose

    if not cfg.tests:
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

        explorer = Explorer(
            test_class,
            target_modules=cfg.explorer.target_modules,
            seed=cfg.explorer.seed,
            max_checkpoints=cfg.explorer.max_checkpoints,
            checkpoint_prob=cfg.explorer.checkpoint_prob,
            checkpoint_strategy=cfg.explorer.checkpoint_strategy,
            fault_toggle_prob=cfg.explorer.fault_toggle_prob,
            record_traces=cfg.report.traces or bool(args.generate_tests),
            workers=cfg.explorer.workers,
        )

        result = explorer.run(
            max_time=cfg.explorer.max_time,
            max_runs=cfg.explorer.max_runs,
            steps_per_run=test_cfg.steps_per_run or cfg.explorer.steps_per_run,
            shrink=not args.no_shrink,
            progress=_ProgressPrinter() if verbose else None,
            resume_from=args.resume,
            save_state_to=args.save_state,
        )

        if verbose:
            _stderr("\n")  # newline after progress

        all_results.append((test_cfg.class_path, result))

        if result.failures:
            exit_code = 1

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
                out.write_text(test_src)
                _stderr(f"Generated tests: {out}\n")

    # -- Report --
    _print_report(all_results, cfg)

    # JSON report
    if cfg.report.format in ("json", "both"):
        _write_json_report(all_results, cfg)

    return exit_code


def _cmd_replay(args: argparse.Namespace) -> int:
    """Replay a saved trace."""
    try:
        trace = Trace.load(args.trace_file)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _stderr(f"Cannot load trace: {e}\n")
        return 1

    _stderr(f"Replaying {trace.test_class} (run {trace.run_id}, {len(trace.steps)} steps)...\n")

    error = replay(trace)
    if error is not None:
        _stderr(f"Failure reproduced: {type(error).__name__}: {error}\n")
        if args.shrink:
            _stderr("Shrinking...\n")
            shrunk = shrink(trace)
            _stderr(f"Shrunk to {len(shrunk.steps)} steps (from {len(trace.steps)})\n")
            if args.output:
                shrunk.save(args.output)
                _stderr(f"Saved: {args.output}\n")
        return 1
    else:
        _stderr("Failure did not reproduce.\n")
        return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run ordeal audit on specified modules."""
    from ordeal.audit import audit, audit_report

    if args.show_generated or args.save_generated:
        # Single-module mode with generated test output
        for mod in args.modules:
            result = audit(mod, test_dir=args.test_dir, max_examples=args.max_examples)
            print(result.summary())
            if args.show_generated and result.generated_test:
                print(f"\n  --- generated test for {mod} ---")
                print(result.generated_test)
                print("  --- end ---")
            if args.save_generated and result.generated_test:
                path = Path(args.save_generated)
                path.write_text(result.generated_test)
                _stderr(f"Saved: {path}\n")
    else:
        report = audit_report(
            args.modules,
            test_dir=args.test_dir,
            max_examples=args.max_examples,
        )
        print(report)
    return 0


def _cmd_mine(args: argparse.Namespace) -> int:
    """Discover properties of a function or all public functions in a module."""
    from importlib import import_module

    from ordeal.mine import mine

    target = args.target
    max_examples = args.max_examples

    # Split target into module path and optional function name
    parts = target.rsplit(".", 1)
    if len(parts) < 2:
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
            _stderr(f"Cannot import: {target}\n")
            return 1

    if attr and hasattr(mod, attr) and callable(getattr(mod, attr)):
        # Single function
        funcs = [(attr, getattr(mod, attr))]
    else:
        # Maybe the full target is a module (e.g. "ordeal.demo")
        from ordeal.auto import _get_public_functions

        try:
            mod = import_module(target)
        except ImportError:
            mod = import_module(mod_path)
        funcs = _get_public_functions(mod)
        if not funcs:
            _stderr(f"No public functions found in {target}\n")
            return 1

    for name, func in funcs:
        try:
            result = mine(func, max_examples=max_examples)
        except (ValueError, TypeError) as e:
            _stderr(f"  skip {name}: {e}\n")
            continue

        print(result.summary())
        if result.not_applicable:
            print(f"    n/a: {', '.join(result.not_applicable)}")
        print()

    return 0


def _cmd_mine_pair(args: argparse.Namespace) -> int:
    """Discover relational properties between two functions."""
    from importlib import import_module

    from ordeal.mine import mine_pair

    def _resolve_func(path: str):
        parts = path.rsplit(".", 1)
        if len(parts) < 2:
            return None
        mod = import_module(parts[0])
        return getattr(mod, parts[1], None)

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
    if result.not_applicable:
        print(f"    n/a: {', '.join(result.not_applicable)}")
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Measure parallel scaling and fit USL parameters."""
    import os

    from ordeal.scaling import analyze as _analyze_scaling

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
    n = 1
    while n <= max_workers:
        _stderr(f"  N={n:2d} ... ")

        explorer = Explorer(
            test_class,
            target_modules=cfg.explorer.target_modules,
            seed=cfg.explorer.seed,
            max_checkpoints=cfg.explorer.max_checkpoints,
            checkpoint_prob=cfg.explorer.checkpoint_prob,
            checkpoint_strategy=cfg.explorer.checkpoint_strategy,
            fault_toggle_prob=cfg.explorer.fault_toggle_prob,
            workers=n,
        )

        import time as _t

        t0 = _t.monotonic()
        result = explorer.run(
            max_time=time_per_trial,
            steps_per_run=cfg.explorer.steps_per_run,
        )
        wall = _t.monotonic() - t0

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


def _cmd_mutate(args: argparse.Namespace) -> int:
    """Run mutation testing on specified targets."""
    from ordeal.mutations import (
        MutationResult,
        mutate_and_test,
        mutate_function_and_test,
    )

    targets: list[str] = args.targets or []
    preset: str | None = args.preset
    operators: list[str] | None = None
    workers: int = args.workers
    threshold: float = args.threshold
    filter_equivalent: bool = not args.no_filter
    equivalence_samples: int = args.equivalence_samples

    # Fall back to config file if no targets given
    if not targets:
        config_path = args.config or "ordeal.toml"
        try:
            cfg = load_config(config_path)
        except FileNotFoundError:
            _stderr(
                "No targets specified. Use positional args or [mutations] in ordeal.toml.\n"
                "  ordeal mutate myapp.scoring.compute\n"
                "  ordeal mutate myapp.scoring\n"
            )
            return 1
        except ConfigError as e:
            _stderr(f"Config error: {e}\n")
            return 1

        if cfg.mutations is None:
            _stderr("No [mutations] section in config.\n")
            return 1

        targets = cfg.mutations.targets
        if not targets:
            _stderr("No targets in [mutations] section.\n")
            return 1

        # Config provides defaults; CLI flags override
        if preset is None and cfg.mutations.operators is None:
            preset = cfg.mutations.preset
        if cfg.mutations.operators is not None and preset is None:
            operators = cfg.mutations.operators
        if args.workers == 1 and cfg.mutations.workers > 1:
            workers = cfg.mutations.workers
        if args.threshold == 0.0 and cfg.mutations.threshold > 0.0:
            threshold = cfg.mutations.threshold
        if not args.no_filter:
            filter_equivalent = cfg.mutations.filter_equivalent
        if args.equivalence_samples == 10 and cfg.mutations.equivalence_samples != 10:
            equivalence_samples = cfg.mutations.equivalence_samples

    # Default preset when nothing specified
    if preset is None and operators is None:
        preset = "standard"

    all_results: list[tuple[str, MutationResult]] = []
    exit_code = 0

    for target in targets:
        _stderr(f"Mutating {target}...\n")

        is_func = _is_function_target(target)

        try:
            if is_func:
                result = mutate_function_and_test(
                    target,
                    operators=operators,
                    preset=preset,
                    workers=workers,
                    filter_equivalent=filter_equivalent,
                    equivalence_samples=equivalence_samples,
                )
            else:
                result = mutate_and_test(
                    target,
                    operators=operators,
                    preset=preset,
                    workers=workers,
                    filter_equivalent=filter_equivalent,
                    equivalence_samples=equivalence_samples,
                )
        except (ImportError, AttributeError, ValueError) as e:
            _stderr(f"  Error: {e}\n")
            exit_code = 1
            continue

        all_results.append((target, result))
        print(result.summary())
        print()

        if threshold > 0.0 and result.score < threshold:
            exit_code = 1

    # Generate test stubs if requested
    if args.generate_stubs:
        stubs_path = Path(args.generate_stubs)
        all_stubs: list[str] = []
        for _, result in all_results:
            stub = result.generate_test_stubs()
            if stub:
                all_stubs.append(stub)
        if all_stubs:
            stubs_path.parent.mkdir(parents=True, exist_ok=True)
            stubs_path.write_text("\n\n".join(all_stubs))
            _stderr(f"Test stubs written: {stubs_path}\n")

    # Final score line — always printed for CI parseability
    if all_results:
        total_mutants = sum(r.total for _, r in all_results)
        total_killed = sum(r.killed for _, r in all_results)
        overall = total_killed / total_mutants if total_mutants > 0 else 1.0
        if len(all_results) > 1:
            print(f"Overall: {total_killed}/{total_mutants} ({overall:.0%})")
        print(f"Score: {total_killed}/{total_mutants} ({overall:.0%})")
        if threshold > 0.0:
            status = "PASS" if overall >= threshold else "FAIL"
            print(f"Threshold: {threshold:.0%} — {status}")

    return exit_code


# ============================================================================
# Reporting
# ============================================================================


def _print_report(
    results: list[tuple[str, ExplorationResult]],
    cfg: OrdealConfig,
) -> None:
    """Print text report to stdout."""
    if cfg.report.format not in ("text", "both"):
        return

    print("\n--- Ordeal Exploration Report ---\n")
    for class_path, result in results:
        print(f"  {class_path}")
        print(
            f"    {result.total_runs} runs, {result.total_steps} steps, "
            f"{result.duration_seconds:.1f}s"
        )
        print(f"    {result.unique_edges} edges, {result.checkpoints_saved} checkpoints")
        if result.failures:
            print(f"    {len(result.failures)} FAILURES:")
            for f in result.failures[:10]:
                steps = f" ({len(f.trace.steps)} steps)" if f.trace else ""
                print(f"      {type(f.error).__name__}: {f.error}{steps}")
        else:
            print("    No failures.")
        print()


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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``ordeal``."""
    parser = argparse.ArgumentParser(
        prog="ordeal",
        description="Ordeal — automated chaos testing for Python",
    )
    sub = parser.add_subparsers(dest="command")

    # -- ordeal explore --
    explore_p = sub.add_parser("explore", help="Run coverage-guided exploration")
    explore_p.add_argument(
        "--config", "-c", default="ordeal.toml", help="Config file (default: ordeal.toml)"
    )
    explore_p.add_argument("--seed", type=int, help="Override RNG seed")
    explore_p.add_argument("--max-time", type=float, help="Override max_time (seconds)")
    explore_p.add_argument("--verbose", "-v", action="store_true", help="Live progress")
    explore_p.add_argument("--no-shrink", action="store_true", help="Skip shrinking")
    explore_p.add_argument(
        "--workers", "-w", type=int, help="Parallel worker processes (default: 1)"
    )
    explore_p.add_argument(
        "--generate-tests",
        type=str,
        default=None,
        metavar="PATH",
        help="Generate pytest tests from exploration traces (e.g. tests/test_generated.py)",
    )
    explore_p.add_argument(
        "--resume",
        type=str,
        default=None,
        metavar="PATH",
        help="Resume from a saved state file (e.g. .ordeal/state.pkl)",
    )
    explore_p.add_argument(
        "--save-state",
        type=str,
        default=None,
        metavar="PATH",
        help="Save exploration state on completion (e.g. .ordeal/state.pkl)",
    )

    # -- ordeal replay --
    replay_p = sub.add_parser("replay", help="Replay a saved trace")
    replay_p.add_argument("trace_file", help="Path to trace JSON file")
    replay_p.add_argument("--shrink", action="store_true", help="Shrink the trace")
    replay_p.add_argument("--output", "-o", help="Save shrunk trace to this path")

    # -- ordeal audit --
    audit_p = sub.add_parser("audit", help="Audit test coverage vs ordeal migration")
    audit_p.add_argument("modules", nargs="+", help="Module paths to audit")
    audit_p.add_argument(
        "--test-dir", "-t", default="tests", help="Test directory (default: tests)"
    )
    audit_p.add_argument(
        "--max-examples", type=int, default=20, help="Examples per function (default: 20)"
    )
    audit_p.add_argument(
        "--show-generated",
        action="store_true",
        help="Print the generated test file for inspection/debugging",
    )
    audit_p.add_argument(
        "--save-generated",
        type=str,
        default=None,
        help="Save generated test file to this path",
    )

    # -- ordeal mine --
    mine_p = sub.add_parser("mine", help="Discover properties of a function or module")
    mine_p.add_argument("target", help="Dotted path: mymod.func or mymod")
    mine_p.add_argument(
        "--max-examples", "-n", type=int, default=500, help="Examples to sample (default: 500)"
    )

    # -- ordeal mine-pair --
    mp_p = sub.add_parser("mine-pair", help="Discover relational properties between two functions")
    mp_p.add_argument("f", help="First function: mymod.func_a")
    mp_p.add_argument("g", help="Second function: mymod.func_b")
    mp_p.add_argument(
        "--max-examples", "-n", type=int, default=200, help="Examples to sample (default: 200)"
    )

    # -- ordeal benchmark --
    bench_p = sub.add_parser("benchmark", help="Measure parallel scaling (USL analysis)")
    bench_p.add_argument(
        "--config", "-c", default="ordeal.toml", help="Config file (default: ordeal.toml)"
    )
    bench_p.add_argument(
        "--max-workers", type=int, default=None, help="Max workers to test (default: CPU count)"
    )
    bench_p.add_argument(
        "--time", type=float, default=10.0, help="Seconds per trial (default: 10)"
    )
    bench_p.add_argument(
        "--metric",
        choices=["runs", "steps", "edges"],
        default="runs",
        help="Throughput metric to fit (default: runs)",
    )

    # -- ordeal mutate --
    mutate_p = sub.add_parser("mutate", help="Run mutation testing")
    mutate_p.add_argument(
        "targets", nargs="*", help="Dotted paths: myapp.scoring.compute or myapp.scoring"
    )
    mutate_p.add_argument(
        "--config",
        "-c",
        default=None,
        help="Config file with [mutations] section (used when no targets given)",
    )
    mutate_p.add_argument(
        "--preset",
        "-p",
        choices=["essential", "standard", "thorough"],
        default=None,
        help="Operator preset (default: standard)",
    )
    mutate_p.add_argument(
        "--workers", "-w", type=int, default=1, help="Parallel workers (default: 1)"
    )
    mutate_p.add_argument(
        "--threshold",
        "-t",
        type=float,
        default=0.0,
        help="Minimum mutation score; exit 1 if below (e.g. 0.8 for 80%%)",
    )
    mutate_p.add_argument(
        "--no-filter", action="store_true", help="Disable equivalent mutant filtering"
    )
    mutate_p.add_argument(
        "--equivalence-samples",
        type=int,
        default=10,
        help="Samples for equivalence filtering (default: 10)",
    )
    mutate_p.add_argument(
        "--generate-stubs",
        type=str,
        default=None,
        metavar="PATH",
        help="Write test stubs for surviving mutants to PATH",
    )

    args = parser.parse_args(argv)

    if args.command == "explore":
        return _cmd_explore(args)
    elif args.command == "replay":
        return _cmd_replay(args)
    elif args.command == "audit":
        return _cmd_audit(args)
    elif args.command == "mine":
        return _cmd_mine(args)
    elif args.command == "mine-pair":
        return _cmd_mine_pair(args)
    elif args.command == "benchmark":
        return _cmd_benchmark(args)
    elif args.command == "mutate":
        return _cmd_mutate(args)
    else:
        parser.print_help()
        return 0
