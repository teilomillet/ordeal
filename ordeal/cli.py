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
import contextlib
import io
import json
import os
import re
import sys
import time as _time
from pathlib import Path
from pprint import pformat
from typing import TYPE_CHECKING, Any, Callable

from ordeal.config import ConfigError, OrdealConfig, load_config

if TYPE_CHECKING:
    from ordeal.explore import ExplorationResult, ProgressSnapshot

# Tests monkeypatch this symbol; keep the override point without paying
# the import cost on every short CLI command.
Explorer = None


def _stderr(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()


def _install_skill(dry_run: bool = False) -> str | None:
    """Copy the bundled SKILL.md into .claude/skills/ordeal/SKILL.md.

    Returns the path written, or *None* if dry_run / already up-to-date.
    """
    src = Path(__file__).parent / "SKILL.md"
    if not src.exists():
        return None
    dest = Path(".claude/skills/ordeal/SKILL.md")
    new_content = src.read_text(encoding="utf-8")
    if dest.exists() and dest.read_text(encoding="utf-8") == new_content:
        return None  # already up-to-date
    if dry_run:
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(new_content, encoding="utf-8")
    return str(dest)


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


_BENCHMARK_SIGNAL_CHECKPOINTS: tuple[float, ...] = (5.0, 10.0, 30.0)
_DEFAULT_REGRESSION_PATH = "tests/test_ordeal_regressions.py"
_DEFAULT_FINDINGS_DIR = ".ordeal/findings"


def _make_signal_profiler(
    checkpoints: tuple[float, ...] = _BENCHMARK_SIGNAL_CHECKPOINTS,
) -> tuple[
    Callable[[ProgressSnapshot], None],
    Callable[[ExplorationResult], list[dict[str, int | float]]],
]:
    """Collect coarse anytime metrics at fixed wall-clock checkpoints."""
    ordered = [cp for cp in checkpoints if cp > 0]
    remaining = list(sorted(dict.fromkeys(ordered)))
    samples: list[dict[str, int | float]] = []

    def _capture(
        seconds: float,
        *,
        elapsed: float,
        runs: int,
        steps: int,
        edges: int,
        checkpoints_seen: int,
        failures: int,
    ) -> None:
        samples.append(
            {
                "seconds": seconds,
                "elapsed": elapsed,
                "runs": runs,
                "steps": steps,
                "edges": edges,
                "checkpoints": checkpoints_seen,
                "failures": failures,
            }
        )

    def _progress(snap: ProgressSnapshot) -> None:
        while remaining and snap.elapsed >= remaining[0]:
            seconds = remaining.pop(0)
            _capture(
                seconds,
                elapsed=snap.elapsed,
                runs=snap.total_runs,
                steps=snap.total_steps,
                edges=snap.unique_edges,
                checkpoints_seen=snap.checkpoints,
                failures=snap.failures,
            )

    def _finalize(result: ExplorationResult) -> list[dict[str, int | float]]:
        for seconds in remaining:
            _capture(
                seconds,
                elapsed=result.duration_seconds,
                runs=result.total_runs,
                steps=result.total_steps,
                edges=result.unique_edges,
                checkpoints_seen=result.checkpoints_saved,
                failures=len(result.failures),
            )
        return samples

    return _progress, _finalize


# ============================================================================
# Commands
# ============================================================================


def _cmd_catalog(args: argparse.Namespace) -> int:
    """Print all ordeal capabilities, organized by subsystem."""
    from ordeal import catalog

    c = catalog()
    total = sum(len(v) for v in c.values())
    print(f"{total} capabilities across {len(c)} subsystems:\n")

    # Derive subsystem descriptions from the first entry's module docstring
    for key in sorted(c):
        entries = c[key]
        # Get the module docstring's first line as description
        first_doc = ""
        if entries:
            qualname = entries[0].get("qualname", "")
            mod_path = qualname.rsplit(".", 1)[0] if "." in qualname else ""
            if mod_path:
                try:
                    mod = __import__(mod_path, fromlist=["_"])
                    first_doc = (mod.__doc__ or "").strip().split("\n")[0]
                except Exception:
                    pass
        if not first_doc:
            # Fallback: first entry's doc
            first_doc = entries[0]["doc"] if entries else ""
        names = ", ".join(e["name"] for e in entries[:4])
        if len(entries) > 4:
            names += ", ..."
        print(f"  {key} ({len(entries)}) — {first_doc}")
        print(f"    {names}")

    print("\nRun 'ordeal scan <module>' to explore a module fully.")
    print("Add '--report-file report.md' to scan or mine for a shareable Markdown report.")
    print(
        "Add '--write-regression' to scan or mine for runnable pytest regressions"
        f" (default: {_DEFAULT_REGRESSION_PATH})."
    )
    print(
        "Add '--save-artifacts' to scan for the full bug bundle:"
        f" {_default_scan_report_path('mymod')} + {_DEFAULT_REGRESSION_PATH}."
    )
    print("Run 'ordeal catalog --detail' for signatures and docs.")
    print("Python: from ordeal import catalog; catalog()")

    if getattr(args, "detail", False):
        for key in sorted(c):
            entries = c[key]
            print(f"\n{key} ({len(entries)}):")
            for item in entries:
                doc = item["doc"]
                sig = item.get("signature", "")
                print(f"  {item['name']}{sig}")
                if doc:
                    print(f"    {doc}")

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Mine a function and verify a specific property — one-step workflow.

    Collapses: mine() → spot property → fuzz with assertion → confirm
    into a single command.  Exit code 0 if property holds, 1 if violated.
    """
    from ordeal.auto import _unwrap
    from ordeal.mine import mine

    target = args.target
    prop = args.property

    # Resolve the function
    mod_path, _, func_name = target.rpartition(".")
    if not mod_path:
        _stderr("Target must be dotted path: module.function\n")
        return 1

    try:
        from importlib import import_module

        mod = import_module(mod_path)
        func = _unwrap(getattr(mod, func_name))
    except (ImportError, AttributeError) as e:
        _stderr(f"Cannot resolve {target}: {e}\n")
        return 1

    max_examples = args.max_examples
    if prop:
        _stderr(f"Checking {target} for '{prop}' ({max_examples} examples)...\n")
    else:
        _stderr(f"Checking {target} contracts ({max_examples} examples)...\n")

    result = mine(func, max_examples=max_examples)
    print(result.summary())

    # --contract mode: check all standard properties that catch bugs
    if not prop:
        contracts = [
            "never None",
            "no NaN",
            "never empty",
            "deterministic",
            "idempotent",
            "finite",
        ]
        violations = []
        for p in result.properties:
            if p.total > 0 and not p.universal and p.name in contracts:
                violations.append(p)
        if violations:
            print(f"\n  {len(violations)} contract violation(s):")
            for v in violations:
                print(f"    FAIL {v.name} ({v.holds}/{v.total})")
                if v.counterexample:
                    print(f"      input: {v.counterexample}")
            return 1
        passing = [
            p for p in result.properties if p.total > 0 and p.universal and p.name in contracts
        ]
        if passing:
            print(f"\n  {len(passing)} contract(s) verified:")
            for p in passing:
                print(f"    PASS {p.name} ({p.holds}/{p.total})")
        return 0

    # Single property mode
    matching = [p for p in result.properties if prop.lower() in p.name.lower()]
    if not matching:
        _stderr(
            f"\n  Property '{prop}' not found. Available: "
            f"{', '.join(p.name for p in result.properties if p.total > 0)}\n"
        )
        return 1

    violations = [p for p in matching if not p.universal]
    if violations:
        print(f"\n  VIOLATION: {violations[0].name} ({violations[0].holds}/{violations[0].total})")
        if violations[0].counterexample:
            print(f"  Counterexample: {violations[0].counterexample}")
        return 1

    holds = [p for p in matching if p.universal]
    if holds:
        print(f"\n  HOLDS: {holds[0].name} ({holds[0].holds}/{holds[0].total})")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    """Run unified exploration: mine + scan + mutate + chaos in one pass.

    This is the recommended entry point for AI assistants.  Point it
    at a module and it does everything: discovers properties, checks
    for crashes, mutation-tests, and chaos-tests.  Returns confidence,
    findings, and frontier.
    """
    from ordeal.state import explore

    inc_private = getattr(args, "include_private", False)
    _stderr(f"Scanning {args.target} (seed={args.seed})...\n")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        state = explore(
            args.target,
            seed=args.seed,
            max_examples=args.max_examples,
            workers=args.workers,
            time_limit=args.time_limit,
            include_private=inc_private,
        )

    if args.json:
        print(state.to_json())
    else:
        print(_format_scan_summary(state))
        if (
            state.findings
            and not getattr(args, "save_artifacts", False)
            and not getattr(args, "report_file", None)
            and not getattr(args, "write_regression", None)
        ):
            print(
                "  tip: add --save-artifacts or use --report-file / --write-regression"
                f" ({_DEFAULT_REGRESSION_PATH})"
            )

    save_artifacts = getattr(args, "save_artifacts", False)
    report_path = args.report_file
    regression_path = args.write_regression
    if save_artifacts and state.findings:
        report_path = report_path or _default_scan_report_path(state.module)
        regression_path = regression_path or _DEFAULT_REGRESSION_PATH
    if report_path:
        _write_scan_report(state, report_path)
    if regression_path:
        _write_scan_regressions(state, regression_path)
    if save_artifacts and not state.findings:
        _stderr("No findings yet; no artifacts written.\n")

    return 1 if state.findings else 0


def _cmd_explore(args: argparse.Namespace) -> int:
    """Run coverage-guided exploration from ordeal.toml."""
    explorer_cls = Explorer
    if explorer_cls is None:
        from ordeal.explore import Explorer as explorer_cls

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
            ngram=cfg.explorer.ngram,
            corpus_dir=corpus_dir,
            rule_swarm=cfg.explorer.rule_swarm,
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

        # Report seed replay results
        if result.seed_replays:
            for sr in result.seed_replays:
                if sr["reproduced"]:
                    _stderr(f"  REGRESSION  {sr['seed_name']}: {sr['error']}\n")
                else:
                    _stderr(f"  fixed       {sr['seed_name']}: no longer reproduces\n")

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


def _cmd_replay(args: argparse.Namespace) -> int:
    """Replay a saved trace."""
    from ordeal.trace import Trace, ablate_faults, replay, shrink

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
            trace = shrunk  # use shrunk trace for ablation
        if args.ablate:
            _stderr("Ablating faults...\n")
            faults = ablate_faults(trace)
            if faults:
                needed = [f for f, necessary in faults.items() if necessary]
                unneeded = [f for f, necessary in faults.items() if not necessary]
                if needed:
                    _stderr(f"Necessary faults: {', '.join(needed)}\n")
                if unneeded:
                    _stderr(f"Unnecessary faults: {', '.join(unneeded)}\n")
                if not needed:
                    _stderr("Bug reproduces without any faults.\n")
            else:
                _stderr("No fault toggles in trace.\n")
        return 1
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


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run ordeal audit on specified modules."""
    from ordeal.audit import audit, audit_report

    if args.show_generated or args.save_generated:
        # Single-module mode with generated test output
        for mod in args.modules:
            result = audit(
                mod,
                test_dir=args.test_dir,
                max_examples=args.max_examples,
                workers=args.workers,
                validation_mode=args.validation_mode,
            )
            print(result.summary())
            if args.show_generated and result.generated_test:
                print(f"\n  --- generated test for {mod} ---")
                print(result.generated_test)
                print("  --- end ---")
            if args.save_generated and result.generated_test:
                path = Path(args.save_generated)
                path.write_text(result.generated_test, encoding="utf-8")
                _stderr(f"Saved: {path}\n")
    else:
        report = audit_report(
            args.modules,
            test_dir=args.test_dir,
            max_examples=args.max_examples,
            workers=args.workers,
            validation_mode=args.validation_mode,
        )
        print(report)
    return 0


def _cmd_mine(args: argparse.Namespace) -> int:
    """Discover properties of a function or all public functions in a module."""
    from importlib import import_module

    from ordeal.mine import _is_suspicious_property, mine

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
            result = mine(func, max_examples=max_examples)
        except (ValueError, TypeError) as e:
            reason = str(e).split(".")[0]
            skipped.append((name, reason))
            continue

        mined_results.append((name, result))
        suspicious += sum(1 for prop in result.properties if _is_suspicious_property(prop))
        print(result.summary())
        if getattr(args, "verbose", False) and result.not_applicable:
            print(f"    n/a: {', '.join(result.not_applicable)}")
        print()

    if skipped:
        print(f"Skipped {len(skipped)} function(s):")
        for name, reason in skipped:
            print(f"  {name}: {reason}")

    if getattr(args, "report_file", None):
        _write_mine_report(
            target=report_target,
            module=report_namespace,
            results=mined_results,
            skipped=skipped,
            path_str=args.report_file,
            include_scan_hint=not report_is_function,
            suspicious_count=suspicious,
        )
    if getattr(args, "write_regression", None):
        _write_mine_regressions(
            target=report_target,
            module=report_namespace,
            results=mined_results,
            skipped=skipped,
            path_str=args.write_regression,
            suspicious_count=suspicious,
        )
    elif suspicious and not getattr(args, "report_file", None):
        print(
            f"tip: add --report-file report.md or --write-regression ({_DEFAULT_REGRESSION_PATH})"
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


def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Measure scaling, mutation latency, or a checked-in perf/quality contract."""
    import os

    from ordeal.scaling import analyze as _analyze_scaling
    from ordeal.scaling import benchmark as _benchmark
    from ordeal.scaling import benchmark_perf_contract as _benchmark_perf_contract

    explorer_cls = Explorer
    if explorer_cls is None:
        from ordeal.explore import Explorer as explorer_cls

    if args.output_json and not args.perf_contract:
        _stderr("--output-json requires --perf-contract\n")
        return 2

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
        install_steps = """\
      - uses: astral-sh/setup-uv@v4
      - run: uv sync"""
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
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
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


def _cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap test files for untested modules."""
    import re
    import subprocess

    from ordeal.mutations import init_project

    target: str | None = args.target or None
    output_dir: str = args.output_dir
    dry_run: bool = args.dry_run
    ci: bool = args.ci
    ci_name: str = args.ci_name

    results = init_project(target=target, output_dir=output_dir, dry_run=dry_run)

    if not results:
        if target:
            _stderr(f"Could not resolve {target!r}. Is it importable?\n")
        else:
            _stderr(
                "No Python package found in the current directory.\n  Usage: ordeal init myapp\n"
            )
        return 1

    pkg = target or results[0]["module"].split(".")[0]

    generated = [r for r in results if r["status"] == "generated"]
    existed = sum(1 for r in results if r["status"] == "exists")

    # --- CI workflow ---
    ci_path: str | None = None
    ci_content: str | None = None
    if ci:
        ci_path = f".github/workflows/{ci_name}.yml"
        ci_content = _generate_ci_workflow(pkg)

    # --- Install AI skill ---
    skill_path = _install_skill(dry_run=dry_run)

    if dry_run:
        _stderr(f"\nordeal init — DRY RUN for {pkg}\n\n")
        for r in generated:
            print(f"\n# --- {r['path']} ---\n")
            print(r["content"])
        if ci_content:
            print(f"\n# --- {ci_path} ---\n")
            print(ci_content)
        n_files = len(generated) + (1 if ci_content else 0) + (1 if skill_path else 0)
        _stderr(f"  Would generate {n_files} file(s)\n\n")
        return 0

    if not generated and not ci:
        _stderr(f"\nordeal init — {pkg}: all modules already have tests.\n\n")
        return 0

    # --- Write CI workflow ---
    if ci_path and ci_content:
        ci_p = Path(ci_path)
        ci_p.parent.mkdir(parents=True, exist_ok=True)
        ci_p.write_text(ci_content, encoding="utf-8")

    if not generated:
        _stderr(f"\nordeal init — {pkg}: all modules already have tests.\n")
        _stderr(f"  Generated: {ci_path}\n\n")
        return 0

    # --- Setup subprocess env ---
    env = dict(os.environ)
    cwd = os.getcwd()
    pypath = env.get("PYTHONPATH", "")
    src = os.path.join(cwd, "src")
    extra = src if os.path.isdir(src) else cwd
    env["PYTHONPATH"] = f"{extra}:{pypath}" if pypath else extra

    def _run_ordeal(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from ordeal.cli import main; import sys; sys.exit(main(" + repr(argv) + "))",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    # --- Phase 1: Verify generated tests pass ---
    test_files = [r["path"] for r in generated]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line", "--no-header", *test_files],
        capture_output=True,
        text=True,
    )
    tests_pass = proc.returncode == 0

    # --- Phase 2: Mutation loop ---
    # Collect function-level targets for more reliable mutation testing
    mut_targets: list[str] = []
    for r in generated:
        content = r.get("content", "")
        mod = r["module"]
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"def test_{mod.replace('.', '_')}_") and "_pinned" in stripped:
                prefix = f"def test_{mod.replace('.', '_')}_"
                func = stripped.split("_pinned")[0].replace(prefix, "")
                mut_targets.append(f"{mod}.{func}")
    if not mut_targets:
        mut_targets = [r["module"] for r in generated]

    mutation_score = ""
    for round_num in range(3):
        mp = _run_ordeal(
            ["mutate", *mut_targets, "-p", "essential", "--generate-stubs", ".ordeal_stubs_tmp.py"]
        )
        for line in mp.stdout.splitlines():
            if line.startswith("Score:"):
                mutation_score = line.strip()
                break
        stubs_path = Path(".ordeal_stubs_tmp.py")
        if "(100%)" in mutation_score or not stubs_path.exists():
            stubs_path.unlink(missing_ok=True)
            break
        stubs = stubs_path.read_text(encoding="utf-8").strip()
        stubs_path.unlink(missing_ok=True)
        if not stubs:
            break
        # Append gap-closing tests
        for r in generated:
            if r["path"]:
                p = Path(r["path"])
                p.write_text(
                    p.read_text(encoding="utf-8") + "\n\n" + stubs + "\n",
                    encoding="utf-8",
                )
                break

    Path(".ordeal_stubs_tmp.py").unlink(missing_ok=True)

    # --- Phase 3: Brief explore ---
    explore_summary = ""
    if Path("ordeal.toml").exists():
        ep = _run_ordeal(["explore", "--max-time", "10", "-c", "ordeal.toml"])
        for line in (ep.stderr + ep.stdout).splitlines():
            if "edge" in line.lower() or "runs:" in line.lower():
                explore_summary = line.strip()
                break

    # --- Count what was generated ---
    n_tests = 0
    n_pinned = 0
    n_properties = 0
    n_chaos = 0
    pinned_values: list[str] = []
    property_names: list[str] = []

    for r in generated:
        content = r.get("content", "")
        # Re-read in case mutation loop appended stubs
        if r["path"] and Path(r["path"]).exists():
            content = Path(r["path"]).read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("def test_"):
                n_tests += 1
                if "_pinned" in stripped:
                    n_pinned += 1
                elif "_properties" in stripped:
                    n_properties += 1
            if "chaos_for(" in stripped:
                n_chaos += 1
            # Collect pinned values for review
            if stripped.startswith("assert ") and "==" in stripped:
                expr = stripped.removeprefix("assert ").strip()
                skip_kw = (
                    "isinstance",
                    "is not",
                    "is None",
                    "len(",
                    "math.",
                    "not ",
                    ">= 0",
                    "== result",
                    "<= result",
                    "== ...",
                )
                if not any(kw in expr for kw in skip_kw):
                    if "pytest.approx(" in expr:
                        expr = re.sub(r"pytest\.approx\(([^)]+)\)", r"\1", expr)
                    pinned_values.append(expr)
            # Collect discovered properties
            if stripped.startswith('"""Discovered:'):
                props = stripped.replace('"""Discovered:', "").rstrip('."""')
                property_names.extend(p.strip() for p in props.split(","))

    # Deduplicate properties
    seen: set[str] = set()
    unique_props: list[str] = []
    for p in property_names:
        if p not in seen:
            seen.add(p)
            unique_props.append(p)

    # --- Print the quality report ---
    _stderr(f"\n{'=' * 60}\n")
    _stderr(f"  ordeal init — quality report for {pkg}\n")
    _stderr(f"{'=' * 60}\n\n")

    _stderr(f"  Scanned:    {len(results)} module(s)")
    if existed:
        _stderr(f" ({existed} already tested)")
    _stderr("\n")

    _stderr(f"  Generated:  {n_tests} tests")
    parts = []
    if n_pinned:
        parts.append(f"{n_pinned} pinned")
    if n_properties:
        parts.append(f"{n_properties} property")
    if n_chaos:
        parts.append(f"{n_chaos} chaos")
    if parts:
        _stderr(f" ({', '.join(parts)})")
    _stderr("\n")

    if unique_props:
        _stderr(f"  Properties: {len(unique_props)} discovered")
        # Show the interesting ones
        interesting = [
            p
            for p in unique_props
            if not p.startswith("output type")
            and p not in ("deterministic", "never None", "no NaN")
        ]
        if interesting:
            _stderr(f" — {', '.join(interesting[:5])}")
        _stderr("\n")

    _stderr(f"  Tests pass: {'yes' if tests_pass else 'NO — check generated files'}\n")

    if mutation_score and "0/0" not in mutation_score:
        _stderr(f"  Mutations:  {mutation_score.removeprefix('Score: ')}\n")

    if explore_summary:
        _stderr(f"  Explored:   {explore_summary}\n")

    _stderr("\n  Files:\n")
    for r in generated:
        _stderr(f"    {r['path']}\n")
    if Path("ordeal.toml").exists():
        _stderr("    ordeal.toml\n")
    if ci_path:
        _stderr(f"    {ci_path}\n")
    if skill_path:
        _stderr(f"    {skill_path}\n")
    _stderr("\n")

    # --- Pinned values for review ---
    if pinned_values:
        _stderr("  Pinned values (verify these match intended behavior):\n")
        for expr in pinned_values:
            _stderr(f"    {expr}\n")
        _stderr("\n")

    # --- JSON to stdout for AI assistants ---
    import json

    report = {
        "package": pkg,
        "modules_scanned": len(results),
        "tests_generated": n_tests,
        "test_breakdown": {"pinned": n_pinned, "property": n_properties, "chaos": n_chaos},
        "properties_discovered": unique_props,
        "tests_pass": tests_pass,
        "mutation_score": mutation_score.removeprefix("Score: ") if mutation_score else None,
        "ci_workflow": ci_path,
        "skill": skill_path,
        "files": [r["path"] for r in generated]
        + (["ordeal.toml"] if Path("ordeal.toml").exists() else [])
        + ([ci_path] if ci_path else [])
        + ([skill_path] if skill_path else []),
        "pinned_values": pinned_values,
        "functions": [
            {"module": r["module"], "status": r["status"], "test_file": r["path"]} for r in results
        ],
    }
    print(json.dumps(report, indent=2))

    return 0


def _cmd_mutate(args: argparse.Namespace) -> int:
    """Run mutation testing on specified targets."""
    from ordeal.mutations import (
        MutationResult,
        NoTestsFoundError,
        generate_starter_tests,
        mutate,
    )

    targets: list[str] = args.targets or []
    preset: str | None = args.preset
    operators: list[str] | None = None
    workers: int = args.workers
    threshold: float = args.threshold
    filter_equivalent: bool = not args.no_filter
    equivalence_samples: int = args.equivalence_samples
    test_filter: str | None = args.test_filter
    mutant_timeout: float | None = args.mutant_timeout
    disk_mutation: bool | None = args.disk_mutation
    resume: bool = args.resume

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
        if test_filter is None and cfg.mutations.test_filter is not None:
            test_filter = cfg.mutations.test_filter
        if mutant_timeout is None and cfg.mutations.mutant_timeout is not None:
            mutant_timeout = cfg.mutations.mutant_timeout

    # Default preset when nothing specified
    if preset is None and operators is None:
        preset = "standard"

    all_results: list[tuple[str, MutationResult]] = []
    exit_code = 0

    for target in targets:
        _stderr(f"Mutating {target}...\n")

        try:
            result = mutate(
                target,
                operators=operators,
                preset=preset,
                workers=workers,
                filter_equivalent=filter_equivalent,
                equivalence_samples=equivalence_samples,
                test_filter=test_filter,
                mutant_timeout=mutant_timeout,
                disk_mutation=disk_mutation,
                resume=resume,
            )
        except NoTestsFoundError as e:
            _stderr(f"  WARNING: No tests found for {target!r}\n")
            starter = generate_starter_tests(target)
            if starter:
                suggested = e.suggested_file or f"tests/test_{target.rsplit('.', 1)[-1]}.py"
                if args.generate_stubs:
                    stubs_path = Path(args.generate_stubs)
                    stubs_path.parent.mkdir(parents=True, exist_ok=True)
                    existing = (
                        stubs_path.read_text(encoding="utf-8") if stubs_path.exists() else ""
                    )
                    sep = "\n\n" if existing else ""
                    stubs_path.write_text(existing + sep + starter, encoding="utf-8")
                    _stderr(f"  Starter tests written: {stubs_path}\n")
                else:
                    # Print the scaffold directly — don't hide it behind a flag
                    print(f"\n# Save to: {suggested}\n")
                    print(starter)
                    _stderr(f"  Or run: ordeal init {target}\n")
            exit_code = 1
            continue
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
            stubs_path.write_text("\n\n".join(all_stubs), encoding="utf-8")
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


def _format_scan_summary(state: Any) -> str:
    """Render a concise, action-oriented summary for ``ordeal scan``."""
    lines = [f"ordeal scan: {state.module}"]
    status = "findings found" if state.findings else "no findings yet"
    lines.append(f"  status: {status}")
    lines.append(f"  confidence: {state.confidence:.0%}")

    lines.append(f"  checked: {', '.join(_scan_checked_items(state))}")

    if state.findings:
        lines.append("  findings:")
        for finding in state.findings[:5]:
            lines.append(f"    - {finding}")
    else:
        lines.append("  findings: none")

    frontier = state.frontier
    if frontier:
        lines.append("  gaps to close:")
        shown = 0
        for name, gaps in frontier.items():
            if shown >= 5:
                break
            lines.append(f"    - {name}: {', '.join(gaps)}")
            shown += 1

    from ordeal.suggest import format_suggestions

    avail = format_suggestions(state)
    if avail:
        lines.append(avail)
    return "\n".join(lines)


def _scan_report_details(state: Any) -> list[dict[str, Any]]:
    """Return structured finding details for scan report generation."""
    details = getattr(state, "finding_details", None)
    if details is not None:
        return list(details)
    return []


def _scan_checked_items(state: Any) -> list[str]:
    """Return the coarse coverage summary for a scan report."""
    checked = [f"{len(state.functions)} functions"]
    if getattr(state, "supervisor_info", None):
        checked.append(f"{state.supervisor_info.get('trajectory_steps', 0)} transitions")
    tree = getattr(state, "tree", None)
    if tree is not None and getattr(tree, "size", 0) > 0:
        checked.append(f"{tree.size} checkpoints")
    return checked


def _trim_report_value(
    value: Any,
    *,
    max_depth: int = 3,
    max_items: int = 6,
    max_string: int = 120,
) -> Any:
    """Trim large nested values so reports stay readable."""
    if max_depth <= 0:
        text = repr(value)
        return text if len(text) <= max_string else text[: max_string - 3] + "..."
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max_string - 3] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        items = list(value.items())
        trimmed = {
            str(key): _trim_report_value(
                item,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string=max_string,
            )
            for key, item in items[:max_items]
        }
        if len(items) > max_items:
            trimmed["..."] = f"+{len(items) - max_items} more field(s)"
        return trimmed
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        trimmed = [
            _trim_report_value(
                item,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string=max_string,
            )
            for item in seq[:max_items]
        ]
        if len(seq) > max_items:
            trimmed.append(f"... +{len(seq) - max_items} more item(s)")
        return trimmed
    text = repr(value)
    return text if len(text) <= max_string else text[: max_string - 3] + "..."


def _json_block(value: Any) -> list[str]:
    """Render a fenced JSON block for Markdown reports."""
    trimmed = _trim_report_value(value)
    return ["```json", json.dumps(trimmed, indent=2, default=str), "```"]


def _python_block(code: str) -> list[str]:
    """Render a fenced Python block for Markdown reports."""
    return ["```python", code.rstrip(), "```"]


def _slugify_report_name(text: str) -> str:
    """Collapse free-form finding names into test-friendly identifiers."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    return slug or "finding"


def _regression_test_name(stub: str) -> str | None:
    """Extract the pytest test name from a generated regression stub."""
    match = re.search(r"^def (test_[0-9A-Za-z_]+)\(", stub, re.MULTILINE)
    return match.group(1) if match else None


def _default_scan_report_path(module: str) -> str:
    """Return the default Markdown artifact path for a scanned module."""
    parts = module.split(".")
    return str(Path(_DEFAULT_FINDINGS_DIR).joinpath(*parts).with_suffix(".md"))


def _python_literal(value: Any, *, trim: bool = True) -> str:
    """Render a stable Python literal for regression stubs."""
    rendered = (
        _trim_report_value(value, max_depth=4, max_items=6, max_string=80) if trim else value
    )
    return pformat(rendered, width=88, sort_dicts=False)


def _property_impact(detail: dict[str, Any]) -> str:
    """Explain why a mined property violation matters."""
    name = detail.get("name", "")
    messages = {
        "deterministic": "the same input produced different outputs across repeated calls.",
        "idempotent": "calling the function again changed a value that should have stabilized.",
        "involution": "running the function twice failed to recover the original value.",
        "never None": "a generated input returned None where callers likely expect a real value.",
        "no NaN": "a generated input produced NaN, which can silently poison downstream math.",
        "commutative": (
            "swapping the operands changed the result, so behavior depends on argument order."
        ),
        "associative": (
            "grouping equivalent operations changed the result,"
            " which hints at an algebraic edge case."
        ),
        "bijective": "distinct inputs collapsed to the same output, so information is being lost.",
    }
    return messages.get(
        name,
        "this property held for most examples but not all,"
        " which suggests a boundary or consistency bug.",
    )


def _render_regression_stub(
    module: str,
    detail: dict[str, Any],
    *,
    trim: bool = True,
) -> str | None:
    """Generate a compact pytest stub for concrete findings when possible."""
    function = detail.get("function")
    if not function:
        return None

    slug = _slugify_report_name(detail.get("name") or detail.get("kind", "finding"))
    test_name = f"test_{function}_{slug}_regression"
    lines = [f"from {module} import {function}", "", "", f"def {test_name}() -> None:"]

    kind = detail.get("kind")
    counterexample = detail.get("counterexample") or {}
    failing_args = detail.get("failing_args")
    raw_input = counterexample.get("input")
    input_args = raw_input if isinstance(raw_input, dict) else None

    if kind == "crash" and isinstance(failing_args, dict):
        lines.append(f"    args = {_python_literal(failing_args, trim=trim)}")
        lines.append(f"    {function}(**args)")
        return "\n".join(lines)

    if kind != "property" or not isinstance(input_args, dict) or not input_args:
        return None

    first_param = next(iter(input_args))
    name = detail.get("name")
    lines.append(f"    args = {_python_literal(input_args, trim=trim)}")

    if name == "deterministic":
        lines.append(f"    first = {function}(**args)")
        lines.append(f"    second = {function}(**args)")
        lines.append("    assert second == first")
        return "\n".join(lines)

    if name == "idempotent":
        lines.append(f"    first = {function}(**args)")
        lines.append("    replay_args = dict(args)")
        lines.append(f"    replay_args[{first_param!r}] = first")
        lines.append(f"    second = {function}(**replay_args)")
        lines.append("    assert second == first")
        return "\n".join(lines)

    if name == "involution":
        lines.append(f"    first = {function}(**args)")
        lines.append("    replay_args = dict(args)")
        lines.append(f"    replay_args[{first_param!r}] = first")
        lines.append(f"    second = {function}(**replay_args)")
        lines.append(f"    assert second == args[{first_param!r}]")
        return "\n".join(lines)

    if name == "never None":
        lines.append(f"    assert {function}(**args) is not None")
        return "\n".join(lines)

    if name == "commutative" and isinstance(counterexample.get("swapped_input"), dict):
        swapped_lit = _python_literal(counterexample["swapped_input"], trim=trim)
        lines.append(f"    swapped = {swapped_lit}")
        lines.append(f"    left = {function}(**args)")
        lines.append(f"    right = {function}(**swapped)")
        lines.append("    assert right == left")
        return "\n".join(lines)

    return None


def _render_finding_section(detail: dict[str, Any]) -> list[str]:
    """Render one finding block for a Markdown dossier."""
    qualname = detail.get("qualname") or detail.get("function", "?")
    kind = detail.get("kind", "finding")
    title = detail.get("name") or detail.get("summary") or kind
    module = detail.get("module", "")

    lines = [f"### {detail['index']}. `{qualname}`", "", f"- Type: {kind}", f"- Finding: {title}"]

    if kind == "property":
        holds = detail.get("holds")
        total = detail.get("total")
        confidence = detail.get("confidence")
        if holds is not None and total is not None and confidence is not None:
            lines.append(f"- Evidence: `{holds}/{total}` examples (`{confidence:.0%}` confidence)")
        lines.append(f"- Why this matters: {_property_impact(detail)}")
        counterexample = detail.get("counterexample")
        if counterexample:
            lines.extend(["", "Counterexample:"])
            lines.extend(_json_block(counterexample))
        stub = _render_regression_stub(module, detail, trim=True)
        if stub:
            lines.extend(["", "Regression test stub:"])
            lines.extend(_python_block(stub))
        lines.extend(
            [
                "",
                "Next steps:",
                f'- `ordeal check {qualname} -p "{detail.get("name", "")}" -n 200`',
                f"- `ordeal mutate {qualname}`",
            ]
        )
        return lines

    if kind == "crash":
        error = detail.get("error") or "unknown error"
        lines.append(f"- Evidence: `{error}`")
        lines.append(
            "- Why this matters: the function crashes under generated inputs,"
            " so basic robustness is not yet established."
        )
        if detail.get("failing_args"):
            lines.extend(["", "Failing input:"])
            lines.extend(_json_block(detail["failing_args"]))
        stub = _render_regression_stub(module, detail, trim=True)
        if stub:
            lines.extend(["", "Regression test stub:"])
            lines.extend(_python_block(stub))
        lines.extend(
            [
                "",
                "Next steps:",
                f"- `ordeal mine {qualname} -n 200`",
                f"- Reproduce the crash directly in a regression test for `{qualname}`",
            ]
        )
        return lines

    if kind == "mutation":
        score = detail.get("mutation_score")
        survived = detail.get("survived_mutants")
        if score is not None:
            lines.append(f"- Evidence: mutation score `{score:.0%}`")
        if survived is not None:
            lines.append(f"- Surviving mutants: `{survived}`")
        lines.append(
            "- Why this matters: existing tests still miss at least one meaningful code change."
        )
        lines.extend(
            [
                "",
                "Next steps:",
                f"- `ordeal mutate {qualname}`",
                f"- Add regression tests for the surviving mutant cases in `{qualname}`",
            ]
        )
        return lines

    lines.append(f"- Evidence: {detail.get('summary', title)}")
    return lines


def _render_findings_report_markdown(report: dict[str, Any]) -> str:
    """Render a shareable Markdown report from normalized finding data."""
    lines = ["# Ordeal Finding Report", ""]
    lines.append(f"Target: `{report['target']}`")
    lines.append(f"Tool: `ordeal {report['tool']}`")
    lines.append(f"Status: {report['status']}")
    confidence = report.get("confidence")
    if confidence is not None:
        lines.append(f"Confidence: `{confidence}`")
    seed = report.get("seed")
    if seed is not None:
        lines.append(f"Seed: `{seed}`")
    lines.append("")

    lines.extend(["## Summary", ""])
    for item in report.get("summary", []):
        lines.append(f"- {item}")
    lines.append("")

    details = report.get("details", [])
    lines.extend(["## Findings", ""])
    if details:
        for idx, detail in enumerate(details, start=1):
            enriched = {"index": idx, **detail}
            lines.extend(_render_finding_section(enriched))
            lines.append("")
    else:
        lines.append("No findings yet.")
        lines.append("")

    gaps = report.get("gaps", [])
    if gaps:
        lines.extend(["## Gaps To Close", ""])
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")

    for title, items in report.get("extra_sections", []):
        if not items:
            continue
        lines.extend([f"## {title}", ""])
        for item in items:
            lines.append(f"- {item}")
        lines.append("")

    lines.extend(["## Suggested Commands", ""])
    for command in report.get("suggested_commands", []):
        lines.append(f"- `{command}`")
    return "\n".join(lines).rstrip() + "\n"


def _build_scan_report(state: Any) -> dict[str, Any]:
    """Normalize scan output into the shared finding report shape."""
    return {
        "target": state.module,
        "tool": "scan",
        "status": "findings found" if state.findings else "no findings yet",
        "confidence": f"{state.confidence:.0%}",
        "seed": getattr(state, "supervisor_info", {}).get("seed"),
        "summary": [
            f"Checked: {', '.join(_scan_checked_items(state))}",
            f"Findings: {len(state.findings)}",
            f"Gaps: {sum(len(v) for v in state.frontier.values()) if state.frontier else 0}",
        ],
        "details": [
            {
                **detail,
                "module": state.module,
                "qualname": f"{state.module}.{detail.get('function', '?')}",
            }
            for detail in _scan_report_details(state)
        ],
        "gaps": [
            f"`{state.module}.{name}`: {', '.join(gaps)}" for name, gaps in state.frontier.items()
        ],
        "suggested_commands": [
            f"ordeal scan {state.module}",
            f"ordeal mine {state.module} -n 200",
            f"ordeal mutate {state.module}",
        ],
    }


def _render_scan_report_markdown(state: Any) -> str:
    """Render a shareable Markdown finding report for `ordeal scan`."""
    return _render_findings_report_markdown(_build_scan_report(state))


def _split_regression_stub(stub: str) -> tuple[str | None, str, str | None]:
    """Split a stub into import line, function body, and test name."""
    lines = stub.rstrip().splitlines()
    import_line = lines[0] if lines and lines[0].startswith("from ") else None
    body_start = next((idx for idx, line in enumerate(lines) if line.startswith("def ")), None)
    body = "\n".join(lines[body_start:]).rstrip() if body_start is not None else stub.rstrip()
    return import_line, body, _regression_test_name(stub)


def _render_regression_file(header: list[str], stubs: list[str]) -> str:
    """Render a fresh regression module from generated stubs."""
    imports: list[str] = []
    seen_imports: set[str] = set()
    bodies: list[str] = []
    for stub in stubs:
        import_line, body, _ = _split_regression_stub(stub)
        if import_line and import_line not in seen_imports:
            imports.append(import_line)
            seen_imports.add(import_line)
        bodies.append(body)

    lines = header[:]
    if imports:
        lines.extend(imports)
        lines.extend(["", ""])
    for idx, body in enumerate(bodies):
        if idx:
            lines.extend(["", ""])
        lines.append(body)
    lines.append("")
    return "\n".join(lines)


def _merge_regression_file(existing: str, stubs: list[str]) -> tuple[str, int, int]:
    """Append stubs into an existing regression file, deduping by test name."""
    source = existing.rstrip()
    existing_imports = set(re.findall(r"^from .+$", existing, re.MULTILINE))
    existing_tests = set(re.findall(r"^def (test_[0-9A-Za-z_]+)\(", existing, re.MULTILINE))
    added = 0
    skipped = 0

    for stub in stubs:
        import_line, body, test_name = _split_regression_stub(stub)
        if test_name and test_name in existing_tests:
            skipped += 1
            continue

        chunk: list[str] = []
        if import_line and import_line not in existing_imports:
            chunk.append(import_line)
            existing_imports.add(import_line)
        if chunk:
            chunk.extend(["", ""])
        chunk.append(body)

        if source:
            source += "\n\n\n" + "\n".join(chunk)
        else:
            source = "\n".join(chunk)
        added += 1
        if test_name:
            existing_tests.add(test_name)

    return source.rstrip() + "\n", added, skipped


def _write_regression_file(
    *,
    path_str: str,
    header: list[str],
    stubs: list[str],
) -> tuple[Path, int, int]:
    """Create or extend a regression file from generated stubs."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        merged, added, skipped = _merge_regression_file(path.read_text(encoding="utf-8"), stubs)
        path.write_text(merged, encoding="utf-8")
        return path, added, skipped
    path.write_text(_render_regression_file(header, stubs), encoding="utf-8")
    return path, len(stubs), 0


def _regression_stubs_from_details(
    *,
    module: str,
    details: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Build runnable regression stubs from normalized finding details."""
    stubs: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for detail in details:
        stub = _render_regression_stub(module, detail, trim=False)
        if stub is None:
            skipped.append(detail.get("qualname") or detail.get("function", "?"))
            continue
        if stub in seen:
            continue
        seen.add(stub)
        stubs.append(stub)
    return stubs, skipped


def _scan_regression_stubs(state: Any) -> tuple[list[str], list[str]]:
    """Build runnable regression stubs from replayable scan findings."""
    details = _build_scan_report(state)["details"]
    return _regression_stubs_from_details(module=state.module, details=details)


def _render_scan_regression_file(state: Any) -> str | None:
    """Render a pytest file from concrete scan findings."""
    stubs, _ = _scan_regression_stubs(state)
    if not stubs:
        return None
    return _render_regression_file(
        [
            '"""Generated by `ordeal scan --write-regression`.',
            "",
            f"Target: {state.module}",
            '"""',
            "",
        ],
        stubs,
    )


def _mine_regression_stubs(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    suspicious_count: int,
) -> tuple[list[str], list[str]]:
    """Build runnable regression stubs from replayable mine findings."""
    report = _build_mine_report(
        target=target,
        module=module,
        results=results,
        skipped=skipped,
        include_scan_hint=False,
        suspicious_count=suspicious_count,
    )
    return _regression_stubs_from_details(module=module, details=report["details"])


def _render_mine_regression_file(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    suspicious_count: int,
) -> str | None:
    """Render a pytest file from concrete mine findings."""
    stubs, _ = _mine_regression_stubs(
        target=target,
        module=module,
        results=results,
        skipped=skipped,
        suspicious_count=suspicious_count,
    )
    if not stubs:
        return None
    return _render_regression_file(
        [
            '"""Generated by `ordeal mine --write-regression`.',
            "",
            f"Target: {target}",
            '"""',
            "",
        ],
        stubs,
    )


def _build_mine_report(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    include_scan_hint: bool,
    suspicious_count: int,
) -> dict[str, Any]:
    """Normalize mine output into the shared finding report shape."""
    from ordeal.mine import _is_suspicious_property

    details: list[dict[str, Any]] = []
    blind_spots: list[str] = []
    for name, result in results:
        blind_spots.extend(result.not_checked)
        suspicious = sorted(
            [prop for prop in result.properties if _is_suspicious_property(prop)],
            key=lambda prop: (-prop.confidence, prop.name),
        )
        for prop in suspicious:
            details.append(
                {
                    "kind": "property",
                    "module": module,
                    "function": name,
                    "qualname": f"{module}.{name}",
                    "name": prop.name,
                    "summary": f"{prop.name} ({prop.confidence:.0%})",
                    "confidence": prop.confidence,
                    "holds": prop.holds,
                    "total": prop.total,
                    "counterexample": prop.counterexample,
                }
            )

    suggested = [f"ordeal mine {target} -n 200", f"ordeal mutate {target}"]
    if include_scan_hint:
        suggested.insert(1, f"ordeal scan {module}")

    return {
        "target": target,
        "tool": "mine",
        "status": "findings found" if details else "no suspicious findings",
        "summary": [
            f"Checked: {len(results)} function(s)",
            f"Suspicious findings: {suspicious_count}",
            f"Skipped: {len(skipped)} function(s)",
        ],
        "details": details,
        "extra_sections": [
            ("What Mine Did Not Check", list(dict.fromkeys(blind_spots))),
            ("Skipped Functions", [f"`{module}.{name}`: {reason}" for name, reason in skipped]),
        ],
        "suggested_commands": suggested,
    }


def _write_scan_report(state: Any, path_str: str) -> None:
    """Write a Markdown report for `ordeal scan`."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_scan_report_markdown(state), encoding="utf-8")
    _stderr(f"Scan report saved: {path}\n")


def _write_scan_regressions(state: Any, path_str: str) -> None:
    """Write runnable pytest regressions for concrete scan findings."""
    stubs, skipped = _scan_regression_stubs(state)
    if not stubs:
        _stderr("No concrete regression tests could be generated from current scan findings.\n")
        if skipped:
            _stderr(f"Skipped {len(skipped)} finding(s) without replayable concrete inputs.\n")
        return
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


def _write_mine_regressions(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    path_str: str,
    suspicious_count: int,
) -> None:
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
        return
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


def _write_mine_report(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    path_str: str,
    include_scan_hint: bool,
    suspicious_count: int,
) -> None:
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
    # Add CWD to sys.path so imports resolve the same way as pytest/python -m.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    parser = argparse.ArgumentParser(
        prog="ordeal",
        description=(
            "Ordeal — discovers what's true about your code.\n\n"
            "Start here:\n"
            "  ordeal scan mymod --save-artifacts\n"
            "                              Save report + regressions\n"
            "  ordeal scan mymod --report-file report.md\n"
            "                              Save a shareable bug report\n"
            "  ordeal scan mymod --write-regression\n"
            "                              Save pytest regressions\n"
            "  ordeal mine mymod.func --report-file finding.md\n"
            "                              Save a finding report\n"
            "  ordeal mine mymod.func --write-regression\n"
            "                              Save pytest regressions\n"
            "  ordeal catalog              See every capability\n"
            "  catalog() in Python         Full API reference"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # -- ordeal catalog --
    cat_p = sub.add_parser(
        "catalog",
        help="Show all capabilities — faults, mining, mutations, exploration, ...",
    )
    cat_p.add_argument("--detail", action="store_true", help="Show full signatures and docstrings")

    # -- ordeal check (targeted property verification) --
    check_p = sub.add_parser(
        "check",
        help="Verify a specific property on a function (mine + assert in one step)",
    )
    check_p.add_argument("target", help="Dotted path: mymod.func")
    check_p.add_argument(
        "--property",
        "-p",
        default=None,
        help="Property to verify. Omit to check all standard contracts.",
    )
    check_p.add_argument(
        "--max-examples",
        "-n",
        type=int,
        default=200,
        help="Examples to test (default: 200)",
    )

    # -- ordeal scan (unified explore) --
    # Description auto-derived from explore().__doc__
    from ordeal.state import explore as _explore_fn

    scan_desc = (_explore_fn.__doc__ or "").strip().split("\n\n")[0]
    scan_p = sub.add_parser(
        "scan",
        help="Explore a module and optionally write reports or pytest regressions",
        description=(
            f"{scan_desc}\n\n"
            f"Use --save-artifacts to save both {_default_scan_report_path('mymod')} and"
            f" {_DEFAULT_REGRESSION_PATH} when findings exist.\n"
            "Use --report-file report.md to save a shareable Markdown bug report.\n"
            f"Use --write-regression or --write-regression PATH to save runnable pytest"
            f" regressions (default: {_DEFAULT_REGRESSION_PATH})."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan_p.add_argument("target", help="Module path (e.g. myapp.scoring)")
    scan_p.add_argument(
        "--seed", type=int, default=42, help="RNG seed for reproducibility (default: 42)"
    )
    scan_p.add_argument(
        "--max-examples", "-n", type=int, default=50, help="Examples per function (default: 50)"
    )
    scan_p.add_argument(
        "--workers", "-w", type=int, default=1, help="Parallel workers for mutation testing"
    )
    scan_p.add_argument(
        "--time-limit", "-t", type=float, default=None, help="Time budget in seconds"
    )
    scan_p.add_argument("--json", action="store_true", help="Output JSON instead of text")
    scan_p.add_argument(
        "--save-artifacts",
        action="store_true",
        help=(
            "When findings exist, write both the default Markdown dossier and"
            f" regression file ({_default_scan_report_path('mymod')}, {_DEFAULT_REGRESSION_PATH})"
        ),
    )
    scan_p.add_argument(
        "--report-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Write a shareable Markdown finding report to PATH",
    )
    scan_p.add_argument(
        "--write-regression",
        type=str,
        default=None,
        nargs="?",
        const=_DEFAULT_REGRESSION_PATH,
        metavar="PATH",
        help=(
            "Write runnable pytest regressions for replayable findings"
            f" (default: {_DEFAULT_REGRESSION_PATH})"
        ),
    )
    scan_p.add_argument(
        "--include-private",
        action="store_true",
        help="Include _private functions (many codebases have logic there)",
    )

    # -- ordeal explore --
    explore_p = sub.add_parser(
        "explore",
        help="Coverage-guided state exploration (reads ordeal.toml)",
    )
    explore_p.add_argument(
        "--config", "-c", default="ordeal.toml", help="Config file (default: ordeal.toml)"
    )
    explore_p.add_argument("--seed", type=int, help="Override RNG seed")
    explore_p.add_argument("--max-time", type=float, help="Override max_time (seconds)")
    explore_p.add_argument("--verbose", "-v", action="store_true", help="Live progress")
    explore_p.add_argument("--no-shrink", action="store_true", help="Skip shrinking")
    explore_p.add_argument("--no-seeds", action="store_true", help="Skip seed corpus replay")
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
    replay_p.add_argument(
        "--ablate", action="store_true", help="Ablate faults to find necessary ones"
    )
    replay_p.add_argument("--output", "-o", help="Save shrunk trace to this path")

    # -- ordeal seeds --
    seeds_p = sub.add_parser("seeds", help="List or manage the persistent seed corpus")
    seeds_p.add_argument(
        "--dir", default=".ordeal/seeds", help="Seed corpus directory (default: .ordeal/seeds)"
    )
    seeds_p.add_argument(
        "--prune-fixed", action="store_true", help="Remove seeds that no longer reproduce"
    )

    # -- ordeal audit --
    audit_p = sub.add_parser(
        "audit",
        help="Audit test coverage vs ordeal migration",
        description=(
            "Compare your current tests with ordeal-generated tests.\n\n"
            "Validation modes:\n"
            "  fast  replay mined inputs against mutants (default, faster)\n"
            "  deep  replay mined inputs, then re-mine mutants for extra search depth"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    audit_p.add_argument("modules", nargs="+", help="Module paths to audit")
    audit_p.add_argument(
        "--test-dir", "-t", default="tests", help="Test directory (default: tests)"
    )
    audit_p.add_argument(
        "--max-examples", type=int, default=20, help="Examples per function (default: 20)"
    )
    audit_p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers for mutation validation (default: 1)",
    )
    audit_p.add_argument(
        "--validation-mode",
        choices=("fast", "deep"),
        default="fast",
        help="Mutation validation mode: fast replay (default) or deep replay + re-mine",
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
    mine_p = sub.add_parser(
        "mine",
        help="Discover properties and optionally write reports or pytest regressions",
        description=(
            "Discover properties of a function or module.\n\n"
            "Use --report-file report.md to save a shareable Markdown finding report.\n"
            f"Use --write-regression or --write-regression PATH to save runnable pytest"
            f" regressions (default: {_DEFAULT_REGRESSION_PATH})."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mine_p.add_argument("target", help="Dotted path: mymod.func or mymod")
    mine_p.add_argument(
        "--max-examples", "-n", type=int, default=500, help="Examples to sample (default: 500)"
    )
    mine_p.add_argument(
        "--verbose", "-v", action="store_true", help="Show n/a properties and extra detail"
    )
    mine_p.add_argument(
        "--include-private",
        action="store_true",
        help="Include _private functions (many codebases have logic there)",
    )
    mine_p.add_argument(
        "--report-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Write a shareable Markdown finding report to PATH",
    )
    mine_p.add_argument(
        "--write-regression",
        type=str,
        default=None,
        nargs="?",
        const=_DEFAULT_REGRESSION_PATH,
        metavar="PATH",
        help=(
            "Write runnable pytest regressions for suspicious findings"
            f" (default: {_DEFAULT_REGRESSION_PATH})"
        ),
    )

    # -- ordeal mine-pair --
    mp_p = sub.add_parser("mine-pair", help="Discover relational properties between two functions")
    mp_p.add_argument("f", help="First function: mymod.func_a")
    mp_p.add_argument("g", help="Second function: mymod.func_b")
    mp_p.add_argument(
        "--max-examples", "-n", type=int, default=200, help="Examples to sample (default: 200)"
    )

    # -- ordeal benchmark --
    bench_p = sub.add_parser(
        "benchmark",
        help="Measure scaling, mutation latency, or a checked-in perf/quality contract",
    )
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
    bench_p.add_argument(
        "--perf-contract",
        default=None,
        help="Run a checked-in perf/quality contract TOML file instead of scaling analysis",
    )
    bench_p.add_argument(
        "--check",
        action="store_true",
        help="Return exit code 1 when a perf-contract case exceeds a time or score-gap budget",
    )
    bench_p.add_argument(
        "--output-json",
        default=None,
        metavar="PATH",
        help="Write perf/quality contract results as JSON to PATH",
    )
    bench_p.add_argument(
        "--tier",
        default=None,
        choices=["pr", "nightly"],
        help="Only run perf-contract cases matching this tier (default: all)",
    )
    bench_p.add_argument(
        "--mutate",
        dest="mutate_targets",
        action="append",
        default=[],
        help="Benchmark mutation latency for this target (repeat for multiple targets)",
    )
    bench_p.add_argument(
        "--repeat",
        type=int,
        default=5,
        help="Fresh subprocess runs per mutation target (default: 5)",
    )
    bench_p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Workers to use for mutation benchmarks (default: 1)",
    )
    bench_p.add_argument(
        "--preset",
        choices=["essential", "standard", "thorough"],
        default="standard",
        help="Mutation preset for mutation benchmarks (default: standard)",
    )
    bench_p.add_argument(
        "--test-filter",
        default=None,
        help="Pytest -k filter for mutation benchmarks",
    )
    bench_p.add_argument(
        "--no-filter-equivalent",
        dest="filter_equivalent",
        action="store_false",
        help="Disable equivalence filtering during mutation benchmarks",
    )
    bench_p.set_defaults(filter_equivalent=True)

    # -- ordeal skill --
    skill_p = sub.add_parser("skill", help="Install ordeal skill for AI coding agents")
    skill_p.add_argument(
        "--dry-run", action="store_true", help="Show what would be written without writing"
    )

    # -- ordeal init --
    init_p = sub.add_parser("init", help="Bootstrap test files for untested modules")
    init_p.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Package path (e.g. myapp); auto-detects if omitted",
    )
    init_p.add_argument(
        "--output-dir",
        "-o",
        default="tests",
        help="Directory to write test files (default: tests)",
    )
    init_p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview without side effects — no files written, no functions executed. "
            "Generates stub tests from signatures only."
        ),
    )
    init_p.add_argument(
        "--ci",
        action="store_true",
        help="Generate a GitHub Actions workflow (.github/workflows/<name>.yml)",
    )
    init_p.add_argument(
        "--ci-name",
        default="ordeal",
        metavar="NAME",
        help="Workflow filename (default: ordeal → .github/workflows/ordeal.yml)",
    )

    # -- ordeal mutate --
    mutate_p = sub.add_parser(
        "mutate",
        help="Test whether your tests catch code changes",
    )
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
        "--test-filter",
        "-k",
        type=str,
        default=None,
        metavar="EXPR",
        help="Pytest -k expression to select tests (avoids running full suite per mutant)",
    )
    mutate_p.add_argument(
        "--mutant-timeout",
        type=float,
        default=None,
        metavar="SECS",
        help="Timeout for mutant generation step in seconds (skip functions that hang)",
    )
    mutate_p.add_argument(
        "--disk-mutation",
        action="store_true",
        default=None,
        help=(
            "Write mutations to disk so subprocesses (Ray, multiprocessing) see them. "
            "Auto-detected when omitted."
        ),
    )
    mutate_p.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Reuse cached results for unchanged targets (cache: .ordeal/mutate/). "
            "Invalidated when module source, test files (test_<module>*.py), "
            "conftest.py, lockfile, or preset/operators change. "
            "Mine oracle results are never cached. "
            "Note: test files not matching test_<module>*.py are not tracked; "
            "use --no-resume or delete .ordeal/mutate/ if using test_filter "
            "with non-standard test names."
        ),
    )
    mutate_p.add_argument(
        "--generate-stubs",
        type=str,
        default=None,
        metavar="PATH",
        help="Write test stubs for surviving mutants to PATH",
    )

    args = parser.parse_args(argv)

    if args.command == "catalog":
        return _cmd_catalog(args)
    elif args.command == "check":
        return _cmd_check(args)
    elif args.command == "scan":
        return _cmd_scan(args)
    elif args.command == "explore":
        return _cmd_explore(args)
    elif args.command == "replay":
        return _cmd_replay(args)
    elif args.command == "seeds":
        return _cmd_seeds(args)
    elif args.command == "audit":
        return _cmd_audit(args)
    elif args.command == "mine":
        return _cmd_mine(args)
    elif args.command == "mine-pair":
        return _cmd_mine_pair(args)
    elif args.command == "benchmark":
        return _cmd_benchmark(args)
    elif args.command == "skill":
        return _cmd_skill(args)
    elif args.command == "init":
        return _cmd_init(args)
    elif args.command == "mutate":
        return _cmd_mutate(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
