from __future__ import annotations


# ruff: noqa
def _diff_spec() -> CommandSpec:
    return CommandSpec(
        name="diff",
        handler=_cmd_diff,
        help="Compare a target across isolated Git revisions",
        description=_diff_command_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        arguments=(
            _arg(
                "target", nargs="?", help="Callable or module target (omit to use [diff].target)"
            ),
            _arg(
                "--config",
                "-c",
                default=None,
                help="Config file with [diff] defaults (default: ordeal.toml if present)",
            ),
            _arg(
                "--base-ref",
                default=None,
                help="Baseline Git ref (default: remote default branch/main/HEAD^)",
            ),
            _arg("--candidate-ref", default=None, help="Candidate Git ref (default: HEAD)"),
            _arg(
                "--max-examples",
                "-n",
                type=int,
                default=None,
                help="Examples per function (default: 100, or [diff].max_examples)",
            ),
            _arg(
                "--seed",
                type=int,
                default=None,
                help="Deterministic input-generation seed (default: 42)",
            ),
            _arg(
                "--rtol", type=float, default=None, help="Relative tolerance for numeric outputs"
            ),
            _arg(
                "--atol", type=float, default=None, help="Absolute tolerance for numeric outputs"
            ),
            _arg(
                "--include-private",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Include _private functions in module targets",
            ),
            _arg(
                "--fixture-registry",
                dest="fixture_registries",
                action="append",
                default=None,
                metavar="MODULE",
                help="Repeat to import a project fixture registry in the base worker",
            ),
            _arg(
                "--replay-attempts",
                type=int,
                default=None,
                metavar="N",
                help="Immediate same-input replays per mismatch (default: 2)",
            ),
            _arg(
                "--sequence-file",
                default=None,
                metavar="PATH",
                help="Replay a JSON operation/fault sequence against TARGET as a zero-argument system factory",
            ),
            _arg(
                "--save-artifacts",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Save JSON and Markdown evidence under .ordeal/diff",
            ),
            _arg(
                "--artifact-dir",
                default=None,
                metavar="PATH",
                help="Artifact directory (default: .ordeal/diff)",
            ),
            _arg(
                "--write-regression",
                nargs="?",
                const="tests/test_ordeal_diff_regression.py",
                default=None,
                metavar="PATH",
                help="Write a pinned base-to-current pytest regression and register it",
            ),
            _arg(
                "--manifest",
                default=_DEFAULT_REGRESSION_MANIFEST,
                metavar="PATH",
                help=f"Shared verify --ci manifest (default: {_DEFAULT_REGRESSION_MANIFEST})",
            ),
            _arg("--json", action="store_true", help="Output machine-readable JSON"),
        ),
    )


def _migrate_spec() -> CommandSpec:
    return CommandSpec(
        name="migrate",
        handler=_cmd_migrate,
        help="Replace a module without copying old or new bugs",
        description=_migrate_command_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        arguments=(
            _arg("base", help="Old importable module used as the behavioral reference"),
            _arg("candidate", help="Replacement importable module to validate"),
            _arg(
                "--config",
                "-c",
                default=None,
                help="Config file containing explicit domain rules in [[contracts]]",
            ),
            _arg(
                "--intended-change",
                dest="intended_changes",
                action="append",
                default=None,
                metavar="SELECTOR",
                help="Function or kind:function explicitly intended to change (repeatable)",
            ),
            _arg("--test-dir", default="tests", help="Base test directory (default: tests)"),
            _arg(
                "--audit-examples",
                type=int,
                default=20,
                help="Examples per callable during base audit (default: 20)",
            ),
            _arg(
                "--mine-examples",
                type=int,
                default=200,
                help="Examples per callable during candidate mining (default: 200)",
            ),
            _arg(
                "--diff-examples",
                type=int,
                default=100,
                help="Examples per shared callable diff (default: 100)",
            ),
            _arg(
                "--scan-examples",
                type=int,
                default=50,
                help="Examples per callable in the candidate-only scan (default: 50)",
            ),
            _arg(
                "--preset",
                choices=["essential", "standard", "thorough"],
                default="standard",
                help="Mutation operator preset (default: standard)",
            ),
            _arg("--workers", type=int, default=1, help="Mutation workers (default: 1)"),
            _arg(
                "--threshold",
                type=float,
                default=1.0,
                help="Required mutation score from 0 to 1 (default: 1.0)",
            ),
            _arg(
                "--evidence-path",
                default=None,
                metavar="PATH",
                help="Migration JSON path (default: .ordeal/migrations/<pair>.json)",
            ),
            _arg(
                "--regression-path",
                default=None,
                metavar="PATH",
                help="Generated pytest path (default: tests/test_ordeal_migration_<candidate>.py)",
            ),
            _arg(
                "--manifest",
                default=_DEFAULT_REGRESSION_MANIFEST,
                metavar="PATH",
                help=f"Shared verify --ci manifest (default: {_DEFAULT_REGRESSION_MANIFEST})",
            ),
            _arg("--json", action="store_true", help="Output machine-readable JSON"),
        ),
    )


def _minepair_spec() -> CommandSpec:
    return CommandSpec(
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
    )


def _benchmark_spec() -> CommandSpec:
    return CommandSpec(
        name="benchmark",
        handler=_cmd_benchmark,
        help="Measure scaling, mutation latency, or a checked-in perf/quality contract",
        defaults={"filter_equivalent": True},
        arguments=(
            _arg(
                "--config", "-c", default="ordeal.toml", help="Config file (default: ordeal.toml)"
            ),
            _arg(
                "--max-workers",
                type=int,
                default=None,
                help="Max workers to test (default: CPU count)",
            ),
            _arg("--time", type=float, default=10.0, help="Seconds per trial (default: 10)"),
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
                help="Return exit code 1 when a perf-contract case exceeds budget or when a bug-manifest outcome or certificate contract fails",
            ),
            _arg(
                "--output-json",
                default=None,
                metavar="PATH",
                help="Write contract, benchmark, certificate, or evidence verification JSON to PATH",
            ),
            _arg(
                "--json",
                action="store_true",
                help="Print contract, benchmark, certificate, or evidence verification JSON to stdout",
            ),
            _arg(
                "--tier",
                default=None,
                choices=["pr", "nightly"],
                help="Only run perf-contract cases matching this tier (default: all)",
            ),
            _arg(
                "--bug-manifest",
                default=None,
                metavar="PATH",
                help="Run a bug benchmark manifest that scores `ordeal scan --json` against curated public or private cases",
            ),
            _arg(
                "--verify-certificate",
                default=None,
                metavar="PATH",
                help="Verify a bug-benchmark JSON certificate, its evidence digest, and its declared metrics",
            ),
            _arg(
                "--verify-evidence",
                default=None,
                metavar="PATH",
                help="Verify one executable, source-backed bug evidence record",
            ),
            _arg(
                "--online-sources",
                action="store_true",
                help="Fetch and hash every pinned authoritative evidence URL",
            ),
            _arg(
                "--certificate-manifest",
                default=None,
                metavar="PATH",
                help="Manifest whose exact bytes must match the certificate's SHA-256 digest",
            ),
            _arg(
                "--benchmark-tier",
                default=None,
                metavar="NAME",
                help="Only run bug-manifest cases whose `tier` matches NAME",
            ),
            _arg(
                "--bugsinpy-root",
                default=None,
                metavar="PATH",
                help="Root of a local BugsInPy checkout; used when bug-manifest cases need `bugsinpy-checkout` or `bugsinpy-compile`",
            ),
            _arg(
                "--checkout-root",
                default=None,
                metavar="PATH",
                help="Directory where bug-manifest runs may materialize temporary BugsInPy checkouts",
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
            _arg("--test-filter", default=None, help="Pytest -k filter for mutation benchmarks"),
            _arg(
                "--no-filter-equivalent",
                dest="filter_equivalent",
                action="store_false",
                help="Disable equivalence filtering during mutation benchmarks",
            ),
        ),
    )


def _skill_spec() -> CommandSpec:
    return CommandSpec(
        name="skill",
        handler=_cmd_skill,
        help="Install ordeal skill for AI coding agents",
        arguments=(
            _arg(
                "--dry-run", action="store_true", help="Show what would be written without writing"
            ),
        ),
    )


def _init_spec() -> CommandSpec:
    return CommandSpec(
        name="init",
        handler=_cmd_init,
        help="Bootstrap test files for untested modules",
        description=_init_command_description,
        arguments=(
            _arg(
                "--config",
                "-c",
                default=None,
                help="Config file with [init] defaults (default: ordeal.toml if present)",
            ),
            _arg(
                "target",
                nargs="?",
                default=None,
                help="Package path (e.g. myapp); auto-detects or uses [init].target if omitted",
            ),
            _arg(
                "--output-dir",
                "-o",
                default=None,
                help="Directory to write test files (default: tests, or [init].output_dir)",
            ),
            _arg(
                "--dry-run",
                action="store_true",
                help="Preview without side effects — no files written, no functions executed. Generates stub tests from signatures only.",
            ),
            _arg(
                "--ci",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Generate a GitHub Actions workflow (.github/workflows/<name>.yml)",
            ),
            _arg(
                "--ci-name",
                default=None,
                metavar="NAME",
                help="Workflow filename stem or basename under .github/workflows (default: ordeal → .github/workflows/ordeal.yml, or [init].ci_name)",
            ),
            _arg(
                "--install-skill",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Also install the bundled AI-agent skill into .claude/skills/ordeal/",
            ),
            _arg(
                "--close-gaps",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Write draft audit stub files for surviving mutation gaps",
            ),
        ),
    )


def _mutate_spec() -> CommandSpec:
    return CommandSpec(
        name="mutate",
        handler=_cmd_mutate,
        help="Test whether your tests catch code changes",
        description=_mutate_command_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        arguments=(
            _arg(
                "targets", nargs="*", help="Dotted paths: myapp.scoring.compute or myapp.scoring"
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
                default=0,
                help="Parallel workers (default: 0, adaptive)",
            ),
            _arg(
                "--threshold",
                "-t",
                type=float,
                default=0.0,
                help="Minimum mutation score; exit 1 if below (e.g. 0.8 for 80%%)",
            ),
            _arg("--no-filter", action="store_true", help="Disable equivalent mutant filtering"),
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
                help="Pytest -k expression to select tests (avoids running full suite per mutant)",
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
                help="Write mutations to disk so subprocesses (Ray, multiprocessing) see them. Auto-detected when omitted.",
            ),
            _arg(
                "--resume",
                action="store_true",
                default=False,
                help="Reuse cached results for unchanged targets (cache: .ordeal/mutate/). Invalidated when module source, test files (test_<module>*.py), conftest.py, lockfile, or preset/operators change. Mine oracle results are never cached. Note: test files not matching test_<module>*.py are not tracked; use --no-resume or delete .ordeal/mutate/ if using test_filter with non-standard test names.",
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
    )
