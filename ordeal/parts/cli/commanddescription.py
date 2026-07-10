from __future__ import annotations

# ruff: noqa


def _command_specs() -> tuple[CommandSpec, ...]:
    """Return the declarative registry for CLI commands."""
    return (
        _catalog_spec(),
        _check_spec(),
        _scan_spec(),
        _verify_spec(),
        _explore_spec(),
        _replay_spec(),
        _seeds_spec(),
        _audit_spec(),
        _mine_spec(),
        _diff_spec(),
        _migrate_spec(),
        _minepair_spec(),
        _benchmark_spec(),
        _skill_spec(),
        _init_spec(),
        _mutate_spec(),
    )


def _resolve_command_description(spec: CommandSpec) -> str | None:
    """Resolve a command description from a static string or callable."""
    description = spec.description
    if callable(description):
        return description()
    return description
