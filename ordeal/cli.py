"""CLI entry point: ``ordeal explore`` and ``ordeal replay``.

    $ ordeal explore                    # reads ordeal.toml
    $ ordeal explore -c ci.toml         # custom config
    $ ordeal explore --max-time 300     # override time
    $ ordeal replay .ordeal/traces/run-42.json
    $ ordeal replay --shrink trace.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time as _time
from pathlib import Path
from typing import Any

from ordeal.config import OrdealConfig, load_config, ConfigError
from ordeal.explore import Explorer, ExplorationResult, ProgressSnapshot
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
        self._last: float = 0.0

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
            record_traces=cfg.report.traces,
        )

        result = explorer.run(
            max_time=cfg.explorer.max_time,
            max_runs=cfg.explorer.max_runs,
            steps_per_run=test_cfg.steps_per_run or cfg.explorer.steps_per_run,
            shrink=not args.no_shrink,
            progress=_ProgressPrinter() if verbose else None,
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
        print(f"    {result.total_runs} runs, {result.total_steps} steps, "
              f"{result.duration_seconds:.1f}s")
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
    explore_p.add_argument("--config", "-c", default="ordeal.toml",
                           help="Config file (default: ordeal.toml)")
    explore_p.add_argument("--seed", type=int, help="Override RNG seed")
    explore_p.add_argument("--max-time", type=float, help="Override max_time (seconds)")
    explore_p.add_argument("--verbose", "-v", action="store_true", help="Live progress")
    explore_p.add_argument("--no-shrink", action="store_true", help="Skip shrinking")

    # -- ordeal replay --
    replay_p = sub.add_parser("replay", help="Replay a saved trace")
    replay_p.add_argument("trace_file", help="Path to trace JSON file")
    replay_p.add_argument("--shrink", action="store_true", help="Shrink the trace")
    replay_p.add_argument("--output", "-o", help="Save shrunk trace to this path")

    args = parser.parse_args(argv)

    if args.command == "explore":
        return _cmd_explore(args)
    elif args.command == "replay":
        return _cmd_replay(args)
    else:
        parser.print_help()
        return 0
