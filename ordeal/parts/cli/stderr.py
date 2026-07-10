from __future__ import annotations
# ruff: noqa
import argparse
import contextlib
import functools
import hashlib
import importlib
import inspect
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time as _time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from pprint import pformat
from textwrap import indent
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from ordeal.config import ConfigError, OrdealConfig, load_config
if TYPE_CHECKING:
    from ordeal.explore import ExplorationResult, ProgressSnapshot
# Tests monkeypatch this symbol; keep the override point without paying
# the import cost on every short CLI command.
Explorer = None
_SAFE_LISTING_CONFIG_WARNING = (
    "config-backed fixture registries, object hooks, and custom contracts "
    "were not imported during target listing; run a real scan/audit/check "
    "to validate them"
)
_DEFAULT_SCAN_MIN_FIXTURE_COMPLETENESS = 0.55
def _stderr(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()
def _metadata_only_hook(kind: str, label: str) -> Callable[..., Any]:
    """Return a placeholder hook used for read-only discovery paths."""

    def _placeholder(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            f"metadata-only {kind} placeholder for {label!r} should not be executed"
        )

    _placeholder.__name__ = f"_ordeal_metadata_only_{kind}"
    _placeholder.__qualname__ = _placeholder.__name__
    _placeholder.__ordeal_metadata_only__ = True
    return _placeholder
def _safe_listing_config_warning(
    *,
    has_fixture_registries: bool = False,
    has_object_hooks: bool = False,
    has_contracts: bool = False,
) -> list[str]:
    """Return a discovery warning when config-backed imports were skipped."""
    if has_fixture_registries or has_object_hooks or has_contracts:
        return [_SAFE_LISTING_CONFIG_WARNING]
    return []
def _workflow_path_from_ci_name(ci_name: str) -> Path:
    """Return a workflow path rooted under ``.github/workflows``."""
    cleaned = str(ci_name).strip()
    if not cleaned:
        raise ValueError("workflow name cannot be empty")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError("workflow name must not contain path separators")
    if cleaned in {".", ".."}:
        raise ValueError("workflow name must not be '.' or '..'")

    candidate = Path(cleaned)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise ValueError("workflow name must be a single filename")

    filename = candidate.name
    if candidate.suffix.lower() not in {".yml", ".yaml"}:
        filename = f"{filename}.yml"
    return Path(".github") / "workflows" / filename
def _workspace_output_path(path_value: str | os.PathLike[str], *, label: str) -> Path:
    """Return *path_value* when it stays inside the current workspace root."""
    cleaned = os.fspath(path_value).strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be empty")
    raw_path = Path(cleaned)
    workspace_root = Path.cwd().resolve()
    candidate = raw_path if raw_path.is_absolute() else workspace_root / raw_path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay within the current workspace") from exc
    return raw_path if not raw_path.is_absolute() else resolved
def _public_scan_mode(mode: str) -> str:
    """Return the preferred public label for one scan mode."""
    return {
        "coverage_gap": "evidence",
        "evidence": "evidence",
        "real_bug": "candidate",
        "candidate": "candidate",
    }.get(str(mode).strip(), str(mode).strip())
def _evidence_class_for_category(category: str | None) -> str | None:
    """Return the user-facing evidence class for one internal category."""
    if category is None:
        return None
    return {
        "likely_bug": "candidate_issue",
        "expected_precondition_failure": "expected_precondition",
    }.get(category, category)
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
_DEFAULT_REGRESSION_MANIFEST = "tests/ordeal-regressions.json"
_DEFAULT_FINDINGS_DIR = ".ordeal/findings"
_PACKAGE_ROOT_SCAN_LIMIT = 8
CLI_CATALOG_SCHEMA_VERSION = 1
_ADVANCED_SCAN_HELP_DESTS = frozenset(
    {
        "scan_targets",
        "seed",
        "mode",
        "security_focus",
        "seed_from_tests",
        "min_contract_fit",
        "min_reachability",
        "min_realism",
        "workers",
        "ignore_properties",
        "ignore_relations",
        "cli_property_overrides",
        "cli_relation_overrides",
        "report_file",
        "write_regression",
        "include_private",
    }
)
class _ScanHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Show the beginner scan path without removing expert options."""

    def _format_actions_usage(
        self,
        actions: Sequence[argparse.Action],
        groups: Sequence[Any],
    ) -> str:
        visible = [action for action in actions if action.dest not in _ADVANCED_SCAN_HELP_DESTS]
        visible_groups = [
            group for group in groups if any(action in visible for action in group._group_actions)
        ]
        return super()._format_actions_usage(visible, visible_groups)

    def add_argument(self, action: argparse.Action) -> None:
        if action.dest in _ADVANCED_SCAN_HELP_DESTS:
            return
        super().add_argument(action)
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
    usage: str | None = None
    defaults: dict[str, Any] = field(default_factory=dict)
    show_in_help: bool = False
@dataclass(frozen=True)
class ScanRuntimeDefaults:
    """Resolved scan runtime config for one target module."""

    max_examples: int
    mode: str = "evidence"
    seed_from_tests: bool = True
    seed_from_fixtures: bool = True
    seed_from_docstrings: bool = True
    seed_from_code: bool = True
    seed_from_call_sites: bool = True
    treat_any_as_weak: bool = True
    proof_bundles: bool = True
    require_replayable: bool = True
    shell_injection_check: bool = False
    auto_contracts: list[str] = field(default_factory=list)
    min_contract_fit: float = 0.55
    min_reachability: float = 0.45
    min_realism: float = 0.55
    min_fixture_completeness: float = _DEFAULT_SCAN_MIN_FIXTURE_COMPLETENESS
    security_focus: bool = False
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
    return _normalize_module_target(target).split(":", 1)[0]
def _resolve_scan_target(target: str | None) -> str:
    """Return an explicit target or infer the current project's package."""
    cleaned = str(target or "").strip()
    if cleaned and cleaned not in {".", "./"}:
        return cleaned

    config_path = Path("ordeal.toml")
    if config_path.exists():
        with contextlib.suppress(FileNotFoundError, ConfigError):
            config = load_config(config_path)
            if len(config.scan) == 1:
                configured = str(config.scan[0].module).strip()
                if configured:
                    return configured

    from ordeal.mutations import _detect_package

    detected = _detect_package()
    if detected:
        return detected
    raise ValueError(
        "could not detect a Python package in the current directory; "
        "pass a module, package, or Python file (for example: ordeal scan myapp.scoring)"
    )
def _target_module_name(target: str) -> str:
    """Return the importable module for dotted or explicit callable targets."""
    normalized = _normalize_module_target(target)
    if ":" in normalized:
        return normalized.split(":", 1)[0]
    module_name, _, _ = normalized.rpartition(".")
    return module_name or target
def _normalize_module_target(target: str) -> str:
    """Resolve one dotted or file-backed target into an importable module target."""
    from ordeal.auto import _python_source_path_to_module_name

    raw_target = str(target)
    module_part = raw_target
    remainder = ""
    raw_target_lower = raw_target.lower()
    if ".py:" in raw_target_lower:
        py_idx = raw_target_lower.find(".py:")
        module_part = raw_target[: py_idx + 3]
        remainder = raw_target[py_idx + 4 :]
    elif ":" in raw_target and not bool(re.match(r"^[A-Za-z]:[\\\\/]", raw_target)):
        explicit_idx = raw_target.rfind(":")
        candidate_prefix = raw_target[:explicit_idx]
        candidate_path = Path(candidate_prefix)
        if (
            candidate_prefix.endswith(".py")
            or candidate_prefix.startswith("./")
            or candidate_prefix.startswith("../")
            or candidate_prefix.startswith("/")
            or bool(re.match(r"^[A-Za-z]:[\\\\/]", candidate_prefix))
            or candidate_path.exists()
        ):
            module_part = candidate_prefix
            remainder = raw_target[explicit_idx + 1 :]
        else:
            module_part, remainder = raw_target.split(":", 1)
    candidate = Path(module_part)
    normalized = module_part
    if (
        candidate.suffix == ".py"
        or module_part.startswith("./")
        or module_part.startswith("../")
        or module_part.startswith("/")
        or bool(re.match(r"^[A-Za-z]:[\\\\/]", module_part))
        or candidate.exists()
    ):
        module_name = _python_source_path_to_module_name(module_part)
        if module_name:
            normalized = module_name
    return normalized if not remainder else f"{normalized}:{remainder}"
def _scan_display_name(module_name: str, target: str) -> str:
    """Return the local callable name used in scan results for *target*."""
    if ":" in target:
        explicit_module, explicit_target = target.split(":", 1)
        if explicit_module != module_name:
            return target
        return explicit_target
    dotted_prefix = f"{module_name}."
    return target[len(dotted_prefix) :] if target.startswith(dotted_prefix) else target
def _camel_to_snake(name: str) -> str:
    """Convert one CamelCase symbol into snake_case."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", str(name).strip()).lower()
def _bootstrap_support_module_name(test_dir: str) -> str:
    """Return the import path used for generated audit support scaffolds."""
    path = Path(test_dir)
    parts = [part for part in path.parts if part not in {"", ".", "/"}]
    if parts and parts[-1] == "ordeal_support":
        return ".".join(parts)
    return ".".join([*(parts or ["tests"]), "ordeal_support"])
def _bootstrap_support_file_path(test_dir: str) -> str:
    """Return the filesystem path for generated audit support scaffolds."""
    return _display_path(Path(test_dir) / "ordeal_support.py")
def _bootstrap_review_scenarios_for_method(owner: type, method_name: str) -> list[str]:
    """Infer a small review-oriented scenario set for one method."""
    names: set[str] = set()
    source_lower = ""
    target = None
    with contextlib.suppress(Exception):
        target = getattr(owner, method_name)
    if target is not None:
        with contextlib.suppress(Exception):
            params = inspect.signature(target).parameters.values()
            for param in params:
                lower = param.name.lower()
                if lower in {"self", "cls", "state"}:
                    continue
                if any(token in lower for token in ("path", "file", "log")):
                    names.update({"space_paths", "quote_paths"})
                if "instruction" in lower:
                    names.add("empty_instruction")
                if "system_prompt" in lower or "prompt" == lower:
                    names.add("no_system_prompt")
        with contextlib.suppress(Exception):
            source_lower = inspect.getsource(target).lower()
    if any(token in source_lower for token in ("path", "paths", "file", "log_file", "log_path")):
        names.update({"space_paths", "quote_paths"})
    if "instruction" in source_lower:
        names.add("empty_instruction")
    if "system_prompt" in source_lower or "system prompt" in source_lower:
        names.add("no_system_prompt")
    if any(
        token in source_lower for token in ("log_file", "log_path", "missing_log", "transcript")
    ):
        names.add("missing_log_file")
    return sorted(names)
