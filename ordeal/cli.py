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
import importlib
import inspect
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
_PACKAGE_ROOT_SCAN_LIMIT = 8
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
    mode: str = "coverage_gap"
    seed_from_tests: bool = True
    seed_from_fixtures: bool = True
    seed_from_docstrings: bool = True
    seed_from_code: bool = True
    seed_from_call_sites: bool = True
    treat_any_as_weak: bool = True
    proof_bundles: bool = True
    require_replayable: bool = True
    auto_contracts: list[str] = field(default_factory=list)
    min_contract_fit: float = 0.55
    min_reachability: float = 0.45
    min_realism: float = 0.55
    min_fixture_completeness: float = 0.0
    fixtures: dict[str, Any] | None = None
    targets: list[str] = field(default_factory=list)
    include_private: bool = False
    object_factories: dict[str, Any] | None = None
    object_setups: dict[str, Any] | None = None
    object_scenarios: dict[str, Any] | None = None
    object_state_factories: dict[str, Any] | None = None
    object_teardowns: dict[str, Any] | None = None
    object_harnesses: dict[str, str] | None = None
    contract_checks: dict[str, list[Any]] = field(default_factory=dict)
    expected_failures: list[str] = field(default_factory=list)
    expected_preconditions: dict[str, list[str]] = field(default_factory=dict)
    registry_warnings: list[str] = field(default_factory=list)
    ignore_contracts: list[str] = field(default_factory=list)
    ignore_properties: list[str] = field(default_factory=list)
    ignore_relations: list[str] = field(default_factory=list)
    contract_overrides: dict[str, list[str]] = field(default_factory=dict)
    expected_properties: dict[str, list[str]] = field(default_factory=dict)
    expected_relations: dict[str, list[str]] = field(default_factory=dict)
    property_overrides: dict[str, list[str]] = field(default_factory=dict)
    relation_overrides: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckRuntimeDefaults:
    """Resolved runtime config for one explicit check target."""

    object_factories: dict[str, Any] | None = None
    object_setups: dict[str, Any] | None = None
    object_scenarios: dict[str, Any] | None = None
    object_state_factories: dict[str, Any] | None = None
    object_teardowns: dict[str, Any] | None = None
    object_harnesses: dict[str, str] | None = None
    contract_checks: list[Any] = field(default_factory=list)
    registry_warnings: list[str] = field(default_factory=list)


def _load_fixture_registry_warnings(
    *,
    shared_modules: Sequence[str] = (),
    extra_modules: Sequence[str] = (),
) -> list[str]:
    """Load shared and per-scan fixture registries, returning warnings."""
    from ordeal.auto import load_project_fixture_registries

    registries = [module for module in (*shared_modules, *extra_modules) if module]
    if not registries:
        return load_project_fixture_registries()
    return load_project_fixture_registries(extra_modules=list(dict.fromkeys(registries)))


def _scan_base_module(target: str) -> str:
    """Return the module component for a scan target."""
    return target.split(":", 1)[0]


def _target_module_name(target: str) -> str:
    """Return the importable module for dotted or explicit callable targets."""
    if ":" in target:
        return target.split(":", 1)[0]
    module_name, _, _ = target.rpartition(".")
    return module_name or target


def _scan_display_name(module_name: str, target: str) -> str:
    """Return the local callable name used in scan results for *target*."""
    if ":" in target:
        explicit_module, explicit_target = target.split(":", 1)
        if explicit_module != module_name:
            return target
        return explicit_target
    dotted_prefix = f"{module_name}."
    return target[len(dotted_prefix) :] if target.startswith(dotted_prefix) else target


def _package_root_scan_sample(
    module_name: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int = _PACKAGE_ROOT_SCAN_LIMIT,
) -> dict[str, Any] | None:
    """Return a bounded representative target sample for broad package-root scans."""
    from ordeal.auto import _resolve_module

    try:
        mod = _resolve_module(module_name)
    except Exception:
        return None
    if not getattr(mod, "__path__", None):
        return None

    runnable_rows = [row for row in rows if bool(row.get("runnable", True))]
    if len(runnable_rows) <= limit:
        return None

    chosen: list[str] = []
    seen_sources: set[str] = set()
    deferred: list[str] = []

    for row in sorted(runnable_rows, key=_package_root_scan_priority):
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        source_module = str(row.get("source_module") or row.get("module") or module_name)
        if source_module not in seen_sources and len(chosen) < limit:
            chosen.append(name)
            seen_sources.add(source_module)
        else:
            deferred.append(name)

    if len(chosen) < limit:
        for name in deferred:
            if name in chosen:
                continue
            chosen.append(name)
            if len(chosen) >= limit:
                break

    if len(chosen) >= len(runnable_rows):
        return None

    return {
        "kind": "package_root_sample",
        "module": module_name,
        "limit": limit,
        "sampled": len(chosen),
        "total_runnable": len(runnable_rows),
        "source_modules": len(
            {
                str(row.get("source_module") or row.get("module") or module_name)
                for row in runnable_rows
            }
        ),
        "targets": chosen,
    }


def _resolve_symbol_path(path: str) -> Any:
    """Resolve ``module:attr`` or dotted import paths into Python objects."""
    import importlib

    module_name, sep, attr_path = path.partition(":")
    if not sep:
        module_name, _, attr_path = path.rpartition(".")
    if not module_name or not attr_path:
        raise ValueError(f"invalid symbol path: {path!r}")
    obj = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _callable_listing_owner(module_name: str, target_name: str) -> tuple[Any | None, Any | None]:
    """Return the class owner and descriptor for ``Class.method`` targets."""
    from ordeal.auto import _resolve_module

    parts = [part for part in target_name.split(".") if part]
    if len(parts) < 2:
        return None, None

    owner: Any = _resolve_module(module_name)
    for part in parts[:-1]:
        try:
            owner = getattr(owner, part)
        except AttributeError:
            return None, None

    if not inspect.isclass(owner):
        return None, None

    try:
        descriptor = inspect.getattr_static(owner, parts[-1])
    except AttributeError:
        return None, None
    return owner, descriptor


def _callable_listing_kind(func: Any, owner: Any | None, descriptor: Any | None) -> str:
    """Return the callable kind for a discovered target."""
    kind = getattr(func, "__ordeal_kind__", None)
    if kind in {"function", "instance", "class", "static"}:
        return str(kind)
    if owner is not None:
        if isinstance(descriptor, staticmethod):
            return "static"
        if isinstance(descriptor, classmethod):
            return "class"
        if inspect.isfunction(descriptor):
            return "instance"
    if inspect.ismethod(func) and inspect.isclass(getattr(func, "__self__", None)):
        return "class"
    return "function"


def _callable_listing_async_state(func: Any) -> str:
    """Return ``async`` for coroutine targets, including wrapped callables."""
    candidate = func
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        if inspect.iscoroutinefunction(candidate):
            return "async"
        seen.add(id(candidate))
        candidate = getattr(candidate, "__wrapped__", None)
    return "sync"


def _callable_listing_rows(
    module_name: str,
    *,
    targets: Sequence[str] | None = None,
    selected_targets: Sequence[str] | None = None,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    contract_checks: Mapping[str, Sequence[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return stable discovery rows for callable targets in *module_name*."""
    from ordeal.auto import (
        _REGISTERED_OBJECT_FACTORIES,
        _REGISTERED_OBJECT_HARNESSES,
        _REGISTERED_OBJECT_SCENARIOS,
        _REGISTERED_OBJECT_SETUPS,
        _REGISTERED_OBJECT_STATE_FACTORIES,
        _REGISTERED_OBJECT_TEARDOWNS,
        _callable_matches_target_selector,
        _infer_strategies,
        _mine_object_harness_hints,
        _resolve_module,
        _resolve_object_harness,
        _resolve_object_hook,
        _selected_public_functions,
        _state_param_name_for_callable,
        _unwrap,
    )

    mod = _resolve_module(module_name)
    merged_factories = dict(_REGISTERED_OBJECT_FACTORIES)
    if object_factories:
        merged_factories.update(object_factories)
    merged_setups = dict(_REGISTERED_OBJECT_SETUPS)
    if object_setups:
        merged_setups.update(object_setups)
    merged_scenarios = dict(_REGISTERED_OBJECT_SCENARIOS)
    if object_scenarios:
        merged_scenarios.update(object_scenarios)
    merged_state_factories = dict(_REGISTERED_OBJECT_STATE_FACTORIES)
    if object_state_factories:
        merged_state_factories.update(object_state_factories)
    merged_teardowns = dict(_REGISTERED_OBJECT_TEARDOWNS)
    if object_teardowns:
        merged_teardowns.update(object_teardowns)
    merged_harnesses = dict(_REGISTERED_OBJECT_HARNESSES)
    if object_harnesses:
        merged_harnesses.update(object_harnesses)
    discovered = _selected_public_functions(
        mod,
        targets=targets,
        include_private=include_private,
        object_factories=merged_factories,
        object_setups=merged_setups,
        object_scenarios=merged_scenarios,
        object_state_factories=merged_state_factories,
        object_teardowns=merged_teardowns,
        object_harnesses=merged_harnesses,
    )
    selectors = [str(raw).strip() for raw in selected_targets or () if str(raw).strip()]

    rows: list[dict[str, Any]] = []
    for name, func in discovered:
        owner, descriptor = _callable_listing_owner(module_name, name)
        kind = _callable_listing_kind(func, owner, descriptor)
        factory_required = kind == "instance"
        factory_configured = bool(
            factory_required
            and owner is not None
            and _resolve_object_hook(owner, merged_factories)
        )
        setup_configured = bool(owner is not None and _resolve_object_hook(owner, merged_setups))
        teardown_configured = bool(
            owner is not None and _resolve_object_hook(owner, merged_teardowns)
        )
        resolved_scenario = (
            _resolve_object_hook(owner, merged_scenarios) if owner is not None else None
        )
        state_param = (
            str(
                getattr(func, "__ordeal_state_param__", None)
                or (_state_param_name_for_callable(_unwrap(func)) if owner is not None else "")
                or ""
            ).strip()
            or None
        )
        state_factory_configured = bool(
            state_param
            and owner is not None
            and _resolve_object_hook(owner, merged_state_factories)
        )
        harness_mode = (
            _resolve_object_harness(owner, merged_harnesses) if owner is not None else "fresh"
        )
        scenario_count = int(getattr(resolved_scenario, "__ordeal_scenario_count__", 0))
        if (
            scenario_count == 0
            and isinstance(resolved_scenario, Sequence)
            and not isinstance(resolved_scenario, (str, bytes, bytearray))
        ):
            scenario_count = len([item for item in resolved_scenario if item is not None])
        if resolved_scenario is not None and scenario_count == 0:
            scenario_count = 1
        skip_reason = getattr(func, "__ordeal_skip_reason__", None)
        inferred_strategies = _infer_strategies(_unwrap(func))
        if factory_required and not factory_configured and not skip_reason:
            skip_reason = "missing object factory"
        if state_param and not state_factory_configured and inferred_strategies is None:
            skip_reason = skip_reason or "missing state factory"
        if inferred_strategies is None and not skip_reason:
            skip_reason = "missing inferable strategies"
        checks = list(contract_checks.get(name, [])) if contract_checks is not None else []
        harness_hints: list[dict[str, Any]] = []
        if owner is not None and kind == "instance":
            for hint in _mine_object_harness_hints(
                getattr(owner, "__module__", module_name),
                getattr(owner, "__name__", "Owner"),
                name.rsplit(".", 1)[-1],
            )[:5]:
                harness_hints.append(
                    {
                        "kind": hint.kind,
                        "suggestion": hint.suggestion,
                        "evidence": hint.evidence,
                        "confidence": round(float(hint.confidence), 2),
                        "config": hint.config,
                    }
                )

        rows.append(
            {
                "module": module_name,
                "source_module": getattr(_unwrap(func), "__module__", module_name),
                "name": name,
                "target": f"{module_name}.{name}",
                "kind": kind,
                "async": _callable_listing_async_state(func),
                "selected": (
                    True
                    if not selectors
                    else any(
                        _callable_matches_target_selector(module_name, name, selector)
                        for selector in selectors
                    )
                ),
                "factory_required": factory_required,
                "factory_configured": factory_configured,
                "setup_configured": setup_configured,
                "state_param": state_param,
                "state_factory_configured": state_factory_configured,
                "teardown_configured": teardown_configured,
                "harness": harness_mode,
                "scenario_count": scenario_count,
                "contract_checks": [str(getattr(check, "name", check)) for check in checks],
                "lifecycle_phase": getattr(func, "__ordeal_lifecycle_phase__", None),
                "harness_hints": harness_hints,
                "runnable": skip_reason is None,
                "skip_reason": skip_reason,
            }
        )

    unmatched_selectors = [
        selector
        for selector in selectors
        if not any(
            _callable_matches_target_selector(module_name, str(row.get("name", "")), selector)
            for row in rows
        )
    ]
    if unmatched_selectors:
        missing = ", ".join(repr(selector) for selector in unmatched_selectors)
        raise ValueError(
            f"target selector(s) {missing} matched no callables in module {module_name!r}"
        )

    return rows


def _harness_hint_summary(hints: Sequence[Mapping[str, Any]]) -> str | None:
    """Render a compact one-line summary of mined harness hints."""
    by_kind: dict[str, str] = {}
    for hint in hints:
        kind = str(hint.get("kind", "")).strip()
        suggestion = str(hint.get("suggestion", "")).strip()
        if kind and suggestion and kind not in by_kind:
            by_kind[kind] = suggestion
    if not by_kind:
        return None
    parts = [f"{kind}={value}" for kind, value in by_kind.items()]
    return "; ".join(parts[:3])


def _harness_hint_config_summary(hints: Sequence[Mapping[str, Any]]) -> str | None:
    """Render a compact one-line summary of mined harness config hints."""
    configs: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        config = hint.get("config")
        if not isinstance(config, Mapping):
            continue
        section = str(config.get("section", "")).strip()
        target = str(config.get("target", "")).strip()
        method = str(config.get("method", "")).strip()
        key = str(config.get("key", "")).strip()
        value = config.get("value")
        value_text = value if isinstance(value, str) else pformat(value, compact=True)
        parts = [part for part in (section, target, method) if part]
        if key:
            parts.append(f"{key}={value_text}")
        elif value is not None:
            parts.append(str(value_text))
        summary = " ".join(parts).strip()
        if summary and summary not in seen:
            seen.add(summary)
            configs.append(summary)
    if not configs:
        return None
    return "; ".join(configs[:3])


def _render_target_listing_parts(row: Mapping[str, Any]) -> list[str]:
    """Return the normalized text fragments for one callable discovery row."""
    factory_text = (
        "not-needed"
        if not row.get("factory_required")
        else f"required, configured={'yes' if row.get('factory_configured') else 'no'}"
    )
    parts = [
        f"kind={row.get('kind')}",
        f"async={row.get('async')}",
        f"selected={'yes' if row.get('selected', True) else 'no'}",
        f"factory={factory_text}",
        f"harness={row.get('harness', 'fresh')}",
        f"setup={'yes' if row.get('setup_configured') else 'no'}",
        (
            "state=not-needed"
            if not row.get("state_param")
            else f"state={'yes' if row.get('state_factory_configured') else 'no'}"
        ),
        f"teardown={'yes' if row.get('teardown_configured') else 'no'}",
        f"scenarios={row.get('scenario_count', 0)}",
        f"runnable={'yes' if row.get('runnable') else 'no'}",
    ]
    contract_checks = list(row.get("contract_checks", []))
    if contract_checks:
        parts.append(f"contracts={','.join(contract_checks)}")
    lifecycle_phase = row.get("lifecycle_phase")
    if lifecycle_phase:
        parts.append(f"phase={lifecycle_phase}")
    skip_reason = row.get("skip_reason")
    if skip_reason:
        parts.append(f"skip={skip_reason}")
    hint_summary = _harness_hint_summary(list(row.get("harness_hints", [])))
    if hint_summary:
        parts.append(f"hints={hint_summary}")
    config_summary = _harness_hint_config_summary(list(row.get("harness_hints", [])))
    if config_summary:
        parts.append(f"configs={config_summary}")
    return parts


def _render_target_listing_text(
    title: str,
    groups: Sequence[Mapping[str, Any]],
    *,
    warnings: Sequence[str] = (),
) -> str:
    """Render callable discovery rows for human-readable CLI output."""
    lines = [title]
    for warning in warnings:
        lines.append(f"warning: {warning}")
    for group in groups:
        module = str(group.get("module", ""))
        targets = list(group.get("targets", []))
        lines.append(f"\n{module}")
        if not targets:
            lines.append("  (no callable targets found)")
            continue
        for row in targets:
            parts = _render_target_listing_parts(row)
            lines.append(f"  {row.get('name', ''):<38} " + "  ".join(parts))
    return "\n".join(lines)


def _build_target_listing_envelope(
    *,
    tool: str,
    target: str,
    groups: Sequence[Mapping[str, Any]],
    warnings: Sequence[str] = (),
) -> Any:
    """Build the agent envelope for callable discovery output."""
    from ordeal.agent_schema import build_agent_envelope

    flat_targets = [row for group in groups for row in list(group.get("targets", []))]
    runnable_count = sum(1 for row in flat_targets if row.get("runnable"))
    skip_count = len(flat_targets) - runnable_count
    status = "exploratory" if skip_count else "ok"
    summary = [
        f"Listed {len(flat_targets)} callable target(s) across {len(groups)} module(s)",
        f"Runnable: {runnable_count}",
        f"Skipped: {skip_count}",
    ]
    if warnings:
        summary.append(f"Warnings: {len(warnings)}")
    return build_agent_envelope(
        tool=tool,
        target=target,
        status=status,
        summary=" | ".join(summary),
        recommended_action=(
            "Use these callable names and metadata directly in `scan`, `audit`, or `mutate`."
        ),
        confidence=None,
        confidence_basis=("target discovery only",),
        findings=(),
        artifacts=(),
        raw_details={
            "target_groups": [dict(group) for group in groups],
            "targets": flat_targets,
            "warnings": list(warnings),
            "runnable_count": runnable_count,
            "skip_count": skip_count,
        },
    )


def _callable_fixture_completeness(rows: Sequence[Mapping[str, Any]]) -> float:
    """Return runnable-target completeness for a callable listing."""
    if not rows:
        return 0.0
    runnable = sum(1 for row in rows if row.get("runnable"))
    return runnable / len(rows)


def _blocked_callable_listing_reason(
    rows: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.0,
) -> str | None:
    """Return a blocking reason when discovery lacks enough runnable targets."""
    if not rows:
        return "no callable targets were discovered"
    completeness = _callable_fixture_completeness(rows)
    if completeness <= 0.0:
        skip_reasons = {str(row.get("skip_reason", "")) for row in rows}
        if {
            "missing object factory",
            "missing state factory",
        } & skip_reasons:
            return "need instance/state harness or object/state factory for discovered methods"
        return "no discovered targets had inferable fixtures or strategies"
    if threshold > 0.0 and completeness < threshold:
        return (
            "fixture completeness is too low for meaningful exploration "
            f"({completeness:.0%} < {threshold:.0%})"
        )
    return None


def _arg(*tokens: str, **kwargs: Any) -> ArgumentSpec:
    """Create a declarative CLI argument spec."""
    return ArgumentSpec(tokens=tokens, kwargs=dict(kwargs))


def _load_optional_config(path_str: str | None) -> OrdealConfig | None:
    """Load a config file when explicitly requested or present in cwd."""
    config_path = Path(path_str or "ordeal.toml")
    if not config_path.exists():
        if path_str is None:
            return None
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return load_config(config_path)


def _cli_or_config(value: Any, fallback: Any) -> Any:
    """Prefer an explicit CLI value, otherwise use the config/default fallback."""
    return fallback if value is None else value


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
    print("Key CLI entrypoints: scan, init, audit, mutate, verify, skill.")
    print("Run 'ordeal skill' or 'ordeal init --install-skill' for local agent guidance.")
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
    """Verify a mined property or an explicit contract on one target."""
    from ordeal.auto import _evaluate_contract_checks, _resolve_explicit_target, _unwrap
    from ordeal.mine import mine

    target = args.target
    prop = args.property
    cli_contract_names = _check_contract_names(args)
    contract_runtime = _resolve_check_runtime_defaults(
        target,
        config_path=getattr(args, "config", None),
    )
    if cli_contract_names and contract_runtime.contract_checks:
        _stderr("Use either --contract or config-backed explicit contracts, not both.\n")
        return 1

    has_explicit_contracts = bool(contract_runtime.contract_checks or cli_contract_names)
    explicit_target = ":" in target

    if prop is not None and has_explicit_contracts:
        _stderr("Use either --property or explicit contracts, not both.\n")
        return 1

    if prop is None and explicit_target and not has_explicit_contracts:
        _stderr(
            f"No explicit contracts configured for {target}. "
            "Add a matching [[contracts]] entry in ordeal.toml, "
            "pass --contract, or use --property.\n"
        )
        return 1

    if prop is None and (explicit_target or has_explicit_contracts):
        module_name = _target_module_name(target)
        target_name = (
            target
            if explicit_target
            else f"{module_name}:{_scan_display_name(module_name, target)}"
        )
        try:
            resolved_name, func = _resolve_explicit_target(
                target_name,
                object_factories=contract_runtime.object_factories,
                object_setups=contract_runtime.object_setups,
                object_scenarios=contract_runtime.object_scenarios,
                object_state_factories=contract_runtime.object_state_factories,
                object_teardowns=contract_runtime.object_teardowns,
                object_harnesses=contract_runtime.object_harnesses,
            )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            _stderr(f"Cannot resolve {target_name}: {exc}\n")
            return 1

        try:
            effective_contract_checks = (
                _build_explicit_contract_checks(func, cli_contract_names)
                if cli_contract_names
                else list(contract_runtime.contract_checks)
            )
        except ValueError as exc:
            _stderr(f"Unknown contract for {target_name}: {exc}\n")
            return 1

        if contract_runtime.registry_warnings:
            for warning in contract_runtime.registry_warnings:
                _stderr(f"warning: {warning}\n")

        if getattr(func, "__ordeal_requires_factory__", False):
            skip_reason = getattr(func, "__ordeal_skip_reason__", "missing object factory")
            _stderr(f"Cannot verify {target_name}: {skip_reason}.\n")
            return 1

        target_listing: dict[str, Any] | None = None
        try:
            target_rows = _callable_listing_rows(
                module_name,
                targets=[target_name],
                selected_targets=[target_name],
                object_factories=contract_runtime.object_factories,
                object_setups=contract_runtime.object_setups,
                object_scenarios=contract_runtime.object_scenarios,
                object_state_factories=contract_runtime.object_state_factories,
                object_teardowns=contract_runtime.object_teardowns,
                object_harnesses=contract_runtime.object_harnesses,
                contract_checks={
                    _scan_display_name(module_name, target_name): effective_contract_checks
                },
            )
            target_listing = next(
                (
                    row
                    for row in target_rows
                    if row.get("name") == _scan_display_name(module_name, target_name)
                ),
                target_rows[0] if target_rows else None,
            )
        except Exception as exc:
            _stderr(f"warning: target metadata unavailable for {target_name}: {exc}\n")

        if target_listing is not None:
            _stderr(
                "Target metadata: "
                + "  ".join(_render_target_listing_parts(target_listing))
                + "\n"
            )

        _stderr(
            f"Checking {target} explicit contract(s) "
            f"({len(effective_contract_checks)} check(s))...\n"
        )
        violations, details = _evaluate_contract_checks(
            _unwrap(func),
            effective_contract_checks,
        )
        report = {
            "tool": "check",
            "target": target,
            "mode": "explicit_contract",
            "summary": [
                f"Checked {len(effective_contract_checks)} explicit contract(s)",
                f"Target: {resolved_name}",
                f"Violations: {len(details)}",
            ],
            "details": details,
            "suggested_commands": [
                (
                    " ".join(
                        [
                            "ordeal check",
                            target,
                            *[f"--contract {name}" for name in cli_contract_names],
                        ]
                    )
                    if cli_contract_names
                    else f"ordeal check {target} --config ordeal.toml"
                ),
            ],
        }
        if getattr(args, "json", False):
            print(
                _build_agent_envelope_from_report(
                    report,
                    status="findings" if details else "ok",
                    confidence=1.0 if details else 0.0,
                    confidence_basis=("explicit contract evaluation",),
                    blocking_reason=None if not details else None,
                    raw_details={
                        "resolved_target": resolved_name,
                        "config_path": getattr(args, "config", None),
                        "contracts": [
                            {
                                "name": check.name,
                                "summary": check.summary,
                                "kwargs": check.kwargs,
                                "metadata": check.metadata,
                            }
                            for check in effective_contract_checks
                        ],
                        "target_listing": target_listing,
                        "violations": violations,
                        "details": details,
                    },
                ).to_json()
            )
        else:
            if details:
                print(f"{len(details)} explicit contract violation(s):")
                for detail in details:
                    print(f"  FAIL {detail.get('summary', detail.get('name', 'contract'))}")
                    proof = detail.get("proof_bundle", {})
                    lifecycle = proof.get("lifecycle") if isinstance(proof, Mapping) else None
                    if lifecycle:
                        print(f"    lifecycle: {lifecycle}")
                return 1
            print(f"PASS explicit contracts for {resolved_name}")
            if target_listing is not None:
                print(
                    "Target metadata: " + "  ".join(_render_target_listing_parts(target_listing))
                )
            return 0

        return 1 if details else 0

    # Fallback: property/mining mode for plain dotted functions.
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
    if getattr(args, "json", False):

        def _property_detail(item: Any) -> dict[str, Any]:
            return {
                "kind": "property",
                "module": mod_path,
                "function": func_name,
                "qualname": target,
                "name": item.name,
                "summary": f"{item.name} ({item.confidence:.0%})",
                "confidence": item.confidence,
                "holds": item.holds,
                "total": item.total,
                "counterexample": item.counterexample,
            }

        if prop:
            matching = [p for p in result.properties if prop.lower() in p.name.lower()]
            if not matching:
                _stderr(
                    f"\n  Property '{prop}' not found. Available: "
                    f"{', '.join(p.name for p in result.properties if p.total > 0)}\n"
                )
                return 1
            selected = matching
        else:
            contracts = [
                "never None",
                "no NaN",
                "never empty",
                "deterministic",
                "idempotent",
                "finite",
            ]
            selected = [p for p in result.properties if p.total > 0 and p.name in contracts]

        report = {
            "tool": "check",
            "target": target,
            "mode": "property" if prop else "contract",
            "summary": [result.summary()],
            "details": [_property_detail(item) for item in selected],
            "suggested_commands": [f"ordeal check {target} -n {max_examples}"],
        }
        print(
            _build_agent_envelope_from_report(
                report,
                status="findings" if any(not p.universal for p in selected) else "ok",
                confidence=max((p.confidence for p in selected), default=0.0),
                confidence_basis=("mine() property mining",),
                raw_details={
                    "max_examples": max_examples,
                    "properties": [
                        {
                            "name": p.name,
                            "holds": p.holds,
                            "total": p.total,
                            "confidence": p.confidence,
                            "universal": p.universal,
                            "counterexample": p.counterexample,
                        }
                        for p in result.properties
                    ],
                },
            ).to_json()
        )
        return 1 if any(not p.universal for p in selected) else 0

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


def _audit_gap_stub_path(output_dir: str, target_name: str) -> Path:
    """Return the draft gap-stub path for one audit target."""
    safe = target_name.replace(".", "_")
    return Path(output_dir) / f"test_{safe}_gaps.py"


def _function_gap_status_rank(status: str) -> int:
    """Rank function-gap statuses from most to least actionable."""
    if status == "uncovered":
        return 0
    if status == "exploratory":
        return 1
    return 2


def _render_audit_function_gap_stub(result: Any, item: Any) -> str:
    """Render a draft review stub from function-level audit evidence."""
    import inspect

    from ordeal.audit import _func_sig_for_codegen
    from ordeal.auto import _resolve_module

    function_name = str(getattr(item, "name", "")).strip()
    target_name = f"{result.module}.{function_name}"
    status = str(getattr(item, "status", "exploratory"))
    epistemic = str(getattr(item, "epistemic", "inferred"))
    covered_body_lines = int(getattr(item, "covered_body_lines", 0) or 0)
    total_body_lines = int(getattr(item, "total_body_lines", 0) or 0)
    evidence = list(getattr(item, "evidence", []))

    reviewed_signature = f"{function_name}(...)"
    call_expr = f"_ordeal_target.{function_name}(...)"
    try:
        mod = _resolve_module(result.module)
        func = mod
        owner_name = None
        method_name = function_name
        if "." in function_name:
            owner_name, method_name = function_name.rsplit(".", 1)
            for part in owner_name.split("."):
                func = getattr(func, part)
            target_owner = func
            func = getattr(target_owner, method_name)
            if inspect.isclass(target_owner):
                descriptor = inspect.getattr_static(target_owner, method_name)
                if isinstance(descriptor, (staticmethod, classmethod)):
                    call_expr = f"_ordeal_target.{owner_name}.{method_name}(...)"
                else:
                    try:
                        target_owner()
                        call_expr = f"_ordeal_target.{owner_name}().{method_name}(...)"
                    except Exception:
                        call_expr = f"_ordeal_target.{owner_name}.{method_name}(...)"
            else:
                call_expr = f"_ordeal_target.{owner_name}.{method_name}(...)"
        else:
            func = getattr(mod, function_name)
        sig_info = _func_sig_for_codegen(func)
        if sig_info is not None:
            param_names, decls, _call_args, _imports = sig_info
            reviewed_signature = f"{function_name}({', '.join(decls)})"
            placeholders = ", ".join("..." for _ in param_names)
            if "." not in function_name:
                call_expr = (
                    f"_ordeal_target.{function_name}({placeholders})"
                    if placeholders
                    else f"_ordeal_target.{function_name}()"
                )
        else:
            signature = inspect.signature(func)
            reviewed_signature = f"{function_name}{signature}"
            if "." not in function_name:
                call_expr = (
                    f"_ordeal_target.{function_name}()"
                    if len(signature.parameters) == 0
                    else f"_ordeal_target.{function_name}(...)"
                )
    except Exception:
        pass

    lines = [
        f'"""Draft review stubs for audit gaps in {target_name}.',
        "",
        "Generated by ordeal.",
        "These are review notes, not runnable regressions yet.",
        f"Epistemic status: {status} [{epistemic}]",
        f"Reviewed signature: {reviewed_signature}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"import {result.module} as _ordeal_target",
        "",
        "# Evidence summary:",
    ]
    if total_body_lines > 0:
        lines.append(f"# - covered body lines: {covered_body_lines}/{total_body_lines}")
    if evidence:
        for entry in evidence[:5]:
            kind = entry.get("kind", "evidence")
            epistemic_note = entry.get("epistemic")
            detail = entry.get("detail", "")
            suffix = f" [{epistemic_note}]" if epistemic_note else ""
            detail = f"# - {kind}{suffix}: {detail}"
            lines.append(detail.rstrip())
    else:
        lines.append("# - no direct evidence recorded")
    lines.extend(
        [
            "",
            "# Why this exists:",
            (
                f"# - write a direct regression for {target_name}"
                if status == "uncovered"
                else "# - replace indirect coverage with a direct regression"
            ),
            (
                "# - the current tests only reach this behavior indirectly"
                if status == "exploratory"
                else "# - there is no effective test coverage yet"
            ),
            "# - keep the assertion small, specific, and reviewable",
            "",
            "# Suggested starting point:",
            f"# def test_{target_name.replace('.', '_')}_gap() -> None:",
            (
                f"#     # TODO: call {reviewed_signature} with a concrete input "
                "that matches the evidence."
            ),
            f"#     result = {call_expr}",
            "#     # TODO: assert the intended contract, not just that the call succeeds.",
            "#     assert ...",
            "",
        ]
    )
    return "\n".join(lines)


def _function_gap_detail_items(
    result: Any,
    *,
    include_exploratory_function_gaps: bool,
) -> list[dict[str, Any]]:
    """Normalize function audits into finding-style detail items."""
    fixture_completeness = float(getattr(result, "fixture_completeness", 0.0))
    function_audits = [
        item
        for item in getattr(result, "function_audits", [])
        if getattr(item, "status", "") != "exercised"
    ]
    function_audits.sort(
        key=lambda item: (
            _function_gap_status_rank(str(getattr(item, "status", ""))),
            -int(getattr(item, "covered_body_lines", 0) or 0),
            str(getattr(item, "name", "")),
        )
    )
    details: list[dict[str, Any]] = []
    for item in function_audits:
        status = str(getattr(item, "status", ""))
        if status == "exploratory" and not include_exploratory_function_gaps:
            continue
        summary = (
            f"{item.name} is only indirectly exercised by current tests"
            if status == "exploratory"
            else f"{item.name} has no effective tests yet"
        )
        details.append(
            {
                "kind": "function_gap",
                "category": "test_strength_gap",
                "summary": summary,
                "module": result.module,
                "function": item.name,
                "qualname": f"{result.module}.{item.name}",
                "details": {
                    "status": status,
                    "priority": _function_gap_status_rank(status),
                    "fixture_completeness": fixture_completeness,
                    "epistemic": getattr(item, "epistemic", ""),
                    "covered_body_lines": getattr(item, "covered_body_lines", 0),
                    "total_body_lines": getattr(item, "total_body_lines", 0),
                    "evidence": list(getattr(item, "evidence", [])),
                },
            }
        )
    return details


def _direct_test_gate_payload(results: Sequence[Any]) -> dict[str, Any]:
    """Summarize direct-test gate status from function-level audit evidence."""
    exploratory: list[str] = []
    uncovered: list[str] = []
    for result in results:
        direct_gaps = getattr(result, "direct_test_gaps", None)
        items = (
            list(direct_gaps)
            if direct_gaps is not None
            else [
                item
                for item in getattr(result, "function_audits", [])
                if getattr(item, "status", "") != "exercised"
            ]
        )
        for item in items:
            qualname = f"{result.module}.{getattr(item, 'name', '')}".rstrip(".")
            status = str(getattr(item, "status", ""))
            if status == "exploratory":
                exploratory.append(qualname)
            elif status == "uncovered":
                uncovered.append(qualname)
    return {
        "passed": not exploratory and not uncovered,
        "exploratory": exploratory,
        "uncovered": uncovered,
    }


def _direct_test_gate_summary(gate: Mapping[str, Any]) -> str:
    """Render a compact direct-test gate summary."""
    exploratory = list(gate.get("exploratory", []))
    uncovered = list(gate.get("uncovered", []))
    if not exploratory and not uncovered:
        return "Direct test gate: PASS"
    parts: list[str] = []
    if exploratory:
        parts.append(f"{len(exploratory)} exploratory")
    if uncovered:
        parts.append(f"{len(uncovered)} uncovered")
    return f"Direct test gate: FAIL ({', '.join(parts)})"


def _write_audit_gap_stubs(
    audit_results: Sequence[Any],
    *,
    output_dir: str,
    include_exploratory_function_gaps: bool = False,
) -> list[dict[str, Any]]:
    """Write draft audit gap stubs from mutation gaps and function evidence."""
    written: list[dict[str, Any]] = []
    written_targets: set[str] = set()
    for result in audit_results:
        for item in result.mutation_gap_stubs:
            content = str(item.get("content", "")).strip()
            target_name = str(item.get("target", "")).strip()
            if not content or not target_name:
                continue
            path = _audit_gap_stub_path(output_dir, target_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            if existing != content + "\n":
                path.write_text(content + "\n", encoding="utf-8")
            written_targets.add(target_name)
            written.append(
                {
                    "module": result.module,
                    "target": target_name,
                    "path": _display_path(path),
                    "source": "mutation_gap",
                }
            )
        function_audits = [
            item
            for item in getattr(result, "function_audits", [])
            if getattr(item, "status", "") != "exercised"
        ]
        function_audits.sort(
            key=lambda item: (
                _function_gap_status_rank(str(getattr(item, "status", ""))),
                -int(getattr(item, "covered_body_lines", 0) or 0),
                str(getattr(item, "name", "")),
            )
        )
        for item in function_audits:
            status = str(getattr(item, "status", ""))
            if status not in {"exploratory", "uncovered"}:
                continue
            if status == "exploratory" and not include_exploratory_function_gaps:
                continue
            target_name = f"{result.module}.{getattr(item, 'name', '')}".strip()
            if not target_name or target_name in written_targets:
                continue
            content = _render_audit_function_gap_stub(result, item).strip()
            path = _audit_gap_stub_path(output_dir, target_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            if existing != content + "\n":
                path.write_text(content + "\n", encoding="utf-8")
            written_targets.add(target_name)
            written.append(
                {
                    "module": result.module,
                    "target": target_name,
                    "path": _display_path(path),
                    "source": "function_audit",
                    "status": status,
                    "epistemic": str(getattr(item, "epistemic", "")),
                }
            )
    return written


def _parse_named_override_spec(raw: str) -> tuple[str, list[str]]:
    """Parse a repeatable ``NAME=value1,value2`` override spec."""
    if "=" not in raw:
        raise argparse.ArgumentTypeError("Expected NAME=value1,value2")
    name, values = raw.split("=", 1)
    name = name.strip()
    items = [item.strip() for item in values.split(",") if item.strip()]
    if not name or not items:
        raise argparse.ArgumentTypeError("Expected NAME=value1,value2")
    return name, items


def _merge_unique_strings(*groups: Sequence[str] | None) -> list[str]:
    """Merge string sequences while preserving order and uniqueness."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not group:
            continue
        for item in group:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _named_override_specs_to_map(
    specs: Sequence[tuple[str, Sequence[str]]] | None,
) -> dict[str, list[str]]:
    """Convert repeatable CLI override specs to a mapping."""
    merged: dict[str, list[str]] = {}
    for name, values in specs or ():
        bucket = merged.setdefault(name, [])
        for value in values:
            if value not in bucket:
                bucket.append(value)
    return merged


def _merge_named_overrides(
    *groups: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    """Merge named override mappings while preserving order and uniqueness."""
    merged: dict[str, list[str]] = {}
    for group in groups:
        if not group:
            continue
        for name, values in group.items():
            bucket = merged.setdefault(str(name), [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
    return merged


def _config_object_specs_for_module(cfg: OrdealConfig, module_name: str) -> list[Any]:
    """Return shared object configs that belong to *module_name*."""
    return [spec for spec in cfg.objects if _target_module_name(spec.target) == module_name]


def _object_runtime_maps(
    object_specs: Sequence[Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, str],
]:
    """Resolve configured object factories, state hooks, and scenarios."""
    factories: dict[str, Any] = {}
    setups: dict[str, Any] = {}
    scenarios: dict[str, Any] = {}
    state_factories: dict[str, Any] = {}
    teardowns: dict[str, Any] = {}
    harnesses: dict[str, str] = {}

    def _resolve_scenario_item(item: object) -> object:
        if isinstance(item, Mapping):
            return dict(item)
        return _resolve_symbol_path(str(item))

    for spec in object_specs:
        target = str(getattr(spec, "target"))
        factory_path = getattr(spec, "factory", None)
        setup_path = getattr(spec, "setup", None)
        state_factory_path = getattr(spec, "state_factory", None)
        teardown_path = getattr(spec, "teardown", None)
        harness = str(getattr(spec, "harness", "fresh") or "fresh").strip().lower()
        scenario_paths = list(getattr(spec, "scenarios", []) or [])
        if factory_path:
            factories[target] = _resolve_symbol_path(str(factory_path))
        if setup_path:
            setups[target] = _resolve_symbol_path(str(setup_path))
        if state_factory_path:
            state_factories[target] = _resolve_symbol_path(str(state_factory_path))
        if teardown_path:
            teardowns[target] = _resolve_symbol_path(str(teardown_path))
        harnesses[target] = harness if harness in {"fresh", "stateful"} else "fresh"
        if scenario_paths:
            resolved_hooks = tuple(_resolve_scenario_item(path) for path in scenario_paths)
            hook: object = resolved_hooks[0] if len(resolved_hooks) == 1 else resolved_hooks
            with contextlib.suppress(Exception):
                setattr(hook, "__ordeal_scenario_count__", len(resolved_hooks))
            scenarios[target] = hook
    return factories, setups, scenarios, state_factories, teardowns, harnesses


def _config_contract_checks_for_module(
    cfg: OrdealConfig,
    module_name: str,
) -> dict[str, list[Any]]:
    """Resolve configured semantic contract probes for *module_name*."""
    from ordeal.auto import ContractCheck, builtin_contract_check

    checks: dict[str, list[Any]] = {}
    for spec in cfg.contracts:
        target = str(spec.target)
        if _target_module_name(target) != module_name:
            continue
        display_name = _scan_display_name(module_name, target)
        bucket = checks.setdefault(display_name, [])
        for check_name in spec.checks:
            resolved_name = str(check_name)
            try:
                builtin_kwargs = dict(spec.kwargs)
                lifecycle_phase = (
                    str(getattr(spec, "phase", None) or builtin_kwargs.pop("phase", "")) or None
                )
                followup_phases = [
                    str(item)
                    for item in (
                        list(getattr(spec, "followup_phases", []) or [])
                        or list(builtin_kwargs.pop("followup_phases", []) or [])
                    )
                    if str(item).strip()
                ] or None
                fault = str(
                    getattr(spec, "fault", None) or builtin_kwargs.pop("fault", "raise") or "raise"
                )
                handler_name = (
                    str(
                        getattr(spec, "handler_name", None)
                        or builtin_kwargs.pop("handler_name", "")
                    )
                    or None
                )
                bucket.append(
                    builtin_contract_check(
                        resolved_name,
                        kwargs=builtin_kwargs,
                        tracked_params=list(spec.tracked_params),
                        protected_keys=list(spec.protected_keys),
                        env_param=spec.env_param,
                        phase=lifecycle_phase,
                        followup_phases=followup_phases,
                        fault=fault,
                        handler_name=handler_name,
                    )
                )
            except ValueError:
                predicate = _resolve_symbol_path(resolved_name)
                summary = inspect.getdoc(predicate)
                bucket.append(
                    ContractCheck(
                        name=resolved_name.rsplit(":", 1)[-1].rsplit(".", 1)[-1],
                        predicate=predicate,
                        kwargs=dict(spec.kwargs),
                        summary=summary.splitlines()[0] if summary else None,
                    )
                )
    return checks


def _resolve_scan_runtime_defaults(
    target: str,
    *,
    requested_examples: int,
    allow_config_override: bool = False,
) -> ScanRuntimeDefaults:
    """Load fixture registries and optional ``[[scan]]`` defaults for *target*."""
    module_name = _scan_base_module(target)
    warnings: list[str] = []

    values: dict[str, Any] = {
        "max_examples": requested_examples,
        "mode": "coverage_gap",
        "seed_from_tests": True,
        "seed_from_fixtures": True,
        "seed_from_docstrings": True,
        "seed_from_code": True,
        "seed_from_call_sites": True,
        "treat_any_as_weak": True,
        "proof_bundles": True,
        "require_replayable": True,
        "auto_contracts": [],
        "min_contract_fit": 0.55,
        "min_reachability": 0.45,
        "min_realism": 0.55,
        "min_fixture_completeness": 0.0,
        "fixtures": None,
        "targets": [],
        "include_private": False,
        "object_factories": None,
        "object_setups": None,
        "object_scenarios": None,
        "object_state_factories": None,
        "object_teardowns": None,
        "object_harnesses": None,
        "contract_checks": {},
        "expected_failures": [],
        "expected_preconditions": {},
        "ignore_contracts": [],
        "ignore_properties": [],
        "ignore_relations": [],
        "contract_overrides": {},
        "expected_properties": {},
        "expected_relations": {},
        "property_overrides": {},
        "relation_overrides": {},
    }

    def _build_defaults() -> ScanRuntimeDefaults:
        return ScanRuntimeDefaults(
            max_examples=int(values["max_examples"]),
            mode=str(values["mode"]),
            seed_from_tests=bool(values["seed_from_tests"]),
            seed_from_fixtures=bool(values["seed_from_fixtures"]),
            seed_from_docstrings=bool(values["seed_from_docstrings"]),
            seed_from_code=bool(values["seed_from_code"]),
            seed_from_call_sites=bool(values["seed_from_call_sites"]),
            treat_any_as_weak=bool(values["treat_any_as_weak"]),
            proof_bundles=bool(values["proof_bundles"]),
            require_replayable=bool(values["require_replayable"]),
            auto_contracts=list(values["auto_contracts"]),
            min_contract_fit=float(values["min_contract_fit"]),
            min_reachability=float(values["min_reachability"]),
            min_realism=float(values["min_realism"]),
            min_fixture_completeness=float(values["min_fixture_completeness"]),
            fixtures=values["fixtures"],
            targets=list(values["targets"]),
            include_private=bool(values["include_private"]),
            object_factories=values["object_factories"],
            object_setups=values["object_setups"],
            object_scenarios=values["object_scenarios"],
            object_state_factories=values["object_state_factories"],
            object_teardowns=values["object_teardowns"],
            object_harnesses=values["object_harnesses"],
            contract_checks=dict(values["contract_checks"]),
            expected_failures=list(values["expected_failures"]),
            expected_preconditions={
                str(name): list(items)
                for name, items in dict(values["expected_preconditions"]).items()
            },
            registry_warnings=list(warnings),
            ignore_contracts=list(values["ignore_contracts"]),
            ignore_properties=list(values["ignore_properties"]),
            ignore_relations=list(values["ignore_relations"]),
            contract_overrides={
                str(name): list(items)
                for name, items in dict(values["contract_overrides"]).items()
            },
            expected_properties={
                str(name): list(items)
                for name, items in dict(values["expected_properties"]).items()
            },
            expected_relations={
                str(name): list(items)
                for name, items in dict(values["expected_relations"]).items()
            },
            property_overrides={
                str(name): list(items)
                for name, items in dict(values["property_overrides"]).items()
            },
            relation_overrides={
                str(name): list(items)
                for name, items in dict(values["relation_overrides"]).items()
            },
        )

    config_path = Path("ordeal.toml")
    if not config_path.exists():
        return _build_defaults()

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ConfigError):
        warnings.extend(_load_fixture_registry_warnings())
        return _build_defaults()

    warnings.extend(_load_fixture_registry_warnings(shared_modules=cfg.fixtures.registries))
    shared_object_specs = _config_object_specs_for_module(cfg, module_name)
    try:
        (
            values["object_factories"],
            values["object_setups"],
            values["object_scenarios"],
            values["object_state_factories"],
            values["object_teardowns"],
            values["object_harnesses"],
        ) = _object_runtime_maps(shared_object_specs)
    except Exception as exc:
        warnings.append(f"object factory config failed for {module_name}: {exc}")
        (
            values["object_factories"],
            values["object_setups"],
            values["object_scenarios"],
        ) = ({}, {}, {})
        (
            values["object_state_factories"],
            values["object_teardowns"],
            values["object_harnesses"],
        ) = (
            {},
            {},
            {},
        )
    try:
        values["contract_checks"] = _config_contract_checks_for_module(cfg, module_name)
    except Exception as exc:
        warnings.append(f"contract config failed for {module_name}: {exc}")
        values["contract_checks"] = {}

    match = next((entry for entry in cfg.scan if entry.module == module_name), None)
    if match is None:
        return _build_defaults()

    if allow_config_override:
        values["max_examples"] = match.max_examples
    values["mode"] = match.mode
    values["seed_from_tests"] = bool(match.seed_from_tests)
    values["seed_from_fixtures"] = bool(match.seed_from_fixtures)
    values["seed_from_docstrings"] = bool(match.seed_from_docstrings)
    values["seed_from_code"] = bool(match.seed_from_code)
    values["seed_from_call_sites"] = bool(match.seed_from_call_sites)
    values["treat_any_as_weak"] = bool(match.treat_any_as_weak)
    values["proof_bundles"] = bool(match.proof_bundles)
    values["require_replayable"] = bool(match.require_replayable)
    values["auto_contracts"] = list(match.auto_contracts)
    values["min_contract_fit"] = float(match.min_contract_fit)
    values["min_reachability"] = float(match.min_reachability)
    values["min_realism"] = float(getattr(match, "min_realism", 0.55))
    values["min_fixture_completeness"] = float(getattr(match, "min_fixture_completeness", 0.0))
    values["targets"] = list(match.targets)
    values["include_private"] = bool(match.include_private)
    values["fixtures"] = _parse_scan_fixture_specs(match.fixtures)
    values["expected_failures"] = list(match.expected_failures)
    values["expected_preconditions"] = {
        str(name): list(items) for name, items in match.expected_preconditions.items()
    }
    warnings.extend(_load_fixture_registry_warnings(extra_modules=match.fixture_registries))
    values["ignore_contracts"] = list(match.ignore_contracts)
    values["ignore_properties"] = list(match.ignore_properties)
    values["ignore_relations"] = list(match.ignore_relations)
    values["contract_overrides"] = {
        str(name): list(items) for name, items in match.contract_overrides.items()
    }
    values["expected_properties"] = {
        str(name): list(items) for name, items in match.expected_properties.items()
    }
    values["expected_relations"] = {
        str(name): list(items) for name, items in match.expected_relations.items()
    }
    values["property_overrides"] = {
        str(name): list(items) for name, items in match.property_overrides.items()
    }
    values["relation_overrides"] = {
        str(name): list(items) for name, items in match.relation_overrides.items()
    }
    return _build_defaults()


def _resolve_check_runtime_defaults(
    target: str,
    *,
    config_path: str | None = None,
) -> CheckRuntimeDefaults:
    """Load config-backed object hooks and explicit contracts for *target*."""
    module_name = _target_module_name(target)
    warnings: list[str] = []
    config = _load_optional_config(config_path)
    if config is None:
        return CheckRuntimeDefaults()

    try:
        object_specs = _config_object_specs_for_module(config, module_name)
        (
            object_factories,
            object_setups,
            object_scenarios,
            object_state_factories,
            object_teardowns,
            object_harnesses,
        ) = _object_runtime_maps(object_specs)
    except Exception as exc:
        warnings.append(f"object config failed for {module_name}: {exc}")
        object_factories, object_setups, object_scenarios = {}, {}, {}
        object_state_factories, object_teardowns, object_harnesses = {}, {}, {}

    try:
        contract_checks = _config_contract_checks_for_module(config, module_name)
    except Exception as exc:
        warnings.append(f"contract config failed for {module_name}: {exc}")
        contract_checks = {}

    display_name = _scan_display_name(module_name, target)
    return CheckRuntimeDefaults(
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
        contract_checks=list(contract_checks.get(display_name, [])),
        registry_warnings=warnings,
    )


def _check_contract_names(args: argparse.Namespace) -> list[str]:
    """Normalize repeated ``--contract`` CLI values."""
    names: list[str] = []
    for raw in list(getattr(args, "contract", []) or []):
        name = str(raw).strip()
        if name and name not in names:
            names.append(name)
    return names


def _scan_target_selectors(args: argparse.Namespace) -> list[str]:
    """Normalize repeated ``--target`` selector values for ``scan``."""
    selectors: list[str] = []
    for raw in list(getattr(args, "scan_targets", []) or []):
        selector = str(raw).strip()
        if selector and selector not in selectors:
            selectors.append(selector)
    return selectors


_BROAD_PACKAGE_SCAN_DEFAULT_MAX_EXAMPLES = 5
_PACKAGE_ROOT_ORCHESTRATION_PREFIXES = (
    "audit",
    "benchmark",
    "chaos_for",
    "explore",
    "fuzz",
    "mine",
    "mutate",
    "replay",
    "scan",
    "verify",
)
_PACKAGE_ROOT_HELPER_NAMES = {
    "auto_configure",
    "always",
    "bounded",
    "catalog",
    "declare",
    "finite",
    "reachable",
    "report",
    "sometimes",
    "unreachable",
}
_PACKAGE_ROOT_HEAVY_MODULE_PREFIXES = (
    "ordeal.audit",
    "ordeal.cli",
    "ordeal.explore",
    "ordeal.mine",
    "ordeal.mutations",
    "ordeal.scaling",
    "ordeal.state",
    "ordeal.trace",
)


def _is_package_module(module_name: str) -> bool:
    """Return whether *module_name* resolves to a package root module."""
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return False
    if getattr(mod, "__path__", None):
        return True
    module_file = getattr(mod, "__file__", "") or ""
    return Path(module_file).name == "__init__.py"


def _package_root_scan_priority(row: Mapping[str, Any]) -> tuple[float, str, str]:
    """Return a descending priority key for representative package-root scan targets."""
    name = str(row.get("name", "")).strip()
    source_module = str(row.get("source_module") or row.get("module") or "")
    score = 0.0
    if bool(row.get("runnable", False)):
        score += 50.0
    if not bool(row.get("factory_required", False)):
        score += 8.0
    if bool(row.get("factory_configured", False)):
        score += 2.0
    if str(row.get("kind", "")) == "function":
        score += 2.0
    if str(row.get("async", "")) == "async":
        score -= 1.0
    if name in _PACKAGE_ROOT_HELPER_NAMES:
        score += 10.0
    if name in {"chaos_test"}:
        score += 4.0
    if any(name.startswith(prefix) for prefix in _PACKAGE_ROOT_ORCHESTRATION_PREFIXES) or name in {
        "generate_starter_tests",
        "init_project",
    }:
        score -= 12.0
    if source_module.startswith(_PACKAGE_ROOT_HEAVY_MODULE_PREFIXES):
        score -= 6.0
    if any(
        token in name
        for token in (
            "classify",
            "extract",
            "filter",
            "prove",
            "fit",
            "analyze",
            "validate",
        )
    ):
        score += 6.0
    if source_module.startswith("hypothesis_temporary_module_"):
        score -= 20.0
    if name.startswith("register_"):
        score -= 8.0
    if name == "builtin_contract_check":
        score -= 10.0
    if name.endswith("_contract"):
        # Contract-builder exports are useful when targeted explicitly, but
        # broad package-root scans should prefer concrete implementation helpers.
        score -= 4.0
    if name.endswith("_strategy"):
        score -= 6.0
    if name in {"activate", "deactivate", "set_seed"}:
        score -= 4.0
    return (-score, source_module, name)


def _build_explicit_contract_checks(func: Any, names: Sequence[str]) -> list[Any]:
    """Build direct built-in contract checks for one resolved callable."""
    from ordeal.auto import _boundary_smoke_inputs, _unwrap, builtin_contract_check

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        tracked_params: list[str] = []
    else:
        tracked_params = [
            param.name for param in sig.parameters.values() if param.name not in {"self", "cls"}
        ]

    phase = (
        str(getattr(func, "__ordeal_lifecycle_phase__", "") or "").strip()
        or str(getattr(_unwrap(func), "__ordeal_lifecycle_phase__", "") or "").strip()
        or None
    )
    smoke_inputs = _boundary_smoke_inputs(func)
    base_kwargs = dict(smoke_inputs[0]) if smoke_inputs else {}
    followup_phases = ("cleanup", "teardown") if phase in {"setup", "rollout"} else None
    checks: list[Any] = []
    for name in names:
        checks.append(
            builtin_contract_check(
                name,
                kwargs=dict(base_kwargs),
                tracked_params=tracked_params,
                phase=phase,
                followup_phases=followup_phases,
            )
        )
    return checks


def _target_harness_mode(
    target: str,
    object_harnesses: Mapping[str, str] | None,
) -> str | None:
    """Resolve one configured harness mode for a scan/audit/mutation target."""
    if not object_harnesses:
        return None
    module_name = _target_module_name(target)
    display_name = _scan_display_name(module_name, target)
    if "." not in display_name:
        return None
    owner_name = display_name.rsplit(".", 1)[0]
    return object_harnesses.get(f"{module_name}:{owner_name}")


def _mutation_contract_context_for_target(
    cfg: OrdealConfig | None,
    target: str,
) -> dict[str, Any]:
    """Build mutation-ranking contract metadata for one configured target."""
    if cfg is None:
        return {}

    from ordeal.mutations import mutation_contract_context

    module_name = _target_module_name(target)
    try:
        checks_by_target = _config_contract_checks_for_module(cfg, module_name)
    except Exception:
        checks_by_target = {}
    try:
        object_specs = _config_object_specs_for_module(cfg, module_name)
        *_unused, object_harnesses = _object_runtime_maps(object_specs)
    except Exception:
        object_harnesses = {}
    display_name = _scan_display_name(module_name, target)
    return mutation_contract_context(
        checks_by_target.get(display_name, ()),
        harness=_target_harness_mode(target, object_harnesses),
    )


def _run_configured_scans(
    scan_entries: Sequence[Any],
    *,
    cfg: OrdealConfig | None = None,
    shared_fixture_registries: Sequence[str] = (),
    verbose: bool = True,
) -> int:
    """Execute ``[[scan]]`` entries from config through the library scan API."""
    from ordeal.auto import scan_module

    exit_code = 0
    for warning in _load_fixture_registry_warnings(shared_modules=shared_fixture_registries):
        _stderr(f"warning: {warning}\n")
    for scan_cfg in scan_entries:
        warnings = _load_fixture_registry_warnings(extra_modules=scan_cfg.fixture_registries)
        for warning in warnings:
            _stderr(f"warning: {warning}\n")
        fixtures = _parse_scan_fixture_specs(scan_cfg.fixtures)
        if verbose:
            _stderr(f"Scanning {scan_cfg.module} from [[scan]]...\n")
        object_factories: dict[str, Any] = {}
        object_setups: dict[str, Any] = {}
        object_scenarios: dict[str, Any] = {}
        object_state_factories: dict[str, Any] = {}
        object_teardowns: dict[str, Any] = {}
        object_harnesses: dict[str, str] = {}
        contract_checks: dict[str, list[Any]] = {}
        if cfg is not None:
            try:
                (
                    object_factories,
                    object_setups,
                    object_scenarios,
                    object_state_factories,
                    object_teardowns,
                    object_harnesses,
                ) = _object_runtime_maps(_config_object_specs_for_module(cfg, scan_cfg.module))
            except Exception as exc:
                _stderr(f"warning: object factory config failed for {scan_cfg.module}: {exc}\n")
            try:
                contract_checks = _config_contract_checks_for_module(cfg, scan_cfg.module)
            except Exception as exc:
                _stderr(f"warning: contract config failed for {scan_cfg.module}: {exc}\n")
        scan_kwargs: dict[str, Any] = {
            "max_examples": scan_cfg.max_examples,
            "mode": scan_cfg.mode,
            "seed_from_tests": scan_cfg.seed_from_tests,
            "seed_from_fixtures": scan_cfg.seed_from_fixtures,
            "seed_from_docstrings": scan_cfg.seed_from_docstrings,
            "seed_from_code": scan_cfg.seed_from_code,
            "seed_from_call_sites": scan_cfg.seed_from_call_sites,
            "treat_any_as_weak": scan_cfg.treat_any_as_weak,
            "proof_bundles": scan_cfg.proof_bundles,
            "auto_contracts": scan_cfg.auto_contracts,
            "require_replayable": scan_cfg.require_replayable,
            "min_contract_fit": scan_cfg.min_contract_fit,
            "min_reachability": scan_cfg.min_reachability,
            "min_realism": getattr(scan_cfg, "min_realism", 0.55),
            "targets": scan_cfg.targets,
            "include_private": scan_cfg.include_private,
            "fixtures": fixtures,
            "object_factories": object_factories,
            "object_setups": object_setups,
            "object_scenarios": object_scenarios,
            "object_state_factories": object_state_factories,
            "object_teardowns": object_teardowns,
            "object_harnesses": object_harnesses,
            "expected_failures": scan_cfg.expected_failures,
            "expected_preconditions": scan_cfg.expected_preconditions,
            "ignore_contracts": scan_cfg.ignore_contracts,
            "ignore_properties": scan_cfg.ignore_properties,
            "ignore_relations": scan_cfg.ignore_relations,
            "contract_overrides": scan_cfg.contract_overrides,
            "expected_properties": scan_cfg.expected_properties,
            "expected_relations": scan_cfg.expected_relations,
            "property_overrides": scan_cfg.property_overrides,
            "relation_overrides": scan_cfg.relation_overrides,
            "contract_checks": contract_checks,
        }
        result = scan_module(scan_cfg.module, **scan_kwargs)
        print(result.summary())
        if not result.passed:
            exit_code = 1
    return exit_code


def _cmd_scan(args: argparse.Namespace) -> int:
    """Run unified exploratory analysis over one module or explicit callable target."""
    from ordeal.state import explore

    scan_target = args.target
    module_name = _scan_base_module(scan_target)
    allow_config_override = args.max_examples == 50
    runtime_defaults = _resolve_scan_runtime_defaults(
        scan_target,
        requested_examples=args.max_examples,
        allow_config_override=allow_config_override,
    )
    scan_mode = str(getattr(args, "mode", None) or runtime_defaults.mode)
    scan_seed_from_tests = (
        runtime_defaults.seed_from_tests
        if getattr(args, "seed_from_tests", None) is None
        else bool(args.seed_from_tests)
    )
    scan_min_contract_fit = float(
        getattr(args, "min_contract_fit", None)
        if getattr(args, "min_contract_fit", None) is not None
        else runtime_defaults.min_contract_fit
    )
    scan_min_reachability = float(
        getattr(args, "min_reachability", None)
        if getattr(args, "min_reachability", None) is not None
        else runtime_defaults.min_reachability
    )
    scan_min_realism = float(
        getattr(args, "min_realism", None)
        if getattr(args, "min_realism", None) is not None
        else runtime_defaults.min_realism
    )
    explicit_target = ":" in scan_target
    cli_target_selectors = _scan_target_selectors(args)
    if explicit_target and cli_target_selectors:
        _stderr("Cannot combine an explicit callable target with --target selectors.\n")
        return 2
    scan_targets = (
        [scan_target]
        if explicit_target
        else list(cli_target_selectors or runtime_defaults.targets)
    )
    inc_private = bool(getattr(args, "include_private", False) or runtime_defaults.include_private)
    scan_ignore_properties = _merge_unique_strings(
        runtime_defaults.ignore_properties,
        getattr(args, "ignore_properties", None),
    )
    scan_ignore_relations = _merge_unique_strings(
        runtime_defaults.ignore_relations,
        getattr(args, "ignore_relations", None),
    )
    scan_property_overrides = _merge_named_overrides(
        runtime_defaults.property_overrides,
        _named_override_specs_to_map(getattr(args, "cli_property_overrides", None)),
    )
    scan_relation_overrides = _merge_named_overrides(
        runtime_defaults.relation_overrides,
        _named_override_specs_to_map(getattr(args, "cli_relation_overrides", None)),
    )
    try:
        scan_target_rows = _callable_listing_rows(
            module_name,
            targets=[scan_target] if explicit_target else None,
            selected_targets=scan_targets,
            include_private=inc_private,
            object_factories=runtime_defaults.object_factories,
            object_setups=runtime_defaults.object_setups,
            object_scenarios=runtime_defaults.object_scenarios,
            object_state_factories=runtime_defaults.object_state_factories,
            object_teardowns=runtime_defaults.object_teardowns,
            object_harnesses=runtime_defaults.object_harnesses,
            contract_checks=runtime_defaults.contract_checks,
        )
    except Exception as exc:
        scan_target_rows = []
        if getattr(args, "list_targets", False):
            if args.json:
                print(
                    _build_blocked_agent_envelope(
                        tool="scan",
                        target=scan_target,
                        summary="cannot resolve callable target metadata",
                        blocking_reason=f"target metadata resolution failed: {exc}",
                        raw_details={"target": scan_target, "error": str(exc)},
                    ).to_json()
                )
                return 1
            _stderr(f"Target metadata resolution failed: {exc}\n")
            return 1
    selected_scan_rows = [
        row for row in scan_target_rows if bool(row.get("selected", True))
    ] or scan_target_rows
    sampling: dict[str, Any] | None = None
    if (
        not explicit_target
        and not cli_target_selectors
        and not runtime_defaults.targets
        and not getattr(args, "list_targets", False)
    ):
        sampling = _package_root_scan_sample(module_name, selected_scan_rows)
        if sampling is not None:
            sampled_targets = list(sampling.get("targets", ()))
            sampled_names = set(sampled_targets)
            selected_scan_rows = [
                row
                for row in selected_scan_rows
                if str(row.get("name", "")).strip() in sampled_names
            ]
            scan_targets = sampled_targets
    scan_notes: list[str] = []
    scan_max_examples = int(runtime_defaults.max_examples)
    scan_seed_from_call_sites = runtime_defaults.seed_from_call_sites
    broad_package_root_scan = sampling is not None
    if sampling is not None:
        scan_notes.append(
            "Package-root scan sampled "
            f"{sampling['sampled']}/{sampling['total_runnable']} runnable exports "
            f"across {sampling['source_modules']} source module(s); "
            "use --list-targets or --target for exhaustive coverage."
        )
    if broad_package_root_scan and scan_seed_from_call_sites:
        scan_seed_from_call_sites = False
        scan_notes.append(
            "Broad package-root scan disabled call-site seed mining for speed; "
            "use --target for deeper realism."
        )
    if (
        broad_package_root_scan
        and args.max_examples == 50
        and runtime_defaults.max_examples == 50
        and scan_max_examples > _BROAD_PACKAGE_SCAN_DEFAULT_MAX_EXAMPLES
    ):
        scan_max_examples = _BROAD_PACKAGE_SCAN_DEFAULT_MAX_EXAMPLES
        scan_notes.append(
            "Broad package-root scan capped max_examples to "
            f"{scan_max_examples} per target; pass -n or use --target for a deeper scan."
        )
    if getattr(args, "list_targets", False):
        groups = [{"module": module_name, "targets": scan_target_rows}]
        if args.json:
            print(
                _build_target_listing_envelope(
                    tool="scan",
                    target=scan_target,
                    groups=groups,
                    warnings=runtime_defaults.registry_warnings,
                ).to_json()
            )
        else:
            print(
                _render_target_listing_text(
                    f"Callable targets for {scan_target}",
                    groups,
                    warnings=runtime_defaults.registry_warnings,
                )
            )
        return 0
    if selected_scan_rows and (
        blocking_reason := _blocked_callable_listing_reason(
            selected_scan_rows,
            threshold=runtime_defaults.min_fixture_completeness,
        )
    ):
        if args.json:
            print(
                _build_blocked_agent_envelope(
                    tool="scan",
                    target=scan_target,
                    summary="scan blocked before exploration",
                    blocking_reason=blocking_reason,
                    suggested_commands=(f"ordeal scan {scan_target} --list-targets",),
                    raw_details={
                        "target": scan_target,
                        "module": module_name,
                        "targets": selected_scan_rows,
                        "warnings": list(runtime_defaults.registry_warnings),
                    },
                ).to_json()
            )
            return 1
        for warning in runtime_defaults.registry_warnings:
            _stderr(f"warning: {warning}\n")
        _stderr(f"Scan blocked: {blocking_reason}\n")
        _stderr(f"  Inspect targets with: ordeal scan {scan_target} --list-targets\n")
        return 1
    if not args.json:
        _stderr(f"Scanning {scan_target} (seed={args.seed})...\n")
        for warning in runtime_defaults.registry_warnings:
            _stderr(f"warning: {warning}\n")
        for note in scan_notes:
            _stderr(f"note: {note}\n")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        state = explore(
            module_name,
            seed=args.seed,
            max_examples=scan_max_examples,
            workers=args.workers,
            time_limit=args.time_limit,
            include_private=inc_private,
            scan_targets=scan_targets,
            scan_fixtures=runtime_defaults.fixtures,
            scan_object_factories=runtime_defaults.object_factories,
            scan_object_setups=runtime_defaults.object_setups,
            scan_object_scenarios=runtime_defaults.object_scenarios,
            scan_object_state_factories=runtime_defaults.object_state_factories,
            scan_object_teardowns=runtime_defaults.object_teardowns,
            scan_object_harnesses=runtime_defaults.object_harnesses,
            scan_expected_failures=runtime_defaults.expected_failures,
            scan_expected_preconditions=runtime_defaults.expected_preconditions,
            scan_ignore_contracts=runtime_defaults.ignore_contracts,
            scan_ignore_properties=scan_ignore_properties,
            scan_ignore_relations=scan_ignore_relations,
            scan_contract_overrides=runtime_defaults.contract_overrides,
            scan_expected_properties=runtime_defaults.expected_properties,
            scan_expected_relations=runtime_defaults.expected_relations,
            scan_property_overrides=scan_property_overrides,
            scan_relation_overrides=scan_relation_overrides,
            scan_contract_checks=runtime_defaults.contract_checks,
            scan_mode=scan_mode,
            scan_seed_from_tests=scan_seed_from_tests,
            scan_seed_from_fixtures=runtime_defaults.seed_from_fixtures,
            scan_seed_from_docstrings=runtime_defaults.seed_from_docstrings,
            scan_seed_from_code=runtime_defaults.seed_from_code,
            scan_seed_from_call_sites=scan_seed_from_call_sites,
            scan_treat_any_as_weak=runtime_defaults.treat_any_as_weak,
            scan_proof_bundles=runtime_defaults.proof_bundles,
            scan_require_replayable=runtime_defaults.require_replayable,
            scan_auto_contracts=runtime_defaults.auto_contracts,
            scan_min_contract_fit=scan_min_contract_fit,
            scan_min_reachability=scan_min_reachability,
            scan_min_realism=scan_min_realism,
            run_mine=False,
            run_scan=True,
            run_mutate=False,
            run_chaos=False,
        )
    if sampling is not None:
        state.supervisor_info = dict(getattr(state, "supervisor_info", {}) or {})
        state.supervisor_info["scan_sampling"] = dict(sampling)
    if scan_notes:
        state.supervisor_info = dict(getattr(state, "supervisor_info", {}) or {})
        state.supervisor_info["scan_scope_notes"] = list(scan_notes)

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
    from types import SimpleNamespace

    from ordeal.audit import audit

    try:
        cfg = _load_optional_config(getattr(args, "config", None))
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    audit_cfg = cfg.audit if cfg is not None else None
    modules = list(args.modules or [])
    if not modules and audit_cfg is not None:
        modules = list(audit_cfg.modules)
    target_specs_by_module: dict[str, dict[str, Any]] = {}

    def _merge_target_spec(spec: Any) -> None:
        target = str(getattr(spec, "target"))
        base_module = _scan_base_module(target)
        bucket = target_specs_by_module.setdefault(base_module, {})
        existing = bucket.get(target)
        if existing is None:
            bucket[target] = spec
            return
        bucket[target] = SimpleNamespace(
            target=target,
            factory=getattr(spec, "factory", None) or getattr(existing, "factory", None),
            setup=getattr(spec, "setup", None) or getattr(existing, "setup", None),
            state_factory=(
                getattr(spec, "state_factory", None) or getattr(existing, "state_factory", None)
            ),
            teardown=getattr(spec, "teardown", None) or getattr(existing, "teardown", None),
            harness=(
                str(getattr(spec, "harness", "") or "").strip()
                or str(getattr(existing, "harness", "fresh") or "fresh").strip()
            ),
            scenarios=list(
                dict.fromkeys(
                    [
                        *list(getattr(existing, "scenarios", []) or []),
                        *list(getattr(spec, "scenarios", []) or []),
                    ]
                )
            ),
            methods=list(getattr(spec, "methods", None) or getattr(existing, "methods", []) or []),
            include_private=bool(
                getattr(spec, "include_private", False)
                or getattr(existing, "include_private", False)
            ),
        )

    if cfg is not None:
        for spec in cfg.objects:
            _merge_target_spec(spec)
    if audit_cfg is not None:
        for spec in audit_cfg.targets:
            _merge_target_spec(spec)

    normalized_target_specs_by_module = {
        module: list(specs.values()) for module, specs in target_specs_by_module.items()
    }
    target_names = modules or list(normalized_target_specs_by_module)
    if not target_names and not normalized_target_specs_by_module:
        _stderr(
            "No modules or audit targets specified. Configure [audit].modules "
            "or [[audit.targets]].\n"
        )
        return 2

    test_dir = str(
        _cli_or_config(
            getattr(args, "test_dir", None),
            audit_cfg.test_dir if audit_cfg else "tests",
        )
    )
    max_examples = int(
        _cli_or_config(
            getattr(args, "max_examples", None),
            audit_cfg.max_examples if audit_cfg else 20,
        )
    )
    workers = int(
        _cli_or_config(
            getattr(args, "workers", None),
            audit_cfg.workers if audit_cfg else 1,
        )
    )
    min_fixture_completeness = float(
        _cli_or_config(
            getattr(args, "min_fixture_completeness", None),
            audit_cfg.min_fixture_completeness if audit_cfg else 0.0,
        )
    )
    validation_mode = str(
        _cli_or_config(
            getattr(args, "validation_mode", None),
            audit_cfg.validation_mode if audit_cfg else "fast",
        )
    )
    show_generated = bool(
        _cli_or_config(
            getattr(args, "show_generated", None),
            audit_cfg.show_generated if audit_cfg else False,
        )
    )
    save_generated = _cli_or_config(
        getattr(args, "save_generated", None),
        audit_cfg.save_generated if audit_cfg else None,
    )
    write_gaps = _cli_or_config(
        getattr(args, "write_gaps", None),
        audit_cfg.write_gaps_dir if audit_cfg else None,
    )
    include_exploratory_function_gaps = bool(
        _cli_or_config(
            getattr(args, "include_exploratory_function_gaps", None),
            audit_cfg.include_exploratory_function_gaps if audit_cfg else False,
        )
    )
    require_direct_tests = bool(
        _cli_or_config(
            getattr(args, "require_direct_tests", None),
            audit_cfg.require_direct_tests if audit_cfg else False,
        )
    )

    object_specs: list[Any] = []
    if cfg is not None:
        object_specs.extend(cfg.objects)
    if audit_cfg is not None:
        object_specs.extend(audit_cfg.targets)

    if getattr(args, "list_targets", False):
        try:
            (
                object_factories,
                object_setups,
                object_scenarios,
                object_state_factories,
                object_teardowns,
                object_harnesses,
            ) = _object_runtime_maps(object_specs)
        except Exception as exc:
            if args.json:
                print(
                    _build_blocked_agent_envelope(
                        tool="audit",
                        target=", ".join(target_names),
                        summary="cannot resolve callable target metadata",
                        blocking_reason=f"target metadata resolution failed: {exc}",
                        raw_details={
                            "target_names": target_names,
                            "error": str(exc),
                        },
                    ).to_json()
                )
                return 1
            _stderr(f"Target metadata resolution failed: {exc}\n")
            return 1

        target_groups: list[dict[str, Any]] = []
        for target in target_names:
            module_name = _scan_base_module(target)
            module_specs = normalized_target_specs_by_module.get(module_name, [])
            module_include_private = bool(
                getattr(args, "include_private", False)
                or any(bool(getattr(spec, "include_private", False)) for spec in module_specs)
            )
            try:
                module_contract_checks = (
                    _config_contract_checks_for_module(cfg, module_name) if cfg else {}
                )
            except Exception:
                module_contract_checks = {}
            rows = _callable_listing_rows(
                module_name,
                include_private=module_include_private,
                object_factories=object_factories,
                object_setups=object_setups,
                object_scenarios=object_scenarios,
                object_state_factories=object_state_factories,
                object_teardowns=object_teardowns,
                object_harnesses=object_harnesses,
                contract_checks=module_contract_checks,
            )
            target_groups.append({"module": module_name, "targets": rows})

        if args.json:
            print(
                _build_target_listing_envelope(
                    tool="audit",
                    target=", ".join(target_names),
                    groups=target_groups,
                ).to_json()
            )
        else:
            print(
                _render_target_listing_text(
                    f"Callable targets for {', '.join(target_names)}",
                    target_groups,
                )
            )
        return 0

    def _collect_results() -> list[Any]:
        collected: list[Any] = []
        for target in target_names:
            module_name = _scan_base_module(target)
            audit_kwargs: dict[str, Any] = {
                "test_dir": test_dir,
                "max_examples": max_examples,
                "workers": workers,
                "validation_mode": validation_mode,
            }
            if min_fixture_completeness > 0.0:
                audit_kwargs["min_fixture_completeness"] = min_fixture_completeness
            if target_specs := normalized_target_specs_by_module.get(module_name):
                audit_kwargs["targets"] = target_specs
            if cfg is not None:
                try:
                    module_contract_checks = _config_contract_checks_for_module(cfg, module_name)
                    if module_contract_checks:
                        audit_kwargs["contract_checks"] = module_contract_checks
                except Exception as exc:
                    _stderr(f"warning: contract config failed for {module_name}: {exc}\n")
            collected.append(audit(target, **audit_kwargs))
        return collected

    results = _collect_results()
    blocked_results = [result for result in results if getattr(result, "blocking_reason", None)]
    direct_test_gate = _direct_test_gate_payload(results) if require_direct_tests else None

    if getattr(args, "json", False):
        saved_generated_path: Path | None = None
        written_gap_files: list[dict[str, Any]] = []
        if save_generated and len(results) == 1 and results[0].generated_test:
            saved_generated_path = Path(save_generated)
            saved_generated_path.write_text(results[0].generated_test, encoding="utf-8")
        if write_gaps:
            written_gap_files = _write_audit_gap_stubs(
                results,
                output_dir=write_gaps,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
        print(
            _build_audit_agent_envelope(
                results,
                saved_generated_path=saved_generated_path,
                written_gap_files=written_gap_files,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
                require_direct_tests=require_direct_tests,
            ).to_json()
        )
        if blocked_results:
            return 1
        if direct_test_gate is not None and not bool(direct_test_gate["passed"]):
            return 1
        return 0

    if show_generated or save_generated or write_gaps:
        # Per-module mode with optional generated or gap-stub output
        for mod, result in zip(target_names, results, strict=False):
            print(
                "\n".join(
                    _audit_summary_lines(
                        result,
                        include_exploratory_function_gaps=include_exploratory_function_gaps,
                    )
                )
            )
            if show_generated and result.generated_test:
                print(f"\n  --- generated test for {mod} ---")
                print(result.generated_test)
                print("  --- end ---")
            if save_generated and result.generated_test:
                path = Path(save_generated)
                path.write_text(result.generated_test, encoding="utf-8")
                _stderr(f"Saved: {path}\n")
        if write_gaps:
            written_gap_files = _write_audit_gap_stubs(
                results,
                output_dir=write_gaps,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
            if written_gap_files:
                _stderr(f"Wrote {len(written_gap_files)} draft gap stub file(s) to {write_gaps}\n")
            else:
                _stderr(f"No draft gap stubs were written to {write_gaps}\n")
    else:
        print(
            _render_audit_report_text(
                results,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
        )

    if direct_test_gate is not None:
        print(f"\n  {_direct_test_gate_summary(direct_test_gate)}")
    if blocked_results:
        return 1
    if direct_test_gate is not None and not bool(direct_test_gate["passed"]):
        gate_suffix = _direct_test_gate_summary(direct_test_gate).removeprefix(
            "Direct test gate: "
        )
        _stderr(f"  Direct tests required: {gate_suffix.lower()}\n")
        return 1
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
      - uses: astral-sh/setup-uv@v7
      - run: uv lock --check
      - run: uv sync --locked --extra dev"""
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
      - uses: actions/checkout@v6
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
                "mode": runtime_defaults.mode,
                "seed_from_tests": runtime_defaults.seed_from_tests,
                "seed_from_fixtures": runtime_defaults.seed_from_fixtures,
                "seed_from_docstrings": runtime_defaults.seed_from_docstrings,
                "seed_from_code": runtime_defaults.seed_from_code,
                "seed_from_call_sites": runtime_defaults.seed_from_call_sites,
                "treat_any_as_weak": runtime_defaults.treat_any_as_weak,
                "proof_bundles": runtime_defaults.proof_bundles,
                "require_replayable": runtime_defaults.require_replayable,
                "auto_contracts": runtime_defaults.auto_contracts,
                "min_contract_fit": runtime_defaults.min_contract_fit,
                "min_reachability": runtime_defaults.min_reachability,
                "min_realism": runtime_defaults.min_realism,
                "fixtures": runtime_defaults.fixtures,
                "expected_failures": runtime_defaults.expected_failures,
                "expected_preconditions": runtime_defaults.expected_preconditions,
            }
            if runtime_defaults.object_factories:
                scan_kwargs["object_factories"] = runtime_defaults.object_factories
            if runtime_defaults.object_setups:
                scan_kwargs["object_setups"] = runtime_defaults.object_setups
            if runtime_defaults.object_scenarios:
                scan_kwargs["object_scenarios"] = runtime_defaults.object_scenarios
            if runtime_defaults.object_state_factories:
                scan_kwargs["object_state_factories"] = runtime_defaults.object_state_factories
            if runtime_defaults.object_teardowns:
                scan_kwargs["object_teardowns"] = runtime_defaults.object_teardowns
            if runtime_defaults.object_harnesses:
                scan_kwargs["object_harnesses"] = runtime_defaults.object_harnesses
            if runtime_defaults.contract_checks:
                scan_kwargs["contract_checks"] = runtime_defaults.contract_checks
            if runtime_defaults.ignore_contracts:
                scan_kwargs["ignore_contracts"] = runtime_defaults.ignore_contracts
            if runtime_defaults.ignore_properties:
                scan_kwargs["ignore_properties"] = runtime_defaults.ignore_properties
            if runtime_defaults.ignore_relations:
                scan_kwargs["ignore_relations"] = runtime_defaults.ignore_relations
            if runtime_defaults.contract_overrides:
                scan_kwargs["contract_overrides"] = runtime_defaults.contract_overrides
            if runtime_defaults.expected_properties:
                scan_kwargs["expected_properties"] = runtime_defaults.expected_properties
            if runtime_defaults.expected_relations:
                scan_kwargs["expected_relations"] = runtime_defaults.expected_relations
            if runtime_defaults.property_overrides:
                scan_kwargs["property_overrides"] = runtime_defaults.property_overrides
            if runtime_defaults.relation_overrides:
                scan_kwargs["relation_overrides"] = runtime_defaults.relation_overrides
            result = scan_module(module, **scan_kwargs)
        except Exception as exc:
            errors.append({"module": module, "error": str(exc)})
            continue

        scanned_modules.append(module)
        functions_checked += len(result.functions)
        skipped_functions += len(result.skipped)

        for function in result.functions:
            qualname = f"{module}.{function.name}"
            crash_category = getattr(function, "crash_category", None) or "speculative_crash"
            if function.promoted and crash_category == "likely_bug":
                findings.append(
                    {
                        "kind": "crash",
                        "category": "likely_bug",
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": _scan_crash_summary(
                            qualname, "likely_bug", function.replayable
                        ),
                        "error": function.error,
                        "failing_args": function.failing_args,
                        "contract_fit": function.contract_fit,
                        "reachability": function.reachability,
                        "realism": function.realism,
                        "sink_signal": function.sink_signal,
                        "input_source": function.input_source,
                        "proof_bundle": function.proof_bundle,
                    }
                )
            elif not function.execution_ok:
                findings.append(
                    {
                        "kind": "crash",
                        "category": crash_category,
                        "module": module,
                        "function": function.name,
                        "qualname": qualname,
                        "summary": _scan_crash_summary(
                            qualname, crash_category, function.replayable
                        ),
                        "error": function.error,
                        "failing_args": function.failing_args,
                        "contract_fit": function.contract_fit,
                        "reachability": function.reachability,
                        "realism": function.realism,
                        "sink_signal": function.sink_signal,
                        "input_source": function.input_source,
                        "proof_bundle": function.proof_bundle,
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

    if any(
        item.get("category") in {"likely_bug", "semantic_contract", "lifecycle_contract"}
        for item in findings
    ):
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

    try:
        cfg = _load_optional_config(getattr(args, "config", None))
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    init_cfg = cfg.init if cfg is not None else None
    audit_cfg = cfg.audit if cfg is not None else None

    target_value = _cli_or_config(args.target, init_cfg.target if init_cfg else None)
    target: str | None = target_value or None
    output_dir = str(_cli_or_config(args.output_dir, init_cfg.output_dir if init_cfg else "tests"))
    dry_run: bool = args.dry_run
    ci = bool(_cli_or_config(args.ci, init_cfg.ci if init_cfg else False))
    ci_name = str(_cli_or_config(args.ci_name, init_cfg.ci_name if init_cfg else "ordeal"))
    install_skill = bool(
        _cli_or_config(args.install_skill, init_cfg.install_skill if init_cfg else False)
    )
    close_gaps = bool(_cli_or_config(args.close_gaps, init_cfg.close_gaps if init_cfg else False))
    gap_output_dir = str(
        _cli_or_config(
            None,
            init_cfg.gap_output_dir if init_cfg and init_cfg.gap_output_dir else output_dir,
        )
    )
    init_mutation_preset = str(
        _cli_or_config(None, init_cfg.mutation_preset if init_cfg else "essential")
    )
    init_scan_max_examples = int(
        _cli_or_config(None, init_cfg.scan_max_examples if init_cfg else 10)
    )
    close_gap_max_examples = audit_cfg.max_examples if audit_cfg is not None else 10
    close_gap_workers = audit_cfg.workers if audit_cfg is not None else 1
    close_gap_validation_mode = audit_cfg.validation_mode if audit_cfg is not None else "fast"
    close_gap_include_exploratory = bool(
        audit_cfg.include_exploratory_function_gaps if audit_cfg is not None else False
    )

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
        audit_target_specs_by_module: dict[str, dict[str, Any]] = {}

        def _merge_target_spec(spec: Any) -> None:
            target = str(getattr(spec, "target"))
            base_module = _scan_base_module(target)
            bucket = audit_target_specs_by_module.setdefault(base_module, {})
            existing = bucket.get(target)
            if existing is None:
                bucket[target] = spec
                return
            from types import SimpleNamespace

            bucket[target] = SimpleNamespace(
                target=target,
                factory=getattr(spec, "factory", None) or getattr(existing, "factory", None),
                setup=getattr(spec, "setup", None) or getattr(existing, "setup", None),
                methods=list(
                    getattr(spec, "methods", None) or getattr(existing, "methods", []) or []
                ),
                include_private=bool(
                    getattr(spec, "include_private", False)
                    or getattr(existing, "include_private", False)
                ),
            )

        if cfg is not None:
            for spec in cfg.objects:
                _merge_target_spec(spec)
        if audit_cfg is not None:
            for spec in audit_cfg.targets:
                _merge_target_spec(spec)
        normalized_audit_specs = {
            module: list(specs.values()) for module, specs in audit_target_specs_by_module.items()
        }
        audit_results = [
            audit(
                module,
                **(
                    {
                        "targets": normalized_audit_specs[module],
                        "test_dir": output_dir,
                        "max_examples": close_gap_max_examples,
                        "workers": close_gap_workers,
                        "validation_mode": close_gap_validation_mode,
                    }
                    if module in normalized_audit_specs
                    else {
                        "test_dir": output_dir,
                        "max_examples": close_gap_max_examples,
                        "workers": close_gap_workers,
                        "validation_mode": close_gap_validation_mode,
                    }
                ),
            )
            for module in generated_modules
        ]
        mutation_score = _aggregate_mutation_score(audit_results)
        gap_stub_files = _write_audit_gap_stubs(
            audit_results,
            output_dir=gap_output_dir,
            include_exploratory_function_gaps=close_gap_include_exploratory,
        )
        weakest_tests = [
            {"module": result.module, **item}
            for result in audit_results
            for item in result.weakest_tests
        ]
    else:
        mp = _run_ordeal(["mutate", *mut_targets, "-p", init_mutation_preset])
        for line in mp.stdout.splitlines():
            if line.startswith("Score:"):
                mutation_score = line.strip()
                break

    # --- Phase 3: Lightweight read-only scan ---
    initial_scan = _run_init_scan(
        [r["module"] for r in generated],
        max_examples=init_scan_max_examples,
    )

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
                "  Gaps:       report-only (use --close-gaps to write draft audit stub files)\n"
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
        "ci_workflow": _display_path(Path(ci_path)) if ci_path else None,
        "install_skill": install_skill,
        "skill": _display_path(Path(skill_path)) if skill_path else None,
        "files": [_display_path(Path(r["path"])) for r in generated if r["path"]]
        + [item["path"] for item in gap_stub_files]
        + ([_display_path(Path("ordeal.toml"))] if Path("ordeal.toml").exists() else [])
        + ([_display_path(Path(ci_path))] if ci_path else [])
        + ([_display_path(Path(skill_path))] if skill_path else []),
        "pinned_values": pinned_values,
        "functions": [
            {
                "module": r["module"],
                "status": r["status"],
                "test_file": _display_path(Path(r["path"])) if r["path"] else None,
            }
            for r in results
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
    promote_clusters_only = True
    cluster_min_size = 2
    cfg: OrdealConfig | None = None

    try:
        cfg = _load_optional_config(args.config)
    except FileNotFoundError:
        if args.config is not None:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=str(args.config),
                        summary=f"config file not found: {args.config}",
                        blocking_reason=f"config file not found: {args.config}",
                        raw_details={"config": args.config},
                    ).to_json()
                )
            else:
                _stderr(f"Config file not found: {args.config}\n")
            return 1
    except ConfigError as e:
        if getattr(args, "json", False):
            print(
                _build_blocked_agent_envelope(
                    tool="mutate",
                    target=str(args.config or "ordeal.toml"),
                    summary=f"config error in {args.config or 'ordeal.toml'}",
                    blocking_reason=str(e),
                    raw_details={"config": args.config or "ordeal.toml"},
                ).to_json()
            )
        else:
            _stderr(f"Config error: {e}\n")
        return 1

    # Fall back to config file if no targets given
    if not targets:
        config_path = args.config or "ordeal.toml"
        if cfg is None:
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

    if cfg is not None and cfg.mutations is not None:
        # Config provides defaults; explicit CLI flags override.
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
        promote_clusters_only = cfg.mutations.promote_clusters_only
        cluster_min_size = cfg.mutations.cluster_min_size

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
        contract_context = _mutation_contract_context_for_target(cfg, target)

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
                        promote_clusters_only=promote_clusters_only,
                        cluster_min_size=cluster_min_size,
                        resume=resume,
                        contract_context=contract_context,
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
                    promote_clusters_only=promote_clusters_only,
                    cluster_min_size=cluster_min_size,
                    resume=resume,
                    contract_context=contract_context,
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
    coverage_gaps = [detail for detail in details if detail.get("category") == "coverage_gap"]
    invalid_inputs = [
        detail for detail in details if detail.get("category") == "invalid_input_crash"
    ]
    robustness = [
        detail
        for detail in details
        if detail.get("category") == "beyond_declared_contract_robustness"
    ]
    exploratory_crashes = [
        detail for detail in details if detail.get("category") == "speculative_crash"
    ]
    exploratory_properties = [
        detail for detail in details if detail.get("category") == "speculative_property"
    ]
    expected = [
        detail for detail in details if detail.get("category") == "expected_precondition_failure"
    ]
    if state.findings:
        status = "findings found"
    elif coverage_gaps:
        status = "coverage gaps found"
    elif exploratory_crashes or exploratory_properties:
        status = "exploratory findings"
    elif robustness:
        status = "robustness findings observed"
    elif invalid_inputs:
        status = "invalid-input crashes observed"
    elif expected:
        status = "expected preconditions observed"
    else:
        status = "no findings yet"
    lines.append(f"  status: {status}")
    lines.append(f"  confidence: {state.confidence:.0%}")

    lines.append(f"  checked: {', '.join(_scan_checked_items(state))}")
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    if isinstance(sampling, Mapping):
        lines.append(
            "  surface sample: "
            f"{sampling.get('sampled', 0)}/{sampling.get('total_runnable', 0)} runnable exports "
            f"across {sampling.get('source_modules', 0)} source module(s); "
            "use --list-targets or --target for exhaustive coverage"
        )

    if state.findings:
        lines.append("  findings:")
        for finding in state.findings[:5]:
            lines.append(f"    - {finding}")
    else:
        lines.append("  findings: none promoted")
        if coverage_gaps:
            lines.append("  coverage gaps:")
            for detail in coverage_gaps[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if invalid_inputs:
            lines.append("  invalid-input crashes:")
            for detail in invalid_inputs[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if robustness:
            lines.append("  beyond-contract robustness:")
            for detail in robustness[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if exploratory_crashes:
            lines.append("  exploratory crashes:")
            for detail in exploratory_crashes[:5]:
                lines.append(f"    - {detail.get('summary', detail.get('function', '?'))}")
        if exploratory_properties:
            lines.append("  exploratory properties:")
            for detail in exploratory_properties[:5]:
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


_SPECULATIVE_SCAN_CATEGORIES = {
    "speculative_crash",
    "speculative_property",
    "invalid_input_crash",
    "beyond_declared_contract_robustness",
    "coverage_gap",
}


def _is_speculative_scan_detail(detail: Mapping[str, Any]) -> bool:
    """Return whether a scan detail is exploratory rather than promoted."""
    return detail.get("category") in _SPECULATIVE_SCAN_CATEGORIES


def _scan_checked_items(state: Any) -> list[str]:
    """Return the coarse coverage summary for a scan report."""
    checked = [f"{len(state.functions)} functions"]
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    if isinstance(sampling, Mapping):
        checked.append(
            "sampled "
            f"{sampling.get('sampled', 0)}/{sampling.get('total_runnable', 0)} runnable exports"
        )
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

    if kind in {"crash", "coverage_gap", "contract"} and isinstance(failing_args, dict):
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


def _append_proof_bundle(lines: list[str], detail: dict[str, Any]) -> None:
    """Render proof-bundle details when present."""
    proof = detail.get("proof_bundle")
    if not isinstance(proof, Mapping):
        return
    witness = proof.get("witness") or proof.get("valid_input_witness")
    contract_basis = proof.get("contract_basis")
    confidence_breakdown = proof.get("confidence_breakdown")
    reproduction = proof.get("minimal_reproduction") or proof.get("reproduction")
    failure_path = proof.get("failure_path") or proof.get("failing_path")
    impact = proof.get("impact") or proof.get("likely_impact")
    verdict = proof.get("verdict")
    if witness:
        lines.extend(["", "Proof bundle:"])
        lines.extend(_json_block(witness))
    if contract_basis:
        lines.extend(["", "Contract basis:"])
        lines.extend(_json_block(contract_basis))
    if confidence_breakdown:
        lines.extend(["", "Confidence breakdown:"])
        lines.extend(_json_block(confidence_breakdown))
    if reproduction:
        lines.extend(["", "Minimal reproduction:"])
        if isinstance(reproduction, Mapping):
            rendered = dict(reproduction)
            snippet = rendered.pop("python_snippet", None)
            if rendered:
                lines.extend(_json_block(rendered))
            if snippet:
                lines.extend(["", "Python snippet:"])
                lines.extend(_python_block(str(snippet)))
        else:
            lines.extend(_json_block(reproduction))
    if failure_path:
        lines.extend(["", "Failure path:"])
        lines.extend(_json_block(failure_path))
    if impact:
        if isinstance(impact, Mapping):
            summary = impact.get("summary")
            if summary:
                lines.append(f"- Likely impact: {summary}")
            extra = {key: value for key, value in impact.items() if key != "summary"}
            if extra:
                lines.extend(["", "Impact details:"])
                lines.extend(_json_block(extra))
        else:
            lines.append(f"- Likely impact: {impact}")
    if isinstance(verdict, Mapping) and verdict.get("demotion_reason"):
        lines.append(f"- Demotion reason: {verdict['demotion_reason']}")


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
        if detail.get("contract_fit") is not None:
            lines.append(
                "- Ranking:"
                f" contract fit={float(detail.get('contract_fit')):.0%},"
                f" reachability={float(detail.get('reachability') or 0.0):.0%},"
                f" realism={float(detail.get('realism') or 0.0):.0%}"
            )
        if detail.get("replay_attempts"):
            lines.append(
                "- Replay:"
                f" `{detail.get('replay_matches', 0)}/{detail.get('replay_attempts', 0)}`"
                " matching replays"
            )
        lines.append(
            "- Why this matters: "
            + str(
                detail.get("proof_bundle", {}).get("likely_impact")
                or "the function crashes under generated inputs."
            )
        )
        if detail.get("failing_args"):
            lines.extend(["", "Failing input:"])
            lines.extend(_json_block(detail["failing_args"]))
        _append_proof_bundle(lines, detail)
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

    if kind == "coverage_gap":
        lines.append(f"- Evidence: `{detail.get('error') or 'gap-triggering crash'}`")
        lines.append(
            "- Ranking:"
            f" contract fit={float(detail.get('contract_fit') or 0.0):.0%},"
            f" reachability={float(detail.get('reachability') or 0.0):.0%},"
            f" realism={float(detail.get('realism') or 0.0):.0%}"
        )
        _append_proof_bundle(lines, detail)
        lines.extend(
            [
                "",
                "Next steps:",
                f"- Add direct tests or fixtures for `{qualname}`",
                f"- Re-scan `{qualname}` in `--mode real_bug` once valid inputs are seeded",
            ]
        )
        return lines

    if kind == "contract":
        lines.append(f"- Evidence: `{detail.get('summary', title)}`")
        _append_proof_bundle(lines, detail)
        category = str(detail.get("category", "semantic_contract"))
        lines.extend(
            [
                "",
                "Next steps:",
                (
                    "- Add a direct lifecycle regression for "
                    f"`{qualname}` with the recorded fault path"
                    if category == "lifecycle_contract"
                    else f"- Add a direct regression for `{qualname}` around the semantic sink"
                ),
                f"- Re-run `ordeal scan {module} --mode real_bug`",
            ]
        )
        return lines

    if kind == "function_gap":
        detail_payload = detail.get("details") or {}
        status = detail_payload.get("status")
        epistemic = detail_payload.get("epistemic")
        covered = detail_payload.get("covered_body_lines")
        total = detail_payload.get("total_body_lines")
        evidence = detail_payload.get("evidence") or []

        if status:
            label = f"{status} [{epistemic}]" if epistemic else str(status)
            lines.append(f"- Function Evidence: {label}")
        if total:
            lines.append(f"- Covered Body Lines: `{covered}/{total}`")
        if evidence:
            lines.extend(["", "Evidence:"])
            for item in evidence[:5]:
                lines.append(f"- `{item.get('kind', 'evidence')}`: {item.get('detail', '')}")
        lines.extend(
            [
                "",
                "Next steps:",
                f"- Add a direct test for `{qualname}` with concrete inputs",
                f"- `ordeal audit {module} --show-generated`",
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
    sampling = getattr(state, "supervisor_info", {}).get("scan_sampling")
    scope_notes = [
        str(note)
        for note in getattr(state, "supervisor_info", {}).get("scan_scope_notes", ())
        if note
    ]
    details = [
        {
            **detail,
            "module": state.module,
            "qualname": f"{state.module}.{detail.get('function', '?')}",
        }
        for detail in _scan_report_details(state)
    ]
    promoted_count = len(getattr(state, "findings", []))
    lifecycle_contract_count = sum(
        1 for detail in details if detail.get("category") == "lifecycle_contract"
    )
    semantic_contract_count = sum(
        1 for detail in details if detail.get("category") == "semantic_contract"
    )
    coverage_gap_count = sum(1 for detail in details if detail.get("category") == "coverage_gap")
    invalid_input_count = sum(
        1 for detail in details if detail.get("category") == "invalid_input_crash"
    )
    robustness_count = sum(
        1 for detail in details if detail.get("category") == "beyond_declared_contract_robustness"
    )
    exploratory_crash_count = sum(
        1 for detail in details if detail.get("category") == "speculative_crash"
    )
    exploratory_property_count = sum(
        1 for detail in details if detail.get("category") == "speculative_property"
    )
    expected_count = sum(
        1 for detail in details if detail.get("category") == "expected_precondition_failure"
    )
    if promoted_count:
        status = "findings found"
    elif exploratory_crash_count or exploratory_property_count or robustness_count:
        status = "exploratory findings"
    elif expected_count:
        status = "expected preconditions observed"
    else:
        status = "no findings yet"
    summary = [
        f"Checked: {', '.join(_scan_checked_items(state))}",
        f"Promoted findings: {promoted_count}",
        f"Lifecycle contracts: {lifecycle_contract_count}",
        f"Semantic contracts: {semantic_contract_count}",
        f"Coverage gaps: {coverage_gap_count}",
        f"Invalid-input crashes: {invalid_input_count}",
        f"Beyond-contract robustness: {robustness_count}",
        f"Exploratory crashes: {exploratory_crash_count}",
        f"Exploratory properties: {exploratory_property_count}",
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
    ]
    if isinstance(sampling, Mapping):
        summary.insert(
            1,
            "Surface sampling: "
            f"{sampling.get('sampled', 0)}/{sampling.get('total_runnable', 0)} "
            "runnable exports checked",
        )
    suggested_commands = [
        f"ordeal scan {state.module}",
        f"ordeal mine {state.module} -n 200",
        f"ordeal mutate {state.module}",
    ]
    extra_sections: list[tuple[str, list[str]]] = []
    if isinstance(sampling, Mapping):
        sampling_notes = [
            note for note in scope_notes if not note.startswith("Package-root scan sampled ")
        ]
        extra_sections.append(
            (
                "Surface Sampling",
                [
                    "Package-root scan sampled "
                    f"{sampling.get('sampled', 0)} of "
                    f"{sampling.get('total_runnable', 0)} runnable exports across "
                    f"{sampling.get('source_modules', 0)} source module(s).",
                    "Use `--list-targets` to inspect the full exported surface.",
                    "Use `--target` to run an exhaustive check on a specific callable or glob.",
                    *sampling_notes,
                ],
            )
        )
        suggested_commands = [
            f"ordeal scan {state.module} --list-targets",
            f"ordeal scan {state.module} --target <selector>",
            f"ordeal scan {state.module} -n 50 --target <selector>",
        ]
    elif scope_notes:
        extra_sections.append(("Scope Notes", scope_notes))
    extra_sections.append(
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
    )
    return {
        "target": state.module,
        "tool": "scan",
        "status": status,
        "confidence": f"{state.confidence:.0%}",
        "seed": getattr(state, "supervisor_info", {}).get("seed"),
        "summary": summary,
        "details": details,
        "gaps": [
            f"`{state.module}.{name}`: {', '.join(gaps)}" for name, gaps in state.frontier.items()
        ],
        "suggested_commands": suggested_commands,
        "extra_sections": extra_sections,
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
    value = detail.get("contract_fit")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _scan_crash_summary(qualname: str, category: str, replayable: bool | None) -> str:
    """Return the CLI summary string for one scan crash."""
    if category == "likely_bug":
        return f"{qualname}: crash safety failed"
    if category == "coverage_gap":
        return f"{qualname}: crash still looks like a coverage gap"
    if category == "beyond_declared_contract_robustness":
        return f"{qualname}: crash sits just beyond the declared contract"
    if category == "invalid_input_crash":
        return f"{qualname}: crash currently looks driven by invalid input"
    if replayable:
        return f"{qualname}: replayable crash on semi-valid inputs, still exploratory"
    return f"{qualname}: unreplayed crash on random inputs"


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
    nested_details = detail.get("details")
    if isinstance(nested_details, Mapping):
        extras.update(dict(nested_details))
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
        if kind == "function_gap":
            status_detail = str(first.get("details", {}).get("status", "gap"))
            if status_detail == "exploratory":
                return f"Add a direct test for {qualname} instead of relying on inferred coverage."
            return f"Write the first effective test for {qualname}."
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
            else ("exploratory" if detail_categories & _SPECULATIVE_SCAN_CATEGORIES else "ok")
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


def _audit_detail_items(
    result: Any,
    *,
    include_exploratory_function_gaps: bool = False,
) -> list[dict[str, Any]]:
    """Normalize one ModuleAudit into finding-style detail items."""
    details: list[dict[str, Any]] = []
    if getattr(result, "blocking_reason", None):
        details.append(
            {
                "kind": "blocked_target",
                "category": "verification_warning",
                "summary": str(result.blocking_reason),
                "module": result.module,
                "qualname": result.module,
                "details": {
                    "fixture_completeness": getattr(result, "fixture_completeness", 0.0),
                },
            }
        )
    for hint in getattr(result, "harness_hints", []):
        details.append(
            {
                "kind": "harness_hint",
                "category": "verification_warning",
                "summary": (
                    f"{hint['function']}: suggested {hint['kind']} -> {hint['suggestion']}"
                ),
                "module": result.module,
                "function": hint["function"],
                "qualname": f"{result.module}.{hint['function']}",
                "details": dict(hint),
            }
        )
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
    for finding in getattr(result, "contract_findings", []):
        function_name = str(finding.get("function", result.module))
        qualname = (
            f"{result.module}.{function_name}"
            if function_name and function_name != result.module
            else result.module
        )
        details.append(
            {
                "kind": "contract",
                "category": str(finding.get("category", "semantic_contract")),
                "summary": str(finding.get("summary", "explicit contract failed")),
                "module": result.module,
                "function": function_name,
                "qualname": qualname,
                "details": dict(finding),
            }
        )
    details.extend(
        _function_gap_detail_items(
            result,
            include_exploratory_function_gaps=include_exploratory_function_gaps,
        )
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


def _audit_summary_lines(
    result: Any,
    *,
    include_exploratory_function_gaps: bool,
) -> list[str]:
    """Render one audit summary with filtered function-gap sections."""
    lines = result.summary().splitlines()
    rendered: list[str] = []
    exploratory_count = int(getattr(result, "function_audit_counts", {}).get("exploratory", 0))
    saw_function_section = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("    functions:"):
            saw_function_section = True
            rendered.append(line)
            for status in ("exercised", "uncovered", "exploratory"):
                if status == "exploratory" and not include_exploratory_function_gaps:
                    continue
                entries = [
                    item
                    for item in getattr(result, "function_audits", [])
                    if getattr(item, "status", "") == status
                ]
                entries.sort(
                    key=lambda item: (
                        -int(getattr(item, "covered_body_lines", 0) or 0),
                        str(getattr(item, "name", "")),
                    )
                )
                if not entries:
                    continue
                names = ", ".join(item.name for item in entries[:5])
                rendered.append(f"      - {entries[0].summary_label()}: {names}")
                if entries[0].evidence:
                    first = entries[0].evidence[0]
                    rendered.append(f"        evidence: {first['kind']} — {first['detail']}")
            if exploratory_count and not include_exploratory_function_gaps:
                rendered.append(
                    "      exploratory gaps hidden by default:"
                    f" {exploratory_count} (use --include-exploratory-function-gaps)"
                )
            i += 1
            while i < len(lines) and (
                lines[i].startswith("      - ") or lines[i].startswith("        evidence:")
            ):
                i += 1
            continue
        rendered.append(line)
        i += 1
    if exploratory_count and not include_exploratory_function_gaps and not saw_function_section:
        rendered.append(
            f"  exploratory gaps hidden by default: {exploratory_count}"
            " (use --include-exploratory-function-gaps)"
        )
    return rendered


def _render_audit_report_text(
    results: Sequence[Any],
    *,
    include_exploratory_function_gaps: bool,
) -> str:
    """Render a human-readable audit report from precomputed results."""
    lines = ["ordeal audit"]
    total_cur_tests = 0
    total_cur_lines = 0
    total_mig_tests = 0
    total_mig_lines = 0
    total_warnings = 0
    total_exploratory = 0

    for result in results:
        lines.extend(
            _audit_summary_lines(
                result,
                include_exploratory_function_gaps=include_exploratory_function_gaps,
            )
        )
        total_cur_tests += result.current_test_count
        total_cur_lines += result.current_test_lines
        total_mig_tests += result.migrated_test_count
        total_mig_lines += result.migrated_lines
        total_warnings += len(result.warnings)
        total_exploratory += result.function_audit_counts["exploratory"]

    if len(results) > 1:
        lines.append("\n  total:")
        lines.append(f"    current:  {total_cur_tests} tests | {total_cur_lines} lines")
        lines.append(f"    migrated: {total_mig_tests} tests | {total_mig_lines} lines")
        if total_cur_tests > 0:
            saved = total_cur_tests - total_mig_tests
            pct = saved / total_cur_tests * 100
            lines.append(f"    saved:    {saved} tests ({pct:.0f}%)")

    if total_exploratory and not include_exploratory_function_gaps:
        lines.append(
            f"\n  exploratory function gaps hidden by default: {total_exploratory}"
            " (use --include-exploratory-function-gaps)"
        )

    if total_warnings:
        lines.append(f"\n  warnings: {total_warnings} total")

    return "\n".join(lines)


def _build_audit_agent_envelope(
    results: Sequence[Any],
    *,
    saved_generated_path: Path | None = None,
    written_gap_files: Sequence[Mapping[str, Any]] = (),
    include_exploratory_function_gaps: bool = False,
    require_direct_tests: bool = False,
) -> Any:
    """Build the agent-facing JSON envelope for `ordeal audit`."""
    from ordeal.audit import _generated_test_path, _module_audit_to_dict

    details = [
        detail
        for result in results
        for detail in _audit_detail_items(
            result,
            include_exploratory_function_gaps=include_exploratory_function_gaps,
        )
    ]
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
    blocked_count = sum(1 for result in results if getattr(result, "blocking_reason", None))
    report = {
        "target": ", ".join(modules),
        "tool": "audit",
        "status": "findings found" if details else "no major gaps found",
        "summary": [
            f"Audited: {len(results)} module(s)",
            f"Findings: {len(details)}",
            f"Blocked modules: {blocked_count}",
            (
                "Function evidence: "
                f"{sum(result.function_audit_counts['exercised'] for result in results)}"
                " exercised, "
                f"{sum(result.function_audit_counts['exploratory'] for result in results)}"
                " exploratory, "
                f"{sum(result.function_audit_counts['uncovered'] for result in results)} uncovered"
            ),
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
        max(int(round(getattr(result, "fixture_completeness", 0.0) * result.total_functions)), 0)
        for result in results
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
    exploratory_count = sum(result.function_audit_counts["exploratory"] for result in results)
    direct_test_gate = _direct_test_gate_payload(results) if require_direct_tests else None
    report["summary"].append(
        "Evidence:"
        f" search depth={evidence['search_depth']['modules']} modules/"
        f"{evidence['search_depth']['coverage_measurements']} measurements,"
        f" replayability={evidence['replayability']:.0%},"
        f" mutation strength={mutation_strength_text},"
        f" fixture completeness={evidence['fixture_completeness']:.0%}"
    )
    if direct_test_gate is not None:
        report["summary"].append(_direct_test_gate_summary(direct_test_gate))
    if exploratory_count and not include_exploratory_function_gaps:
        report["summary"].append(
            "Exploratory function gaps hidden by default: "
            f"{exploratory_count} (use --include-exploratory-function-gaps)"
        )
    if written_gap_files:
        report["summary"].append(f"Gap stubs written: {len(written_gap_files)}")
    report["extra_sections"] = [
        (
            "Function-Level Evidence",
            [
                (
                    f"{result.module}: "
                    f"{result.function_audit_counts['exercised']} exercised [verified], "
                    f"{result.function_audit_counts['exploratory']} exploratory [inferred], "
                    f"{result.function_audit_counts['uncovered']} no effective tests [none]"
                )
                for result in results
            ],
        ),
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
        ),
    ]
    if written_gap_files:
        report["extra_sections"].append(
            (
                "Draft Gap Stubs",
                [
                    f"{item.get('target', 'unknown')} -> {item.get('path', '')}"
                    for item in written_gap_files
                ],
            )
        )
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
    for item in written_gap_files:
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        metadata = {key: value for key, value in item.items() if key != "path"}
        artifacts.append(_agent_artifact("gap-stub", path, "draft audit gap stub", **metadata))
    blocking_reason = None
    blocked_reasons = list(
        dict.fromkeys(
            str(result.blocking_reason)
            for result in results
            if getattr(result, "blocking_reason", None)
        )
    )
    if blocked_reasons:
        blocking_reason = "; ".join(blocked_reasons)
    if direct_test_gate is not None and not bool(direct_test_gate["passed"]):
        blocking_reason = (
            "direct tests required for "
            f"{len(direct_test_gate['exploratory']) + len(direct_test_gate['uncovered'])}"
            " function(s)"
        )
    return _build_agent_envelope_from_report(
        report,
        status=(
            "blocked"
            if blocking_reason is not None
            else ("findings" if details else ("exploratory" if exploratory_count else "ok"))
        ),
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
        blocking_reason=blocking_reason,
        artifacts=artifacts,
        raw_details={
            "report": report,
            "modules": [_module_audit_to_dict(result) for result in results],
            "function_audits": [
                {"module": result.module, **item}
                for result in results
                for item in _module_audit_to_dict(result).get("function_audits", [])
            ],
            "gap_stub_files": [dict(item) for item in written_gap_files],
            "direct_test_gate": (
                {"required": True, **direct_test_gate} if direct_test_gate is not None else None
            ),
            "evidence_dimensions": evidence,
        },
        suggested_test_file=(
            str(saved_generated_path)
            if saved_generated_path is not None
            else (
                str(written_gap_files[0].get("path", ""))
                if len(written_gap_files) == 1 and written_gap_files[0].get("path")
                else None
            )
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
        "Use `--list-targets` to inspect how ordeal sees functions, methods, async callables,\n"
        " and whether object factories are configured.\n"
        "Use `--target` to limit module scans to specific callable selectors or globs such as\n"
        " `mutate`, `Env.*`, or `pkg.mod:Env.run`.\n"
        "Use `--ignore-property`, `--ignore-relation`, `--property-override`, and\n"
        " `--relation-override` to suppress noisy mined signals without changing code.\n"
        "Use explicit targets like `pkg.mod:Env.build_env_vars`, shared `[[objects]]`,\n"
        " and `[[contracts]]` in ordeal.toml for stateful OO code and shell/path/env checks.\n"
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
        "  deep  replay mined inputs, then re-mine mutants for extra search depth\n\n"
        "Use --list-targets to inspect the callable surface that audit can see, including\n"
        " methods that need configured factories.\n"
        "Use [audit] in ordeal.toml to persist module lists, validation depth, "
        "and direct-test policy, and reuse shared `[[objects]]` for bound methods.\n"
        "Use --write-gaps PATH to emit draft review stubs for surviving mutants "
        "and function-level coverage gaps."
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
        "summary. Use [init] in ordeal.toml to persist bootstrap defaults, and "
        "use --install-skill / --close-gaps to opt into extra writes."
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
                    help="Repeat to check one or more named built-in contracts directly.",
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
        ),
        CommandSpec(
            name="scan",
            handler=_cmd_scan,
            help="Explore a module and optionally write reports or pytest regressions",
            description=_scan_command_description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            arguments=(
                _arg(
                    "target",
                    help=(
                        "Module or explicit callable target (e.g. myapp.scoring or myapp:Env.run)"
                    ),
                ),
                _arg(
                    "--target",
                    dest="scan_targets",
                    action="append",
                    default=None,
                    metavar="SELECTOR",
                    help=(
                        "Limit a module scan to callable selector(s); accepts local names, "
                        "explicit targets, or glob patterns like mutate, Env.*, or ordeal:mut* "
                        "(repeatable)"
                    ),
                ),
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
                    "--mode",
                    choices=("coverage_gap", "real_bug"),
                    default=None,
                    help="Promotion mode: gap-oriented or strict real-bug ranking",
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
                _arg(
                    "--time-limit",
                    "-t",
                    type=float,
                    default=None,
                    help="Time budget in seconds",
                ),
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
                _arg(
                    "--list-targets",
                    action="store_true",
                    help="List callable targets and metadata, then exit",
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
                _arg(
                    "modules",
                    nargs="*",
                    help="Module paths to audit (omit to use [audit].modules)",
                ),
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
                    help=(
                        "Parallel workers for mutation validation (default: 1, or [audit].workers)"
                    ),
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
                    help=("Include exploratory function gaps in audit findings and draft stubs"),
                ),
                _arg(
                    "--require-direct-tests",
                    action=argparse.BooleanOptionalAction,
                    default=None,
                    help=("Return exit code 1 when exploratory function coverage is all indirect"),
                ),
                _arg(
                    "--list-targets",
                    action="store_true",
                    help="List callable targets and metadata, then exit",
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
                    "--config",
                    "-c",
                    default=None,
                    help="Config file with [init] defaults (default: ordeal.toml if present)",
                ),
                _arg(
                    "target",
                    nargs="?",
                    default=None,
                    help=(
                        "Package path (e.g. myapp); auto-detects or uses [init].target if omitted"
                    ),
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
                    help=(
                        "Preview without side effects — no files written, no functions executed. "
                        "Generates stub tests from signatures only."
                    ),
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
                    help=(
                        "Workflow filename (default: ordeal → .github/workflows/ordeal.yml,"
                        " or [init].ci_name)"
                    ),
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
            "Common CLI entrypoints:\n"
            "  ordeal scan <module>          exploratory module analysis\n"
            "  ordeal init [package]         starter tests + ordeal.toml\n"
            "  ordeal audit <module>         test-quality comparison\n"
            "  ordeal mutate <target>        mutation testing\n"
            "  ordeal skill                  install the bundled local agent guide\n\n"
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
