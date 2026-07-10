from __future__ import annotations
# ruff: noqa
def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser for ``ordeal``."""
    parser = argparse.ArgumentParser(
        prog="ordeal",
        description=(
            "Ordeal finds replayable failures and helps keep them fixed.\n\n"
            "Start here:\n"
            "  ordeal scan .              find failures; write nothing\n"
            "  ordeal scan . --save       save evidence and a regression\n"
            "  ordeal verify <id>         verify the fix against the same witness\n"
            "  ordeal verify --ci         guard every saved regression\n\n"
            "If auto-detection fails, pass a package, module, Python file, or callable.\n"
            "Expert workflows remain available; run `ordeal catalog` to discover them."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    for spec in _command_specs():
        add_parser_kwargs: dict[str, Any] = {}
        if spec.show_in_help:
            add_parser_kwargs["help"] = spec.help
        description = _resolve_command_description(spec)
        if description is not None:
            add_parser_kwargs["description"] = description
        if spec.formatter_class is not None:
            add_parser_kwargs["formatter_class"] = spec.formatter_class
        if spec.usage is not None:
            add_parser_kwargs["usage"] = spec.usage
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
def _catalog_text_first_line(text: str) -> str:
    """Return the first non-empty line from *text*."""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
def _catalog_text_detail(text: str) -> str:
    """Return a descriptive paragraph from *text* when available."""
    paragraphs = [" ".join(block.split()) for block in str(text or "").split("\n\n")]
    filtered = [
        paragraph
        for paragraph in paragraphs
        if paragraph and not paragraph.lower().startswith(("use ", "run "))
    ]
    return filtered[1] if len(filtered) > 1 else ""
def _cli_catalog_input_summaries(arguments: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render compact input summaries from structured CLI arguments."""
    results: list[str] = []
    for argument in arguments[:8]:
        flags = list(argument.get("flags", []))
        label = flags[0] if flags else str(argument.get("name", "")).strip()
        if not label:
            continue
        value_type = str(argument.get("value_type", "")).strip()
        if argument.get("accepts_value") and value_type and value_type != "bool":
            label = f"{label}: {value_type}"
        if bool(argument.get("required")) and not flags:
            label += " (required)"
        results.append(label)
    return results
def _cli_catalog_output_summaries(
    name: str,
    arguments: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Infer output summaries from CLI argument schema."""
    outputs = ["capability map" if name == "catalog" else "terminal summary"]
    argument_names = {str(argument.get("name", "")).strip() for argument in arguments}
    joined_help = " ".join(str(argument.get("help", "")).lower() for argument in arguments)
    if {"json", "output_json"} & argument_names or " json" in joined_help:
        outputs.append("JSON")
    if any("report" in arg_name for arg_name in argument_names) or "markdown" in joined_help:
        outputs.append("reports")
    if {"write_regression", "generate_tests", "write_gaps", "save_generated"} & argument_names:
        outputs.append("generated files")
    if "save_artifacts" in argument_names:
        outputs.append("artifact bundle")
    return list(dict.fromkeys(outputs))
def _cli_catalog_applies_to(
    name: str,
    description: str,
    arguments: Sequence[Mapping[str, Any]],
) -> str:
    """Infer a neutral applicability hint from command schema."""
    detail = _catalog_text_detail(description)
    if detail:
        return detail
    names = {str(argument.get("name", "")).strip() for argument in arguments}
    hints: list[str] = []
    if {"target", "targets"} & names:
        hints.append("named callable or module targets")
    if {"module", "modules"} & names:
        hints.append("module-level inputs")
    if "trace_file" in names:
        hints.append("saved trace files")
    if "finding_id" in names:
        hints.append("saved finding identifiers")
    if "config" in names:
        hints.append("config-driven runs")
    if not hints and name == "catalog":
        hints.append("live capability discovery")
    return ", ".join(dict.fromkeys(hints)) or "repo-local terminal workflows"
def _cli_catalog_learn_more(name: str) -> list[str]:
    """Return adjacent CLI discovery surfaces for one command."""
    if name == "diff":
        return [
            "ordeal diff --help",
            "docs/guides/revision-diff.md",
            "docs/guides/revision-diff-troubleshooting.md",
            "docs/reference/revision-diff-schema.md",
            "docs/concepts/differential-testing.md",
            "docs/concepts/divergence-evidence.md",
            "docs/guides/divergence-evidence.md",
            "docs/guides/divergence-evidence-troubleshooting.md",
            "docs/reference/divergence-evidence-schema.md",
            "ordeal catalog --json",
        ]
    if name == "migrate":
        return [
            "ordeal migrate --help",
            "docs/concepts/safe-migrations.md",
            "docs/guides/migration-workflow.md",
            "docs/reference/api.md#migration-workflow",
            "ordeal catalog --json",
        ]
    return [f"ordeal {name} --help", "ordeal catalog --json"]
def _cli_catalog_examples(name: str, usage: str) -> list[str]:
    """Return copyable examples for one CLI catalog entry."""
    if name == "diff":
        return [
            "ordeal diff mypkg.scoring --base-ref origin/main --candidate-ref HEAD",
            "ordeal diff mypkg.scoring --base-ref origin/main --save-artifacts",
            "ordeal diff --json  # uses [diff] from ordeal.toml",
        ]
    if name == "migrate":
        return [
            "ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml",
            (
                "ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml "
                "--intended-change behavior:normalize"
            ),
        ]
    return [usage] if usage else []
def command_catalog() -> list[dict[str, Any]]:
    """Return a structured catalog of CLI commands derived from argparse."""
    parser = _build_parser()
    registered_help = {spec.name: spec.help for spec in _command_specs()}
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
            description = subparser.description or ""
            help_text = registered_help.get(name) or choice_help.get(name, "")
            capability = _catalog_text_first_line(description) or help_text
            entries.append(
                {
                    "name": name,
                    "schema_version": CLI_CATALOG_SCHEMA_VERSION,
                    "qualname": f"ordeal.cli.{name}",
                    "doc": help_text,
                    "usage": usage,
                    "description": description,
                    "arguments": arguments,
                    "capability": capability,
                    "applies_to": _cli_catalog_applies_to(name, description, arguments),
                    "inputs": _cli_catalog_input_summaries(arguments),
                    "outputs": _cli_catalog_output_summaries(name, arguments),
                    "examples": _cli_catalog_examples(name, usage),
                    "learn_more": _cli_catalog_learn_more(name),
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
    try:
        return handler(args)
    except Exception as exc:
        if getattr(args, "command", None) != "scan":
            raise
        target = str(getattr(args, "target", None) or ".")
        limitation_kind = "import" if isinstance(exc, ImportError) else "tool_orchestration"
        reason = f"{type(exc).__name__}: {exc}"
        evidence = {
            "schema": "ordeal.scan-limitation/v1",
            "status": "blocked",
            "subject": {"target": target},
            "limitation": {"kind": limitation_kind, "reason": reason},
            "boundaries": {
                "establishes": "Ordeal could not complete the scan command.",
                "does_not_establish": [
                    "that the target crashed",
                    "that the target is correct",
                ],
            },
        }
        if limitation_kind == "import":
            evidence["boundaries"]["establishes"] = (
                "The target could not be imported; target behavior was not observed."
            )
        if bool(getattr(args, "json", False)):
            print(
                _build_blocked_agent_envelope(
                    tool="scan",
                    target=target,
                    summary="scan blocked by an Ordeal-side limitation",
                    blocking_reason=reason,
                    suggested_commands=(f"ordeal scan {target} --list-targets",),
                    raw_details={"evidence": evidence},
                ).to_json()
            )
        else:
            _stderr(f"Scan blocked ({limitation_kind}): {reason}\n")
            _stderr("  This is not evidence that the target crashed.\n")
        return 1
if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
