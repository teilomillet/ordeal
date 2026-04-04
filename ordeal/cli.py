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
import hashlib
import io
import json
import os
import re
import shlex
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from pprint import pformat
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

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
CLI_CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ArgumentSpec:
    """Declarative definition of one CLI argument."""

    tokens: tuple[str, ...]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class CommandSpec:
    """Declarative definition of one CLI command."""

    name: str
    handler: Callable[[argparse.Namespace], int]
    help: str
    arguments: tuple[ArgumentSpec, ...] = ()
    description: str | Callable[[], str] | None = None
    formatter_class: type[argparse.HelpFormatter] | None = None
    defaults: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScanRuntimeDefaults:
    """Resolved scan runtime config for one target module."""

    max_examples: int
    fixtures: dict[str, Any] | None
    expected_failures: list[str]
    registry_warnings: list[str]
    ignore_properties: list[str] = field(default_factory=list)
    ignore_relations: list[str] = field(default_factory=list)
    property_overrides: dict[str, list[str]] = field(default_factory=dict)
    relation_overrides: dict[str, list[str]] = field(default_factory=dict)


def _arg(*tokens: str, **kwargs: Any) -> ArgumentSpec:
    """Create a declarative CLI argument spec."""
    return ArgumentSpec(tokens=tokens, kwargs=dict(kwargs))


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

    command_entries = c.get("cli", [])
    if command_entries:
        print("\nCLI commands:")
        for entry in command_entries:
            print(f"  {entry['name']:<10} {entry.get('doc', '')}")
    print("\nRun 'ordeal --help' for the full live CLI surface.")
    print("Run 'ordeal <command> --help' for command-specific options.")
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


def _parse_scan_fixture_specs(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert TOML scan fixture specs into Hypothesis strategies."""
    import hypothesis.strategies as st

    if not raw:
        return None

    fixtures: dict[str, Any] = {}
    for name, value in raw.items():
        if isinstance(value, str) and "," in value:
            fixtures[name] = st.sampled_from(value.split(","))
        elif isinstance(value, str):
            fixtures[name] = st.just(value)
        else:
            fixtures[name] = st.just(value)
    return fixtures


def _resolve_scan_runtime_defaults(
    target: str,
    *,
    requested_examples: int,
    allow_config_override: bool = False,
) -> ScanRuntimeDefaults:
    """Load fixture registries and optional ``[[scan]]`` defaults for *target*."""
    from ordeal.auto import load_project_fixture_registries

    warnings = load_project_fixture_registries()
    effective_examples = requested_examples
    fixtures = None
    expected_failures: list[str] = []
    ignore_properties: list[str] = []
    ignore_relations: list[str] = []
    property_overrides: dict[str, list[str]] = {}
    relation_overrides: dict[str, list[str]] = {}

    config_path = Path("ordeal.toml")
    if not config_path.exists():
        return ScanRuntimeDefaults(
            max_examples=effective_examples,
            fixtures=fixtures,
            expected_failures=expected_failures,
            registry_warnings=warnings,
        )

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ConfigError):
        return ScanRuntimeDefaults(
            max_examples=effective_examples,
            fixtures=fixtures,
            expected_failures=expected_failures,
            registry_warnings=warnings,
        )

    match = next((entry for entry in cfg.scan if entry.module == target), None)
    if match is None:
        return ScanRuntimeDefaults(
            max_examples=effective_examples,
            fixtures=fixtures,
            expected_failures=expected_failures,
            registry_warnings=warnings,
        )

    if allow_config_override:
        effective_examples = match.max_examples
    fixtures = _parse_scan_fixture_specs(match.fixtures)
    expected_failures = list(match.expected_failures)
    warnings.extend(load_project_fixture_registries(extra_modules=match.fixture_registries))
    ignore_properties = list(match.ignore_properties)
    ignore_relations = list(match.ignore_relations)
    property_overrides = {
        str(name): list(values) for name, values in match.property_overrides.items()
    }
    relation_overrides = {
        str(name): list(values) for name, values in match.relation_overrides.items()
    }
    return ScanRuntimeDefaults(
        max_examples=effective_examples,
        fixtures=fixtures,
        expected_failures=expected_failures,
        registry_warnings=warnings,
        ignore_properties=ignore_properties,
        ignore_relations=ignore_relations,
        property_overrides=property_overrides,
        relation_overrides=relation_overrides,
    )


def _run_configured_scans(
    scan_entries: Sequence[Any],
    *,
    verbose: bool = True,
) -> int:
    """Execute ``[[scan]]`` entries from config through the library scan API."""
    from ordeal.auto import load_project_fixture_registries, scan_module

    exit_code = 0
    for scan_cfg in scan_entries:
        warnings = load_project_fixture_registries(extra_modules=scan_cfg.fixture_registries)
        for warning in warnings:
            _stderr(f"warning: {warning}\n")
        fixtures = _parse_scan_fixture_specs(scan_cfg.fixtures)
        if verbose:
            _stderr(f"Scanning {scan_cfg.module} from [[scan]]...\n")
        result = scan_module(
            scan_cfg.module,
            max_examples=scan_cfg.max_examples,
            fixtures=fixtures,
            expected_failures=scan_cfg.expected_failures,
            ignore_properties=scan_cfg.ignore_properties,
            property_overrides=scan_cfg.property_overrides,
        )
        print(result.summary())
        if not result.passed:
            exit_code = 1
    return exit_code


def _cmd_scan(args: argparse.Namespace) -> int:
    """Run unified exploration: mine + scan + mutate + chaos in one pass.

    This is the recommended entry point for AI assistants.  Point it
    at a module and it does everything: discovers properties, checks
    for crashes, mutation-tests, and chaos-tests.  Returns confidence,
    findings, and frontier.
    """
    from ordeal.state import explore

    inc_private = getattr(args, "include_private", False)
    allow_config_override = args.max_examples == 50
    runtime_defaults = _resolve_scan_runtime_defaults(
        args.target,
        requested_examples=args.max_examples,
        allow_config_override=allow_config_override,
    )
    if not args.json:
        _stderr(f"Scanning {args.target} (seed={args.seed})...\n")
        for warning in runtime_defaults.registry_warnings:
            _stderr(f"warning: {warning}\n")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        state = explore(
            args.target,
            seed=args.seed,
            max_examples=runtime_defaults.max_examples,
            workers=args.workers,
            time_limit=args.time_limit,
            include_private=inc_private,
            scan_fixtures=runtime_defaults.fixtures,
            scan_expected_failures=runtime_defaults.expected_failures,
            scan_ignore_properties=runtime_defaults.ignore_properties,
            scan_property_overrides=runtime_defaults.property_overrides,
        )

    if not args.json:
        print(_format_scan_summary(state))
        if (
            (state.findings or _scan_report_details(state))
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
    written_report_path: Path | None = None
    written_regression_path: Path | None = None
    index_path: Path | None = None
    has_details = bool(state.findings or _scan_report_details(state))
    if save_artifacts and has_details:
        report_path = report_path or _default_scan_report_path(state.module)
        regression_path = regression_path or _DEFAULT_REGRESSION_PATH
    if report_path:
        written_report_path = _write_scan_report(state, report_path)
    if regression_path:
        written_regression_path = _write_scan_regressions(state, regression_path)
    if save_artifacts and not has_details:
        _stderr("No findings yet; no artifacts written.\n")
    if save_artifacts and has_details and written_report_path is not None:
        bundle_path, bundle = _write_scan_bundle(
            state,
            path_str=_artifact_bundle_path(str(written_report_path)),
            report_path=written_report_path,
            regression_path=written_regression_path,
        )
        index_path = _write_scan_artifact_index(
            bundle=bundle,
            bundle_path=bundle_path,
        )
        if not args.json:
            _print_scan_artifact_workflow(
                module=state.module,
                report_path=written_report_path,
                bundle_path=bundle_path,
                finding_ids=[finding["finding_id"] for finding in bundle["findings"]],
                regression_path=written_regression_path,
                index_path=index_path,
            )

    if args.json:
        print(
            _build_scan_agent_envelope(
                state,
                written_report_path=written_report_path,
                written_regression_path=written_regression_path,
                index_path=index_path,
            ).to_json()
        )

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
        if cfg.scan:
            return _run_configured_scans(cfg.scan, verbose=verbose)
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


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run ordeal audit on specified modules."""
    from ordeal.audit import audit, audit_report

    if getattr(args, "json", False):
        results = [
            audit(
                mod,
                test_dir=args.test_dir,
                max_examples=args.max_examples,
                workers=args.workers,
                validation_mode=args.validation_mode,
            )
            for mod in args.modules
        ]
        saved_generated_path: Path | None = None
        if args.save_generated and len(results) == 1 and results[0].generated_test:
            saved_generated_path = Path(args.save_generated)
            saved_generated_path.write_text(results[0].generated_test, encoding="utf-8")
        print(
            _build_audit_agent_envelope(
                results,
                saved_generated_path=saved_generated_path,
            ).to_json()
        )
        return 0

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
            result = mine(func, max_examples=max_examples)
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


def _run_init_scan(modules: Sequence[str], *, max_examples: int = 10) -> dict[str, Any]:
    """Run a bounded, read-only scan over freshly bootstrapped modules."""
    from ordeal.auto import scan_module

    deduped_modules = [module for module in dict.fromkeys(modules) if module]
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scanned_modules: list[str] = []
    functions_checked = 0
    skipped_functions = 0

    for module in deduped_modules:
        try:
            runtime_defaults = _resolve_scan_runtime_defaults(
                module,
                requested_examples=max_examples,
                allow_config_override=False,
            )
            scan_kwargs: dict[str, Any] = {
                "max_examples": runtime_defaults.max_examples,
                "fixtures": runtime_defaults.fixtures,
                "expected_failures": runtime_defaults.expected_failures,
            }
            if runtime_defaults.ignore_properties:
                scan_kwargs["ignore_properties"] = runtime_defaults.ignore_properties
            if runtime_defaults.property_overrides:
                scan_kwargs["property_overrides"] = runtime_defaults.property_overrides
            result = scan_module(module, **scan_kwargs)
        except Exception as exc:
            errors.append({"module": module, "error": str(exc)})
            continue

        scanned_modules.append(module)
        functions_checked += len(result.functions)
        skipped_functions += len(result.skipped)

        for function in result.functions:
            qualname = f"{module}.{function.name}"
            if not function.passed and function.replayable:
                findings.append(
                    {
                        "kind": "crash",
                        "category": "likely_bug",
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": f"{qualname}: crash safety failed",
                        "error": function.error,
                        "failing_args": function.failing_args,
                    }
                )
            elif not function.passed:
                findings.append(
                    {
                        "kind": "crash",
                        "category": "speculative_property",
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": f"{qualname}: crash safety failed",
                        "error": function.error,
                        "failing_args": function.failing_args,
                    }
                )
            for violation in function.property_violations:
                findings.append(
                    {
                        "kind": "property",
                        "category": "speculative_property",
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": f"{qualname}: {violation}",
                    }
                )
            for note in function.contract_violation_details:
                findings.append(
                    {
                        **note,
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                    }
                )

    if any(item.get("category") == "likely_bug" for item in findings):
        status = "findings found"
    elif findings:
        status = "exploratory findings"
    elif scanned_modules or not errors:
        status = "no findings yet"
    else:
        status = "scan unavailable"

    return {
        "status": status,
        "modules": scanned_modules,
        "functions_checked": functions_checked,
        "skipped_functions": skipped_functions,
        "findings": findings,
        "errors": errors,
        "max_examples": max_examples,
        "available_commands": [
            f"ordeal scan {module} --save-artifacts" for module in deduped_modules
        ],
    }


def _cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap test files for untested modules."""
    import re
    import subprocess

    from ordeal.audit import audit
    from ordeal.mutations import init_project

    target: str | None = args.target or None
    output_dir: str = args.output_dir
    dry_run: bool = args.dry_run
    ci: bool = args.ci
    ci_name: str = args.ci_name
    install_skill: bool = args.install_skill
    close_gaps: bool = args.close_gaps

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
    skill_path = _install_skill(dry_run=dry_run) if install_skill else None

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

    def _aggregate_mutation_score(results: Sequence[Any]) -> str:
        counts = [result.mutation_score_counts for result in results]
        concrete = [count for count in counts if count is not None]
        if not concrete:
            return ""
        killed = sum(count[0] for count in concrete)
        total = sum(count[1] for count in concrete)
        if total <= 0:
            return ""
        return f"{killed}/{total} ({(killed / total):.0%})"

    def _gap_stub_path(output_dir: str, target_name: str) -> Path:
        safe = target_name.replace(".", "_")
        return Path(output_dir) / f"test_{safe}_gaps.py"

    def _write_init_gap_stubs(
        audit_results: Sequence[Any],
        *,
        output_dir: str,
    ) -> list[dict[str, Any]]:
        written: list[dict[str, Any]] = []
        for result in audit_results:
            for item in result.mutation_gap_stubs:
                content = str(item.get("content", "")).strip()
                target_name = str(item.get("target", "")).strip()
                if not content or not target_name:
                    continue
                path = _gap_stub_path(output_dir, target_name)
                path.parent.mkdir(parents=True, exist_ok=True)
                existing = path.read_text(encoding="utf-8") if path.exists() else None
                if existing != content + "\n":
                    path.write_text(content + "\n", encoding="utf-8")
                written.append(
                    {
                        "module": result.module,
                        "target": target_name,
                        "path": str(path),
                    }
                )
        return written

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
    gap_stub_files: list[dict[str, Any]] = []
    weakest_tests: list[dict[str, Any]] = []
    if close_gaps:
        generated_modules = [r["module"] for r in generated]
        audit_results = [
            audit(module, test_dir=output_dir, max_examples=10) for module in generated_modules
        ]
        mutation_score = _aggregate_mutation_score(audit_results)
        gap_stub_files = _write_init_gap_stubs(audit_results, output_dir=output_dir)
        weakest_tests = [
            {"module": result.module, **item}
            for result in audit_results
            for item in result.weakest_tests
        ]
    else:
        mp = _run_ordeal(["mutate", *mut_targets, "-p", "essential"])
        for line in mp.stdout.splitlines():
            if line.startswith("Score:"):
                mutation_score = line.strip()
                break

    # --- Phase 3: Lightweight read-only scan ---
    initial_scan = _run_init_scan([r["module"] for r in generated], max_examples=10)

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
        if not close_gaps:
            _stderr(
                "  Gaps:       report-only"
                " (use --close-gaps to write draft audit stub files)\n"
            )
    if close_gaps and gap_stub_files:
        _stderr(f"  Gaps:       wrote {len(gap_stub_files)} draft stub file(s) from audit\n")
    if close_gaps and weakest_tests:
        preview = ", ".join(
            f"{item['test']} ({item['kills']} kill(s))" for item in weakest_tests[:3]
        )
        _stderr(f"  Weakest:    {preview}\n")

    if initial_scan["status"] in {"findings found", "exploratory findings"}:
        _stderr(
            "  Initial scan:"
            f" {len(initial_scan['findings'])} finding(s)"
            f" across {len(initial_scan['modules'])} module(s)\n"
        )
        for finding in initial_scan["findings"][:3]:
            _stderr(f"    {finding['summary']}\n")
        remaining = len(initial_scan["findings"]) - 3
        if remaining > 0:
            _stderr(f"    ... {remaining} more finding(s)\n")
    elif initial_scan["status"] == "scan unavailable":
        _stderr(f"  Initial scan: unavailable ({len(initial_scan['errors'])} module error(s))\n")
        for error in initial_scan["errors"][:2]:
            _stderr(f"    {error['module']}: {error['error']}\n")
    else:
        summary = (
            "  Initial scan:"
            f" no findings yet ({initial_scan['functions_checked']} function(s) checked"
        )
        if initial_scan["skipped_functions"]:
            summary += f", {initial_scan['skipped_functions']} skipped"
        summary += ")\n"
        _stderr(summary)

    _stderr("\n  Files:\n")
    for r in generated:
        _stderr(f"    {r['path']}\n")
    for item in gap_stub_files:
        _stderr(f"    {item['path']}\n")
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
        "initial_scan": initial_scan,
        "close_gaps": close_gaps,
        "gap_stub_files": gap_stub_files,
        "weakest_tests": weakest_tests,
        "ci_workflow": ci_path,
        "install_skill": install_skill,
        "skill": skill_path,
        "files": [r["path"] for r in generated]
        + [item["path"] for item in gap_stub_files]
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
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary="no mutation targets configured",
                        blocking_reason="no targets specified and no config file found",
                        suggested_commands=(
                            "ordeal mutate myapp.scoring.compute",
                            "ordeal mutate myapp.scoring",
                        ),
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
            _stderr(
                "No targets specified. Use positional args or [mutations] in ordeal.toml.\n"
                "  ordeal mutate myapp.scoring.compute\n"
                "  ordeal mutate myapp.scoring\n"
            )
            return 1
        except ConfigError as e:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary=f"config error in {config_path}",
                        blocking_reason=str(e),
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
            _stderr(f"Config error: {e}\n")
            return 1

        if cfg.mutations is None:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary="no [mutations] section in config",
                        blocking_reason="config has no [mutations] section",
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
            _stderr("No [mutations] section in config.\n")
            return 1

        targets = cfg.mutations.targets
        if not targets:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary="no mutation targets in config",
                        blocking_reason="config [mutations] section has no targets",
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
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
    blockers: list[dict[str, Any]] = []
    exit_code = 0
    stubs_path = Path(args.generate_stubs) if args.generate_stubs else None

    for target in targets:
        if not getattr(args, "json", False):
            _stderr(f"Mutating {target}...\n")

        try:
            if getattr(args, "json", False):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
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
            else:
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
            if not getattr(args, "json", False):
                _stderr(f"  WARNING: No tests found for {target!r}\n")
            starter = generate_starter_tests(target)
            suggested = e.suggested_file or f"tests/test_{target.rsplit('.', 1)[-1]}.py"
            blockers.append(
                {
                    "target": target,
                    "summary": f"No tests found for {target}",
                    "suggested_test_file": suggested,
                    "starter_tests": starter,
                }
            )
            if starter:
                if args.generate_stubs:
                    assert stubs_path is not None
                    stubs_path.parent.mkdir(parents=True, exist_ok=True)
                    existing = (
                        stubs_path.read_text(encoding="utf-8") if stubs_path.exists() else ""
                    )
                    sep = "\n\n" if existing else ""
                    stubs_path.write_text(existing + sep + starter, encoding="utf-8")
                    if not getattr(args, "json", False):
                        _stderr(f"  Starter tests written: {stubs_path}\n")
                elif not getattr(args, "json", False):
                    # Print the scaffold directly — don't hide it behind a flag
                    print(f"\n# Save to: {suggested}\n")
                    print(starter)
                    _stderr(f"  Or run: ordeal init {target}\n")
            exit_code = 1
            continue
        except (ImportError, AttributeError, ValueError) as e:
            if getattr(args, "json", False):
                blockers.append(
                    {
                        "target": target,
                        "summary": str(e),
                        "suggested_test_file": None,
                        "starter_tests": None,
                    }
                )
            else:
                _stderr(f"  Error: {e}\n")
            exit_code = 1
            continue

        all_results.append((target, result))
        if not getattr(args, "json", False):
            print(result.summary())
            print()

        if threshold > 0.0 and result.score < threshold:
            exit_code = 1

    # Generate test stubs if requested
    if args.generate_stubs and stubs_path is not None:
        all_stubs: list[str] = []
        for _, result in all_results:
            stub = result.generate_test_stubs()
            if stub:
                all_stubs.append(stub)
        if all_stubs:
            stubs_path.parent.mkdir(parents=True, exist_ok=True)
            stubs_path.write_text("\n\n".join(all_stubs), encoding="utf-8")
            if not getattr(args, "json", False):
                _stderr(f"Test stubs written: {stubs_path}\n")

    # Final score line — always printed for CI parseability
    if all_results:
        total_mutants = sum(r.total for _, r in all_results)
        total_killed = sum(r.killed for _, r in all_results)
        overall = total_killed / total_mutants if total_mutants > 0 else 1.0
        if len(all_results) > 1 and not getattr(args, "json", False):
            print(f"Overall: {total_killed}/{total_mutants} ({overall:.0%})")
        if not getattr(args, "json", False):
            print(f"Score: {total_killed}/{total_mutants} ({overall:.0%})")
        if threshold > 0.0 and not getattr(args, "json", False):
            status = "PASS" if overall >= threshold else "FAIL"
            print(f"Threshold: {threshold:.0%} — {status}")

    if getattr(args, "json", False):
        print(
            _build_mutate_agent_envelope(
                targets=targets,
                results=all_results,
                blockers=blockers,
                threshold=threshold,
                stubs_path=stubs_path,
            ).to_json()
        )

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
    details = _scan_report_details(state)
    exploratory = [
        detail for detail in details if detail.get("category") == "speculative_property"
    ]
    expected = [
        detail for detail in details if detail.get("category") == "expected_precondition_failure"
    ]
    if state.findings:
        status = "findings found"
    elif exploratory:
        status = "exploratory findings"
    elif expected:
        status = "expected preconditions observed"
    else:
        status = "no findings yet"
    lines.append(f"  status: {status}")
    lines.append(f"  confidence: {state.confidence:.0%}")

    lines.append(f"  checked: {', '.join(_scan_checked_items(state))}")

    if state.findings:
        lines.append("  findings:")
        for finding in state.findings[:5]:
            lines.append(f"    - {finding}")
    else:
        lines.append("  findings: none promoted")
        if exploratory:
            lines.append("  exploratory:")
            for detail in exploratory[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if expected:
            lines.append("  expected preconditions:")
            for detail in expected[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")

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


def _scan_evidence_dimensions(state: Any) -> dict[str, Any]:
    """Expose scan evidence as interpretable dimensions, not one score."""
    functions = getattr(state, "functions", {}) or {}
    skipped = list(getattr(state, "skipped", []))
    details = _scan_report_details(state)
    replayable = sum(
        1
        for detail in details
        if detail.get("replayable")
        or detail.get("counterexample") is not None
        or detail.get("failing_args") is not None
    )
    mutation_scores = [
        float(getattr(func_state, "mutation_score"))
        for func_state in functions.values()
        if getattr(func_state, "mutation_score", None) is not None
    ]
    total_functions = len(functions) + len(skipped)
    return {
        "search_depth": {
            "functions": len(functions),
            "transitions": getattr(state, "supervisor_info", {}).get("trajectory_steps", 0),
            "checkpoints": getattr(getattr(state, "tree", None), "size", 0),
        },
        "replayability": {
            "replayable_findings": replayable,
            "total_findings": len(details),
        },
        "mutation_strength": (
            sum(mutation_scores) / len(mutation_scores) if mutation_scores else None
        ),
        "fixture_completeness": (len(functions) / total_functions if total_functions > 0 else 1.0),
    }


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
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".md"])


def _default_scan_bundle_path(module: str) -> str:
    """Return the default JSON artifact path for a scanned module."""
    parts = module.split(".")
    return "/".join([_DEFAULT_FINDINGS_DIR, *parts[:-1], parts[-1] + ".json"])


def _default_artifact_index_path() -> str:
    """Return the default artifact index path for saved scan findings."""
    return f"{_DEFAULT_FINDINGS_DIR}/index.json"


def _display_path(path: Path) -> str:
    """Render a path in a stable, shell-friendly form for CLI output."""
    return path.as_posix()


def _shell_command(*parts: str) -> str:
    """Join shell arguments into a displayable command string."""
    return shlex.join(parts)


def _artifact_bundle_path(report_path: str) -> str:
    """Derive the JSON bundle path from a Markdown report path."""
    return str(Path(report_path).with_suffix(".json"))


def _finding_identity(detail: dict[str, Any]) -> dict[str, Any]:
    """Return the stable identity fields for one finding."""
    return {
        "module": detail.get("module"),
        "function": detail.get("function"),
        "kind": detail.get("kind"),
        "name": detail.get("name"),
    }


def _finding_fingerprint(detail: dict[str, Any]) -> str:
    """Return a stable fingerprint for correlating the same finding across runs."""
    payload = json.dumps(_finding_identity(detail), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _annotate_finding(detail: dict[str, Any]) -> dict[str, Any]:
    """Attach stable IDs to a normalized finding detail record."""
    fingerprint = _finding_fingerprint(detail)
    module = detail.get("module")
    stub = _render_regression_stub(module, detail, trim=False) if module else None
    return {
        **detail,
        "finding_id": f"fnd_{fingerprint[:12]}",
        "fingerprint": fingerprint,
        "status": "open",
        "regression_test": _regression_test_name(stub) if stub else None,
    }


def _read_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON artifact from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON artifact with stable formatting."""
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _resolve_artifact_path(path_str: str | None, *, workspace: str | None = None) -> Path | None:
    """Resolve an artifact path against the recorded workspace when needed."""
    if not path_str:
        return None
    path = Path(path_str)
    if path.is_absolute():
        return path
    if workspace:
        return Path(workspace) / path
    return path


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
    category = detail.get("category")
    title = detail.get("name") or detail.get("summary") or kind
    module = detail.get("module", "")

    lines = [f"### {detail['index']}. `{qualname}`", "", f"- Type: {kind}", f"- Finding: {title}"]
    if category:
        lines.append(f"- Category: {category}")

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
        if detail.get("replay_attempts"):
            lines.append(
                "- Replay:"
                f" `{detail.get('replay_matches', 0)}/{detail.get('replay_attempts', 0)}`"
                " matching replays"
            )
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
                (
                    f"- Reproduce the crash directly in a regression test for `{qualname}`"
                    if detail.get("replayable")
                    else f"- Re-run `{qualname}` with the recorded input to confirm the failure"
                ),
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
    evidence = _scan_evidence_dimensions(state)
    search_depth = evidence["search_depth"]
    replayability = evidence["replayability"]
    mutation_strength = evidence["mutation_strength"]
    fixture_completeness = evidence["fixture_completeness"]
    details = [
        {
            **detail,
            "module": state.module,
            "qualname": f"{state.module}.{detail.get('function', '?')}",
        }
        for detail in _scan_report_details(state)
    ]
    promoted_count = len(getattr(state, "findings", []))
    exploratory_count = sum(
        1 for detail in details if detail.get("category") == "speculative_property"
    )
    expected_count = sum(
        1 for detail in details if detail.get("category") == "expected_precondition_failure"
    )
    if promoted_count:
        status = "findings found"
    elif exploratory_count:
        status = "exploratory findings"
    elif expected_count:
        status = "expected preconditions observed"
    else:
        status = "no findings yet"
    return {
        "target": state.module,
        "tool": "scan",
        "status": status,
        "confidence": f"{state.confidence:.0%}",
        "seed": getattr(state, "supervisor_info", {}).get("seed"),
        "summary": [
            f"Checked: {', '.join(_scan_checked_items(state))}",
            f"Promoted findings: {promoted_count}",
            f"Exploratory findings: {exploratory_count}",
            f"Expected precondition failures: {expected_count}",
            f"Gaps: {sum(len(v) for v in state.frontier.values()) if state.frontier else 0}",
            (
                "Evidence:"
                f" search depth={search_depth['functions']} functions/"
                f"{search_depth['transitions']} transitions/"
                f"{search_depth['checkpoints']} checkpoints,"
                f" replayability={replayability['replayable_findings']}/"
                f"{replayability['total_findings']},"
                f" mutation strength="
                f"{(f'{mutation_strength:.0%}' if mutation_strength is not None else 'n/a')},"
                f" fixture completeness={fixture_completeness:.0%}"
            ),
        ],
        "details": details,
        "gaps": [
            f"`{state.module}.{name}`: {', '.join(gaps)}" for name, gaps in state.frontier.items()
        ],
        "suggested_commands": [
            f"ordeal scan {state.module}",
            f"ordeal mine {state.module} -n 200",
            f"ordeal mutate {state.module}",
        ],
        "extra_sections": [
            (
                "Evidence Dimensions",
                [
                    (
                        "search depth: "
                        f"{search_depth['functions']} functions, "
                        f"{search_depth['transitions']} transitions, "
                        f"{search_depth['checkpoints']} checkpoints"
                    ),
                    (
                        "replayability: "
                        f"{replayability['replayable_findings']}/"
                        f"{replayability['total_findings']} findings have concrete inputs"
                    ),
                    (
                        "mutation strength: "
                        + (
                            f"{mutation_strength:.0%}"
                            if mutation_strength is not None
                            else "not measured yet"
                        )
                    ),
                    f"fixture completeness: {fixture_completeness:.0%}",
                ],
            )
        ],
    }


def _render_scan_report_markdown(state: Any) -> str:
    """Render a shareable Markdown finding report for `ordeal scan`."""
    return _render_findings_report_markdown(_build_scan_report(state))


def _build_scan_bundle(
    state: Any,
    *,
    report_path: Path,
    regression_path: Path | None,
) -> dict[str, Any]:
    """Build the machine-readable scan artifact bundle."""
    report = _build_scan_report(state)
    findings = [_annotate_finding(detail) for detail in report["details"]]
    saved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "version": 1,
        "saved_at": saved_at,
        "tool": "scan",
        "target": report["target"],
        "workspace": os.getcwd(),
        "status": report["status"],
        "confidence": round(state.confidence, 4),
        "seed": report.get("seed"),
        "summary": report["summary"],
        "gaps": report["gaps"],
        "finding_count": len(findings),
        "findings": findings,
        "artifacts": {
            "report": _display_path(report_path),
            "bundle": None,
            "regression": _display_path(regression_path) if regression_path else None,
            "index": _display_path(Path(_default_artifact_index_path())),
        },
        "commands": {
            "pytest": (
                _shell_command("uv", "run", "pytest", _display_path(regression_path), "-q")
                if regression_path
                else None
            ),
            "rescan": _shell_command(
                "uv",
                "run",
                "ordeal",
                "scan",
                state.module,
                "--save-artifacts",
            ),
        },
    }


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


def _detail_confidence(detail: Mapping[str, Any]) -> float | None:
    """Extract a numeric confidence-like score when one exists naturally."""
    value = detail.get("confidence")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    value = detail.get("mutation_score")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _detail_location(detail: Mapping[str, Any]) -> str | None:
    """Normalize common location fields into a stable string."""
    if detail.get("location") is not None:
        return str(detail["location"])
    line = detail.get("line")
    col = detail.get("col")
    if line is None:
        return None
    if col is None:
        return f"L{line}"
    return f"L{line}:{col}"


def _detail_target(detail: Mapping[str, Any], fallback: str) -> str:
    """Pick the most specific target name available for a detail."""
    qualname = detail.get("qualname")
    if qualname is not None:
        return str(qualname)
    function = detail.get("function")
    module = detail.get("module")
    if function is not None and module is not None:
        return f"{module}.{function}"
    if function is not None:
        return str(function)
    return fallback


def _agent_finding_from_detail(detail: Mapping[str, Any], fallback_target: str) -> Any:
    """Convert a shared report detail into an agent-schema finding."""
    from ordeal.agent_schema import AgentFinding

    target = _detail_target(detail, fallback_target)
    summary = str(detail.get("summary") or detail.get("name") or detail.get("kind", "finding"))
    extras = {
        key: value
        for key, value in detail.items()
        if key
        not in {
            "kind",
            "summary",
            "confidence",
            "target",
            "location",
            "qualname",
        }
    }
    return AgentFinding(
        kind=str(detail.get("kind", "finding")),
        summary=summary,
        confidence=_detail_confidence(detail),
        target=target,
        location=_detail_location(detail),
        details=extras,
    )


def _agent_artifact(kind: str, uri: str | Path, description: str, **metadata: Any) -> Any:
    """Build an agent-schema artifact."""
    from ordeal.agent_schema import AgentArtifact

    return AgentArtifact(
        kind=kind,
        uri=Path(uri).as_posix() if isinstance(uri, Path) else str(uri),
        description=description,
        metadata=metadata,
    )


def _report_summary_text(report: Mapping[str, Any]) -> str:
    """Collapse the shared report summary list into one agent-facing sentence."""
    items = [str(item) for item in report.get("summary", []) if item]
    if not items:
        return str(report.get("status", "completed"))
    return f"{report.get('status', 'completed')}: {' | '.join(items)}"


def _recommended_action_for_report(report: Mapping[str, Any]) -> str:
    """Return the highest-value next action for an agent consumer."""
    details = list(report.get("details", []))
    target = str(report.get("target", ""))
    tool = str(report.get("tool", "ordeal"))
    if details:
        first = details[0]
        qualname = _detail_target(first, target)
        kind = str(first.get("kind", "finding"))
        if kind == "property":
            return f"Write a regression test for {qualname} from the recorded counterexample."
        if kind == "crash":
            return f"Reproduce the crash in a regression test for {qualname}."
        if kind == "mutation":
            return f"Strengthen tests for {qualname} until the surviving mutant is killed."
        if kind == "coverage_gap":
            return f"Add a targeted test to cover the uncovered behavior in {qualname}."
        if kind == "fixture_gap":
            return f"Provide fixtures or constructors so ordeal can verify {qualname}."
        if kind == "mutation_gap":
            return f"Strengthen tests or mined properties for {qualname}."
        if kind == "warning":
            return (
                f"Resolve the verification warning for {qualname}"
                f" before trusting this {tool} result."
            )
    suggested = list(report.get("suggested_commands", []))
    if suggested:
        return f"Run `{suggested[0]}` next."
    return f"No immediate follow-up required for {target}."


def _build_agent_envelope_from_report(
    report: Mapping[str, Any],
    *,
    status: str,
    confidence: float | None = None,
    confidence_basis: Sequence[str] = (),
    blocking_reason: str | None = None,
    artifacts: Sequence[Any] = (),
    raw_details: Mapping[str, Any] | None = None,
    suggested_test_file: str | None = None,
) -> Any:
    """Wrap a shared report dict in the stable agent envelope."""
    from ordeal.agent_schema import build_agent_envelope

    target = str(report.get("target", ""))
    details = list(report.get("details", []))
    findings = [_agent_finding_from_detail(detail, target) for detail in details]
    return build_agent_envelope(
        tool=str(report.get("tool", "ordeal")),
        target=target,
        status=status,
        summary=_report_summary_text(report),
        recommended_action=str(
            report.get("recommended_action") or _recommended_action_for_report(report)
        ),
        suggested_commands=tuple(str(item) for item in report.get("suggested_commands", [])),
        suggested_test_file=suggested_test_file,
        confidence=confidence,
        confidence_basis=tuple(str(item) for item in confidence_basis),
        blocking_reason=blocking_reason,
        findings=findings,
        artifacts=artifacts,
        raw_details=dict(raw_details or {}),
    )


def _scan_state_payload(state: Any) -> dict[str, Any]:
    """Serialize scan state for agent consumers without assuming a concrete type."""
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())

    to_json = getattr(state, "to_json", None)
    if callable(to_json):
        return dict(json.loads(to_json()))

    return {
        "module": getattr(state, "module", None),
        "confidence": getattr(state, "confidence", None),
        "findings": list(getattr(state, "findings", [])),
        "finding_details": _scan_report_details(state),
        "frontier": dict(getattr(state, "frontier", {})),
        "skipped": list(getattr(state, "skipped", [])),
        "supervisor_info": dict(getattr(state, "supervisor_info", {})),
    }


def _build_scan_agent_envelope(
    state: Any,
    *,
    written_report_path: Path | None = None,
    written_regression_path: Path | None = None,
    index_path: Path | None = None,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal scan`."""
    report = _build_scan_report(state)
    evidence = _scan_evidence_dimensions(state)
    detail_categories = {detail.get("category") for detail in report.get("details", [])}
    artifacts: list[Any] = []
    if written_report_path is not None:
        artifacts.append(
            _agent_artifact("report", written_report_path, "shareable finding report")
        )
    if written_regression_path is not None:
        artifacts.append(
            _agent_artifact("regression", written_regression_path, "generated pytest regressions")
        )
    if index_path is not None:
        artifacts.append(_agent_artifact("index", index_path, "artifact index"))
    return _build_agent_envelope_from_report(
        report,
        status=(
            "findings"
            if state.findings
            else ("exploratory" if "speculative_property" in detail_categories else "ok")
        ),
        confidence=float(getattr(state, "confidence", 0.0)),
        confidence_basis=(
            (
                "search depth: "
                f"{evidence['search_depth']['functions']} functions, "
                f"{evidence['search_depth']['transitions']} transitions, "
                f"{evidence['search_depth']['checkpoints']} checkpoints"
            ),
            (
                "replayability: "
                f"{evidence['replayability']['replayable_findings']}/"
                f"{evidence['replayability']['total_findings']} findings"
            ),
            (
                "mutation strength: "
                + (
                    f"{evidence['mutation_strength']:.0%}"
                    if evidence["mutation_strength"] is not None
                    else "not measured"
                )
            ),
            f"fixture completeness: {evidence['fixture_completeness']:.0%}",
        ),
        artifacts=artifacts,
        raw_details={
            "report": report,
            "state": _scan_state_payload(state),
            "seed": getattr(state, "supervisor_info", {}).get("seed"),
            "finding_count": len(report.get("details", [])),
            "gap_count": len(report.get("gaps", [])),
            "evidence_dimensions": evidence,
        },
        suggested_test_file=(_DEFAULT_REGRESSION_PATH if report.get("details") else None),
    )


def _build_mine_agent_envelope(
    *,
    target: str,
    module: str,
    results: list[tuple[str, Any]],
    skipped: list[tuple[str, str]],
    include_scan_hint: bool,
    suspicious_count: int,
    report_path: Path | None = None,
    regression_path: Path | None = None,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal mine`."""
    report = _build_mine_report(
        target=target,
        module=module,
        results=results,
        skipped=skipped,
        include_scan_hint=include_scan_hint,
        suspicious_count=suspicious_count,
    )
    details = list(report.get("details", []))
    confidence = max((_detail_confidence(detail) or 0.0 for detail in details), default=None)
    artifacts: list[Any] = []
    if report_path is not None:
        artifacts.append(_agent_artifact("report", report_path, "shareable finding report"))
    if regression_path is not None:
        artifacts.append(
            _agent_artifact("regression", regression_path, "generated pytest regressions")
        )
    return _build_agent_envelope_from_report(
        report,
        status=("blocked" if skipped and not results else ("findings" if details else "ok")),
        confidence=confidence,
        confidence_basis=(
            f"checked {len(results)} function(s)",
            f"{suspicious_count} suspicious finding(s)",
            "property confidence is derived from holds/total examples",
        ),
        blocking_reason=(
            "all candidate functions were skipped" if skipped and not results else None
        ),
        artifacts=artifacts,
        raw_details={
            "report": report,
            "results": [
                {
                    "function": name,
                    "result": result,
                }
                for name, result in results
            ],
            "checked_functions": [name for name, _ in results],
            "skipped_functions": skipped,
            "include_scan_hint": include_scan_hint,
        },
        suggested_test_file=(
            str(regression_path)
            if regression_path is not None
            else (_DEFAULT_REGRESSION_PATH if details else None)
        ),
    )


def _audit_detail_items(result: Any) -> list[dict[str, Any]]:
    """Normalize one ModuleAudit into finding-style detail items."""
    details: list[dict[str, Any]] = []
    score_fraction = result.mutation_score_fraction
    if score_fraction is not None and score_fraction < 1.0:
        details.append(
            {
                "kind": "mutation_gap",
                "category": "test_strength_gap",
                "summary": f"mutation score {result.mutation_score}",
                "confidence": score_fraction,
                "module": result.module,
                "qualname": result.module,
                "details": {"validation_mode": result.validation_mode},
            }
        )
    for gap in result.mutation_gaps:
        details.append(
            {
                "kind": "mutation",
                "category": "test_strength_gap",
                "summary": f"{gap['location']} {gap['description']}",
                "module": result.module,
                "qualname": gap["target"],
                "details": {
                    "source_line": gap.get("source_line"),
                    "remediation": gap.get("remediation"),
                },
            }
        )
    for function_name in result.gap_functions:
        details.append(
            {
                "kind": "fixture_gap",
                "category": "test_strength_gap",
                "summary": f"{function_name} needs fixtures before ordeal can verify it",
                "module": result.module,
                "function": function_name,
                "qualname": f"{result.module}.{function_name}",
            }
        )
    for item in result.weakest_tests:
        details.append(
            {
                "kind": "warning",
                "category": "test_strength_gap",
                "summary": f"{item['test']} only killed {item['kills']} mutant(s)",
                "module": result.module,
                "qualname": result.module,
            }
        )
    for suggestion in result.suggestions:
        details.append(
            {
                "kind": "coverage_gap",
                "category": "test_strength_gap",
                "summary": suggestion,
                "module": result.module,
                "qualname": result.module,
            }
        )
    for warning in result.warnings:
        details.append(
            {
                "kind": "warning",
                "category": "verification_warning",
                "summary": warning,
                "module": result.module,
                "qualname": result.module,
            }
        )
    return details


def _build_audit_agent_envelope(
    results: Sequence[Any],
    *,
    saved_generated_path: Path | None = None,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal audit`."""
    from ordeal.audit import _generated_test_path, _module_audit_to_dict

    details = [detail for result in results for detail in _audit_detail_items(result)]
    modules = [result.module for result in results]
    suggested_commands = []
    for module in modules:
        suggested_commands.extend(
            [
                f"ordeal audit {module} --show-generated",
                f"ordeal mine {module} -n 200",
                f"ordeal mutate {module}",
            ]
        )
    seen: set[str] = set()
    deduped_commands = [cmd for cmd in suggested_commands if not (cmd in seen or seen.add(cmd))]
    report = {
        "target": ", ".join(modules),
        "tool": "audit",
        "status": "findings found" if details else "no major gaps found",
        "summary": [
            f"Audited: {len(results)} module(s)",
            f"Findings: {len(details)}",
            (
                "Coverage preserved:"
                f" {sum(1 for result in results if result.coverage_preserved)}"
                f"/{len(results)}"
            ),
        ],
        "details": details,
        "suggested_commands": deduped_commands,
    }
    verified_measurements = sum(
        int(result.current_coverage.status.value == "verified")
        + int(result.migrated_coverage.status.value == "verified")
        for result in results
    )
    total_measurements = max(len(results) * 2, 1)
    mutation_fractions = [
        result.mutation_score_fraction
        for result in results
        if result.mutation_score_fraction is not None
    ]
    total_functions = sum(max(result.total_functions, 0) for result in results)
    covered_functions = sum(
        max(result.total_functions - len(result.gap_functions), 0) for result in results
    )
    evidence = {
        "search_depth": {"modules": len(results), "coverage_measurements": total_measurements},
        "replayability": verified_measurements / total_measurements,
        "mutation_strength": (
            sum(mutation_fractions) / len(mutation_fractions) if mutation_fractions else None
        ),
        "fixture_completeness": (
            covered_functions / total_functions if total_functions > 0 else 1.0
        ),
    }
    mutation_strength_text = (
        f"{evidence['mutation_strength']:.0%}"
        if evidence["mutation_strength"] is not None
        else "n/a"
    )
    report["summary"].append(
        "Evidence:"
        f" search depth={evidence['search_depth']['modules']} modules/"
        f"{evidence['search_depth']['coverage_measurements']} measurements,"
        f" replayability={evidence['replayability']:.0%},"
        f" mutation strength={mutation_strength_text},"
        f" fixture completeness={evidence['fixture_completeness']:.0%}"
    )
    report["extra_sections"] = [
        (
            "Evidence Dimensions",
            [
                (
                    "search depth: "
                    f"{evidence['search_depth']['modules']} modules, "
                    f"{evidence['search_depth']['coverage_measurements']} "
                    "verified-or-attempted measurements"
                ),
                f"replayability: {evidence['replayability']:.0%}",
                (
                    "mutation strength: "
                    + (
                        mutation_strength_text
                        if mutation_strength_text != "n/a"
                        else "not measured yet"
                    )
                ),
                f"fixture completeness: {evidence['fixture_completeness']:.0%}",
            ],
        )
    ]
    artifacts: list[Any] = []
    if saved_generated_path is not None:
        artifacts.append(
            _agent_artifact("generated-test", saved_generated_path, "saved ordeal-generated test")
        )
    else:
        for result in results:
            generated_path = _generated_test_path(result.module)
            if generated_path.exists():
                artifacts.append(
                    _agent_artifact(
                        "generated-test",
                        generated_path,
                        "ordeal-generated migrated test",
                        module=result.module,
                    )
                )
    return _build_agent_envelope_from_report(
        report,
        status="findings" if details else "ok",
        confidence=verified_measurements / total_measurements,
        confidence_basis=(
            (
                "search depth: "
                f"{evidence['search_depth']['modules']} modules, "
                f"{evidence['search_depth']['coverage_measurements']} measurements"
            ),
            f"replayability: {evidence['replayability']:.0%}",
            (
                "mutation strength: "
                + (mutation_strength_text if mutation_strength_text != "n/a" else "not measured")
            ),
            f"fixture completeness: {evidence['fixture_completeness']:.0%}",
        ),
        artifacts=artifacts,
        raw_details={
            "report": report,
            "modules": [_module_audit_to_dict(result) for result in results],
            "evidence_dimensions": evidence,
        },
        suggested_test_file=(
            str(saved_generated_path) if saved_generated_path is not None else None
        ),
    )


def _mutant_to_detail(target: str, mutant: Any) -> dict[str, Any]:
    """Normalize a surviving mutant into a finding-style detail item."""
    return {
        "kind": "mutation",
        "category": "test_strength_gap",
        "summary": f"{mutant.location} {mutant.description}",
        "module": target.rsplit(".", 1)[0] if "." in target else target,
        "qualname": target,
        "location": mutant.location,
        "details": {
            "operator": mutant.operator,
            "source_line": mutant.source_line,
            "remediation": mutant.remediation,
        },
    }


def _build_mutate_agent_envelope(
    *,
    targets: Sequence[str],
    results: Sequence[tuple[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
    threshold: float,
    stubs_path: Path | None = None,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal mutate`."""
    details = []
    for target, result in results:
        details.extend(_mutant_to_detail(target, mutant) for mutant in result.survived)
    for blocker in blockers:
        details.append(
            {
                "kind": "no_tests",
                "summary": str(blocker["summary"]),
                "qualname": str(blocker["target"]),
                "details": {
                    "suggested_test_file": blocker.get("suggested_test_file"),
                    "starter_tests": blocker.get("starter_tests"),
                },
            }
        )
    suggested_commands: list[str] = []
    for blocker in blockers:
        suggested_commands.append(f"ordeal init {blocker['target']}")
    for target, _ in results:
        suggested_commands.append(f"ordeal mutate {target}")
    if results:
        cmd = f"ordeal mutate {results[0][0]} --generate-stubs {_DEFAULT_REGRESSION_PATH}"
        suggested_commands.append(cmd)
    seen: set[str] = set()
    deduped_commands = [cmd for cmd in suggested_commands if not (cmd in seen or seen.add(cmd))]
    total_mutants = sum(result.total for _, result in results)
    total_killed = sum(result.killed for _, result in results)
    overall = total_killed / total_mutants if total_mutants > 0 else (None if blockers else 1.0)
    status = "ok"
    if blockers and not results:
        status = "blocked"
    elif details:
        status = "findings"
    report = {
        "target": ", ".join(targets),
        "tool": "mutate",
        "status": "findings found" if details else "all mutants killed",
        "summary": [
            f"Targets: {len(targets)}",
            f"Mutants tested: {total_mutants}",
            f"Survivors: {sum(len(result.survived) for _, result in results)}",
        ],
        "details": details,
        "suggested_commands": deduped_commands,
    }
    blocking_reason = str(blockers[0]["summary"]) if blockers and not results else None
    artifacts = (
        [_agent_artifact("regression", stubs_path, "generated mutation test stubs")]
        if stubs_path is not None and stubs_path.exists()
        else []
    )
    recommended = _recommended_action_for_report(report)
    if blockers and not results:
        target = blockers[0]["target"]
        recommended = (
            f"Bootstrap tests with `ordeal init {target}` or save the provided starter scaffold."
        )
    return _build_agent_envelope_from_report(
        {**report, "recommended_action": recommended},
        status=status,
        confidence=overall,
        confidence_basis=(
            f"{total_mutants} mutant(s) tested" if total_mutants else "no mutants tested",
            f"threshold={threshold:.0%}" if threshold > 0 else "no threshold configured",
        ),
        blocking_reason=blocking_reason,
        artifacts=artifacts,
        raw_details={
            "targets": [
                {
                    "target": target,
                    "score": result.score,
                    "killed": result.killed,
                    "total": result.total,
                    "diagnostics": result.diagnostics,
                    "survived_mutants": result.survived,
                    "timings": result.timings,
                }
                for target, result in results
            ],
            "blockers": list(blockers),
            "overall_score": overall,
            "threshold": threshold,
        },
        suggested_test_file=(
            str(stubs_path)
            if stubs_path is not None
            else (
                str(blockers[0].get("suggested_test_file"))
                if blockers
                else (_DEFAULT_REGRESSION_PATH if details else None)
            )
        ),
    )


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


def _write_scan_report(state: Any, path_str: str) -> Path:
    """Write a Markdown report for `ordeal scan`."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_scan_report_markdown(state), encoding="utf-8")
    _stderr(f"Scan report saved: {path}\n")
    return path


def _write_scan_bundle(
    state: Any,
    *,
    path_str: str,
    report_path: Path,
    regression_path: Path | None,
) -> tuple[Path, dict[str, Any]]:
    """Write a machine-readable JSON finding bundle for `ordeal scan`."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = _build_scan_bundle(
        state,
        report_path=report_path,
        regression_path=regression_path,
    )
    bundle["artifacts"]["bundle"] = _display_path(path)
    path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
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


def _print_scan_artifact_workflow(
    *,
    module: str,
    report_path: Path,
    bundle_path: Path,
    finding_ids: list[str],
    regression_path: Path | None,
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
    print(f"  index: {_display_path(index_path)}")
    print("available:")
    if len(finding_ids) == 1 and regression_path is not None:
        verify_cmd = _shell_command("uv", "run", "ordeal", "verify", finding_ids[0])
        print(f"  verify: {verify_cmd}")
    if regression_path is not None:
        run_cmd = _shell_command("uv", "run", "pytest", _display_path(regression_path), "-q")
        print(f"  pytest: {run_cmd}")
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
    fallback_workspace = str(index_path.parent.parent.parent)

    for entry in reversed(entries):
        artifacts = entry.get("artifacts") or {}
        bundle_path = _resolve_artifact_path(
            artifacts.get("bundle"),
            workspace=entry.get("workspace") or fallback_workspace,
        )
        if bundle_path is None or not bundle_path.exists():
            continue
        bundle = _read_json_file(bundle_path)
        for finding in bundle.get("findings", []):
            if finding.get("finding_id") == finding_id:
                return bundle_path, bundle, finding
    return None


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


def _cmd_verify(args: argparse.Namespace) -> int:
    """Re-run the saved regression for one finding ID."""
    import subprocess

    index_path = Path(args.index)
    try:
        located = _locate_saved_finding(args.finding_id, index_path=index_path)
    except json.JSONDecodeError as exc:
        _stderr(f"Artifact data is not valid JSON: {exc}\n")
        return 2
    if located is None:
        _stderr(f"Finding not found in artifact index: {args.finding_id}\n")
        return 2

    bundle_path, bundle, finding = located
    command = _verification_command(bundle, finding)
    if command is None:
        _stderr(
            f"No runnable regression is recorded for {args.finding_id}. "
            "Re-run `ordeal scan --save-artifacts` first.\n"
        )
        return 2

    run_args, display_command = command
    workspace = bundle.get("workspace")
    proc = subprocess.run(
        run_args,
        cwd=workspace,
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
    }
    _write_json_file(bundle_path, bundle)

    _append_index_entry(
        index_path,
        {
            "kind": "verification",
            "created_at": checked_at,
            "module": bundle.get("target"),
            "workspace": workspace,
            "finding_id": args.finding_id,
            "status": verification_status,
            "qualname": finding.get("qualname"),
            "exit_code": proc.returncode,
            "artifacts": dict(bundle.get("artifacts", {})),
            "commands": {
                "verify": display_command,
            },
        },
    )

    print(f"verify: {args.finding_id}")
    print(f"  target: {finding.get('qualname', bundle.get('target', '?'))}")
    print(f"  status: {verification_status}")
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
    from ordeal.state import explore as _explore_fn

    scan_desc = (_explore_fn.__doc__ or "").strip().split("\n\n")[0]
    return (
        f"{scan_desc}\n\n"
        "Scan is exploratory first: prioritize replayable, semantically plausible findings.\n"
        "For stronger signals on a mature codebase, prefer `ordeal audit` and `ordeal mutate`.\n"
        f"Use --save-artifacts to save both {_default_scan_report_path('mymod')} and"
        f" {_default_scan_bundle_path('mymod')} + {_DEFAULT_REGRESSION_PATH},"
        f" then update {_default_artifact_index_path()}.\n"
        "When one finding is saved, the workflow prints an exact"
        " `ordeal verify <finding-id>` follow-up command.\n"
        "Use --report-file report.md to save a shareable Markdown bug report.\n"
        f"Use --write-regression or --write-regression PATH to save runnable pytest"
        f" regressions (default: {_DEFAULT_REGRESSION_PATH})."
    )


def _verify_command_description() -> str:
    """Return the long-form `ordeal verify` help description."""
    return (
        "Re-run a saved regression from `.ordeal/findings/index.json`.\n\n"
        "Use the stable `finding_id` from a JSON bug bundle or index entry.\n"
        "Verification updates the bundle status and appends a verification event"
        " to the artifact index."
    )


def _audit_command_description() -> str:
    """Return the long-form `ordeal audit` help description."""
    return (
        "Compare your current tests with ordeal-generated tests.\n\n"
        "Validation modes:\n"
        "  fast  replay mined inputs against mutants (default, faster)\n"
        "  deep  replay mined inputs, then re-mine mutants for extra search depth"
    )


def _mine_command_description() -> str:
    """Return the long-form `ordeal mine` help description."""
    return (
        "Discover properties of a function or module.\n\n"
        "Use --report-file report.md to save a shareable Markdown finding report.\n"
        f"Use --write-regression or --write-regression PATH to save runnable pytest"
        f" regressions (default: {_DEFAULT_REGRESSION_PATH})."
    )


def _init_command_description() -> str:
    """Return the long-form `ordeal init` help description."""
    return (
        "Bootstrap starter tests and ordeal.toml. By default this writes only the "
        "starter files, validates them, and prints a lightweight read-only scan "
        "summary. Use --install-skill and --close-gaps to opt into extra writes."
    )


def _command_specs() -> tuple[CommandSpec, ...]:
    """Return the declarative registry for CLI commands."""
    return (
        CommandSpec(
            name="catalog",
            handler=_cmd_catalog,
            help="Show all capabilities — faults, mining, mutations, exploration, ...",
            arguments=(
                _arg("--detail", action="store_true", help="Show full signatures and docstrings"),
            ),
        ),
        CommandSpec(
            name="check",
            handler=_cmd_check,
            help="Verify a specific property on a function (mine + assert in one step)",
            arguments=(
                _arg("target", help="Dotted path: mymod.func"),
                _arg(
                    "--property",
                    "-p",
                    default=None,
                    help="Property to verify. Omit to check all standard contracts.",
                ),
                _arg(
                    "--max-examples",
                    "-n",
                    type=int,
                    default=200,
                    help="Examples to test (default: 200)",
                ),
            ),
        ),
        CommandSpec(
            name="scan",
            handler=_cmd_scan,
            help="Explore a module and optionally write reports or pytest regressions",
            description=_scan_command_description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            arguments=(
                _arg("target", help="Module path (e.g. myapp.scoring)"),
                _arg(
                    "--seed",
                    type=int,
                    default=42,
                    help="RNG seed for reproducibility (default: 42)",
                ),
                _arg(
                    "--max-examples",
                    "-n",
                    type=int,
                    default=50,
                    help="Examples per function (default: 50)",
                ),
                _arg(
                    "--workers",
                    "-w",
                    type=int,
                    default=1,
                    help="Parallel workers for mutation testing",
                ),
                _arg(
                    "--time-limit",
                    "-t",
                    type=float,
                    default=None,
                    help="Time budget in seconds",
                ),
                _arg("--json", action="store_true", help="Output JSON instead of text"),
                _arg(
                    "--save-artifacts",
                    action="store_true",
                    help=(
                        "When findings exist, write the default Markdown dossier, JSON bundle,"
                        f" and regression file ({_default_scan_report_path('mymod')},"
                        f" {_default_scan_bundle_path('mymod')}, {_DEFAULT_REGRESSION_PATH})"
                        f" and update {_default_artifact_index_path()}"
                    ),
                ),
                _arg(
                    "--report-file",
                    type=str,
                    default=None,
                    metavar="PATH",
                    help="Write a shareable Markdown finding report to PATH",
                ),
                _arg(
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
                ),
                _arg(
                    "--include-private",
                    action="store_true",
                    help="Include _private functions (many codebases have logic there)",
                ),
            ),
        ),
        CommandSpec(
            name="verify",
            handler=_cmd_verify,
            help="Re-run the saved regression for one finding ID",
            description=_verify_command_description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            arguments=(
                _arg("finding_id", help="Stable finding ID (e.g. fnd_dcb0fc0808d3)"),
                _arg(
                    "--index",
                    default=_default_artifact_index_path(),
                    metavar="PATH",
                    help=f"Artifact index path (default: {_default_artifact_index_path()})",
                ),
            ),
        ),
        CommandSpec(
            name="explore",
            handler=_cmd_explore,
            help="Coverage-guided state exploration (reads ordeal.toml)",
            arguments=(
                _arg(
                    "--config",
                    "-c",
                    default="ordeal.toml",
                    help="Config file (default: ordeal.toml)",
                ),
                _arg("--seed", type=int, help="Override RNG seed"),
                _arg("--max-time", type=float, help="Override max_time (seconds)"),
                _arg("--verbose", "-v", action="store_true", help="Live progress"),
                _arg("--no-shrink", action="store_true", help="Skip shrinking"),
                _arg("--no-seeds", action="store_true", help="Skip seed corpus replay"),
                _arg(
                    "--workers",
                    "-w",
                    type=int,
                    help="Parallel worker processes (default: 1)",
                ),
                _arg(
                    "--generate-tests",
                    type=str,
                    default=None,
                    metavar="PATH",
                    help=(
                        "Generate pytest tests from exploration traces"
                        " (e.g. tests/test_generated.py)"
                    ),
                ),
                _arg(
                    "--resume",
                    type=str,
                    default=None,
                    metavar="PATH",
                    help="Resume from a saved state file (e.g. .ordeal/state.pkl)",
                ),
                _arg(
                    "--save-state",
                    type=str,
                    default=None,
                    metavar="PATH",
                    help="Save exploration state on completion (e.g. .ordeal/state.pkl)",
                ),
            ),
        ),
        CommandSpec(
            name="replay",
            handler=_cmd_replay,
            help="Replay a saved trace",
            arguments=(
                _arg("trace_file", help="Path to trace JSON file"),
                _arg("--shrink", action="store_true", help="Shrink the trace"),
                _arg(
                    "--ablate",
                    action="store_true",
                    help="Ablate faults to find necessary ones",
                ),
                _arg("--output", "-o", help="Save shrunk trace to this path"),
                _arg("--json", action="store_true", help="Output agent-facing JSON"),
            ),
        ),
        CommandSpec(
            name="seeds",
            handler=_cmd_seeds,
            help="List or manage the persistent seed corpus",
            arguments=(
                _arg(
                    "--dir",
                    default=".ordeal/seeds",
                    help="Seed corpus directory (default: .ordeal/seeds)",
                ),
                _arg(
                    "--prune-fixed",
                    action="store_true",
                    help="Remove seeds that no longer reproduce",
                ),
            ),
        ),
        CommandSpec(
            name="audit",
            handler=_cmd_audit,
            help="Audit test coverage vs ordeal migration",
            description=_audit_command_description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            arguments=(
                _arg("modules", nargs="+", help="Module paths to audit"),
                _arg(
                    "--test-dir",
                    "-t",
                    default="tests",
                    help="Test directory (default: tests)",
                ),
                _arg(
                    "--max-examples",
                    type=int,
                    default=20,
                    help="Examples per function (default: 20)",
                ),
                _arg(
                    "--workers",
                    type=int,
                    default=1,
                    help="Parallel workers for mutation validation (default: 1)",
                ),
                _arg(
                    "--validation-mode",
                    choices=("fast", "deep"),
                    default="fast",
                    help="Validation mode: fast replay (default) or deep re-mine",
                ),
                _arg(
                    "--show-generated",
                    action="store_true",
                    help="Print the generated test file for inspection/debugging",
                ),
                _arg(
                    "--save-generated",
                    type=str,
                    default=None,
                    help="Save generated test file to this path",
                ),
                _arg("--json", action="store_true", help="Output agent-facing JSON"),
            ),
        ),
        CommandSpec(
            name="mine",
            handler=_cmd_mine,
            help="Discover properties and optionally write reports or pytest regressions",
            description=_mine_command_description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            arguments=(
                _arg("target", help="Dotted path: mymod.func or mymod"),
                _arg(
                    "--max-examples",
                    "-n",
                    type=int,
                    default=500,
                    help="Examples to sample (default: 500)",
                ),
                _arg(
                    "--verbose",
                    "-v",
                    action="store_true",
                    help="Show n/a properties and extra detail",
                ),
                _arg(
                    "--include-private",
                    action="store_true",
                    help="Include _private functions (many codebases have logic there)",
                ),
                _arg(
                    "--report-file",
                    type=str,
                    default=None,
                    metavar="PATH",
                    help="Write a shareable Markdown finding report to PATH",
                ),
                _arg(
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
                ),
                _arg("--json", action="store_true", help="Output agent-facing JSON"),
            ),
        ),
        CommandSpec(
            name="mine-pair",
            handler=_cmd_mine_pair,
            help="Discover relational properties between two functions",
            arguments=(
                _arg("f", help="First function: mymod.func_a"),
                _arg("g", help="Second function: mymod.func_b"),
                _arg(
                    "--max-examples",
                    "-n",
                    type=int,
                    default=200,
                    help="Examples to sample (default: 200)",
                ),
            ),
        ),
        CommandSpec(
            name="benchmark",
            handler=_cmd_benchmark,
            help="Measure scaling, mutation latency, or a checked-in perf/quality contract",
            defaults={"filter_equivalent": True},
            arguments=(
                _arg(
                    "--config",
                    "-c",
                    default="ordeal.toml",
                    help="Config file (default: ordeal.toml)",
                ),
                _arg(
                    "--max-workers",
                    type=int,
                    default=None,
                    help="Max workers to test (default: CPU count)",
                ),
                _arg(
                    "--time",
                    type=float,
                    default=10.0,
                    help="Seconds per trial (default: 10)",
                ),
                _arg(
                    "--metric",
                    choices=["runs", "steps", "edges"],
                    default="runs",
                    help="Throughput metric to fit (default: runs)",
                ),
                _arg(
                    "--perf-contract",
                    default=None,
                    help="Run a perf/quality contract TOML instead of scaling analysis",
                ),
                _arg(
                    "--check",
                    action="store_true",
                    help=(
                        "Return exit code 1 when a perf-contract case exceeds a time"
                        " or score-gap budget"
                    ),
                ),
                _arg(
                    "--output-json",
                    default=None,
                    metavar="PATH",
                    help="Write perf/quality contract results as JSON to PATH",
                ),
                _arg(
                    "--json",
                    action="store_true",
                    help="Print perf/quality contract results as JSON to stdout",
                ),
                _arg(
                    "--tier",
                    default=None,
                    choices=["pr", "nightly"],
                    help="Only run perf-contract cases matching this tier (default: all)",
                ),
                _arg(
                    "--mutate",
                    dest="mutate_targets",
                    action="append",
                    default=[],
                    help="Benchmark mutation latency for this target (repeatable)",
                ),
                _arg(
                    "--repeat",
                    type=int,
                    default=5,
                    help="Fresh subprocess runs per mutation target (default: 5)",
                ),
                _arg(
                    "--workers",
                    type=int,
                    default=1,
                    help="Workers to use for mutation benchmarks (default: 1)",
                ),
                _arg(
                    "--preset",
                    choices=["essential", "standard", "thorough"],
                    default="standard",
                    help="Mutation preset for mutation benchmarks (default: standard)",
                ),
                _arg(
                    "--test-filter",
                    default=None,
                    help="Pytest -k filter for mutation benchmarks",
                ),
                _arg(
                    "--no-filter-equivalent",
                    dest="filter_equivalent",
                    action="store_false",
                    help="Disable equivalence filtering during mutation benchmarks",
                ),
            ),
        ),
        CommandSpec(
            name="skill",
            handler=_cmd_skill,
            help="Install ordeal skill for AI coding agents",
            arguments=(
                _arg(
                    "--dry-run",
                    action="store_true",
                    help="Show what would be written without writing",
                ),
            ),
        ),
        CommandSpec(
            name="init",
            handler=_cmd_init,
            help="Bootstrap test files for untested modules",
            description=_init_command_description,
            arguments=(
                _arg(
                    "target",
                    nargs="?",
                    default=None,
                    help="Package path (e.g. myapp); auto-detects if omitted",
                ),
                _arg(
                    "--output-dir",
                    "-o",
                    default="tests",
                    help="Directory to write test files (default: tests)",
                ),
                _arg(
                    "--dry-run",
                    action="store_true",
                    help=(
                        "Preview without side effects — no files written, no functions executed. "
                        "Generates stub tests from signatures only."
                    ),
                ),
                _arg(
                    "--ci",
                    action="store_true",
                    help="Generate a GitHub Actions workflow (.github/workflows/<name>.yml)",
                ),
                _arg(
                    "--ci-name",
                    default="ordeal",
                    metavar="NAME",
                    help="Workflow filename (default: ordeal → .github/workflows/ordeal.yml)",
                ),
                _arg(
                    "--install-skill",
                    action="store_true",
                    help="Also install the bundled AI-agent skill into .claude/skills/ordeal/",
                ),
                _arg(
                    "--close-gaps",
                    action="store_true",
                    help="Write draft audit stub files for surviving mutation gaps",
                ),
            ),
        ),
        CommandSpec(
            name="mutate",
            handler=_cmd_mutate,
            help="Test whether your tests catch code changes",
            arguments=(
                _arg(
                    "targets",
                    nargs="*",
                    help="Dotted paths: myapp.scoring.compute or myapp.scoring",
                ),
                _arg(
                    "--config",
                    "-c",
                    default=None,
                    help="Config file with [mutations] section (used when no targets given)",
                ),
                _arg(
                    "--preset",
                    "-p",
                    choices=["essential", "standard", "thorough"],
                    default=None,
                    help="Operator preset (default: standard)",
                ),
                _arg(
                    "--workers",
                    "-w",
                    type=int,
                    default=1,
                    help="Parallel workers (default: 1)",
                ),
                _arg(
                    "--threshold",
                    "-t",
                    type=float,
                    default=0.0,
                    help="Minimum mutation score; exit 1 if below (e.g. 0.8 for 80%%)",
                ),
                _arg(
                    "--no-filter",
                    action="store_true",
                    help="Disable equivalent mutant filtering",
                ),
                _arg(
                    "--equivalence-samples",
                    type=int,
                    default=10,
                    help="Samples for equivalence filtering (default: 10)",
                ),
                _arg(
                    "--test-filter",
                    "-k",
                    type=str,
                    default=None,
                    metavar="EXPR",
                    help=(
                        "Pytest -k expression to select tests"
                        " (avoids running full suite per mutant)"
                    ),
                ),
                _arg(
                    "--mutant-timeout",
                    type=float,
                    default=None,
                    metavar="SECS",
                    help="Timeout in seconds for mutant generation (skip hangs)",
                ),
                _arg(
                    "--disk-mutation",
                    action="store_true",
                    default=None,
                    help=(
                        "Write mutations to disk so subprocesses (Ray, multiprocessing) see them. "
                        "Auto-detected when omitted."
                    ),
                ),
                _arg(
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
                ),
                _arg(
                    "--generate-stubs",
                    type=str,
                    default=None,
                    metavar="PATH",
                    help="Write test stubs for surviving mutants to PATH",
                ),
                _arg("--json", action="store_true", help="Output agent-facing JSON"),
            ),
        ),
    )


def _resolve_command_description(spec: CommandSpec) -> str | None:
    """Resolve a command description from a static string or callable."""
    description = spec.description
    if callable(description):
        return description()
    return description


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser for ``ordeal``."""
    parser = argparse.ArgumentParser(
        prog="ordeal",
        description=(
            "Ordeal — discovers what's true about your code.\n\n"
            "Use `ordeal scan <module>` for the fastest end-to-end path.\n"
            "Run `ordeal <command> --help` for command-specific options.\n"
            "Use `ordeal catalog` or `from ordeal import catalog; catalog()`"
            " for runtime discovery."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    for spec in _command_specs():
        add_parser_kwargs: dict[str, Any] = {"help": spec.help}
        description = _resolve_command_description(spec)
        if description is not None:
            add_parser_kwargs["description"] = description
        if spec.formatter_class is not None:
            add_parser_kwargs["formatter_class"] = spec.formatter_class
        subparser = sub.add_parser(spec.name, **add_parser_kwargs)
        for argument in spec.arguments:
            subparser.add_argument(*argument.tokens, **argument.kwargs)
        subparser.set_defaults(_handler=spec.handler, **spec.defaults)

    return parser


def _catalog_argument(action: argparse.Action) -> dict[str, Any]:
    """Convert one argparse action into a structured CLI-argument entry."""
    positional = not bool(action.option_strings)
    nargs = action.nargs
    required = bool(getattr(action, "required", False))
    if positional:
        required = nargs not in ("?", "*")

    kind = "positional" if positional else "option"
    if isinstance(
        action,
        (
            argparse._StoreTrueAction,
            argparse._StoreFalseAction,
            argparse._CountAction,
        ),
    ):
        kind = "flag"

    accepts_value = not isinstance(
        action,
        (
            argparse._StoreTrueAction,
            argparse._StoreFalseAction,
            argparse._CountAction,
        ),
    )
    repeatable = isinstance(action, argparse._AppendAction)
    variadic = nargs in ("*", "+")
    value_optional = nargs == "?"

    value_type: str | None
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        value_type = "bool"
    elif isinstance(action, argparse._CountAction):
        value_type = "int"
    elif action.type is not None:
        value_type = getattr(action.type, "__name__", str(action.type))
    elif action.choices:
        sample = next(iter(action.choices), None)
        value_type = type(sample).__name__ if sample is not None else "str"
    elif accepts_value:
        value_type = "str"
    else:
        value_type = None

    semantics = "flag"
    if isinstance(action, argparse._CountAction):
        semantics = "counter"
    elif repeatable:
        semantics = "repeatable"
    elif variadic:
        semantics = "variadic"
    elif value_optional:
        semantics = "optional_value"
    elif accepts_value:
        semantics = "value"

    entry: dict[str, Any] = {
        "name": action.dest,
        "schema_version": CLI_CATALOG_SCHEMA_VERSION,
        "kind": kind,
        "required": required,
        "help": action.help or "",
        "accepts_value": accepts_value,
        "repeatable": repeatable,
        "variadic": variadic,
        "value_optional": value_optional,
        "semantics": semantics,
    }
    if action.option_strings:
        entry["flags"] = list(action.option_strings)
    if nargs is not None:
        entry["nargs"] = nargs
    if action.metavar is not None:
        entry["metavar"] = action.metavar
    if action.default not in (None, argparse.SUPPRESS):
        entry["default"] = action.default
    if action.choices is not None and not isinstance(action.choices, dict):
        entry["choices"] = list(action.choices)
    if value_type is not None:
        entry["value_type"] = value_type
    return entry


def command_catalog() -> list[dict[str, Any]]:
    """Return a structured catalog of CLI commands derived from argparse."""
    parser = _build_parser()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        choice_help = {choice.dest: choice.help or "" for choice in action._choices_actions}
        entries: list[dict[str, Any]] = []
        for name, subparser in sorted(action.choices.items()):
            arguments = [
                _catalog_argument(sub_action)
                for sub_action in subparser._actions
                if not isinstance(sub_action, (argparse._HelpAction, argparse._SubParsersAction))
            ]
            usage = subparser.format_usage().strip()
            if usage.startswith("usage: "):
                usage = usage.removeprefix("usage: ")
            entries.append(
                {
                    "name": name,
                    "schema_version": CLI_CATALOG_SCHEMA_VERSION,
                    "qualname": f"ordeal.cli.{name}",
                    "doc": choice_help.get(name, ""),
                    "usage": usage,
                    "description": subparser.description or "",
                    "arguments": arguments,
                }
            )
        return entries
    return []


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``ordeal``."""
    # Add CWD to sys.path so imports resolve the same way as pytest/python -m.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
