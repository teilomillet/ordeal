from __future__ import annotations
# ruff: noqa
import ast
import asyncio
import builtins
import contextlib
import copy
import fnmatch
import functools
import hashlib
import importlib
import importlib.util
import inspect
import os
import re
import shlex
import sys
import textwrap
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from pprint import pformat
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Literal, Union, get_args, get_origin
import hypothesis.strategies as st
from hypothesis import given, settings
from hypothesis.stateful import initialize, rule
from ordeal.chaos import ChaosTest
from ordeal.faults import Fault
from ordeal.introspection import safe_get_annotations
from ordeal.invariants import Invariant
from ordeal.quickcheck import strategy_for_type
# ============================================================================
# Result types
# ============================================================================


@dataclass
class FunctionResult:
    """Result of testing a single function."""

    name: str
    passed: bool
    execution_ok: bool = True
    verdict: str = "clean"
    error: str | None = None
    error_type: str | None = None
    failing_args: dict[str, Any] | None = None
    crash_category: str | None = None
    property_violations: list[str] = field(default_factory=list)
    property_violation_details: list[dict[str, Any]] = field(default_factory=list)
    contract_violations: list[str] = field(default_factory=list)
    contract_violation_details: list[dict[str, Any]] = field(default_factory=list)
    replayable: bool | None = None
    replay_attempts: int = 0
    replay_matches: int = 0
    minimization: dict[str, Any] | None = None
    contract_fit: float | None = None
    reachability: float | None = None
    realism: float | None = None
    sink_signal: float | None = None
    sink_categories: list[str] = field(default_factory=list)
    input_sources: list[dict[str, str]] = field(default_factory=list)
    input_source: str | None = None
    proof_bundle: dict[str, Any] | None = None
    source_sha256: str | None = None
    limitation_kind: str | None = None
    blocking_reason: str | None = None

    def __post_init__(self) -> None:
        """Normalize legacy manual constructions onto the verdict model."""
        if self.limitation_kind is not None:
            self.verdict = "blocked"
            self.passed = True
        if self.verdict == "clean":
            if self.crash_category is not None:
                self.verdict = _verdict_for_crash(self.crash_category)
            elif any(
                detail.get("category") == "lifecycle_contract"
                for detail in self.contract_violation_details
            ):
                self.verdict = "lifecycle_contract"
            elif any(
                detail.get("category") == "semantic_contract"
                for detail in self.contract_violation_details
            ):
                self.verdict = "semantic_contract"
            elif any(
                detail.get("category") == "expected_precondition_failure"
                for detail in self.contract_violation_details
            ):
                self.verdict = "expected_precondition_failure"
            elif self.property_violations:
                self.verdict = "exploratory_property"
        if self.crash_category is not None and self.error is not None and self.execution_ok:
            self.execution_ok = False
        if self.promoted:
            self.passed = False
        elif self.verdict == "semantic_contract" and self.contract_violations:
            self.passed = True
        elif (
            self.verdict != "clean"
            and not self.contract_violations
            and self.crash_category is not None
        ):
            self.passed = True

    @property
    def promoted(self) -> bool:
        """Whether this result should count as a promoted failure/finding."""
        if self.verdict == "promoted_real_bug":
            return _scan_crash_promoted(
                category=self.crash_category,
                replayable=self.replayable,
                proof_bundle=self.proof_bundle,
                sink_categories=self.sink_categories,
            )
        if self.verdict == "semantic_contract":
            return any(
                _contract_violation_promoted(detail) for detail in self.contract_violation_details
            )
        return self.verdict in {
            "promoted_real_bug",
            "lifecycle_contract",
        }

    @property
    def exploratory(self) -> bool:
        """Whether this result is exploratory instead of promoted."""
        return self.verdict in {
            "exploratory_crash",
            "exploratory_property",
            "coverage_gap",
            "invalid_input_crash",
            "beyond_declared_contract_robustness",
        } or (self.verdict in {"promoted_real_bug", "semantic_contract"} and not self.promoted)

    def __str__(self) -> str:
        if self.verdict == "blocked":
            reason = self.blocking_reason or self.error or "Ordeal could not reach the target"
            return f"  BLOCKED  {self.name}: {reason}"
        if self.execution_ok and not self.property_violations and not self.contract_violations:
            return f"  PASS  {self.name}"
        if not self.execution_ok:
            label = "FAIL" if self.promoted else "WARN"
            return f"  {label}  {self.name}: {self.error}"
        if self.contract_violations:
            viols = "; ".join(self.contract_violations)
            label = "FAIL" if self.promoted else "NOTE"
            return f"  {label}  {self.name}: {viols}"
        viols = "; ".join(self.property_violations)
        return f"  WARN  {self.name}: {viols}"
ScanMode = Literal["coverage_gap", "evidence", "real_bug", "candidate"]
_SCAN_MODE_ALIASES: dict[str, Literal["coverage_gap", "real_bug"]] = {
    "coverage_gap": "coverage_gap",
    "evidence": "coverage_gap",
    "real_bug": "real_bug",
    "candidate": "real_bug",
}
def _normalize_scan_mode(mode: str) -> Literal["coverage_gap", "real_bug"]:
    """Return the canonical internal scan mode for one public mode name."""
    normalized = _SCAN_MODE_ALIASES.get(str(mode).strip())
    if normalized is None:
        raise ValueError(f"mode must be one of {set(_SCAN_MODE_ALIASES)}, got {mode!r}")
    return normalized
def _public_scan_mode(mode: str) -> str:
    """Return the preferred public label for one scan mode."""
    normalized = _normalize_scan_mode(mode)
    return "candidate" if normalized == "real_bug" else "evidence"
_PROMOTED_SCAN_VERDICTS = {
    "promoted_real_bug",
    "semantic_contract",
    "lifecycle_contract",
}
_REPLAY_MATCH_BASIS = "same exception type, message, and terminal source location"
_CONTRACT_GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "transport": ("json_roundtrip", "http_shape"),
    "transport_semantics": ("json_roundtrip", "http_shape"),
    "wire": ("json_roundtrip", "http_shape"),
    "shell": ("shell_safe", "command_arg_stability", "subprocess_argv", "shell_injection"),
    "shell_path_safety": (
        "shell_safe",
        "command_arg_stability",
        "subprocess_argv",
        "shell_injection",
    ),
    "shell_injection_check": ("shell_injection",),
    "path": ("quoted_paths",),
    "env": ("protected_env_keys",),
    "protected_env_vars": ("protected_env_keys",),
    "cleanup_teardown": ("cleanup_attempts_all", "teardown_attempts_all"),
    "cancellation_safety": (
        "cleanup_after_cancellation",
        "rollout_cancellation_triggers_cleanup",
    ),
    "json_tool_call_normalization": ("json_roundtrip", "http_shape"),
}
_SECURITY_SINK_WEIGHTS: dict[str, float] = {
    "shell": 1.0,
    "subprocess": 1.0,
    "filesystem_write": 1.0,
    "deserialization": 1.0,
    "import": 0.95,
    "ipc": 0.95,
    "symlink": 0.9,
    "path": 0.85,
    "env": 0.75,
    "json_tool_call": 0.7,
    "http": 0.65,
    "sql": 0.75,
}
_SECURITY_BUCKET_TO_SINKS: dict[str, tuple[str, ...]] = {
    "shell": ("shell", "subprocess"),
    "path": ("path", "filesystem_write", "symlink"),
    "mapping": ("env", "json_tool_call", "http"),
    "json": ("json_tool_call", "deserialization"),
    "import": ("import",),
    "serialized": ("deserialization",),
    "ipc": ("ipc",),
    "symlink": ("symlink", "path"),
}
_SECURITY_PROBE_VALUES: dict[str, tuple[Any, ...]] = {
    "path": ("../security_probe.txt", "nested/../../security_probe.txt"),
    "symlink": ("link/../security_probe.txt",),
}
_SECURITY_ARTIFACT_MUTATION_VALUES: dict[str, tuple[Any, ...]] = {
    "serialized_bytes": (
        b'{"artifact":"../security_probe.txt","trace":"seed-1.json"}',
        b"ODRL\x00checkpoint",
    ),
    "serialized_text": (
        '{"artifact":"../security_probe.txt","trace":"seed-1.json"}',
        "checkpoint = '../security_probe.txt'\ntrace = 'seed-1.json'\n",
    ),
    "serialized_mapping": (
        {"artifact": "../security_probe.txt", "trace": "seed-1.json"},
        {"checkpoint": "seed-1", "resume": True},
    ),
    "json_text": (
        '{"config":"../security_probe.txt","kind":"artifact"}',
        '{"tool_call":{"name":"json","arguments":{"config":"../security_probe.txt"}}}',
    ),
    "json_mapping": (
        {"config": "../security_probe.txt", "kind": "artifact"},
        {"tool_call": {"name": "json", "arguments": {"config": "../security_probe.txt"}}},
    ),
    "ipc_text": ("ordeal-security-probe", "ordeal/security/probe"),
    "ipc_bytes": (b"ordeal-security-probe",),
    "ipc_mapping": (
        {"channel": "ordeal-security-probe", "checkpoint": "seed-1"},
        {"segment": "ordeal-security-probe", "descriptor": "ring-0"},
    ),
    "import_text": ("json", "json.tool"),
}
_SHELL_INJECTION_PROBE_VALUE = "ordeal-probe; echo ordeal_shell_probe"
_SHELL_INJECTION_META_CHARS = frozenset(";&|`$><()[]{}*?\n")
_SHELL_TAINT_CLEAN = 0
_SHELL_TAINT_SAFE = 1
_SHELL_TAINT_UNSAFE = 2
_DEFAULT_SHELL_INJECTION_SINK_SPECS: tuple[dict[str, Any], ...] = (
    {
        "pattern": r"(^|\.)subprocess\.(run|Popen|check_call|check_output)$",
        "category": "shell_injection",
        "taint_args": (0, "args", "command", "cmd", "argv"),
    },
    {
        "pattern": r"(^|\.)os\.(system|popen)$",
        "category": "shell_injection",
        "taint_args": (0, "command", "cmd"),
    },
    {
        "pattern": r"(^|\.)(execute_command|run_command|shell)$",
        "category": "shell_injection",
        "taint_args": (0, "command", "cmd"),
    },
)
_SECURITY_SHAPER_TOKENS = {
    "artifact",
    "build",
    "bundle",
    "checkpoint",
    "config",
    "decode",
    "descriptor",
    "frame",
    "header",
    "normalize",
    "parse",
    "path",
    "prepare",
    "render",
    "resolve",
    "seed",
    "target",
    "trace",
    "validate",
    "verify",
}
_SECURITY_SIDE_EFFECT_TOKENS = {
    "accept",
    "attach",
    "connect",
    "create",
    "delete",
    "dump",
    "execute",
    "import_module",
    "listen",
    "load_state",
    "loads(",
    "mkdir",
    "mmap",
    "open(",
    "pickle.load",
    "pickle.loads",
    "popen",
    "publish",
    "recv",
    "remove",
    "run(",
    "save",
    "send_bytes",
    "sharedmemory",
    "shared_memory(",
    "socket(",
    "subprocess",
    "unlink",
    "unpickler",
    "upload",
    "write(",
    "write_bytes",
    "write_text",
}
@dataclass(frozen=True)
class ContractCheck:
    """Explicit semantic contract probe for a scanned callable."""

    name: str
    predicate: Callable[[Any], bool] = field(repr=False)
    kwargs: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
@dataclass(frozen=True)
class _ShellInjectionFlow:
    """One static shell-injection path from input to sink."""

    sink: str
    line: int | None = None
    parameter: str | int | None = None
    call_path: tuple[str, ...] = ()
    source_params: tuple[str, ...] = ()
class ContractNotApplicable(RuntimeError):
    """Signal that a semantic contract does not apply to one observed output."""
@dataclass
class ScanResult:
    """Result of scanning a module."""

    module: str
    functions: list[FunctionResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    expected_failure_names: list[str] = field(default_factory=list)

    @property
    def results(self) -> list[FunctionResult]:
        """Deprecated alias for ``.functions``."""
        import warnings

        warnings.warn(
            "ScanResult.results was renamed to .functions",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.functions

    @property
    def expected_failures(self) -> list[FunctionResult]:
        """Functions that failed but were listed in *expected_failures*."""
        return [
            f for f in self.functions if not f.passed and f.name in self.expected_failure_names
        ]

    @property
    def passed(self) -> bool:
        """True if every tested function passed or is an expected failure."""
        return all(f.passed or f.name in self.expected_failure_names for f in self.functions)

    @property
    def total(self) -> int:
        return len(self.functions)

    @property
    def failed(self) -> int:
        return sum(
            1 for f in self.functions if not f.passed and f.name not in self.expected_failure_names
        )

    @property
    def verdict_counts(self) -> dict[str, int]:
        """Count scan results by their primary verdict."""
        counts: dict[str, int] = {}
        for function in self.functions:
            counts[function.verdict] = counts.get(function.verdict, 0) + 1
        return counts

    def summary(self) -> str:
        verdict_counts = self.verdict_counts
        bits = [f"{self.total} functions"]
        if self.failed:
            bits.append(f"{self.failed} promoted")
        if verdict_counts.get("coverage_gap"):
            bits.append(f"{verdict_counts['coverage_gap']} coverage gap(s)")
        exploratory = sum(
            verdict_counts.get(name, 0)
            for name in (
                "exploratory_crash",
                "exploratory_property",
                "invalid_input_crash",
                "beyond_declared_contract_robustness",
            )
        )
        if exploratory:
            bits.append(f"{exploratory} exploratory")
        if verdict_counts.get("expected_precondition_failure"):
            bits.append(f"{verdict_counts['expected_precondition_failure']} expected precondition")
        if verdict_counts.get("blocked"):
            bits.append(f"{verdict_counts['blocked']} blocked")
        lines = [f"scan_module({self.module!r}): {', '.join(bits)}"]
        for f in self.functions:
            if not f.passed and f.name in self.expected_failure_names:
                lines.append(f"  XFAIL {f.name}: {f.error}")
            else:
                lines.append(str(f))
        if self.expected_failure_names:
            lines.append(f"  ({len(self.expected_failures)} expected failure(s))")
        if self.skipped:
            reasons: dict[str, int] = {}
            for _, reason in self.skipped:
                reasons[reason] = reasons.get(reason, 0) + 1
            reason_bits = ", ".join(
                f"{count} {reason}" if count > 1 else f"1 {reason}"
                for reason, count in sorted(reasons.items())
            )
            lines.append(f"  ({len(self.skipped)} skipped: {reason_bits})")
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)
@dataclass
class FuzzResult:
    """Result of fuzzing a single function."""

    function: str
    examples: int
    failures: list[Exception] = field(default_factory=list)
    failing_args: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    def summary(self) -> str:
        if self.passed:
            return f"fuzz({self.function}): {self.examples} examples, passed"
        lines = [
            f"fuzz({self.function}): {self.examples} examples, "
            f"{len(self.failures)} failure(s): {self.failures[0]}"
        ]
        if self.failing_args is not None:
            lines.append(f"  Failing input: {self.failing_args!r}")
        return "\n".join(lines)
