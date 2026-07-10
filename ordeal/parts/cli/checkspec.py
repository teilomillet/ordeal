from __future__ import annotations
# ruff: noqa
def _check_spec() -> CommandSpec:
    return CommandSpec(
        name="check",
        handler=_cmd_check,
        help="Verify a property or explicit contract on a callable target",
        arguments=(
            _arg("target", help="Callable target: mymod.func or mymod:Class.method"),
            _arg(
                "--config",
                default=None,
                help="Optional ordeal.toml path (default: ./ordeal.toml when present)",
            ),
            _arg(
                "--property",
                "-p",
                default=None,
                help="Property to verify. Omit to check all standard contracts.",
            ),
            _arg(
                "--contract",
                action="append",
                default=[],
                help="Repeat to check one or more built-in contracts or pack aliases directly.",
            ),
            _arg(
                "--max-examples",
                "-n",
                type=int,
                default=200,
                help="Examples to test (default: 200)",
            ),
            _arg("--json", action="store_true", help="Emit a JSON agent envelope"),
        ),
    )
def _scan_spec() -> CommandSpec:
    return CommandSpec(
        name="scan",
        handler=_cmd_scan,
        help="Find replayable failures and show the next action",
        description=_scan_command_description,
        formatter_class=_ScanHelpFormatter,
        usage="%(prog)s [-h] [-n MAX_EXAMPLES] [-t TIME_LIMIT] [--deepen] [--base-ref BASE_REF] [--allow-service-faults] [--json] [--save] [--list-targets] [target]",
        show_in_help=True,
        arguments=(
            _arg(
                "target",
                nargs="?",
                default=None,
                help="Package, module, Python file, or callable; omit or use '.' to auto-detect",
            ),
            _arg(
                "--target",
                dest="scan_targets",
                action="append",
                default=None,
                metavar="SELECTOR",
                help="Limit a module scan to callable selector(s); accepts local names, explicit targets, or glob patterns like mutate, Env.*, or ordeal:mut* (repeatable)",
            ),
            _arg(
                "--seed", type=int, default=42, help="RNG seed for reproducibility (default: 42)"
            ),
            _arg(
                "--max-examples",
                "-n",
                type=int,
                default=50,
                help="Examples per function (default: 50)",
            ),
            _arg(
                "--mode",
                choices=("evidence", "candidate", "coverage_gap", "real_bug"),
                default=None,
                help="Surfacing mode: evidence-first or stricter candidate ranking",
            ),
            _arg(
                "--security-focus",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Bias scan toward trust-boundary sinks and deterministic security probes",
            ),
            _arg(
                "--seed-from-tests",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Learn valid input shapes from adjacent pytest files before fuzzing",
            ),
            _arg(
                "--min-contract-fit",
                type=float,
                default=None,
                help="Minimum contract-fit score required for promotion",
            ),
            _arg(
                "--min-reachability",
                type=float,
                default=None,
                help="Minimum reachability score required for promotion",
            ),
            _arg(
                "--min-realism",
                type=float,
                default=None,
                help="Minimum semantic-realism score required for promotion",
            ),
            _arg(
                "--workers",
                "-w",
                type=int,
                default=1,
                help="Parallel workers for mutation testing",
            ),
            _arg("--time-limit", "-t", type=float, default=None, help="Time budget in seconds"),
            _arg(
                "--deepen",
                action="store_true",
                help="Use the remaining time budget for one safe planned follow-up",
            ),
            _arg(
                "--base-ref",
                default=None,
                help="Prioritize operations changed since this Git revision",
            ),
            _arg(
                "--allow-service-faults",
                action="store_true",
                help="Allow a planned Compose service-fault experiment; off by default",
            ),
            _arg("--evidence-fault", default=None, help=argparse.SUPPRESS),
            _arg(
                "--ignore-property",
                dest="ignore_properties",
                action="append",
                default=None,
                metavar="NAME",
                help="Suppress mined property NAME (repeatable)",
            ),
            _arg(
                "--ignore-relation",
                dest="ignore_relations",
                action="append",
                default=None,
                metavar="NAME",
                help="Suppress mined relation NAME (repeatable)",
            ),
            _arg(
                "--property-override",
                dest="cli_property_overrides",
                action="append",
                type=_parse_named_override_spec,
                default=None,
                metavar="FUNC=PROP[,PROP...]",
                help="Suppress mined properties for one function (repeatable)",
            ),
            _arg(
                "--relation-override",
                dest="cli_relation_overrides",
                action="append",
                type=_parse_named_override_spec,
                default=None,
                metavar="FUNC=REL[,REL...]",
                help="Suppress mined relations for one function (repeatable)",
            ),
            _arg("--json", action="store_true", help="Output JSON instead of text"),
            _arg(
                "--save",
                "--save-artifacts",
                dest="save_artifacts",
                action="store_true",
                help="Save a replayable finding as an evidence bundle and pytest regression",
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
                help=f"Write runnable pytest regressions for replayable findings (default: {_DEFAULT_REGRESSION_PATH})",
            ),
            _arg(
                "--include-private",
                action="store_true",
                help="Include _private functions (many codebases have logic there)",
            ),
            _arg(
                "--list-targets",
                action="store_true",
                help="List callable targets, surface metadata, and ranked config hints, then exit",
            ),
        ),
    )
def _verify_spec() -> CommandSpec:
    return CommandSpec(
        name="verify",
        handler=_cmd_verify,
        help="Verify one saved fix or guard every bound regression in CI",
        description=_verify_command_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        show_in_help=True,
        arguments=(
            _arg("finding_id", nargs="?", help="Stable finding ID (omit with --ci)"),
            _arg(
                "--index",
                default=_default_artifact_index_path(),
                metavar="PATH",
                help=f"Artifact index path (default: {_default_artifact_index_path()})",
            ),
            _arg(
                "--manifest",
                default=_DEFAULT_REGRESSION_MANIFEST,
                metavar="PATH",
                help=f"Portable CI regression manifest (default: {_DEFAULT_REGRESSION_MANIFEST})",
            ),
            _arg(
                "--allow-unsafe-artifacts",
                action="store_true",
                help="Trust the artifact index and bundle enough to run the recorded pytest regression",
            ),
            _arg(
                "--ci",
                action="store_true",
                help="Read-only: verify every saved binding and regression inside the current workspace",
            ),
        ),
    )
def _explore_spec() -> CommandSpec:
    return CommandSpec(
        name="explore",
        handler=_cmd_explore,
        help="Coverage-guided or long-lived service exploration (reads ordeal.toml)",
        arguments=(
            _arg(
                "--config", "-c", default="ordeal.toml", help="Config file (default: ordeal.toml)"
            ),
            _arg(
                "--runner",
                choices=["python", "compose"],
                default="python",
                help="Execution boundary: Python state machine or Docker Compose services",
            ),
            _arg("--seed", type=int, help="Override RNG seed"),
            _arg("--max-time", type=float, help="Override max_time (seconds)"),
            _arg(
                "--replay-attempts",
                type=int,
                default=None,
                metavar="N",
                help="Replay a Compose failure N times (default: [compose].replay_attempts)",
            ),
            _arg(
                "--save-artifacts",
                action="store_true",
                help="For a replay-backed Compose failure, commit a portable trace binding under tests/ and update tests/ordeal-regressions.json",
            ),
            _arg("--verbose", "-v", action="store_true", help="Live progress"),
            _arg("--no-shrink", action="store_true", help="Skip shrinking"),
            _arg("--no-seeds", action="store_true", help="Skip seed corpus replay"),
            _arg(
                "--prune-fixed-seeds",
                action="store_true",
                help="Delete replayed seeds that no longer reproduce",
            ),
            _arg("--workers", "-w", type=int, help="Parallel worker processes (default: 1)"),
            _arg(
                "--generate-tests",
                type=str,
                default=None,
                metavar="PATH",
                help="Generate pytest tests from exploration traces (e.g. tests/test_generated.py)",
            ),
            _arg(
                "--resume",
                type=str,
                default=None,
                metavar="PATH",
                help="Resume from a trusted saved state file (pickle; requires --allow-unsafe-resume)",
            ),
            _arg(
                "--allow-unsafe-resume",
                action="store_true",
                help="Allow loading the trusted pickle file passed to --resume",
            ),
            _arg(
                "--save-state",
                type=str,
                default=None,
                metavar="PATH",
                help="Save exploration state on completion (trusted-only pickle, e.g. .ordeal/state.pkl)",
            ),
            _arg(
                "--json", action="store_true", help="Print complete Compose run evidence as JSON"
            ),
        ),
    )
def _replay_spec() -> CommandSpec:
    return CommandSpec(
        name="replay",
        handler=_cmd_replay,
        help="Replay a saved trace",
        arguments=(
            _arg("trace_file", help="Path to trace JSON file"),
            _arg("--shrink", action="store_true", help="Shrink the trace"),
            _arg("--ablate", action="store_true", help="Ablate faults to find necessary ones"),
            _arg("--output", "-o", help="Save shrunk trace to this path"),
            _arg(
                "--attempts",
                type=int,
                default=None,
                metavar="N",
                help="Replay a Compose trace N times (default: recorded config)",
            ),
            _arg("--json", action="store_true", help="Output agent-facing JSON"),
        ),
    )
def _seeds_spec() -> CommandSpec:
    return CommandSpec(
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
                "--prune-fixed", action="store_true", help="Remove seeds that no longer reproduce"
            ),
        ),
    )
def _audit_spec() -> CommandSpec:
    return CommandSpec(
        name="audit",
        handler=_cmd_audit,
        help="Audit test coverage vs ordeal migration",
        description=_audit_command_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        arguments=(
            _arg("modules", nargs="*", help="Module paths to audit (omit to use [audit].modules)"),
            _arg(
                "--config",
                "-c",
                default=None,
                help="Config file with [audit] defaults (default: ordeal.toml if present)",
            ),
            _arg(
                "--test-dir",
                "-t",
                default=None,
                help="Test directory (default: tests, or [audit].test_dir)",
            ),
            _arg(
                "--max-examples",
                type=int,
                default=None,
                help="Examples per function (default: 20, or [audit].max_examples)",
            ),
            _arg(
                "--workers",
                type=int,
                default=None,
                help="Isolated mutation-validation processes (default: 1, or [audit].workers)",
            ),
            _arg(
                "--validation-mode",
                choices=("fast", "deep"),
                default=None,
                help="Validation mode: fast replay (default) or deep re-mine",
            ),
            _arg(
                "--show-generated",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Print the generated test file for inspection/debugging",
            ),
            _arg(
                "--save-generated",
                type=str,
                default=None,
                help="Save generated test file to this path",
            ),
            _arg(
                "--write-gaps",
                type=str,
                default=None,
                metavar="PATH",
                help="Write draft audit gap stubs to PATH",
            ),
            _arg(
                "--include-exploratory-function-gaps",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Include exploratory function gaps in audit findings and draft stubs",
            ),
            _arg(
                "--require-direct-tests",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Return exit code 1 when exploratory function coverage is all indirect",
            ),
            _arg(
                "--list-targets",
                action="store_true",
                help="List callable targets, surface metadata, and ranked config hints, then exit",
            ),
            _arg("--json", action="store_true", help="Output agent-facing JSON"),
        ),
    )
def _mine_spec() -> CommandSpec:
    return CommandSpec(
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
                "--verbose", "-v", action="store_true", help="Show n/a properties and extra detail"
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
                help=f"Write runnable pytest regressions for suspicious findings (default: {_DEFAULT_REGRESSION_PATH})",
            ),
            _arg("--json", action="store_true", help="Output agent-facing JSON"),
        ),
    )
