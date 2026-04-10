"""Zero-boilerplate testing. Point at code, get tests.

Three primitives:

1. **scan_module** — smoke-test every public function::

       result = scan_module("myapp.scoring")
       assert result.passed

2. **fuzz** — deep-fuzz a single function::

       result = fuzz(myapp.scoring.compute)
       result = fuzz(myapp.scoring.compute, model=model_strategy)

3. **chaos_for** — auto-generate a ChaosTest from a module's API::

       TestScoring = chaos_for("myapp.scoring", invariants=[finite])
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import copy
import fnmatch
import functools
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
    contract_fit: float | None = None
    reachability: float | None = None
    realism: float | None = None
    sink_signal: float | None = None
    sink_categories: list[str] = field(default_factory=list)
    input_sources: list[dict[str, str]] = field(default_factory=list)
    input_source: str | None = None
    proof_bundle: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Normalize legacy manual constructions onto the verdict model."""
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


# ============================================================================
# Common parameter name → strategy inference
# ============================================================================

# Maps parameter names (and patterns) to sensible Hypothesis strategies.
# This eliminates the most common fixture boilerplate — users only need
# to provide strategies for truly domain-specific types.
COMMON_NAME_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    # Text
    "text": st.text(min_size=0, max_size=200),
    "prompt": st.text(min_size=1, max_size=200),
    "response": st.text(min_size=0, max_size=500),
    "message": st.text(min_size=1, max_size=200),
    "content": st.text(min_size=0, max_size=500),
    "query": st.text(min_size=1, max_size=200),
    "input": st.text(min_size=0, max_size=200),
    "output": st.text(min_size=0, max_size=500),
    "label": st.text(min_size=1, max_size=50),
    "name": st.text(min_size=1, max_size=50),
    "key": st.text(min_size=1, max_size=50),
    "description": st.text(min_size=0, max_size=200),
    # Numeric
    "seed": st.integers(min_value=0, max_value=2**31 - 1),
    "random_seed": st.integers(min_value=0, max_value=2**31 - 1),
    "threshold": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "alpha": st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    "probability": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "weight": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "tolerance": st.floats(min_value=1e-10, max_value=0.1, allow_nan=False),
    "tol": st.floats(min_value=1e-10, max_value=0.1, allow_nan=False),
    "count": st.integers(min_value=0, max_value=100),
    "n": st.integers(min_value=1, max_value=100),
    "size": st.integers(min_value=1, max_value=100),
    "max_tokens": st.integers(min_value=1, max_value=500),
    "max_iterations": st.integers(min_value=1, max_value=20),
    "num_prompts": st.integers(min_value=1, max_value=50),
    "top_k": st.integers(min_value=1, max_value=10),
    "batch_size": st.integers(min_value=1, max_value=32),
    # Boolean
    "verbose": st.booleans(),
    "strict": st.booleans(),
    "normalize": st.booleans(),
}

# Suffix patterns: if param name ends with these, use this strategy
_SUFFIX_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    "_text": st.text(min_size=0, max_size=200),
    "_path": st.text(min_size=1, max_size=50),
    "_count": st.integers(min_value=0, max_value=100),
    "_size": st.integers(min_value=1, max_value=100),
    "_rate": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_prob": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_threshold": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_weight": st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "_flag": st.booleans(),
    "_enabled": st.booleans(),
}


# User-registered strategies (project-specific, added at runtime)
_REGISTERED_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {}
_REGISTERED_OBJECT_FACTORIES: dict[str, Any] = {}
_REGISTERED_OBJECT_SETUPS: dict[str, Any] = {}
_REGISTERED_OBJECT_SCENARIOS: dict[str, Any] = {}
_REGISTERED_OBJECT_STATE_FACTORIES: dict[str, Any] = {}
_REGISTERED_OBJECT_TEARDOWNS: dict[str, Any] = {}
_REGISTERED_OBJECT_HARNESSES: dict[str, str] = {}
_BOUNDARY_SMOKE_VALUES: dict[object, tuple[object, ...]] = {
    bool: (False, True),
    int: (0, 1, -1),
    float: (0.0, 1.0, -1.0),
    str: ("", "a"),
    bytes: (b"", b"x"),
}
_VALID_SCAN_MODES: set[str] = set(_SCAN_MODE_ALIASES)
_WEAK_CONTRACT_FIT = 0.35
_SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS = 0.55
_SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS = 0.55
_SEMANTIC_CONTRACT_STRONG_REALISM = 0.75


@dataclass(frozen=True)
class SeedExample:
    """One concrete input shape derived from existing code or tests."""

    kwargs: dict[str, Any]
    source: str
    evidence: str


@dataclass(frozen=True)
class CandidateInput:
    """One deterministic input candidate with provenance metadata."""

    kwargs: dict[str, Any]
    origin: str
    rationale: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessHint:
    """One mined suggestion for configuring a stateful object target."""

    kind: str
    suggestion: str
    evidence: str
    confidence: float = 0.5
    score: float = 0.5
    signals: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutoObjectRuntime:
    """One conservative runtime assembled from mined harness hints."""

    factory: Any | None = None
    factory_source: str | None = None
    setup: Any | None = None
    setup_source: str | None = None
    state_factory: Any | None = None
    state_factory_source: str | None = None
    teardown: Any | None = None
    teardown_source: str | None = None
    scenarios: tuple[Any, ...] = ()
    scenario_source: str | None = None
    harness: str | None = None
    harness_source: str | None = None
    hints: tuple[HarnessHint, ...] = ()


_DEFAULT_AUTO_CONTRACTS = (
    "shell_safe",
    "quoted_paths",
    "command_arg_stability",
    "protected_env_keys",
    "json_roundtrip",
    "http_shape",
    "subprocess_argv",
    "lifecycle_attempts_all",
    "lifecycle_followup",
)

_HARNESS_HINT_SIGNAL_WEIGHTS: dict[str, float] = {
    "returns_target_instance": 0.28,
    "constructor_like": 0.1,
    "mentions_target_tokens": 0.08,
    "pytest_fixture": 0.06,
    "test_evidence": 0.05,
    "support_file": 0.06,
    "state_compatible": 0.14,
    "returns_mapping": 0.08,
    "lifecycle_cleanup": 0.08,
    "collaborator_overlap": 0.08,
    "doc_evidence": 0.03,
}


@functools.lru_cache(maxsize=512)
def _read_python_source(path_str: str) -> str:
    """Read one Python file with a tiny cache for repeated scan lookups."""
    return Path(path_str).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=512)
def _parse_python_source(path_str: str) -> ast.AST | None:
    """Parse one Python file, returning ``None`` on syntax or I/O failure."""
    try:
        return ast.parse(_read_python_source(path_str))
    except Exception:
        return None


def _literal_ast_value(node: ast.AST) -> Any:
    """Return a Python literal from *node*, or ``None`` when unsupported."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        values = [_literal_ast_value(item) for item in node.elts]
        if any(value is _MISSING for value in values):
            return _MISSING
        return values
    if isinstance(node, ast.Tuple):
        values = [_literal_ast_value(item) for item in node.elts]
        if any(value is _MISSING for value in values):
            return _MISSING
        return tuple(values)
    if isinstance(node, ast.Set):
        values = [_literal_ast_value(item) for item in node.elts]
        if any(value is _MISSING for value in values):
            return _MISSING
        return set(values)
    if isinstance(node, ast.Dict):
        keys = [_literal_ast_value(item) for item in node.keys]
        values = [_literal_ast_value(item) for item in node.values]
        if any(item is _MISSING for item in (*keys, *values)):
            return _MISSING
        return dict(zip(keys, values, strict=False))
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        operand = node.operand.value
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Call):
        func_name = _call_name(node.func)
        if (
            func_name
            in {
                "Path",
                "PurePath",
                "PosixPath",
                "WindowsPath",
                "pathlib.Path",
                "pathlib.PurePath",
                "pathlib.PosixPath",
                "pathlib.WindowsPath",
            }
            and len(node.args) == 1
        ):
            value = _literal_ast_value(node.args[0])
            if isinstance(value, str):
                return Path(value)
    return _MISSING


def _literal_ast_value_with_env(
    node: ast.AST,
    bindings: Mapping[str, Any] | None = None,
) -> Any:
    """Return a literal value, resolving simple local-name bindings when available."""
    if isinstance(node, ast.Name) and bindings is not None:
        if node.id in bindings:
            return bindings[node.id]
    return _literal_ast_value(node)


_MISSING = object()


def _call_name(node: ast.AST) -> str | None:
    """Return the dotted name for *node* when it is name-like."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        root = _call_name(node.value)
        if root is None:
            return None
        return f"{root}.{node.attr}"
    return None


def _symbol_hint_value(path: Path, symbol_name: str) -> str:
    """Return a TOML-ready file-path symbol reference for one local helper."""
    try:
        display = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        display = path.resolve()
    return f"{display.as_posix()}:{symbol_name}"


def _simple_assigned_names(target: ast.AST) -> list[str]:
    """Return simple local names assigned by *target*."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in target.elts:
            names.extend(_simple_assigned_names(item))
        return names
    return []


def _iter_scope_statements(scope: ast.AST) -> Sequence[ast.stmt]:
    """Return the direct statements for a module or function scope."""
    if isinstance(scope, ast.Module):
        return scope.body
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return scope.body
    return ()


def _yield_cleanup_mentions(node: ast.AST) -> bool:
    """Return whether the post-yield body mentions teardown-like cleanup."""
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr.lower() in {"cleanup", "close", "stop", "teardown", "reset"}
        for child in ast.walk(node)
    )


def _callable_supports_optional_instance_call(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Return whether *node* can be called with zero args or one instance arg."""
    positional = [*node.args.posonlyargs, *node.args.args]
    if positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    positional_defaults = [
        None,
    ] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    required_positional = [
        arg
        for arg, default in zip(positional, positional_defaults, strict=True)
        if default is None
    ]
    required_kwonly = [
        arg
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
        if default is None
    ]
    return not required_kwonly and len(required_positional) <= 1


@functools.lru_cache(maxsize=64)
def _pytest_fixture_catalog(path_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Return simple metadata for pytest fixtures across the given files."""
    catalog: dict[str, dict[str, Any]] = {}
    for path_str in path_keys:
        tree = _parse_python_source(path_str)
        if tree is None:
            continue
        path = Path(path_str).resolve()
        try:
            display_path = path.relative_to(Path.cwd())
        except ValueError:
            display_path = path
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_fixture = any(
                _call_name(decorator.func) == "pytest.fixture"
                if isinstance(decorator, ast.Call)
                else _call_name(decorator) == "pytest.fixture"
                for decorator in node.decorator_list
            )
            if not is_fixture:
                continue
            info = catalog.setdefault(
                node.name,
                {
                    "values": [],
                    "return_names": set(),
                    "yield_cleanup": False,
                    "text": "",
                    "evidence": f"{display_path}:{getattr(node, 'lineno', '?')}",
                    "symbol": _symbol_hint_value(path, node.name),
                },
            )
            annotation_text = ""
            with contextlib.suppress(Exception):
                if getattr(node, "returns", None) is not None:
                    annotation_text = ast.unparse(node.returns)
            body_text = ""
            with contextlib.suppress(Exception):
                body_text = " ".join(ast.unparse(stmt) for stmt in node.body[:8])
            text_parts = [node.name, annotation_text, ast.get_docstring(node) or "", body_text]
            info["text"] = " ".join(str(part).lower() for part in text_parts if part).strip()
            if annotation_text:
                lowered = annotation_text.lower()
                info["return_names"].add(lowered)
                info["return_names"].update(
                    token.lower()
                    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", annotation_text)
                )
            saw_yield = False
            yield_index: int | None = None
            for index, stmt in enumerate(node.body):
                value_node: ast.AST | None = None
                if isinstance(stmt, ast.Return):
                    value_node = stmt.value
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
                    value_node = stmt.value.value
                    saw_yield = True
                    yield_index = index
                if value_node is None:
                    if saw_yield and _yield_cleanup_mentions(stmt):
                        info["yield_cleanup"] = True
                    continue
                value = _literal_ast_value(value_node)
                if value is not _MISSING and value not in info["values"]:
                    info["values"].append(value)
                if isinstance(value_node, ast.Call):
                    callee = _call_name(value_node.func)
                    if callee:
                        info["return_names"].add(callee.lower())
                        info["return_names"].add(callee.rsplit(".", 1)[-1].lower())
            if saw_yield and yield_index is not None:
                trailing = node.body[yield_index + 1 :]
                if any(_yield_cleanup_mentions(item) for item in trailing):
                    info["yield_cleanup"] = True
    return catalog


def _scope_literal_bindings(
    scope: ast.AST,
    fixture_catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return simple literal bindings visible inside one module or function scope."""
    bindings: dict[str, Any] = {}
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]:
            values = list(fixture_catalog.get(arg.arg, {}).get("values", ()))
            if values:
                bindings[arg.arg] = values[0]
    for stmt in _iter_scope_statements(scope):
        if isinstance(stmt, ast.Assign):
            value = _literal_ast_value_with_env(stmt.value, bindings)
            if value is _MISSING:
                continue
            for target in stmt.targets:
                for name in _simple_assigned_names(target):
                    bindings[name] = value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            value = _literal_ast_value_with_env(stmt.value, bindings)
            if value is _MISSING:
                continue
            for name in _simple_assigned_names(stmt.target):
                bindings[name] = value
    return bindings


def _class_import_aliases(
    tree: ast.AST,
    *,
    module_name: str,
    class_name: str | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Return imported module, callable, and class aliases for a target module."""
    module_aliases: set[str] = set()
    callable_aliases: set[str] = set()
    class_aliases: set[str] = {class_name} if class_name else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            for alias in node.names:
                if imported_module == module_name:
                    imported_name = alias.asname or alias.name
                    callable_aliases.add(imported_name)
                    if class_name and alias.name == class_name:
                        class_aliases.add(imported_name)
                elif f"{imported_module}.{alias.name}" == module_name:
                    module_aliases.add(alias.asname or alias.name)
    return module_aliases, callable_aliases, class_aliases


def _factory_like_helper_names(
    tree: ast.AST,
    *,
    class_tokens: set[str],
    fixture_catalog: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Return helper names that likely build the target object."""
    helpers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        info = fixture_catalog.get(node.name, {})
        lowered = str(info.get("text", node.name)).lower()
        return_names = {str(item).lower() for item in info.get("return_names", set())}
        if (
            node.name.startswith(("make_", "build_", "create_", "new_"))
            or any(token in node.name.lower() for token in class_tokens)
        ) and (return_names & class_tokens or any(token in lowered for token in class_tokens)):
            helpers.add(node.name)
    return helpers


def _matches_target_fixture(
    info: Mapping[str, Any] | None,
    *,
    class_tokens: set[str],
) -> bool:
    """Return whether one fixture metadata record looks like the target object."""
    if not info:
        return False
    return_names = {str(item).lower() for item in info.get("return_names", set())}
    lowered = str(info.get("text", "")).lower()
    return bool(return_names & class_tokens) or any(token in lowered for token in class_tokens)


def _scope_instance_names(
    scope: ast.AST,
    *,
    class_tokens: set[str],
    class_aliases: set[str],
    factory_names: set[str],
    fixture_catalog: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Return local names in *scope* that likely hold the target instance."""
    instance_names: set[str] = set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]:
            info = fixture_catalog.get(arg.arg)
            if _matches_target_fixture(info, class_tokens=class_tokens) or any(
                token in arg.arg.lower() for token in class_tokens
            ):
                instance_names.add(arg.arg)
    for stmt in _iter_scope_statements(scope):
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        callee = _call_name(value.func)
        if callee is None:
            continue
        leaf = callee.rsplit(".", 1)[-1]
        if leaf not in class_aliases and leaf not in factory_names:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for target in targets:
            for name in _simple_assigned_names(target):
                instance_names.add(name)
    return instance_names


def _call_matches_bound_method(
    call: ast.Call,
    *,
    method_name: str,
    instance_names: set[str],
    class_aliases: set[str],
    factory_names: set[str],
) -> bool:
    """Return whether *call* looks like a target instance-method invocation."""
    if not isinstance(call.func, ast.Attribute) or call.func.attr != method_name:
        return False
    receiver = call.func.value
    if isinstance(receiver, ast.Name):
        return receiver.id in instance_names
    if isinstance(receiver, ast.Call):
        callee = _call_name(receiver.func)
        if callee is None:
            return False
        leaf = callee.rsplit(".", 1)[-1]
        return leaf in class_aliases or leaf in factory_names
    return False


def _append_seed_example(
    bucket: list[SeedExample],
    *,
    kwargs: dict[str, Any],
    source: str,
    evidence: str,
) -> None:
    """Append one seed example when its kwargs are not already present."""
    if not kwargs:
        return
    for existing in bucket:
        if existing.kwargs == kwargs:
            return
    bucket.append(SeedExample(kwargs=dict(kwargs), source=source, evidence=evidence))


def _source_file_for_callable(func: Any) -> Path | None:
    """Return the source file for *func* when available."""
    with contextlib.suppress(OSError, TypeError):
        source_file = inspect.getsourcefile(_unwrap(func)) or inspect.getfile(_unwrap(func))
        if source_file:
            return Path(source_file).resolve()
    return None


@functools.lru_cache(maxsize=128)
def _candidate_seed_files_cached(
    module_name: str,
    workspace_root: str,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return likely test files and project call-site files for *module_name*."""
    root_path = Path(workspace_root)
    test_files: list[Path] = []
    project_files: list[Path] = []

    tests_dir = root_path / "tests"
    if tests_dir.is_dir():
        with contextlib.suppress(Exception):
            from ordeal.audit import _find_test_file_evidence

            test_files = [
                Path(item.path)
                for item in _find_test_file_evidence(module_name, tests_dir)
                if Path(item.path).is_file()
            ]

    module_path_parts = module_name.split(".")
    roots = [root_path, root_path / "src"]
    module_path: Path | None = None
    for root in roots:
        candidate = root.joinpath(*module_path_parts)
        if candidate.with_suffix(".py").exists():
            module_path = candidate.with_suffix(".py")
            break
        if (candidate / "__init__.py").exists():
            module_path = candidate / "__init__.py"
            break
    if module_path is None:
        return tuple(test_files), tuple(project_files)

    package_root = module_path.parent
    for path in package_root.rglob("*.py"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == module_path.resolve():
            continue
        if any(part in {"tests", ".venv", "site-packages"} for part in resolved.parts):
            continue
        project_files.append(resolved)
        if len(project_files) >= 24:
            break
    return tuple(test_files), tuple(project_files)


def _candidate_seed_files(module_name: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return likely test files and project call-site files for *module_name*."""
    return _candidate_seed_files_cached(module_name, str(Path.cwd().resolve()))


def _fixture_literals_for_params(
    param_names: set[str],
    files: Sequence[Path],
) -> dict[str, list[Any]]:
    """Return literal pytest fixture values keyed by matching parameter name."""
    fixtures: dict[str, list[Any]] = {}
    catalog = _pytest_fixture_catalog(
        tuple(str(path.resolve()) for path in files if path.exists())
    )
    for name in sorted(param_names):
        values = list(catalog.get(name, {}).get("values", ()))
        if values:
            fixtures[name] = values
    return fixtures


def _seed_value_from_node(
    node: ast.AST,
    *,
    bindings: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve a seed value from a literal node or a bound local name."""
    if isinstance(node, ast.Name) and bindings and node.id in bindings:
        return bindings[node.id]
    return _literal_ast_value(node)


def _parametrize_arg_names(node: ast.AST) -> list[str]:
    """Return parameter names from a ``pytest.mark.parametrize`` decorator."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [item.strip() for item in node.value.split(",") if item.strip()]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                text = item.value.strip()
                if text:
                    names.append(text)
        return names
    return []


def _parametrize_bindings(values_node: ast.AST, names: Sequence[str]) -> list[dict[str, Any]]:
    """Return literal binding rows from a ``pytest.mark.parametrize`` value node."""
    if not names:
        return []
    rows_value = _literal_ast_value(values_node)
    if rows_value is _MISSING:
        return []
    if len(names) == 1 and not isinstance(rows_value, (list, tuple)):
        return [{names[0]: rows_value}]
    rows = list(rows_value) if isinstance(rows_value, (list, tuple)) else [rows_value]
    bindings: list[dict[str, Any]] = []
    for row in rows:
        if len(names) == 1:
            bindings.append({names[0]: row})
            continue
        if not isinstance(row, (list, tuple)) or len(row) != len(names):
            continue
        bindings.append(dict(zip(names, row, strict=False)))
    return bindings


def _function_parametrize_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict[str, Any]]:
    """Return merged literal binding rows for one parametrized test function."""
    cases: list[dict[str, Any]] = [{}]
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if _call_name(decorator.func) not in {"pytest.mark.parametrize", "mark.parametrize"}:
            continue
        if len(decorator.args) < 2:
            continue
        names = _parametrize_arg_names(decorator.args[0])
        rows = _parametrize_bindings(decorator.args[1], names) if names else []
        if not names or not rows:
            continue
        expanded: list[dict[str, Any]] = []
        for base in cases:
            for row in rows:
                merged = dict(base)
                merged.update(row)
                expanded.append(merged)
        if expanded:
            cases = expanded
    return cases or [{}]


def _call_seed_examples_from_files(
    func: Any,
    files: Sequence[Path],
    *,
    source: str,
) -> list[SeedExample]:
    """Extract literal call examples for *func* from Python files."""
    callable_obj = func
    target = _unwrap(callable_obj)
    try:
        sig = inspect.signature(target)
    except Exception:
        return []

    module_name = getattr(target, "__module__", "")
    leaf_name = getattr(target, "__name__", "")
    if not module_name or not leaf_name:
        return []
    method_name = str(getattr(callable_obj, "__ordeal_method_name__", leaf_name) or leaf_name)
    owner = getattr(callable_obj, "__ordeal_owner__", None)
    callable_kind = getattr(callable_obj, "__ordeal_kind__", None)
    class_tokens = {
        token.lower()
        for token in (
            [getattr(owner, "__name__", "")] + _camel_case_tokens(getattr(owner, "__name__", ""))
        )
        if token
    }

    param_names = [name for name in sig.parameters if name not in {"self", "cls"}]
    if not param_names:
        return []
    hidden_state_param = None
    if getattr(callable_obj, "__ordeal_state_factory__", None) is not None:
        hidden_state_param = getattr(callable_obj, "__ordeal_state_param__", None)

    examples: list[SeedExample] = []
    fixture_paths: set[Path] = {path.resolve() for path in files if path.exists()}
    for path in list(fixture_paths):
        for parent in [path.parent, *path.parents]:
            candidate = parent / "conftest.py"
            if candidate.exists():
                fixture_paths.add(candidate.resolve())
            if parent.resolve() == Path.cwd().resolve():
                break
    fixture_catalog = _pytest_fixture_catalog(tuple(str(path) for path in sorted(fixture_paths)))
    for path in files:
        tree = _parse_python_source(str(path))
        if tree is None:
            continue

        module_aliases, imported_names, class_aliases = _class_import_aliases(
            tree,
            module_name=module_name,
            class_name=getattr(owner, "__name__", None),
        )
        factory_names = _factory_like_helper_names(
            tree,
            class_tokens=class_tokens,
            fixture_catalog=fixture_catalog,
        )

        scopes: list[tuple[ast.AST, list[dict[str, Any]]]] = [(tree, [{}])]
        scopes.extend(
            (node, _function_parametrize_bindings(node))
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for scope, bindings_list in scopes:
            base_bindings = _scope_literal_bindings(scope, fixture_catalog)
            instance_names = _scope_instance_names(
                scope,
                class_tokens=class_tokens,
                class_aliases=class_aliases,
                factory_names=factory_names,
                fixture_catalog=fixture_catalog,
            )
            for stmt in _iter_scope_statements(scope):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for node in ast.walk(stmt):
                    if not isinstance(node, ast.Call):
                        continue
                    if callable_kind == "instance":
                        matched = _call_matches_bound_method(
                            node,
                            method_name=method_name,
                            instance_names=instance_names,
                            class_aliases=class_aliases,
                            factory_names=factory_names,
                        )
                    else:
                        func_name = _call_name(node.func)
                        if func_name is None:
                            continue
                        matched = func_name == leaf_name or func_name.split(".")[-1] == leaf_name
                        if not matched:
                            continue
                        if (
                            "." in func_name
                            and func_name.split(".", 1)[0] not in module_aliases
                            and func_name.split(".")[-1] != leaf_name
                        ):
                            continue
                        if (
                            "." not in func_name
                            and imported_names
                            and func_name not in imported_names
                        ):
                            continue

                    if not matched:
                        continue

                    for bindings in bindings_list or [{}]:
                        merged_bindings = dict(base_bindings)
                        merged_bindings.update(bindings)
                        kwargs: dict[str, Any] = {}
                        positional_params = iter(param_names)
                        supported = True
                        call_args = list(node.args)
                        if hidden_state_param and len(call_args) == len(param_names) + 1:
                            call_args = call_args[1:]
                        for arg in call_args:
                            param_name = next(positional_params, None)
                            if param_name is None:
                                supported = False
                                break
                            value = _seed_value_from_node(arg, bindings=merged_bindings)
                            if value is _MISSING:
                                supported = False
                                break
                            kwargs[param_name] = value
                        if not supported:
                            continue
                        for kw in node.keywords:
                            if kw.arg is None:
                                supported = False
                                break
                            if hidden_state_param and kw.arg == hidden_state_param:
                                continue
                            value = _seed_value_from_node(kw.value, bindings=merged_bindings)
                            if value is _MISSING:
                                supported = False
                                break
                            kwargs[kw.arg] = value
                        if not supported or not kwargs:
                            continue
                        _append_seed_example(
                            examples,
                            kwargs=kwargs,
                            source=source,
                            evidence=f"{path.name}:{getattr(node, 'lineno', 0)}",
                        )
    return examples


def _doctest_seed_examples(func: Any) -> list[SeedExample]:
    """Extract literal call examples from doctest-style docstrings."""
    target = _unwrap(func)
    doc = inspect.getdoc(target) or ""
    name = getattr(target, "__name__", "")
    if not doc or not name:
        return []

    examples: list[SeedExample] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped.startswith(">>> ") or f"{name}(" not in stripped:
            continue
        expr = stripped.removeprefix(">>> ").strip()
        try:
            node = ast.parse(expr, mode="eval").body
        except SyntaxError:
            continue
        if not isinstance(node, ast.Call):
            continue
        try:
            sig = inspect.signature(target)
        except Exception:
            continue
        param_names = [param for param in sig.parameters if param not in {"self", "cls"}]
        kwargs: dict[str, Any] = {}
        supported = True
        for param_name, arg in zip(param_names, node.args, strict=False):
            value = _literal_ast_value(arg)
            if value is _MISSING:
                supported = False
                break
            kwargs[param_name] = value
        for kw in node.keywords:
            if kw.arg is None:
                supported = False
                break
            value = _literal_ast_value(kw.value)
            if value is _MISSING:
                supported = False
                break
            kwargs[kw.arg] = value
        if supported and kwargs:
            _append_seed_example(
                examples,
                kwargs=kwargs,
                source="docstring",
                evidence=stripped,
            )
    return examples


def _numeric_boundary_neighbors(value: int | float) -> list[Any]:
    """Return nearby values for one numeric boundary witness."""
    if isinstance(value, bool):
        return [value]
    if isinstance(value, int):
        return [value, value + 1, value - 1]
    return [value, value + 1.0, value - 1.0]


def _source_boundary_examples(func: Any) -> list[SeedExample]:
    """Mine explicit boundary constants from comparisons in the function body."""
    target = _unwrap(func)
    source_file = _source_file_for_callable(target)
    if source_file is None:
        return []
    try:
        source_text = inspect.getsource(target)
    except (OSError, TypeError):
        return []
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []

    try:
        sig = inspect.signature(target)
    except Exception:
        return []
    params = [name for name in sig.parameters if name not in {"self", "cls"}]
    if not params:
        return []

    hints = safe_get_annotations(target)
    base_kwargs: dict[str, Any] = {}
    for name in params:
        values = []
        if name in hints:
            values.extend(_boundary_values_for_hint(hints[name]))
        if values:
            base_kwargs[name] = values[0]
    if set(base_kwargs) != set(params):
        return []
    examples: list[SeedExample] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        lhs = _call_name(node.left)
        if lhs not in params:
            continue
        for comp in node.comparators:
            value = _literal_ast_value(comp)
            if value is _MISSING:
                continue
            values = (
                _numeric_boundary_neighbors(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else [value]
            )
            for candidate_value in values:
                kwargs = dict(base_kwargs)
                kwargs[lhs] = candidate_value
                _append_seed_example(
                    examples,
                    kwargs=kwargs,
                    source="source_boundary",
                    evidence=f"{source_file.name}:{getattr(node, 'lineno', 0)}",
                )
    return examples


def _seed_examples_for_callable(
    func: Any,
    *,
    seed_from_tests: bool,
    seed_from_fixtures: bool,
    seed_from_docstrings: bool,
    seed_from_code: bool,
    seed_from_call_sites: bool,
) -> list[SeedExample]:
    """Collect concrete input witnesses from tests, fixtures, docs, and code."""
    target = _unwrap(func)
    module_name = getattr(target, "__module__", "")
    if not module_name:
        return []

    examples: list[SeedExample] = []
    test_files, project_files = _candidate_seed_files(module_name)

    try:
        sig = inspect.signature(target)
        param_names = {name for name in sig.parameters if name not in {"self", "cls"}}
    except Exception:
        param_names = set()

    if seed_from_fixtures and param_names:
        fixture_files = [
            Path.cwd() / "conftest.py",
            Path.cwd() / "tests" / "conftest.py",
            *test_files,
        ]
        fixture_values = _fixture_literals_for_params(param_names, fixture_files)
        if fixture_values:
            kwargs = {name: values[0] for name, values in fixture_values.items() if values}
            _append_seed_example(
                examples,
                kwargs=kwargs,
                source="fixture",
                evidence=", ".join(sorted(fixture_values)[:4]),
            )

    if seed_from_tests:
        for example in _call_seed_examples_from_files(target, test_files, source="test"):
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )

    if seed_from_docstrings:
        for example in _doctest_seed_examples(target):
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )

    if seed_from_code:
        for example in _source_boundary_examples(target):
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )

    if seed_from_call_sites:
        for example in _call_seed_examples_from_files(target, project_files, source="call_site"):
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )
    return examples


def _seed_values_for_param(seed_examples: Sequence[SeedExample], name: str) -> list[Any]:
    """Return distinct observed values for one parameter from seed examples."""
    values: list[Any] = []
    for example in seed_examples:
        if name not in example.kwargs:
            continue
        value = example.kwargs[name]
        if any(existing == value for existing in values):
            continue
        values.append(value)
    return values


def _bias_strategies_with_seed_examples(
    strategies: dict[str, st.SearchStrategy[Any]],
    seed_examples: Sequence[SeedExample],
) -> dict[str, st.SearchStrategy[Any]]:
    """Bias inferred strategies toward previously observed concrete values."""
    biased: dict[str, st.SearchStrategy[Any]] = {}
    for name, strategy in strategies.items():
        seed_values = _seed_values_for_param(seed_examples, name)
        if not seed_values:
            biased[name] = strategy
            continue
        biased[name] = st.one_of(st.sampled_from(seed_values), strategy)
    return biased


def _hint_accepts_value(hint: Any, value: Any) -> bool:
    """Return whether a hint provides positive evidence for *value*."""
    if hint in {Any, object, inspect._empty}:
        return True
    if hint is None:
        return value is None
    origin = get_origin(hint)
    if origin is Literal:
        return value in get_args(hint)
    if origin is Union:
        return any(_hint_accepts_value(arg, value) for arg in get_args(hint))
    if hint is type(None):
        return value is None
    if value is None:
        return False
    if origin is not None:
        with contextlib.suppress(TypeError):
            return isinstance(value, origin)
        return True
    with contextlib.suppress(TypeError):
        return isinstance(value, hint)
    return True


def _hint_is_weak(hint: Any) -> bool:
    """Return whether *hint* is too broad to justify arbitrary fuzzing."""
    return hint in {Any, object, inspect._empty} or hint is None


def _contract_assessment(
    func: Any,
    kwargs: dict[str, Any],
    *,
    seed_examples: Sequence[SeedExample],
    treat_any_as_weak: bool,
) -> tuple[float, float, list[str], list[dict[str, str]]]:
    """Score contract fit and reachability for one concrete input."""
    target = _unwrap(func)
    hints = safe_get_annotations(target)
    reasons: list[str] = []
    evidence: list[dict[str, str]] = []
    fit = 0.0
    reachability = 0.0

    for example in seed_examples:
        if example.kwargs == kwargs:
            weight = {
                "test": 0.45,
                "fixture": 0.4,
                "call_site": 0.35,
                "docstring": 0.25,
                "source_boundary": 0.25,
            }.get(example.source, 0.2)
            fit += weight
            reachability += weight
            evidence.append(
                {
                    "source": example.source,
                    "detail": example.evidence,
                }
            )

    for name, value in kwargs.items():
        hint = hints.get(name, inspect._empty)
        if hint is inspect._empty:
            fit += 0.04
            if treat_any_as_weak:
                reasons.append(f"{name} lacks a precise type hint")
            continue
        if _hint_accepts_value(hint, value):
            if treat_any_as_weak and _hint_is_weak(hint):
                fit += 0.03
                reasons.append(f"{name} uses a broad annotation")
            else:
                fit += 0.14
        else:
            fit -= 0.35
            reasons.append(f"{name} does not match its type hint")
            if value is None:
                reasons.append(f"{name}=None is outside the annotated contract")

    fit = max(0.0, min(fit, 1.0))
    if not evidence:
        reachability += fit * 0.5
    reachability = max(0.0, min(reachability, 1.0))
    return fit, reachability, reasons, evidence


def _sink_category_matches(
    func: Any,
    *,
    security_focus: bool = False,
) -> list[tuple[str, bool, bool]]:
    """Return inferred sink categories plus whether source/param evidence matched."""
    target = _unwrap(func)
    source = ""
    with contextlib.suppress(OSError, TypeError):
        source = inspect.getsource(target).lower()
    try:
        params = {
            name.lower()
            for name in inspect.signature(target).parameters
            if name not in {"self", "cls"}
        }
    except Exception:
        params = set()

    categories: list[tuple[str, bool, bool]] = []
    checks = [
        (
            "shell",
            ("shell=True", "shlex", "execute_command", "subprocess", "command"),
            {"command", "cmd", "argv"},
        ),
        (
            "path",
            ("path", "quote", "upload_content"),
            {"path", "cwd", "workdir", "filename"},
        ),
        (
            "env",
            ("setdefault", "environ", "env_vars", "os.environ"),
            {"env", "env_vars", "environment"},
        ),
        (
            "json_tool_call",
            (
                "json.dumps",
                "json.dump",
                "json.loads",
                "json.load",
                "tool_call",
                "tool_calls",
                "response.json",
                "request.json",
                "model_dump",
                "model_validate_json",
            ),
            {"payload", "tool_call", "tool_calls"},
        ),
        (
            "http",
            (
                "http://",
                "https://",
                "httpx.",
                "requests.",
                "aiohttp",
                "status_code",
                ".headers",
                "content-type",
            ),
            {"headers", "body", "payload", "request"},
        ),
        (
            "sql",
            ("select ", "insert ", "update ", "delete ", "execute("),
            {"query", "sql"},
        ),
        (
            "subprocess",
            ("subprocess", "argv", "run(", "popen("),
            {"argv", "command", "cmd"},
        ),
    ]
    if security_focus:
        checks.extend(
            [
                (
                    "filesystem_write",
                    (
                        "write_text",
                        "write_bytes",
                        ".write(",
                        "mkdir(",
                        "touch(",
                        "save_generated",
                        "write_gaps",
                        "output_dir",
                    ),
                    {"output", "output_dir", "write_gaps", "save_generated", "path", "filename"},
                ),
                (
                    "import",
                    (
                        "importlib",
                        "import_module",
                        "__import__",
                        "module_from_spec",
                        "spec_from_file_location",
                    ),
                    {"module", "module_name", "class_path", "target", "hook", "plugin"},
                ),
                (
                    "deserialization",
                    (
                        "pickle.load",
                        "pickle.loads",
                        "msgpack",
                        "marshal",
                        "cbor",
                        "yaml.load",
                        "yaml.safe_load",
                        "plistlib",
                        "from_bytes",
                        "tomllib.load",
                        "tomllib.loads",
                        "json.loads",
                        "json.load",
                        "literal_eval",
                    ),
                    {
                        "artifact",
                        "blob",
                        "bundle",
                        "checkpoint",
                        "config",
                        "frame",
                        "manifest",
                        "payload",
                        "resume",
                        "session",
                        "snapshot",
                        "state",
                        "trace",
                    },
                ),
                (
                    "ipc",
                    (
                        "shared_memory",
                        "sharedmemory",
                        "multiprocessing",
                        "ring buffer",
                        "checkpoint pool",
                        "socket",
                        "pipe(",
                        "connection",
                        "recv_bytes",
                        "send_bytes",
                        "sharedmemorymanager",
                        "queue(",
                        "mmap",
                    ),
                    {
                        "channel",
                        "checkpoint",
                        "descriptor",
                        "fd",
                        "mailbox",
                        "pipe",
                        "queue",
                        "ring",
                        "segment",
                        "shared_memory",
                        "shm",
                        "sock",
                        "topic",
                    },
                ),
                (
                    "symlink",
                    ("symlink", "readlink", "is_symlink", ".resolve("),
                    {"link", "symlink", "target_path"},
                ),
            ]
        )
    for category, tokens, param_names in checks:
        source_match = any(token in source for token in tokens)
        param_match = bool(params & param_names)
        if category in {"json_tool_call", "http", "sql", "filesystem_write"}:
            matched = source_match
        else:
            matched = source_match or param_match
        if matched:
            categories.append((category, source_match, param_match))
    return categories


def _source_backed_sink_categories(func: Any, *, security_focus: bool = False) -> list[str]:
    """Return sink categories backed by concrete source evidence."""
    return [
        category
        for category, source_match, _param_match in _sink_category_matches(
            func,
            security_focus=security_focus,
        )
        if source_match
    ]


def _infer_sink_categories(func: Any, *, security_focus: bool = False) -> list[str]:
    """Infer semantic sink categories from source and parameter names."""
    categories = [
        category
        for category, _source_match, _param_match in _sink_category_matches(
            func,
            security_focus=security_focus,
        )
    ]
    return categories


def _critical_security_sinks(sink_categories: Sequence[str]) -> list[str]:
    """Return high-risk sink categories in descending weight order."""
    return sorted(
        {
            str(category)
            for category in sink_categories
            if _SECURITY_SINK_WEIGHTS.get(str(category), 0.0) >= 0.9
        },
        key=lambda name: (-_SECURITY_SINK_WEIGHTS.get(name, 0.0), name),
    )


def _semantic_bucket_targets_sink(bucket: str, sink_categories: Sequence[str]) -> bool:
    """Return whether one semantic bucket can reach the inferred sink set."""
    return _sink_signal_for_bucket(bucket, sink_categories) > 0.0


def _proof_bundle_critical_sinks(proof_bundle: Mapping[str, Any] | None) -> list[str] | None:
    """Return explicit critical-sink evidence from one proof bundle when present."""
    if not isinstance(proof_bundle, Mapping):
        return None
    for key in ("impact", "contract_basis"):
        section = proof_bundle.get(key)
        if isinstance(section, Mapping) and "critical_sinks" in section:
            return [str(item) for item in list(section.get("critical_sinks", ()) or ())]
    if "critical_sinks" in proof_bundle:
        return [str(item) for item in list(proof_bundle.get("critical_sinks", ()) or ())]
    return None


def _proof_bundle_replayable(
    proof_bundle: Mapping[str, Any] | None,
    replayable: bool | None,
) -> bool:
    """Return whether replay evidence confirms the proof bundle witness."""
    if isinstance(proof_bundle, Mapping):
        reproduction = proof_bundle.get("reproduction")
        if isinstance(reproduction, Mapping) and reproduction.get("replayable") is not None:
            return bool(reproduction.get("replayable"))
        confidence = proof_bundle.get("confidence_breakdown")
        if isinstance(confidence, Mapping):
            replayability = confidence.get("replayability")
            if isinstance(replayability, (int, float)):
                return replayability >= 1.0
    return bool(replayable)


def _proof_verdict_promoted(
    proof_bundle: Mapping[str, Any] | None,
    *,
    default: bool = False,
) -> bool:
    """Return the explicit proof-bundle promotion verdict when present."""
    if isinstance(proof_bundle, Mapping):
        verdict = proof_bundle.get("verdict")
        if isinstance(verdict, Mapping) and verdict.get("promoted") is not None:
            return bool(verdict.get("promoted"))
    return default


def _contract_violation_promoted(detail: Mapping[str, Any] | None) -> bool:
    """Return whether one contract violation should count as a promoted finding."""
    if not isinstance(detail, Mapping):
        return False
    category = str(detail.get("category") or "")
    if category == "lifecycle_contract":
        return True
    if category != "semantic_contract":
        return False
    return _proof_verdict_promoted(detail.get("proof_bundle"), default=False)


def _scan_crash_promoted(
    *,
    category: str | None,
    replayable: bool | None,
    proof_bundle: Mapping[str, Any] | None = None,
    sink_categories: Sequence[str] = (),
) -> bool:
    """Return whether one crash should count as a promoted finding."""
    if category != "likely_bug":
        return False
    critical_sinks = _proof_bundle_critical_sinks(proof_bundle)
    if critical_sinks is None:
        critical_sinks = _critical_security_sinks(sink_categories)
    if not critical_sinks:
        return True
    return isinstance(proof_bundle, Mapping) and _proof_bundle_replayable(proof_bundle, replayable)


def _reportable_crash_category(
    *,
    category: str | None,
    replayable: bool | None,
    proof_bundle: Mapping[str, Any] | None = None,
    sink_categories: Sequence[str] = (),
) -> str:
    """Return the user-facing crash category after proof-based promotion gating."""
    normalized = str(category or "speculative_crash")
    if normalized == "likely_bug" and not _scan_crash_promoted(
        category=normalized,
        replayable=replayable,
        proof_bundle=proof_bundle,
        sink_categories=sink_categories,
    ):
        return "speculative_crash"
    return normalized


def _sink_signal_for_bucket(bucket: str, sink_categories: Sequence[str]) -> float:
    """Return the sink weight for one semantic bucket."""
    weights = [
        _SECURITY_SINK_WEIGHTS.get(category, 0.0)
        for category in _SECURITY_BUCKET_TO_SINKS.get(bucket, ())
        if category in sink_categories
    ]
    return max(weights, default=0.0)


def _expand_contract_names(names: Sequence[str] | None) -> set[str]:
    """Expand contract aliases like ``transport`` into concrete built-ins."""
    expanded: set[str] = set()
    for raw in names or ():
        name = str(raw).strip()
        if not name:
            continue
        expanded.update(_CONTRACT_GROUP_ALIASES.get(name, (name,)))
    return expanded


def _expand_contract_names_ordered(names: Sequence[str] | None) -> list[str]:
    """Expand contract aliases while preserving the caller's order."""
    expanded: list[str] = []
    seen: set[str] = set()
    for raw in names or ():
        name = str(raw).strip()
        if not name:
            continue
        for concrete in _CONTRACT_GROUP_ALIASES.get(name, (name,)):
            if concrete in seen:
                continue
            seen.add(concrete)
            expanded.append(concrete)
    return expanded


def _traceback_path(exc: BaseException) -> list[str]:
    """Return a short traceback path for proof bundles."""
    frames: list[str] = []
    for frame in traceback.extract_tb(exc.__traceback__):
        frames.append(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}")
    return frames[-6:]


def _json_ready_proof(value: Any) -> Any:
    """Convert proof-bundle payloads into JSON-friendly structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, Mapping):
        return {str(key): _json_ready_proof(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_ready_proof(item) for item in value]
    return repr(value)


def _sink_likely_impact(sink_categories: Sequence[str], exc: BaseException) -> str:
    """Summarize likely impact for a failing sink-aware witness."""
    if "shell" in sink_categories or "subprocess" in sink_categories:
        return "command construction may break valid shell or subprocess execution"
    if "filesystem_write" in sink_categories:
        return "filesystem writes may escape the intended root or clobber generated files"
    if "import" in sink_categories:
        return "import resolution may load attacker-chosen modules, hooks, or classes"
    if "deserialization" in sink_categories:
        return "artifact or checkpoint parsing may trust unsafe serialized payloads"
    if "ipc" in sink_categories:
        return "shared-memory or IPC payload handling may trust forged cross-process data"
    if "symlink" in sink_categories:
        return "path resolution may follow symlinks across trust boundaries"
    if "path" in sink_categories:
        return "path quoting or normalization may corrupt filesystem operations"
    if "env" in sink_categories:
        return "environment shaping may overwrite or drop protected keys"
    if "json_tool_call" in sink_categories:
        return "JSON or tool-call normalization may reject valid payloads"
    if "http" in sink_categories:
        return "HTTP header/body shaping may break valid request construction"
    if "sql" in sink_categories:
        return "query construction may reject valid SQL-shaped inputs"
    if isinstance(exc, (TypeError, ValueError)):
        return "valid-looking inputs may still hit an unchecked contract boundary"
    return "replayable failure on a contract-fitting input"


def register_fixture(name: str, strategy: st.SearchStrategy[Any]) -> None:
    """Register a named fixture strategy for auto-scan.

    Registered strategies have highest priority after explicit fixtures.
    Call this in ``conftest.py`` to teach ordeal about project-specific
    types::

        from ordeal.auto import register_fixture
        import hypothesis.strategies as st

        register_fixture("model", st.builds(make_mock_model))
        register_fixture("direction", st.builds(make_unit_vector))

    After registration, ``scan_module`` and ``fuzz`` will auto-resolve
    parameters named ``model`` or ``direction`` without explicit fixtures.
    """
    _REGISTERED_STRATEGIES[name] = strategy


def register_object_factory(name: str, factory: Any) -> None:
    """Register an object factory for class-method targets.

    Use this for methods that need a prebuilt instance or collaborators.
    The factory can be sync or async and may return the instance directly.
    """
    _REGISTERED_OBJECT_FACTORIES[name] = factory


def register_object_setup(name: str, setup: Any) -> None:
    """Register a per-instance setup hook for class-method targets."""
    _REGISTERED_OBJECT_SETUPS[name] = setup


def register_object_scenario(name: str, scenario: Any) -> None:
    """Register one or more collaborator scenario hooks for class-method targets."""
    _REGISTERED_OBJECT_SCENARIOS[name] = scenario


def register_object_state_factory(name: str, state_factory: Any) -> None:
    """Register a per-method state factory for class-method targets."""
    _REGISTERED_OBJECT_STATE_FACTORIES[name] = state_factory


def register_object_teardown(name: str, teardown: Any) -> None:
    """Register a per-instance teardown hook for class-method targets."""
    _REGISTERED_OBJECT_TEARDOWNS[name] = teardown


def register_object_harness(name: str, harness: str) -> None:
    """Register how ordeal should exercise a class target.

    Valid values are ``"fresh"`` and ``"stateful"``.
    """
    resolved = str(harness).strip().lower() or "fresh"
    if resolved not in {"fresh", "stateful"}:
        raise ValueError("object harness must be 'fresh' or 'stateful'")
    _REGISTERED_OBJECT_HARNESSES[name] = resolved


def _strategy_for_name(name: str) -> st.SearchStrategy[Any] | None:
    """Try to infer a strategy from the parameter name alone."""
    # 1. User-registered (project-specific, highest priority)
    if name in _REGISTERED_STRATEGIES:
        return _REGISTERED_STRATEGIES[name]
    # 2. Built-in common names
    if name in COMMON_NAME_STRATEGIES:
        return COMMON_NAME_STRATEGIES[name]
    # 3. Suffix patterns
    for suffix, strategy in _SUFFIX_STRATEGIES.items():
        if name.endswith(suffix):
            return strategy
    return None


# ============================================================================
# Helpers
# ============================================================================


def _resolve_module(module: str | ModuleType) -> ModuleType:
    if isinstance(module, str):
        return importlib.import_module(module)
    return module


def _is_fatal_discovery_exception(exc: BaseException) -> bool:
    """Return whether *exc* should abort discovery immediately."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError))


def _unwrap(func: Any) -> Any:
    """Unwrap decorated functions to reach the original callable.

    Handles Ray ``@ray.remote`` (`._function``), ``functools.wraps``
    (``__wrapped__`` chains), and Celery-style patterns.
    """
    import inspect

    func = getattr(func, "_function", func)
    if getattr(func, "__ordeal_keep_wrapped__", False):
        return func
    try:
        func = inspect.unwrap(
            func,
            stop=lambda wrapped: getattr(wrapped, "__ordeal_keep_wrapped__", False),
        )
    except (ValueError, TypeError):
        pass
    return func


def _resolve_awaitable(value: Any) -> Any:
    """Resolve an awaitable value without forcing callers to use async APIs."""
    if not inspect.isawaitable(value):
        return value
    try:
        return asyncio.run(value)
    except RuntimeError as exc:
        if "asyncio.run()" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(value)
        finally:
            loop.close()


def _call_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call *func* and synchronously resolve any returned awaitable."""
    return _resolve_awaitable(func(*args, **kwargs))


def _signature_without_first_context(
    func: Any,
    *,
    omit_names: Sequence[str] = (),
) -> inspect.Signature:
    """Return a callable signature with contextual parameters removed."""
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    if params and params[0].name in {"self", "cls"}:
        params = params[1:]
    omitted = set(omit_names)
    if omitted:
        params = [param for param in params if param.name not in omitted]
    return sig.replace(parameters=params)


def _object_hook_candidates(owner: type) -> list[str]:
    """Return the registry keys that may refer to *owner*."""
    candidates = [
        f"{owner.__module__}:{owner.__qualname__}",
        f"{owner.__module__}.{owner.__qualname__}",
        f"{owner.__module__}:{owner.__name__}",
        f"{owner.__module__}.{owner.__name__}",
        owner.__qualname__,
        owner.__name__,
    ]
    return list(dict.fromkeys(candidates))


def _resolve_object_hook(owner: type, hooks: dict[str, Any] | None) -> Any | None:
    """Resolve a registered object hook for *owner* from several key styles."""
    if not hooks:
        return None
    for candidate in _object_hook_candidates(owner):
        if candidate in hooks:
            return hooks[candidate]
    return None


def _scenario_path_target(instance: Any, path: str) -> tuple[Any, str]:
    """Resolve a dotted scenario path against *instance*."""
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise ValueError("scenario path must not be empty")
    target = instance
    for part in parts[:-1]:
        target = getattr(target, part)
    return target, parts[-1]


def _scenario_exception_from_spec(spec: Any) -> BaseException:
    """Build a concrete exception object from a scenario spec."""
    if isinstance(spec, BaseException):
        return spec
    if isinstance(spec, type) and issubclass(spec, BaseException):
        return spec()
    if isinstance(spec, Mapping):
        exc_type = (
            str(
                spec.get("type") or spec.get("exception") or spec.get("name") or "RuntimeError"
            ).strip()
            or "RuntimeError"
        )
        message = spec.get("message", spec.get("value", ""))
    else:
        text = str(spec).strip()
        if not text:
            return RuntimeError("injected collaborator failure")
        if ":" in text:
            exc_type, message = text.split(":", 1)
        else:
            return RuntimeError(text)
    exc_cls = getattr(builtins, str(exc_type).strip(), RuntimeError)
    if not isinstance(exc_cls, type) or not issubclass(exc_cls, BaseException):
        exc_cls = RuntimeError
    try:
        return exc_cls(str(message).strip()) if str(message).strip() else exc_cls()
    except Exception:
        return RuntimeError(str(spec))


def _scenario_stub_wrapper(
    original: Any,
    *,
    return_value: Any = None,
    error: BaseException | None = None,
) -> Any:
    """Wrap one collaborator method so it returns or raises a fixed outcome."""
    is_async = inspect.iscoroutinefunction(original)

    if is_async:

        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if error is not None:
                raise error
            return return_value

    else:

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if error is not None:
                raise error
            return return_value

    return wrapper


def _make_pack_method_stub(original: Any, behavior: Callable[..., Any]) -> Any:
    """Wrap a collaborator method while preserving its async/sync shape."""
    is_async = inspect.iscoroutinefunction(getattr(original, "__func__", original))

    if is_async:

        @functools.wraps(original)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            return behavior(*args, **kwargs)

    else:

        @functools.wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            return behavior(*args, **kwargs)

    return wrapped


def _apply_collaborator_pack(
    instance: Any,
    *,
    attr_names: Sequence[str],
    method_behaviors: Mapping[str, Callable[..., Any]],
) -> Any:
    """Attach a built-in fake collaborator to the first matching instance attr."""
    for attr_name in attr_names:
        collaborator = getattr(instance, attr_name, None)
        if collaborator is None:
            continue
        fake = SimpleNamespace()
        for method_name, behavior in method_behaviors.items():
            original = getattr(collaborator, method_name, None)
            if callable(original):
                setattr(fake, method_name, _make_pack_method_stub(original, behavior))
        if fake.__dict__:
            setattr(instance, attr_name, fake)
    return instance


def _subprocess_response_stub(*args: Any, **kwargs: Any) -> SimpleNamespace:
    """Return a stable subprocess-like response object."""
    return SimpleNamespace(
        args=list(args),
        kwargs=dict(kwargs),
        returncode=0,
        stdout="",
        stderr="",
    )


def _http_response_stub(*args: Any, **kwargs: Any) -> SimpleNamespace:
    """Return a stable HTTP-like response object."""
    return SimpleNamespace(
        args=list(args),
        kwargs=dict(kwargs),
        status_code=200,
        headers={},
        text="",
        content=b"",
        json=lambda: {},
    )


def _sequence_arg(values: Sequence[Any]) -> Sequence[Any] | None:
    """Return the first batch-like argument from *values*."""
    for value in values:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return value
    return None


def _model_vector(width: int = 4) -> list[float]:
    """Return one stable embedding-like vector."""
    return [0.0 for _ in range(width)]


def _model_prediction_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable prediction payload shaped like the input batch."""
    batch = _sequence_arg([*args, *kwargs.values()])
    if batch is None:
        return 0.5
    return [0.5 for _ in range(max(1, len(batch)))]


def _model_probability_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable probability payload shaped like the input batch."""
    batch = _sequence_arg([*args, *kwargs.values()])
    row = [0.5, 0.5]
    if batch is None:
        return row
    return [list(row) for _ in range(max(1, len(batch)))]


def _embedding_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable embedding or batch of embeddings."""
    batch = _sequence_arg([*args, *kwargs.values()])
    if batch is None:
        return _model_vector()
    return [_model_vector() for _ in range(max(1, len(batch)))]


def _feature_payload_stub(*args: Any, **kwargs: Any) -> Any:
    """Return one stable feature row or batch of feature rows."""
    batch = _sequence_arg([*args, *kwargs.values()])
    row = {"feature_0": 0.5, "feature_1": 1.0}
    if batch is None:
        return dict(row)
    return [dict(row) for _ in range(max(1, len(batch)))]


def _apply_state_store_pack(instance: Any) -> Any:
    """Attach a shared in-memory state store to matching instance collaborators."""
    store: dict[str, Any] = {}

    def _make_behavior(method_name: str) -> Callable[..., Any]:
        def behavior(*args: Any, **kwargs: Any) -> Any:
            if method_name == "get":
                key = args[0] if args else kwargs.get("key")
                default = args[1] if len(args) > 1 else kwargs.get("default")
                return store.get(key, default)
            if method_name in {"set", "put", "save"}:
                if len(args) >= 2:
                    key, value = args[0], args[1]
                else:
                    key = kwargs.get("key", kwargs.get("name", "value"))
                    value = kwargs.get("value", args[0] if args else None)
                store[str(key)] = _clone_scenario_value(value)
                return value
            if method_name == "load":
                return dict(store)
            if method_name == "delete":
                key = args[0] if args else kwargs.get("key")
                if key is not None:
                    store.pop(str(key), None)
                return None
            if method_name == "clear":
                store.clear()
                return None
            return dict(store)

        return behavior

    return _apply_collaborator_pack(
        instance,
        attr_names=("state_store", "store", "cache", "session_state"),
        method_behaviors={
            "get": _make_behavior("get"),
            "set": _make_behavior("set"),
            "put": _make_behavior("put"),
            "save": _make_behavior("save"),
            "load": _make_behavior("load"),
            "delete": _make_behavior("delete"),
            "clear": _make_behavior("clear"),
        },
    )


def _apply_subprocess_pack(instance: Any) -> Any:
    """Attach a stable subprocess/runner collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "subprocess",
            "runner",
            "command_runner",
            "process_runner",
            "executor",
        ),
        method_behaviors={
            "run": _subprocess_response_stub,
            "execute_command": _subprocess_response_stub,
            "check_output": lambda *args, **kwargs: "",
            "popen": _subprocess_response_stub,
            "call": lambda *args, **kwargs: 0,
        },
    )


def _apply_sandbox_pack(instance: Any) -> Any:
    """Attach a stable sandbox-client collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=("sandbox_client", "sandbox", "client"),
        method_behaviors={
            "execute_command": _subprocess_response_stub,
            "run": _subprocess_response_stub,
            "upload_content": lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                uploaded=True,
            ),
            "download_content": lambda *args, **kwargs: b"",
            "fetch_content": lambda *args, **kwargs: b"",
            "list_files": lambda *args, **kwargs: [],
        },
    )


def _apply_upload_download_pack(instance: Any) -> Any:
    """Attach a stable upload/download collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "upload_download",
            "storage_client",
            "artifact_client",
            "uploader",
            "downloader",
            "client",
        ),
        method_behaviors={
            "upload": lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                uploaded=True,
            ),
            "upload_content": lambda *args, **kwargs: SimpleNamespace(
                ok=True,
                uploaded=True,
            ),
            "download": lambda *args, **kwargs: b"",
            "download_content": lambda *args, **kwargs: b"",
            "fetch_content": lambda *args, **kwargs: b"",
            "list_files": lambda *args, **kwargs: [],
        },
    )


def _apply_http_pack(instance: Any) -> Any:
    """Attach a stable HTTP client collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=("http_client", "session", "transport", "client"),
        method_behaviors={
            "request": _http_response_stub,
            "get": _http_response_stub,
            "post": _http_response_stub,
            "put": _http_response_stub,
            "patch": _http_response_stub,
            "delete": _http_response_stub,
            "send": _http_response_stub,
        },
    )


def _apply_model_inference_pack(instance: Any) -> Any:
    """Attach a stable model-inference collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "model",
            "predictor",
            "scorer",
            "embedder",
            "encoder",
            "classifier",
            "reranker",
            "model_client",
        ),
        method_behaviors={
            "predict": _model_prediction_stub,
            "predict_proba": _model_probability_stub,
            "transform": _embedding_stub,
            "embed": _embedding_stub,
            "encode": _embedding_stub,
            "score": lambda *args, **kwargs: 0.5,
            "classify": lambda *args, **kwargs: {"label": "ok", "score": 0.5},
            "infer": _model_prediction_stub,
            "run": _model_prediction_stub,
        },
    )


def _apply_feature_store_pack(instance: Any) -> Any:
    """Attach a stable feature-store collaborator pack to *instance*."""
    return _apply_collaborator_pack(
        instance,
        attr_names=(
            "feature_store",
            "vector_store",
            "embedding_store",
            "retriever",
            "feature_client",
        ),
        method_behaviors={
            "get": _feature_payload_stub,
            "fetch": _feature_payload_stub,
            "lookup": _feature_payload_stub,
            "get_features": _feature_payload_stub,
            "fetch_features": _feature_payload_stub,
            "lookup_features": _feature_payload_stub,
            "put": lambda *args, **kwargs: True,
            "upsert": lambda *args, **kwargs: True,
        },
    )


_BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS: dict[str, dict[str, Any]] = {
    "subprocess": {
        "aliases": ("subprocess_runner",),
        "description": (
            "Stub subprocess-style runners and command executors with successful no-op results."
        ),
        "hook": _apply_subprocess_pack,
    },
    "sandbox": {
        "aliases": ("sandbox_client",),
        "description": (
            "Stub sandbox clients with upload/download helpers and successful command execution."
        ),
        "hook": _apply_sandbox_pack,
    },
    "upload_download": {
        "aliases": (
            "upload_download_client",
            "upload",
            "upload_client",
            "download",
            "download_client",
        ),
        "description": (
            "Stub storage, upload, and download collaborators with safe in-memory responses."
        ),
        "hook": _apply_upload_download_pack,
    },
    "model_inference": {
        "aliases": (
            "model_client",
            "predictor",
            "embedder",
            "encoder",
            "classifier",
        ),
        "description": (
            "Stub model-style collaborators with stable prediction, "
            "embedding, and scoring outputs."
        ),
        "hook": _apply_model_inference_pack,
    },
    "feature_store": {
        "aliases": (
            "vector_store",
            "embedding_store",
            "feature_client",
            "retriever",
        ),
        "description": (
            "Stub feature-store collaborators with stable row-shaped feature payloads."
        ),
        "hook": _apply_feature_store_pack,
    },
    "http": {
        "aliases": ("http_client",),
        "description": "Stub HTTP clients and transports with stable 200-style response objects.",
        "hook": _apply_http_pack,
    },
    "state_store": {
        "aliases": (),
        "description": (
            "Attach an in-memory key/value store for cache or session-style collaborators."
        ),
        "hook": _apply_state_store_pack,
    },
}
_BUILTIN_OBJECT_SCENARIO_LIBRARY_ALIASES = {
    alias: name
    for name, spec in _BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS.items()
    for alias in (name, *spec["aliases"])
}


def available_object_scenario_libraries() -> tuple[dict[str, Any], ...]:
    """Return the canonical built-in collaborator scenario library catalog."""
    return tuple(
        {
            "name": name,
            "aliases": list(spec["aliases"]),
            "description": str(spec["description"]),
        }
        for name, spec in _BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS.items()
    )


def _builtin_object_scenario_hook(name: str) -> Callable[[Any], Any] | None:
    """Return a named collaborator scenario pack hook, if known."""
    normalized = str(name).strip().lower()
    canonical = _BUILTIN_OBJECT_SCENARIO_LIBRARY_ALIASES.get(normalized)
    if canonical is None:
        return None
    spec = _BUILTIN_OBJECT_SCENARIO_LIBRARY_SPECS[canonical]
    return spec["hook"]


def _scenario_hook_from_spec(spec: Mapping[str, Any]) -> Callable[[Any], Any]:
    """Compile one TOML-friendly collaborator scenario spec into a hook."""
    kind = str(spec.get("kind") or spec.get("action") or spec.get("op") or "").strip().lower()
    if not kind:
        if spec.get("error") is not None or spec.get("exception") is not None:
            kind = "stub_raise"
        elif spec.get("path") is not None or spec.get("target") is not None:
            kind = "setattr"
    if not kind and spec.get("pack") is not None:
        kind = "pack"
    path = spec.get("path") or spec.get("target") or spec.get("attr") or spec.get("name")
    pack = spec.get("pack") or spec.get("library") or spec.get("scenario")

    def _setattr_hook(instance: Any) -> Any:
        if path is None:
            raise ValueError("scenario setattr spec needs a path")
        target, attr_name = _scenario_path_target(instance, str(path))
        setattr(target, attr_name, spec.get("value"))
        return instance

    def _stub_return_hook(instance: Any) -> Any:
        if path is None:
            raise ValueError("scenario stub_return spec needs a path")
        target, attr_name = _scenario_path_target(instance, str(path))
        original = getattr(target, attr_name)
        if not callable(original):
            raise ValueError(f"scenario path {path!r} does not resolve to a callable")
        setattr(
            target,
            attr_name,
            _scenario_stub_wrapper(original, return_value=spec.get("value")),
        )
        return instance

    def _stub_raise_hook(instance: Any) -> Any:
        if path is None:
            raise ValueError("scenario stub_raise spec needs a path")
        target, attr_name = _scenario_path_target(instance, str(path))
        original = getattr(target, attr_name)
        if not callable(original):
            raise ValueError(f"scenario path {path!r} does not resolve to a callable")
        setattr(
            target,
            attr_name,
            _scenario_stub_wrapper(
                original,
                error=_scenario_exception_from_spec(
                    spec.get("error", spec.get("exception", spec.get("value")))
                ),
            ),
        )
        return instance

    def _pack_hook(instance: Any) -> Any:
        if pack is None:
            raise ValueError("scenario pack spec needs a pack name")
        hook = _builtin_object_scenario_hook(str(pack))
        if hook is None:
            raise ValueError(f"unknown built-in scenario pack: {pack!r}")
        return hook(instance)

    match kind:
        case "setattr" | "assign" | "set":
            return _setattr_hook
        case "stub_return" | "return" | "returns":
            return _stub_return_hook
        case "stub_raise" | "raise" | "raises":
            return _stub_raise_hook
        case "pack":
            return _pack_hook
        case _:
            raise ValueError(f"unsupported scenario kind {kind!r}")


def _expand_object_scenario_hooks(hook: Any) -> tuple[Any, ...]:
    """Normalize a scenario entry into one or more executable hooks."""
    if hook is None:
        return ()
    if callable(hook):
        return (hook,)
    if isinstance(hook, (str, bytes)):
        builtin = _builtin_object_scenario_hook(hook.decode() if isinstance(hook, bytes) else hook)
        if builtin is not None:
            return (builtin,)
        raise ValueError(f"unknown built-in scenario pack: {hook!r}")
    if isinstance(hook, Mapping):
        if "pack" in hook or "library" in hook or "scenario" in hook:
            return (_scenario_hook_from_spec(hook),)
        return (_scenario_hook_from_spec(hook),)
    if isinstance(hook, Sequence):
        compiled: list[Any] = []
        for item in hook:
            compiled.extend(_expand_object_scenario_hooks(item))
        return tuple(compiled)
    return (hook,)


def _resolve_object_hooks(owner: type, hooks: dict[str, Any] | None) -> tuple[Any, ...]:
    """Resolve one or more registered hooks for *owner* from several key styles."""
    hook = _resolve_object_hook(owner, hooks)
    if hook is None:
        return ()
    return _expand_object_scenario_hooks(hook)


def _resolve_object_harness(owner: type, harnesses: dict[str, str] | None) -> str:
    """Resolve the configured harness mode for *owner*."""
    if not harnesses:
        return "fresh"
    for candidate in _object_hook_candidates(owner):
        if candidate in harnesses:
            resolved = str(harnesses[candidate]).strip().lower()
            if resolved in {"fresh", "stateful"}:
                return resolved
    return "fresh"


def _lifecycle_phase(method_name: str, method: Any | None = None) -> str | None:
    """Infer a coarse lifecycle phase from decorator attrs or method names."""
    target = _unwrap(getattr(method, "__func__", method)) if method is not None else None
    if target is not None:
        for phase in ("setup", "rollout", "stop", "cleanup", "teardown"):
            if getattr(target, phase, False) or (
                getattr(target, f"{phase}_priority", None) is not None
            ):
                return phase
    lowered = method_name.lower()
    exact = {
        "setup_state": "setup",
        "post_sandbox_setup": "setup",
        "post_rollout": "rollout",
    }
    if lowered in exact:
        return exact[lowered]
    for phase in ("setup", "cleanup", "teardown", "stop", "rollout"):
        if phase in lowered:
            return phase
    return None


def _snapshot_instance_state(instance: Any) -> Any:
    """Capture a best-effort snapshot of instance state for lifecycle predicates."""
    state = getattr(instance, "__dict__", None)
    if not isinstance(state, dict):
        return None
    try:
        return copy.deepcopy(state)
    except Exception:
        return {key: repr(value) for key, value in state.items()}


def _lifecycle_phase_members(
    owner: type,
    phase: str,
    *,
    exclude: Sequence[str] = (),
) -> list[str]:
    """Return public owner methods that look like members of one lifecycle phase."""
    excluded = set(exclude)
    members: list[str] = []
    for name, raw_attr in inspect.getmembers_static(owner):
        if name.startswith("_") or name in excluded:
            continue
        if _lifecycle_phase(name, raw_attr) != phase:
            continue
        if isinstance(raw_attr, (staticmethod, classmethod)) or inspect.isfunction(raw_attr):
            members.append(name)
    return members


def _lifecycle_fault_exception(name: str) -> BaseException:
    """Return the concrete exception raised for one lifecycle fault name."""
    if name in {"cancel", "cancel_rollout"}:
        return asyncio.CancelledError("injected rollout cancellation")
    if name == "raise_setup_hook":
        return RuntimeError("injected setup failure")
    if name == "raise_teardown_hook":
        return RuntimeError("injected teardown failure")
    if name == "raise_cleanup_handler":
        return RuntimeError("injected cleanup handler failure")
    if name == "raise_teardown_handler":
        return RuntimeError("injected teardown handler failure")
    return RuntimeError(f"injected lifecycle fault: {name}")


@contextlib.contextmanager
def _active_contract_faults(
    func: Any,
    faults: Sequence[str],
) -> Any:
    """Temporarily attach contract-scoped lifecycle fault names to *func*."""
    if not faults:
        yield
        return
    previous = getattr(func, "__ordeal_contract_faults__", None)
    setattr(func, "__ordeal_contract_faults__", tuple(faults))
    try:
        yield
    finally:
        if previous is None:
            with contextlib.suppress(AttributeError):
                delattr(func, "__ordeal_contract_faults__")
        else:
            setattr(func, "__ordeal_contract_faults__", previous)


@contextlib.contextmanager
def _active_instance_probe(
    func: Any,
    probe: Any | None,
) -> Any:
    """Temporarily attach an instance probe to one wrapped callable."""
    previous = getattr(func, "__ordeal_instance_probe__", None)
    setattr(func, "__ordeal_instance_probe__", probe)
    try:
        yield
    finally:
        setattr(func, "__ordeal_instance_probe__", previous)


@contextlib.contextmanager
def _lifecycle_fault_runtime(
    instance: Any,
    owner: type,
    *,
    method_name: str,
    setup: Any | None = None,
    teardown: Any | None = None,
    fault_names: Sequence[str] = (),
) -> Any:
    """Patch lifecycle collaborators on *instance* for contract-driven probes."""
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    applied_faults: list[str] = []
    phase_candidates: dict[str, list[str]] = {}
    restore: list[tuple[Any, str, Any]] = []
    fired: dict[str, bool] = {}

    def _record(
        *,
        phase: str,
        name: str,
        kind: str,
        injected: bool = False,
        raised: bool = False,
        error_type: str | None = None,
    ) -> None:
        events.append(
            {
                "phase": phase,
                "name": name,
                "kind": kind,
                "injected": injected,
                "raised": raised,
                "error_type": error_type,
            }
        )

    def _hook_wrapper(hook: Any, *, phase: str, fault_name: str | None) -> Any:
        if hook is None:
            return None

        @functools.wraps(hook)
        def wrapped_hook(current: Any) -> Any:
            should_inject = bool(fault_name) and not fired.get(str(fault_name), False)
            event = {
                "phase": phase,
                "name": getattr(hook, "__name__", phase),
                "kind": "hook",
                "injected": should_inject,
                "raised": False,
                "error_type": None,
            }
            events.append(event)
            if should_inject:
                fired[str(fault_name)] = True
                event["raised"] = True
                exc = _lifecycle_fault_exception(str(fault_name))
                event["error_type"] = type(exc).__name__
                raise exc
            try:
                return _call_with_optional_instance_arg(hook, current)
            except BaseException as exc:
                event["raised"] = True
                event["error_type"] = type(exc).__name__
                raise

        return wrapped_hook

    setup_hook = _hook_wrapper(
        setup,
        phase="setup",
        fault_name="raise_setup_hook" if "raise_setup_hook" in fault_names else None,
    )
    teardown_hook = _hook_wrapper(
        teardown,
        phase="teardown",
        fault_name="raise_teardown_hook" if "raise_teardown_hook" in fault_names else None,
    )
    if setup_hook is not None and "raise_setup_hook" in fault_names:
        applied_faults.append("raise_setup_hook")
    if teardown_hook is not None and "raise_teardown_hook" in fault_names:
        applied_faults.append("raise_teardown_hook")

    phase_faults = {
        "raise_cleanup_handler": "cleanup",
        "raise_teardown_handler": "teardown",
        "cancel_rollout": "rollout",
    }
    for fault_name, phase in phase_faults.items():
        if fault_name not in fault_names:
            continue
        names = _lifecycle_phase_members(owner, phase, exclude=(method_name,))
        phase_candidates[phase] = names
        if not names:
            warnings.append(f"{fault_name}: no {phase} handlers found to inject")
            continue
        injected_name = names[0]
        for name in names:
            original = getattr(instance, name)
            is_async = inspect.iscoroutinefunction(getattr(original, "__func__", original))
            if is_async:

                @functools.wraps(original)
                async def wrapper(
                    *args: Any,
                    __orig: Any = original,
                    __name: str = name,
                    __phase: str = phase,
                    __fault_name: str = fault_name,
                    __inject: bool = name == injected_name,
                    **kwargs: Any,
                ) -> Any:
                    should_inject = __inject and not fired.get(__fault_name, False)
                    event = {
                        "phase": __phase,
                        "name": __name,
                        "kind": "handler",
                        "injected": should_inject,
                        "raised": False,
                        "error_type": None,
                    }
                    events.append(event)
                    if should_inject:
                        fired[__fault_name] = True
                        event["raised"] = True
                        exc = _lifecycle_fault_exception(__fault_name)
                        event["error_type"] = type(exc).__name__
                        raise exc
                    try:
                        result = __orig(*args, **kwargs)
                        if inspect.isawaitable(result):
                            return await result
                        return result
                    except BaseException as exc:
                        event["raised"] = True
                        event["error_type"] = type(exc).__name__
                        raise
            else:

                @functools.wraps(original)
                def wrapper(
                    *args: Any,
                    __orig: Any = original,
                    __name: str = name,
                    __phase: str = phase,
                    __fault_name: str = fault_name,
                    __inject: bool = name == injected_name,
                    **kwargs: Any,
                ) -> Any:
                    should_inject = __inject and not fired.get(__fault_name, False)
                    event = {
                        "phase": __phase,
                        "name": __name,
                        "kind": "handler",
                        "injected": should_inject,
                        "raised": False,
                        "error_type": None,
                    }
                    events.append(event)
                    if should_inject:
                        fired[__fault_name] = True
                        event["raised"] = True
                        exc = _lifecycle_fault_exception(__fault_name)
                        event["error_type"] = type(exc).__name__
                        raise exc
                    try:
                        return _call_sync(__orig, *args, **kwargs)
                    except BaseException as exc:
                        event["raised"] = True
                        event["error_type"] = type(exc).__name__
                        raise

            restore.append((instance, name, original))
            setattr(instance, name, wrapper)
        applied_faults.append(fault_name)

    runtime = {
        "events": events,
        "warnings": warnings,
        "applied_faults": applied_faults,
        "phase_candidates": phase_candidates,
        "setup_hook": setup_hook or setup,
        "teardown_hook": teardown_hook or teardown,
    }
    try:
        yield runtime
    finally:
        for obj, attr_name, original in reversed(restore):
            setattr(obj, attr_name, original)


def _discover_lifecycle_handlers(
    owner: type | Any,
    phase: str,
    *,
    exclude_method: str | None = None,
) -> list[str]:
    """Return public handler names that look like they belong to one lifecycle phase."""
    cls = owner if inspect.isclass(owner) else type(owner)
    handlers: list[str] = []
    for name, raw_attr in inspect.getmembers_static(cls):
        if name.startswith("_") or name == exclude_method:
            continue
        if not (isinstance(raw_attr, (staticmethod, classmethod)) or inspect.isfunction(raw_attr)):
            continue
        if _lifecycle_phase(name, raw_attr) == phase:
            handlers.append(name)
    return sorted(dict.fromkeys(handlers))


def _contract_seed_kwargs(func: Any) -> dict[str, Any]:
    """Return one deterministic concrete input for a contract probe."""
    candidates = _candidate_inputs(
        func,
        fixtures=None,
        mutate_observed_inputs=False,
    )
    if not candidates:
        return {}
    return dict(candidates[0].kwargs)


def _instance_probe_result(
    probe: Any | None,
    *,
    instance: Any,
    owner: type | None,
    method_name: str,
) -> tuple[Callable[[], None] | None, dict[str, Any]]:
    """Apply a temporary instance probe and normalize its cleanup/context payload."""
    if probe is None:
        return None, {}
    result = probe(
        instance=instance,
        owner=owner,
        method_name=method_name,
    )
    if result is None:
        return None, {}
    if callable(result):
        return result, {}
    if isinstance(result, tuple) and len(result) == 2:
        cleanup, details = result
        if isinstance(details, Mapping):
            return cleanup, dict(details)
        return cleanup, {}
    if isinstance(result, Mapping):
        return None, dict(result)
    return None, {}


def _state_param_name_for_callable(func: Any) -> str | None:
    """Return the likely runtime state parameter name for *func*."""
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    hints = safe_get_annotations(target)
    params = [param for param in sig.parameters.values() if param.name not in {"self", "cls"}]
    for param in params:
        lowered = param.name.lower()
        hint = hints.get(param.name)
        hint_name = getattr(hint, "__name__", "")
        hint_text = str(hint_name or hint).lower()
        if lowered == "state" or lowered.endswith("_state") or "state" in hint_text:
            return param.name
    return None


def _call_with_optional_instance_arg(hook: Any, instance: Any) -> Any:
    """Call *hook* with zero or one instance argument."""
    hook = _unwrap(hook)
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        try:
            return _call_sync(hook, instance)
        except TypeError:
            return _call_sync(hook)

    params = list(signature.parameters.values())
    required = [
        param
        for param in params
        if param.default is inspect.Parameter.empty
        and param.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    accepts_varargs = any(
        param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for param in params
    )
    if accepts_varargs or required or params:
        try:
            return _call_sync(hook, instance)
        except TypeError:
            if not required:
                return _call_sync(hook)
            raise
    return _call_sync(hook)


def _is_metadata_only_hook(hook: Any) -> bool:
    """Return whether *hook* is a read-only placeholder from CLI metadata mode."""
    return bool(getattr(hook, "__ordeal_metadata_only__", False))


def _python_source_path_to_module_name(path_str: str) -> str | None:
    """Convert a project-relative Python path into an importable module name."""
    path = Path(path_str)
    if path.suffix != ".py":
        return None
    for root in (Path.cwd() / "src", Path.cwd()):
        with contextlib.suppress(ValueError):
            rel = path.resolve().relative_to(root.resolve())
            rel_parts = rel.parts[:-1] if rel.name == "__init__.py" else rel.with_suffix("").parts
            if rel_parts:
                return ".".join(rel_parts)
    resolved = path.resolve()
    module_parts = [] if resolved.name == "__init__.py" else [resolved.stem]
    parent = resolved.parent
    while (parent / "__init__.py").exists():
        module_parts.append(parent.name)
        parent = parent.parent
    if len(module_parts) > 1:
        return ".".join(reversed(module_parts))
    return None


_DISCOVERY_IGNORED_PATH_PARTS = {
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "dist-packages",
    "node_modules",
    "site-packages",
    "venv",
}


def _is_project_discovery_path(path: Path, *, workspace_root: Path | None = None) -> bool:
    """Return whether *path* should contribute to learned repo evidence."""
    resolved = path.resolve()
    root = (workspace_root or Path.cwd()).resolve()
    try:
        parts = resolved.relative_to(root).parts
    except ValueError:
        parts = resolved.parts
    return not any(part in _DISCOVERY_IGNORED_PATH_PARTS for part in parts)


def _harness_hint_path_signals(evidence: str) -> tuple[str, ...]:
    """Return generic discovery signals implied by one hint evidence path."""
    path_text = str(evidence).split(":", 1)[0].replace("\\", "/").lower()
    signals: list[str] = []
    if "/tests/" in f"/{path_text}" or path_text.startswith("tests/"):
        signals.append("test_evidence")
    name = Path(path_text).name
    if any(token in name for token in ("support", "fixture", "factory", "conftest")):
        signals.append("support_file")
    if path_text.endswith(".md"):
        signals.append("doc_evidence")
    return tuple(signals)


def _harness_hint_signal_strength(signals: Sequence[str]) -> float:
    """Return the weighted evidence strength for one hint signal set."""
    unique = dict.fromkeys(str(item) for item in signals if str(item).strip())
    return round(
        sum(_HARNESS_HINT_SIGNAL_WEIGHTS.get(signal, 0.0) for signal in unique),
        3,
    )


def _score_harness_hint(confidence: float, signals: Sequence[str]) -> float:
    """Return one bounded compatibility score for a mined harness hint."""
    strength = _harness_hint_signal_strength(signals)
    score = float(confidence) + (strength * 0.04)
    return round(min(score, 1.0), 4)


def _hint_sort_key(hint: HarnessHint) -> tuple[float, float, int, str, str]:
    """Return a stable descending sort key for mined harness hints."""
    return (
        -float(hint.score),
        -float(hint.confidence),
        -_harness_hint_signal_strength(hint.signals),
        -len(hint.signals),
        hint.kind,
        hint.suggestion,
    )


def _resolve_symbol_path(path: str) -> Any:
    """Resolve ``module:attr`` or dotted import paths into Python objects."""
    import importlib.util

    module_name, sep, attr_path = path.partition(":")
    candidate_file = Path(module_name)
    if sep and (
        candidate_file.suffix == ".py"
        or candidate_file.exists()
        or module_name.startswith("./")
        or module_name.startswith("../")
    ):
        file_path = candidate_file
        if not file_path.is_absolute():
            file_path = (Path.cwd() / file_path).resolve()
        if not file_path.exists():
            raise ValueError(f"invalid symbol path: {path!r}")
        spec = importlib.util.spec_from_file_location(
            f"_ordeal_symbol_{abs(hash((str(file_path), attr_path)))}",
            file_path,
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"invalid symbol path: {path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        obj: Any = module
        for part in attr_path.split("."):
            obj = getattr(obj, part)
        return obj
    if not sep:
        module_name, _, attr_path = path.rpartition(".")
    if not module_name or not attr_path:
        raise ValueError(f"invalid symbol path: {path!r}")
    obj = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _hint_symbol_path(value: object) -> str | None:
    """Normalize one mined hint value into an importable symbol path when possible."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower().startswith("docs mention "):
        return None
    match = re.match(r"^(?P<path>.+?\.py):(?P<line>\d+):(?P<name>[A-Za-z_][\w]*)$", text)
    if match is None:
        return text if ":" in text or "." in text else None
    module_name = _python_source_path_to_module_name(match.group("path"))
    if module_name is None:
        return None
    return f"{module_name}:{match.group('name')}"


def _scenario_pack_from_hint(hint: HarnessHint) -> str | None:
    """Infer one built-in scenario pack from a mined collaborator hint."""
    text = " ".join(
        [
            hint.kind,
            hint.suggestion,
            hint.evidence,
            str(hint.config.get("value", "")) if isinstance(hint.config, Mapping) else "",
        ]
    ).lower()
    if any(token in text for token in ("sandbox", "execute_command", "upload_content")):
        return "sandbox_client"
    if any(
        token in text
        for token in (
            "feature_store",
            "vector_store",
            "embedding_store",
            "feature_client",
            "fetch_features",
            "lookup_features",
        )
    ):
        return "feature_store"
    if any(
        token in text
        for token in (
            "model.predict",
            "predictor",
            "embedder",
            "encoder",
            "classifier",
            "predict_proba",
            "embedding client",
        )
    ):
        return "model_inference"
    if any(token in text for token in ("artifact", "storage", "download", "upload")):
        return "upload_download"
    if any(token in text for token in ("http", "request", "session", "transport")):
        return "http_client"
    if any(token in text for token in ("state_store", "session_state", "cache", "store")):
        return "state_store"
    if any(token in text for token in ("subprocess", "runner", "popen", "command_runner")):
        return "subprocess"
    return None


def _single_resolved_hint(
    hints: Sequence[HarnessHint],
    *,
    kind: str,
    min_confidence: float,
) -> tuple[Any | None, HarnessHint | None]:
    """Resolve the strongest mined hook for *kind* when evidence is decisive."""
    resolved: dict[str, tuple[Any, HarnessHint]] = {}
    for hint in hints:
        if hint.kind != kind or float(hint.score) < min_confidence:
            continue
        symbol_path = _hint_symbol_path(getattr(hint, "config", {}).get("value"))
        if symbol_path is None:
            continue
        try:
            obj = _resolve_symbol_path(symbol_path)
        except BaseException as exc:
            if _is_fatal_discovery_exception(exc):
                raise
            continue
        current = resolved.get(symbol_path)
        if current is None or _hint_sort_key(hint) < _hint_sort_key(current[1]):
            resolved[symbol_path] = (obj, hint)
    if not resolved:
        return None, None
    ranked = sorted(resolved.values(), key=lambda item: _hint_sort_key(item[1]))
    top_obj, top_hint = ranked[0]
    if len(ranked) == 1:
        return top_obj, top_hint
    second_hint = ranked[1][1]
    top_score = float(top_hint.score)
    second_score = float(second_hint.score)
    decisive_signals = {"returns_target_instance", "lifecycle_cleanup"}
    top_strength = _harness_hint_signal_strength(top_hint.signals)
    second_strength = _harness_hint_signal_strength(second_hint.signals)
    if (
        top_score - second_score >= 0.01
        or top_strength - second_strength >= 0.04
        or decisive_signals & set(top_hint.signals)
    ):
        return top_obj, top_hint
    return None, None


def _single_scenario_pack_hint(
    hints: Sequence[HarnessHint],
    *,
    min_confidence: float,
) -> tuple[tuple[Any, ...], HarnessHint | None]:
    """Resolve one high-confidence built-in scenario pack from mined hints."""
    packs: dict[str, HarnessHint] = {}
    for hint in hints:
        if float(hint.score) < min_confidence:
            continue
        if hint.kind == "scenario_pack":
            raw_value = getattr(hint, "config", {}).get("value")
            values = (
                list(raw_value)
                if isinstance(raw_value, Sequence)
                and not isinstance(raw_value, (str, bytes, bytearray))
                else [raw_value]
            )
            for value in values:
                if not isinstance(value, str):
                    continue
                pack = value.strip()
                if _builtin_object_scenario_hook(pack) is not None:
                    current = packs.get(pack)
                    if current is None or _hint_sort_key(hint) < _hint_sort_key(current):
                        packs[pack] = hint
        elif hint.kind == "client_fixture":
            pack = _scenario_pack_from_hint(hint)
            if pack:
                current = packs.get(pack)
                if current is None or _hint_sort_key(hint) < _hint_sort_key(current):
                    packs[pack] = hint
    if not packs:
        return (), None
    ranked = sorted(packs.items(), key=lambda item: _hint_sort_key(item[1]))
    pack_name, hint = ranked[0]
    if len(ranked) > 1:
        second_hint = ranked[1][1]
        if (
            float(hint.score) - float(second_hint.score) < 0.01
            and _harness_hint_signal_strength(hint.signals)
            - _harness_hint_signal_strength(second_hint.signals)
            < 0.04
        ):
            return (), None
    hook = _builtin_object_scenario_hook(pack_name)
    if hook is None:
        return (), None
    return (hook,), hint


def _mined_object_runtime(owner: type, method_name: str) -> AutoObjectRuntime:
    """Resolve one conservative object runtime from mined harness hints."""
    hints = tuple(_mine_object_harness_hints(owner.__module__, owner.__name__, method_name))
    factory, factory_hint = _single_resolved_hint(hints, kind="factory", min_confidence=0.85)
    setup, setup_hint = _single_resolved_hint(hints, kind="setup", min_confidence=0.8)
    state_factory, state_hint = _single_resolved_hint(
        hints,
        kind="state_factory",
        min_confidence=0.8,
    )
    teardown, teardown_hint = _single_resolved_hint(hints, kind="teardown", min_confidence=0.75)
    scenarios, scenario_hint = _single_scenario_pack_hint(hints, min_confidence=0.75)
    harness = (
        "stateful"
        if state_factory is not None or setup is not None or teardown is not None or scenarios
        else None
    )
    harness_source = "mined" if harness is not None else None
    return AutoObjectRuntime(
        factory=factory,
        factory_source="mined" if factory_hint is not None else None,
        setup=setup,
        setup_source="mined" if setup_hint is not None else None,
        state_factory=state_factory,
        state_factory_source="mined" if state_hint is not None else None,
        teardown=teardown,
        teardown_source="mined" if teardown_hint is not None else None,
        scenarios=scenarios,
        scenario_source="mined" if scenario_hint is not None else None,
        harness=harness,
        harness_source=harness_source,
        hints=hints,
    )


def _verify_auto_object_runtime(
    owner: type,
    *,
    factory: Any | None,
    setup: Any | None = None,
    scenarios: Sequence[Any] | None = None,
    state_factory: Any | None = None,
    state_param: str | None = None,
    factory_source: str | None = None,
    setup_source: str | None = None,
    scenario_source: str | None = None,
    state_factory_source: str | None = None,
) -> tuple[bool, str | None]:
    """Dry-run mined object harness pieces before treating a method as runnable."""
    mined_sources = {
        "factory": factory_source,
        "setup": setup_source,
        "scenario": scenario_source,
        "state_factory": state_factory_source,
    }
    if not any(source == "mined" for source in mined_sources.values()):
        return True, None
    if (
        (factory_source == "configured" and _is_metadata_only_hook(factory))
        or (setup_source == "configured" and _is_metadata_only_hook(setup))
        or (
            scenario_source == "configured"
            and any(_is_metadata_only_hook(hook) for hook in scenarios or ())
        )
        or (state_factory_source == "configured" and _is_metadata_only_hook(state_factory))
    ):
        return True, None
    if factory is None:
        return False, "auto-harness dry-run could not find an object factory"
    try:
        instance = _call_sync(_unwrap(factory))
    except Exception as exc:
        return (
            False,
            f"auto-harness dry-run failed during factory invocation: {type(exc).__name__}: {exc}",
        )
    if not isinstance(instance, owner):
        return (
            False,
            "auto-harness dry-run returned "
            f"{type(instance).__name__}, expected {owner.__qualname__}",
        )
    try:
        if setup is not None and setup_source == "mined":
            instance = _apply_instance_hook(instance, setup)
        if scenarios and scenario_source == "mined":
            instance = _apply_instance_hooks(instance, scenarios)
        if state_factory is not None and state_factory_source == "mined" and state_param:
            _build_state_value(state_factory, instance=instance)
    except Exception as exc:
        return (
            False,
            "auto-harness dry-run failed while preparing the instance: "
            f"{type(exc).__name__}: {exc}",
        )
    return True, None


def _build_state_value(
    state_factory: Any | None,
    *,
    instance: Any,
) -> Any:
    """Build one state object for a bound method invocation."""
    if state_factory is None:
        raise ValueError("state factory is not configured")
    return _call_with_optional_instance_arg(state_factory, instance)


def _prepare_bound_method_call(
    target: Any,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    *,
    instance: Any,
    state_factory: Any | None,
    state_param: str | None,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Normalize wrapper args into kwargs and inject configured state when needed."""
    if not state_param or state_factory is None:
        return tuple(args), dict(kwargs)

    wrapper_sig = _signature_without_first_context(target, omit_names=(state_param,))
    bound = wrapper_sig.bind_partial(*args, **kwargs)
    call_kwargs = dict(bound.arguments)
    call_kwargs.setdefault(state_param, _build_state_value(state_factory, instance=instance))
    return (), call_kwargs


def _apply_instance_hook(instance: Any, hook: Any | None) -> Any:
    """Apply a setup or scenario hook and keep any replacement instance."""
    if hook is None:
        return instance
    if isinstance(hook, Mapping):
        return _apply_instance_scenario_spec(instance, hook)
    result = _call_sync(_unwrap(hook), instance)
    return instance if result is None else result


def _apply_instance_hooks(instance: Any, hooks: Sequence[Any] | None) -> Any:
    """Apply a sequence of setup/scenario hooks in order."""
    current = instance
    for hook in hooks or ():
        current = _apply_instance_hook(current, hook)
    return current


def _normalize_scenario_path(path: str) -> str:
    """Normalize a scenario target path relative to one configured instance."""
    cleaned = path.strip()
    for prefix in ("self.", "instance.", "obj."):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def _resolve_scenario_target(instance: Any, path: str) -> tuple[Any, str]:
    """Resolve ``foo.bar.baz`` into ``(foo.bar, "baz")`` on *instance*."""
    cleaned = _normalize_scenario_path(path)
    parts = [part for part in cleaned.split(".") if part]
    if not parts:
        raise ValueError("scenario path is empty")
    current = instance
    for part in parts[:-1]:
        current = getattr(current, part)
    return current, parts[-1]


def _clone_scenario_value(value: object) -> object:
    """Clone configured scenario values when possible to avoid cross-call sharing."""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _scenario_exception(error: object) -> BaseException:
    """Coerce one TOML-friendly exception description into an exception instance."""
    if isinstance(error, BaseException):
        return error
    if inspect.isclass(error) and issubclass(error, BaseException):
        return error()
    if isinstance(error, Mapping):
        name = str(error.get("type") or error.get("name") or "RuntimeError").strip()
        message = str(error.get("message") or error.get("detail") or "").strip()
        exc_type = getattr(builtins, name, RuntimeError)
        if inspect.isclass(exc_type) and issubclass(exc_type, BaseException):
            return exc_type(message)
        return RuntimeError(f"{name}: {message}" if message else name)
    if isinstance(error, str):
        name, sep, message = error.partition(":")
        exc_name = name.strip() or "RuntimeError"
        exc_type = getattr(builtins, exc_name, RuntimeError)
        detail = message.strip() if sep else error.strip()
        if inspect.isclass(exc_type) and issubclass(exc_type, BaseException):
            return exc_type(detail)
        return RuntimeError(detail or exc_name)
    return RuntimeError(repr(error))


def _scenario_stub(original: Any, *, value: object = None, error: object | None = None) -> Any:
    """Build one stub wrapper that preserves async behavior for collaborators."""
    is_async = inspect.iscoroutinefunction(getattr(original, "__func__", original))

    if is_async:

        async def wrapped(*_args: Any, **_kwargs: Any) -> Any:
            if error is not None:
                raise _scenario_exception(error)
            return _clone_scenario_value(value)

    else:

        def wrapped(*_args: Any, **_kwargs: Any) -> Any:
            if error is not None:
                raise _scenario_exception(error)
            return _clone_scenario_value(value)

    return functools.wraps(original)(wrapped)


def _apply_instance_scenario_spec(
    instance: Any,
    spec: Mapping[str, object],
) -> Any:
    """Apply one declarative collaborator scenario spec to *instance*."""
    kind = str(spec.get("kind") or spec.get("action") or "").strip().lower()
    path = str(spec.get("path") or spec.get("attr") or spec.get("target") or "").strip()
    if not kind:
        raise ValueError("scenario spec is missing 'kind'")
    if not path:
        raise ValueError("scenario spec is missing 'path'")

    target, attr_name = _resolve_scenario_target(instance, path)
    match kind:
        case "setattr":
            setattr(target, attr_name, _clone_scenario_value(spec.get("value")))
        case "stub_return":
            original = getattr(target, attr_name)
            setattr(
                target,
                attr_name,
                _scenario_stub(original, value=spec.get("value")),
            )
        case "stub_raise":
            original = getattr(target, attr_name)
            setattr(
                target,
                attr_name,
                _scenario_stub(
                    original,
                    error=spec.get("error") or spec.get("exception") or "RuntimeError",
                ),
            )
        case _:
            raise ValueError(f"unsupported scenario kind: {kind!r}")
    return instance


def _make_sync_callable(
    func: Any,
    *,
    qualname: str | None = None,
    keep_wrapped: bool = False,
) -> Any:
    """Wrap *func* so callers can invoke sync or async callables uniformly."""

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return _call_sync(func, *args, **kwargs)

    try:
        wrapped.__signature__ = inspect.signature(func)
    except (TypeError, ValueError):
        pass
    if qualname is not None:
        wrapped.__qualname__ = qualname
    if keep_wrapped:
        wrapped.__ordeal_keep_wrapped__ = True
    return wrapped


def _resolve_method_callable(
    owner: type,
    method_name: str,
    raw_attr: Any,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Resolve a class attribute into a sync-capable callable."""
    qualname = f"{owner.__qualname__}.{method_name}"
    if isinstance(raw_attr, staticmethod) or isinstance(raw_attr, classmethod):
        bound = getattr(owner, method_name)
        if inspect.iscoroutinefunction(bound):
            return qualname, _make_sync_callable(
                bound,
                qualname=qualname,
                keep_wrapped=True,
            )
        return qualname, bound

    factory = _resolve_object_hook(owner, object_factories)
    setup = _resolve_object_hook(owner, object_setups)
    scenarios = _resolve_object_hooks(owner, object_scenarios)
    state_factory = _resolve_object_hook(owner, object_state_factories)
    teardown = _resolve_object_hook(owner, object_teardowns)
    harness = _resolve_object_harness(owner, object_harnesses)
    factory_source = "configured" if factory is not None else None
    setup_source = "configured" if setup is not None else None
    scenario_source = "configured" if scenarios else None
    state_factory_source = "configured" if state_factory is not None else None
    teardown_source = "configured" if teardown is not None else None
    harness_source = "configured" if harness != "fresh" else None
    mined_runtime = _mined_object_runtime(owner, method_name)
    if factory is None and mined_runtime.factory is not None:
        factory = mined_runtime.factory
        factory_source = mined_runtime.factory_source
    if setup is None and mined_runtime.setup is not None:
        setup = mined_runtime.setup
        setup_source = mined_runtime.setup_source
    if not scenarios and mined_runtime.scenarios:
        scenarios = mined_runtime.scenarios
        scenario_source = mined_runtime.scenario_source
    if state_factory is None and mined_runtime.state_factory is not None:
        state_factory = mined_runtime.state_factory
        state_factory_source = mined_runtime.state_factory_source
    if teardown is None and mined_runtime.teardown is not None:
        teardown = mined_runtime.teardown
        teardown_source = mined_runtime.teardown_source
    if harness == "fresh" and mined_runtime.harness is not None:
        harness = mined_runtime.harness
        harness_source = mined_runtime.harness_source
    state_param = _state_param_name_for_callable(raw_attr)
    if inspect.isfunction(raw_attr):
        if factory is None:
            return (
                qualname,
                _make_unbound_method_placeholder(
                    owner,
                    method_name,
                    raw_attr,
                    state_param=state_param,
                    state_factory=state_factory,
                    state_factory_source=state_factory_source,
                    harness_hints=mined_runtime.hints,
                ),
            )
        harness_verified, harness_dry_run_error = _verify_auto_object_runtime(
            owner,
            factory=factory,
            setup=setup,
            scenarios=scenarios,
            state_factory=state_factory,
            state_param=state_param,
            factory_source=factory_source,
            setup_source=setup_source,
            scenario_source=scenario_source,
            state_factory_source=state_factory_source,
        )
        return (
            qualname,
            _make_bound_method_callable(
                owner,
                method_name,
                raw_attr,
                factory=factory,
                setup=setup,
                scenarios=scenarios,
                state_factory=state_factory,
                state_param=state_param,
                teardown=teardown,
                harness=harness,
                harness_hints=mined_runtime.hints,
                factory_source=factory_source,
                setup_source=setup_source,
                scenario_source=scenario_source,
                state_factory_source=state_factory_source,
                teardown_source=teardown_source,
                harness_source=harness_source,
                harness_verified=harness_verified,
                harness_dry_run_error=harness_dry_run_error,
            ),
        )

    return qualname, _make_sync_callable(getattr(owner, method_name), qualname=qualname)


def _make_bound_method_callable(
    owner: type,
    method_name: str,
    method: Any,
    *,
    factory: Any,
    setup: Any | None = None,
    scenarios: Sequence[Any] | None = None,
    state_factory: Any | None = None,
    state_param: str | None = None,
    teardown: Any | None = None,
    harness: str = "fresh",
    harness_hints: Sequence[HarnessHint] | None = None,
    factory_source: str | None = None,
    setup_source: str | None = None,
    scenario_source: str | None = None,
    state_factory_source: str | None = None,
    teardown_source: str | None = None,
    harness_source: str | None = None,
    harness_verified: bool = True,
    harness_dry_run_error: str | None = None,
) -> Any:
    """Build a sync wrapper that creates a fresh object per invocation."""
    target = _unwrap(method)
    lifecycle_phase = _lifecycle_phase(method_name, target)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        instance: Any = None
        result: Any = None
        error: BaseException | None = None
        before_state = None
        call_args = tuple(args)
        call_kwargs = dict(kwargs)
        teardown_called = False
        teardown_error: str | None = None
        probe_cleanup: Callable[[], None] | None = None
        probe_context: dict[str, Any] = {}
        lifecycle_runtime: dict[str, Any] = {}
        call_stage = "factory"
        failure_stage: str | None = None
        fault_names = tuple(getattr(wrapped, "__ordeal_contract_faults__", ()))
        try:
            instance = _call_sync(factory)
            before_state = _snapshot_instance_state(instance)
            with _lifecycle_fault_runtime(
                instance,
                owner,
                method_name=method_name,
                setup=setup,
                teardown=teardown,
                fault_names=fault_names,
            ) as lifecycle_runtime:
                runtime_setup = lifecycle_runtime.get("setup_hook", setup)
                runtime_teardown = lifecycle_runtime.get("teardown_hook", teardown)
                try:
                    call_stage = "probe"
                    probe_cleanup, probe_context = _instance_probe_result(
                        getattr(wrapped, "__ordeal_instance_probe__", None),
                        instance=instance,
                        owner=owner,
                        method_name=method_name,
                    )
                    call_stage = "setup"
                    instance = _apply_instance_hook(instance, runtime_setup)
                    call_stage = "scenario"
                    instance = _apply_instance_hooks(instance, scenarios)
                    before_state = _snapshot_instance_state(instance)
                    bound = getattr(instance, method_name)
                    call_stage = "prepare"
                    call_args, call_kwargs = _prepare_bound_method_call(
                        target,
                        args,
                        kwargs,
                        instance=instance,
                        state_factory=state_factory,
                        state_param=state_param,
                    )
                    call_stage = "invoke"
                    result = _call_sync(bound, *call_args, **call_kwargs)
                    return result
                except BaseException as exc:
                    error = exc
                    failure_stage = call_stage
                    raise
                finally:
                    if runtime_teardown is not None:
                        call_stage = "teardown"
                        teardown_called = True
                        try:
                            _call_with_optional_instance_arg(runtime_teardown, instance)
                        except BaseException as exc:
                            teardown_error = f"{type(exc).__name__}: {exc}"
                            if error is None:
                                error = exc
                                raise
        except BaseException as exc:
            error = exc
            if failure_stage is None:
                failure_stage = call_stage
            raise
        finally:
            wrapped.__ordeal_last_call_context__ = {
                "instance": instance,
                "before_state": before_state,
                "after_state": (
                    _snapshot_instance_state(instance) if instance is not None else None
                ),
                "kwargs": dict(call_kwargs),
                "args": tuple(call_args),
                "method_name": method_name,
                "owner": owner,
                "harness": harness,
                "result": result,
                "error": error,
                "teardown_called": teardown_called,
                "teardown_error": teardown_error,
                "lifecycle_phase": lifecycle_phase,
                "lifecycle_runtime": lifecycle_runtime,
                "call_stage": call_stage,
                "failure_stage": failure_stage,
                **probe_context,
            }
            if probe_cleanup is not None:
                probe_cleanup()

    try:
        wrapped.__signature__ = _signature_without_first_context(
            target,
            omit_names=((state_param,) if state_factory is not None and state_param else ()),
        )
    except (TypeError, ValueError):
        pass
    wrapped.__qualname__ = f"{owner.__qualname__}.{method_name}"
    wrapped.__ordeal_requires_factory__ = False
    wrapped.__ordeal_owner__ = owner
    wrapped.__ordeal_method_name__ = method_name
    wrapped.__ordeal_factory__ = factory
    wrapped.__ordeal_factory_source__ = factory_source
    wrapped.__ordeal_setup__ = setup
    wrapped.__ordeal_setup_source__ = setup_source
    wrapped.__ordeal_scenario__ = (scenarios or (None,))[0]
    wrapped.__ordeal_scenarios__ = tuple(scenarios or ())
    wrapped.__ordeal_scenario_source__ = scenario_source
    wrapped.__ordeal_state_factory__ = state_factory
    wrapped.__ordeal_state_factory_source__ = state_factory_source
    wrapped.__ordeal_state_param__ = state_param
    wrapped.__ordeal_teardown__ = teardown
    wrapped.__ordeal_teardown_source__ = teardown_source
    wrapped.__ordeal_harness__ = harness
    wrapped.__ordeal_harness_source__ = harness_source
    wrapped.__ordeal_kind__ = "instance"
    wrapped.__ordeal_lifecycle_phase__ = lifecycle_phase
    wrapped.__ordeal_keep_wrapped__ = True
    wrapped.__ordeal_instance_probe__ = None
    wrapped.__ordeal_auto_harness__ = any(
        source == "mined"
        for source in (
            factory_source,
            setup_source,
            scenario_source,
            state_factory_source,
            teardown_source,
            harness_source,
        )
    )
    wrapped.__ordeal_harness_verified__ = harness_verified
    wrapped.__ordeal_harness_dry_run_error__ = harness_dry_run_error
    wrapped.__ordeal_harness_hints__ = tuple(
        harness_hints or _mine_object_harness_hints(owner.__module__, owner.__name__, method_name)
    )
    return wrapped


def _make_unbound_method_placeholder(
    owner: type,
    method_name: str,
    method: Any,
    *,
    state_param: str | None = None,
    state_factory: Any | None = None,
    state_factory_source: str | None = None,
    harness_hints: Sequence[HarnessHint] | None = None,
) -> Any:
    """Build a placeholder callable for a method that still needs a factory."""
    target = _unwrap(method)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(f"{owner.__qualname__}.{method_name} needs an object factory")

    try:
        wrapped.__signature__ = _signature_without_first_context(target)
    except (TypeError, ValueError):
        pass
    wrapped.__qualname__ = f"{owner.__qualname__}.{method_name}"
    wrapped.__ordeal_requires_factory__ = True
    wrapped.__ordeal_owner__ = owner
    wrapped.__ordeal_method_name__ = method_name
    wrapped.__ordeal_kind__ = "instance"
    wrapped.__ordeal_harness__ = "fresh"
    wrapped.__ordeal_harness_source__ = None
    wrapped.__ordeal_state_factory__ = state_factory
    wrapped.__ordeal_state_factory_source__ = state_factory_source
    wrapped.__ordeal_state_param__ = state_param
    wrapped.__ordeal_lifecycle_phase__ = _lifecycle_phase(method_name, target)
    wrapped.__ordeal_skip_reason__ = "missing object factory"
    wrapped.__ordeal_keep_wrapped__ = True
    wrapped.__ordeal_instance_probe__ = None
    wrapped.__ordeal_auto_harness__ = False
    wrapped.__ordeal_factory_source__ = None
    wrapped.__ordeal_setup_source__ = None
    wrapped.__ordeal_scenario_source__ = None
    wrapped.__ordeal_teardown_source__ = None
    wrapped.__ordeal_harness_hints__ = tuple(
        harness_hints or _mine_object_harness_hints(owner.__module__, owner.__name__, method_name)
    )
    wrapped.__ordeal_scenarios__ = ()
    return wrapped


def _callable_skip_reason(func: Any) -> str | None:
    """Return a human-readable reason a generated callable is not runnable."""
    if getattr(func, "__ordeal_requires_factory__", False):
        return getattr(func, "__ordeal_skip_reason__", "missing object factory")
    return None


def _resolve_explicit_target(
    target: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Resolve ``module:callable`` or ``module:Class.method`` targets."""
    module_name, sep, attr_path = target.partition(":")
    if not sep or not attr_path:
        raise ValueError("Explicit targets must use 'module:callable' syntax")

    obj = _resolve_module(module_name)
    parts = [part for part in attr_path.split(".") if part]
    if not parts:
        raise ValueError("Explicit targets must name a callable")

    for part in parts[:-1]:
        obj = getattr(obj, part)

    final_name = parts[-1]
    if inspect.isclass(obj):
        static_attr = inspect.getattr_static(obj, final_name, None)
        if static_attr is None:
            raise AttributeError(f"{target} does not exist")
        return _resolve_method_callable(
            obj,
            final_name,
            static_attr,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )

    resolved = getattr(obj, final_name)
    if inspect.isclass(resolved):
        raise TypeError(f"{target} resolves to a class, not a callable")
    if not callable(resolved):
        raise TypeError(f"{target} does not resolve to a callable")

    qualname = (
        final_name
        if obj.__class__.__module__ == "builtins"
        else f"{getattr(obj, '__qualname__', obj.__class__.__name__)}.{final_name}"
    )
    return qualname, resolved


def _is_exact_target_selector(selector: str) -> bool:
    """Return whether *selector* is an exact callable selector, not a glob."""
    text = str(selector).strip()
    return bool(text) and not any(char in text for char in "*?[]")


def _resolve_local_target(
    mod: ModuleType,
    target: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Resolve one local callable selector like ``foo`` or ``Env.render``."""
    selector = str(target).strip()
    module_variants = [f"{mod.__name__}."]
    module_parts = [part for part in str(mod.__name__).split(".") if part]
    for index in range(1, len(module_parts)):
        module_variants.append(f"{'.'.join(module_parts[index:])}.")
    for prefix in module_variants:
        if selector.startswith(prefix):
            selector = selector[len(prefix) :]
            break
    try:
        return _resolve_explicit_target(
            f"{mod.__name__}:{selector}",
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"target selector {target!r} matched no callables in module {mod.__name__!r}"
        ) from exc


def _selected_public_functions(
    mod: ModuleType,
    *,
    targets: Sequence[str] | None = None,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> list[tuple[str, Any]]:
    """Return discovered callables filtered to *targets* when provided."""
    normalized_targets = [str(raw).strip() for raw in targets or () if str(raw).strip()]
    if normalized_targets and all(
        _is_exact_target_selector(target) for target in normalized_targets
    ):
        selected: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for target in normalized_targets:
            if ":" in target:
                base_module = target.split(":", 1)[0]
                if base_module != mod.__name__:
                    raise ValueError(
                        f"target {target!r} does not belong to module {mod.__name__!r}"
                    )
                name, func = _resolve_explicit_target(
                    target,
                    object_factories=object_factories,
                    object_setups=object_setups,
                    object_scenarios=object_scenarios,
                    object_state_factories=object_state_factories,
                    object_teardowns=object_teardowns,
                    object_harnesses=object_harnesses,
                )
            else:
                name, func = _resolve_local_target(
                    mod,
                    target,
                    object_factories=object_factories,
                    object_setups=object_setups,
                    object_scenarios=object_scenarios,
                    object_state_factories=object_state_factories,
                    object_teardowns=object_teardowns,
                    object_harnesses=object_harnesses,
                )
            if name in seen:
                continue
            seen.add(name)
            selected.append((name, func))
        return selected

    discovered = _get_public_functions(
        mod,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )
    if not normalized_targets:
        return discovered

    discovered_map = {name: func for name, func in discovered}
    selected: list[tuple[str, Any]] = []
    seen: set[str] = set()

    for target in normalized_targets:
        if ":" in target:
            base_module = target.split(":", 1)[0]
            if base_module != mod.__name__:
                raise ValueError(f"target {target!r} does not belong to module {mod.__name__!r}")

        matched_names = [
            name
            for name in discovered_map
            if _callable_matches_target_selector(mod.__name__, name, target)
        ]
        if matched_names:
            for name in matched_names:
                if name in seen:
                    continue
                seen.add(name)
                selected.append((name, discovered_map[name]))
            continue

        if target in discovered_map:
            name = target
            func = discovered_map[target]
        elif ":" in target:
            name, func = _resolve_explicit_target(
                target,
                object_factories=object_factories,
                object_setups=object_setups,
                object_scenarios=object_scenarios,
                object_state_factories=object_state_factories,
                object_teardowns=object_teardowns,
                object_harnesses=object_harnesses,
            )
        else:
            raise ValueError(
                f"target selector {target!r} matched no callables in module {mod.__name__!r}"
            )

        if name in seen:
            continue
        seen.add(name)
        selected.append((name, func))
    return selected


def _callable_matches_target_selector(module_name: str, name: str, selector: str) -> bool:
    """Return whether *selector* matches discovered callable *name*."""
    raw_selector = str(selector).strip()
    if not raw_selector:
        return False
    variants: list[str] = [
        name,
        f"{module_name}.{name}",
        f"{module_name}:{name}",
    ]
    module_parts = [part for part in str(module_name).split(".") if part]
    for index in range(1, len(module_parts)):
        suffix = ".".join(module_parts[index:])
        variants.extend((f"{suffix}.{name}", f"{suffix}:{name}"))
    return any(fnmatch.fnmatchcase(variant, raw_selector) for variant in variants)


def _command_tokens(value: Any) -> list[str] | None:
    """Return command tokens for shell-like return values."""
    if isinstance(value, os.PathLike):
        return [os.fspath(value)]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, (str, os.PathLike)) for item in value
    ):
        return [os.fspath(item) for item in value]
    return None


def _tracked_string_args(
    kwargs: Mapping[str, Any],
    tracked_params: Sequence[str] | None,
) -> list[str]:
    """Return the string argument values tracked by a semantic contract."""
    names = list(
        tracked_params
        or [name for name, value in kwargs.items() if isinstance(value, (str, os.PathLike))]
    )
    tracked: list[str] = []
    for name in names:
        value = kwargs.get(name)
        if isinstance(value, str):
            tracked.append(value)
        elif isinstance(value, os.PathLike):
            tracked.append(os.fspath(value))
    return tracked


def _tracked_token_count(tokens: Sequence[str], raw: str) -> int:
    """Count occurrences of a tracked argument, allowing slash normalization."""
    variants = {raw}
    if "/" in raw or "\\" in raw:
        variants.add(raw.replace("\\", "/"))
        variants.add(raw.replace("/", "\\"))
    return sum(1 for token in tokens if token in variants)


def _contract_check_is_static(check: ContractCheck) -> bool:
    """Return whether *check* is static-only and should avoid execution."""
    return bool(check.metadata.get("static_only"))


def _shell_injection_probe_kwargs(
    kwargs: Mapping[str, Any],
    tracked_params: Sequence[str] | None,
) -> dict[str, Any]:
    """Return probe kwargs with shell-metacharacter payloads in tracked string fields."""
    probe_kwargs = dict(kwargs)
    candidate_names = list(tracked_params or probe_kwargs)
    mutated = False
    for name in candidate_names:
        value = probe_kwargs.get(name)
        if isinstance(value, (str, os.PathLike)):
            probe_kwargs[name] = _SHELL_INJECTION_PROBE_VALUE
            mutated = True
    if not mutated:
        for name, value in list(probe_kwargs.items()):
            if isinstance(value, (str, os.PathLike)):
                probe_kwargs[name] = _SHELL_INJECTION_PROBE_VALUE
                mutated = True
    return probe_kwargs


def _shell_value_has_metacharacters(value: Any) -> bool:
    """Return whether *value* contains shell-significant metacharacters."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    return isinstance(value, str) and any(ch in value for ch in _SHELL_INJECTION_META_CHARS)


def _shell_taint_from_value(value: Any) -> int:
    """Return shell-taint severity for one concrete value."""
    if _shell_value_has_metacharacters(value):
        return _SHELL_TAINT_UNSAFE
    return _SHELL_TAINT_CLEAN


def _merge_shell_taint(*states: int) -> int:
    """Return the most dangerous shell-taint state."""
    return max(states, default=_SHELL_TAINT_CLEAN)


def _merge_shell_envs(*envs: Mapping[str, int]) -> dict[str, int]:
    """Merge branch-local shell taint environments conservatively."""
    merged: dict[str, int] = {}
    keys = {key for env in envs for key in env}
    for key in keys:
        merged[key] = _merge_shell_taint(
            *(int(env.get(key, _SHELL_TAINT_CLEAN)) for env in envs),
        )
    return merged


def _callable_display_name(func: Any) -> str:
    """Return a stable display name for *func* in diagnostics."""
    module_name, qual_parts, leaf_name = _call_target_parts(func)
    qualname = ".".join([*qual_parts, leaf_name]) if qual_parts else leaf_name
    return f"{module_name}.{qualname}" if module_name else qualname


def _function_ast_bundle(
    func: Any,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, int, ModuleType | None, type | None] | None:
    """Return parsed AST plus source metadata for *func* when available."""
    target = _unwrap(func)
    try:
        source_lines, start_line = inspect.getsourcelines(target)
        tree = ast.parse(textwrap.dedent("".join(source_lines)))
    except (OSError, TypeError, SyntaxError):
        return None
    node = next(
        (item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    if node is None:
        return None
    module_name = getattr(target, "__module__", "")
    module: ModuleType | None = None
    with contextlib.suppress(Exception):
        module = importlib.import_module(module_name)
    owner = getattr(func, "__ordeal_owner__", None)
    return node, start_line, module, owner


def _resolve_shell_call_name(
    node: ast.Call,
    *,
    module: ModuleType | None,
    owner: type | None,
) -> str | None:
    """Resolve one AST call target into a dotted name when possible."""
    raw_name = _call_to_string(node)
    func_expr = node.func
    if isinstance(func_expr, ast.Name):
        if module is not None:
            obj = getattr(module, func_expr.id, None)
            if callable(obj) and not inspect.isclass(obj):
                obj = _unwrap(obj)
                obj_name = getattr(obj, "__name__", func_expr.id)
                obj_module = getattr(obj, "__module__", "")
                if obj_module and obj_name:
                    return f"{obj_module}.{obj_name}"
        return raw_name
    if (
        isinstance(func_expr, ast.Attribute)
        and isinstance(func_expr.value, ast.Name)
        and func_expr.value.id in {"self", "cls"}
        and owner is not None
    ):
        method = inspect.getattr_static(owner, func_expr.attr, None)
        if callable(method) and not inspect.isclass(method):
            method = _unwrap(method)
            return ".".join(
                part
                for part in (
                    getattr(owner, "__module__", ""),
                    getattr(owner, "__name__", ""),
                    getattr(method, "__name__", func_expr.attr),
                )
                if part
            )
    return raw_name


def _shell_sink_specs() -> tuple[dict[str, Any], ...]:
    """Return built-in sink specs relevant to shell-injection analysis."""
    return _DEFAULT_SHELL_INJECTION_SINK_SPECS


def _matching_shell_sink_specs(call_name: str | None) -> list[dict[str, Any]]:
    """Return shell sink specs whose pattern matches *call_name*."""
    if not call_name:
        return []
    matches: list[dict[str, Any]] = []
    for spec in _shell_sink_specs():
        pattern = str(spec.get("pattern", "")).strip()
        if not pattern:
            continue
        with contextlib.suppress(re.error):
            if re.search(pattern, call_name):
                matches.append(spec)
    return matches


def _shell_call_arg(
    node: ast.Call,
    parameter: str | int,
) -> ast.AST | None:
    """Return the AST expression bound to one sink parameter selector."""
    if isinstance(parameter, int):
        return node.args[parameter] if 0 <= parameter < len(node.args) else None
    for keyword in node.keywords:
        if keyword.arg == parameter:
            return keyword.value
    return None


def _subprocess_shell_enabled(node: ast.Call) -> bool:
    """Return whether a subprocess-like call explicitly enables shell parsing."""
    for keyword in node.keywords:
        if keyword.arg != "shell":
            continue
        if isinstance(keyword.value, ast.Constant):
            return bool(keyword.value.value)
        return True
    return False


def _shell_expr_taint(
    expr: ast.AST | None,
    *,
    env: Mapping[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[int, _ShellInjectionFlow | None]:
    """Return shell taint for one expression plus any nested sink flow."""
    if expr is None:
        return _SHELL_TAINT_CLEAN, None
    if isinstance(expr, ast.Name):
        return int(env.get(expr.id, _SHELL_TAINT_CLEAN)), None
    if isinstance(expr, ast.Constant):
        return _shell_taint_from_value(expr.value), None
    if isinstance(expr, ast.JoinedStr):
        states = []
        for value in expr.values:
            if isinstance(value, ast.FormattedValue):
                state, flow = _shell_expr_taint(
                    value.value,
                    env=env,
                    module=module,
                    owner=owner,
                    depth=depth,
                    call_path=call_path,
                    seen=seen,
                )
                if flow is not None:
                    return state, flow
                states.append(state)
        if any(state == _SHELL_TAINT_UNSAFE for state in states):
            return _SHELL_TAINT_UNSAFE, None
        if any(state == _SHELL_TAINT_SAFE for state in states):
            return _SHELL_TAINT_SAFE, None
        return _SHELL_TAINT_CLEAN, None
    if isinstance(expr, ast.BinOp):
        left_state, flow = _shell_expr_taint(
            expr.left,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return left_state, flow
        right_state, flow = _shell_expr_taint(
            expr.right,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return right_state, flow
        return _merge_shell_taint(left_state, right_state), None
    if isinstance(expr, (ast.List, ast.Tuple)):
        states: list[int] = []
        for item in expr.elts:
            state, flow = _shell_expr_taint(
                item,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return state, flow
            states.append(state)
        if any(state != _SHELL_TAINT_CLEAN for state in states):
            return _SHELL_TAINT_SAFE, None
        return _SHELL_TAINT_CLEAN, None
    if isinstance(expr, ast.Call):
        call_name = _resolve_shell_call_name(expr, module=module, owner=owner)
        if call_name:
            flow = _shell_sink_uses_tainted_input(
                expr,
                call_name=call_name,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                start_line=0,
                seen=seen,
            )
            if flow is not None:
                return _SHELL_TAINT_UNSAFE, flow
        if call_name in {"shlex.quote", "quote"} and expr.args:
            arg_state, flow = _shell_expr_taint(
                expr.args[0],
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return arg_state, flow
            if arg_state != _SHELL_TAINT_CLEAN:
                return _SHELL_TAINT_SAFE, None
            return _SHELL_TAINT_CLEAN, None
        if (
            isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "format"
            and isinstance(expr.func.value, ast.Constant)
            and isinstance(expr.func.value.value, str)
        ):
            states: list[int] = []
            for item in (*expr.args, *(keyword.value for keyword in expr.keywords)):
                state, flow = _shell_expr_taint(
                    item,
                    env=env,
                    module=module,
                    owner=owner,
                    depth=depth,
                    call_path=call_path,
                    seen=seen,
                )
                if flow is not None:
                    return state, flow
                states.append(state)
            return _merge_shell_taint(*states), None
        if call_name in {"str", "repr", "os.fspath", "pathlib.Path"} and expr.args:
            return _shell_expr_taint(
                expr.args[0],
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
        helper = _resolve_local_shell_helper(call_name, module=module, owner=owner)
        if helper is not None and depth > 0:
            arg_states, flow = _shell_call_arg_states(
                expr,
                helper,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return _SHELL_TAINT_UNSAFE, flow
            helper_flow, return_state = _analyze_shell_flow(
                helper,
                arg_states,
                depth=depth - 1,
                call_path=(*call_path, _callable_display_name(helper)),
                seen=seen,
            )
            if helper_flow is not None:
                return _SHELL_TAINT_UNSAFE, helper_flow
            return return_state, None
    return _SHELL_TAINT_CLEAN, None


def _resolve_local_shell_helper(
    call_name: str | None,
    *,
    module: ModuleType | None,
    owner: type | None,
) -> Any | None:
    """Resolve same-module or same-class helpers for interprocedural shell analysis."""
    if not call_name:
        return None
    leaf = call_name.rsplit(".", 1)[-1]
    if module is not None:
        helper = getattr(module, leaf, None)
        if callable(helper) and not inspect.isclass(helper):
            helper = _unwrap(helper)
            if getattr(helper, "__module__", None) == module.__name__:
                return helper
    if owner is not None and "." in call_name and call_name.split(".")[0] in {"self", "cls"}:
        helper = inspect.getattr_static(owner, leaf, None)
        if callable(helper) and not inspect.isclass(helper):
            return helper
    if owner is not None and call_name.endswith(f".{leaf}"):
        helper = inspect.getattr_static(owner, leaf, None)
        if callable(helper) and not inspect.isclass(helper):
            return helper
    return None


def _shell_call_arg_states(
    node: ast.Call,
    helper: Any,
    *,
    env: Mapping[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[dict[str, int], _ShellInjectionFlow | None]:
    """Map one helper call's tainted arguments onto the callee's parameters."""
    try:
        params = [
            param
            for param in inspect.signature(_unwrap(helper)).parameters.values()
            if param.name not in {"self", "cls"}
        ]
    except Exception:
        return {}, None
    states: dict[str, int] = {}
    for index, arg in enumerate(node.args):
        if index >= len(params):
            break
        state, flow = _shell_expr_taint(
            arg,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return states, flow
        states[params[index].name] = state
    param_names = {param.name for param in params}
    for keyword in node.keywords:
        if keyword.arg is None or keyword.arg not in param_names:
            continue
        state, flow = _shell_expr_taint(
            keyword.value,
            env=env,
            module=module,
            owner=owner,
            depth=depth,
            call_path=call_path,
            seen=seen,
        )
        if flow is not None:
            return states, flow
        states[keyword.arg] = state
    return states, None


def _shell_sink_uses_tainted_input(
    node: ast.Call,
    *,
    call_name: str,
    env: Mapping[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    start_line: int,
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> _ShellInjectionFlow | None:
    """Return a flow when tainted input reaches one shell sink unsafely."""
    for spec in _matching_shell_sink_specs(call_name):
        for parameter in tuple(spec.get("taint_args", ())):
            expr = _shell_call_arg(node, parameter)
            if expr is None:
                continue
            state, nested_flow = _shell_expr_taint(
                expr,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if nested_flow is not None:
                return nested_flow
            if state != _SHELL_TAINT_UNSAFE:
                continue
            if "subprocess." in call_name and not _subprocess_shell_enabled(node):
                if isinstance(expr, (ast.List, ast.Tuple, ast.Name)):
                    continue
            line = start_line + getattr(node, "lineno", 1) - 1 if start_line > 0 else None
            source_params = tuple(
                sorted(name for name, value in env.items() if value == _SHELL_TAINT_UNSAFE)
            )
            return _ShellInjectionFlow(
                sink=call_name,
                line=line,
                parameter=parameter,
                call_path=call_path,
                source_params=source_params,
            )
    return None


def _shell_bind_targets(target: ast.AST, state: int, env: dict[str, int]) -> None:
    """Bind one assignment target in the local shell-taint environment."""
    if isinstance(target, ast.Name):
        env[target.id] = state
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _shell_bind_targets(item, state, env)


def _analyze_shell_statements(
    statements: Sequence[ast.stmt],
    *,
    env: dict[str, int],
    module: ModuleType | None,
    owner: type | None,
    depth: int,
    call_path: tuple[str, ...],
    start_line: int,
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[_ShellInjectionFlow | None, dict[str, int], int]:
    """Walk one function body, propagating taint and surfacing the first sink flow."""
    return_state = _SHELL_TAINT_CLEAN
    for statement in statements:
        if isinstance(statement, ast.Assign):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            for target in statement.targets:
                _shell_bind_targets(target, state, env)
            continue
        if isinstance(statement, ast.AnnAssign):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            _shell_bind_targets(statement.target, state, env)
            continue
        if isinstance(statement, ast.AugAssign):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            if isinstance(statement.target, ast.Name):
                env[statement.target.id] = _merge_shell_taint(
                    env.get(statement.target.id, _SHELL_TAINT_CLEAN),
                    state,
                )
            continue
        if isinstance(statement, ast.Return):
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            return_state = _merge_shell_taint(return_state, state)
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call_name = _resolve_shell_call_name(statement.value, module=module, owner=owner)
            flow = _shell_sink_uses_tainted_input(
                statement.value,
                call_name=call_name or "",
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                start_line=start_line,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            state, flow = _shell_expr_taint(
                statement.value,
                env=env,
                module=module,
                owner=owner,
                depth=depth,
                call_path=call_path,
                seen=seen,
            )
            if flow is not None:
                return flow, env, return_state
            return_state = _merge_shell_taint(return_state, state)
            continue
        branch_lists: list[Sequence[ast.stmt]] = []
        if isinstance(statement, ast.If):
            branch_lists = [statement.body, statement.orelse]
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            branch_lists = [statement.body, statement.orelse]
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            branch_lists = [statement.body]
        elif isinstance(statement, ast.Try):
            branch_lists = [
                statement.body,
                statement.orelse,
                statement.finalbody,
                *(handler.body for handler in statement.handlers),
            ]
        if branch_lists:
            branch_envs: list[dict[str, int]] = [dict(env)]
            branch_returns: list[int] = [return_state]
            for branch in branch_lists:
                flow, branch_env, branch_return = _analyze_shell_statements(
                    branch,
                    env=dict(env),
                    module=module,
                    owner=owner,
                    depth=depth,
                    call_path=call_path,
                    start_line=start_line,
                    seen=seen,
                )
                if flow is not None:
                    return flow, env, return_state
                branch_envs.append(branch_env)
                branch_returns.append(branch_return)
            env = _merge_shell_envs(*branch_envs)
            return_state = _merge_shell_taint(*branch_returns)
    return None, env, return_state


def _analyze_shell_flow(
    func: Any,
    param_states: Mapping[str, int],
    *,
    depth: int,
    call_path: tuple[str, ...],
    seen: set[tuple[str, tuple[tuple[str, int], ...], int]],
) -> tuple[_ShellInjectionFlow | None, int]:
    """Analyze one callable for shell-injection flow and tainted return construction."""
    key = (
        _callable_display_name(func),
        tuple(sorted((str(name), int(state)) for name, state in param_states.items())),
        depth,
    )
    if key in seen:
        return None, _SHELL_TAINT_CLEAN
    seen.add(key)
    bundle = _function_ast_bundle(func)
    if bundle is None:
        return None, _SHELL_TAINT_CLEAN
    node, start_line, module, owner = bundle
    flow, _env, return_state = _analyze_shell_statements(
        node.body,
        env={str(name): int(state) for name, state in param_states.items()},
        module=module,
        owner=owner,
        depth=depth,
        call_path=call_path,
        start_line=start_line,
        seen=seen,
    )
    return flow, return_state


def _record_static_contract_context(func: Any, context: Mapping[str, Any] | None) -> None:
    """Store one static-analysis context payload on *func* for later reporting."""
    setattr(func, "__ordeal_last_static_contract_context__", dict(context or {}))


def _static_shell_injection_flow(
    func: Any,
    kwargs: Mapping[str, Any],
) -> _ShellInjectionFlow | None:
    """Return a static shell-injection flow from *kwargs* into a known shell sink."""
    param_states = {
        str(name): _shell_taint_from_value(value)
        for name, value in kwargs.items()
        if _shell_taint_from_value(value) != _SHELL_TAINT_CLEAN
    }
    if not param_states:
        return None
    flow, _return_state = _analyze_shell_flow(
        func,
        param_states,
        depth=3,
        call_path=(_callable_display_name(func),),
        seen=set(),
    )
    return flow


def shell_safe_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a shell-safety probe for command construction helpers."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable("shell_safe only applies to command-builder outputs")
        for raw in _tracked_string_args(kwargs, tracked_params):
            if any(ch in raw for ch in " \t;&|`$><()[]{}*?"):
                if _tracked_token_count(tokens, raw) != 1:
                    return False
        return True

    return ContractCheck(
        name="shell_safe",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="shell-unsafe string interpolation",
    )


def shell_injection_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a static shell-injection oracle for command-executing helpers."""

    def predicate(
        value: Any,
        *,
        func: Any,
        kwargs: Mapping[str, Any],
        **_extra: Any,
    ) -> bool:
        del value
        if not any(_shell_value_has_metacharacters(item) for item in kwargs.values()):
            raise ContractNotApplicable(
                "shell_injection only applies to string inputs containing shell metacharacters"
            )
        flow = _static_shell_injection_flow(func, kwargs)
        _record_static_contract_context(
            func,
            (
                {
                    "kind": "shell_injection",
                    "sink": flow.sink,
                    "line": flow.line,
                    "parameter": flow.parameter,
                    "call_path": list(flow.call_path),
                    "source_params": list(flow.source_params),
                }
                if flow is not None
                else None
            ),
        )
        return flow is None

    return ContractCheck(
        name="shell_injection",
        kwargs=_shell_injection_probe_kwargs(kwargs, tracked_params),
        predicate=predicate,
        summary="shell metacharacters can reach a shell sink without quoting",
        metadata={"static_only": True},
    )


def quoted_paths_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a path-quoting probe for command builders."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable("quoted_paths only applies to command-builder outputs")
        for raw in _tracked_string_args(kwargs, tracked_params):
            if "/" in raw or "\\" in raw or " " in raw:
                if _tracked_token_count(tokens, raw) != 1:
                    return False
        return True

    return ContractCheck(
        name="quoted_paths",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="path quoting or escaping regression",
    )


def command_arg_stability_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a probe that ensures tracked args survive command construction."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable(
                "command_arg_stability only applies to command-builder outputs"
            )
        for raw in _tracked_string_args(kwargs, tracked_params):
            if _tracked_token_count(tokens, raw) != 1:
                return False
        return True

    return ContractCheck(
        name="command_arg_stability",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="command construction invariant failed",
    )


def protected_env_keys_contract(
    *,
    kwargs: dict[str, Any],
    protected_keys: Sequence[str],
    env_param: str | None = None,
) -> ContractCheck:
    """Build a probe that checks protected env keys survive updates."""
    resolved_env_param = env_param or next(
        (name for name, value in kwargs.items() if isinstance(value, Mapping)),
        None,
    )

    def predicate(value: Any) -> bool:
        if resolved_env_param is None:
            return False
        original = kwargs.get(resolved_env_param)
        if not isinstance(original, Mapping) or not isinstance(value, Mapping):
            return False
        return all(value.get(key) == original.get(key) for key in protected_keys)

    return ContractCheck(
        name="protected_env_keys",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="protected env-var contract violated",
    )


def json_roundtrip_contract(
    *,
    kwargs: dict[str, Any],
) -> ContractCheck:
    """Build a probe that checks returned values survive JSON normalization."""
    import json

    def predicate(value: Any) -> bool:
        try:
            json.dumps(value)
        except Exception:
            return False
        return True

    return ContractCheck(
        name="json_roundtrip",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="JSON/tool-call normalization regression",
    )


def http_shape_contract(
    *,
    kwargs: dict[str, Any],
) -> ContractCheck:
    """Build a probe that checks HTTP-like payloads keep string-shaped headers and body."""

    def _mapping_is_httpish(value: Mapping[Any, Any]) -> bool:
        for key, item in value.items():
            if not isinstance(key, str):
                return False
            if isinstance(item, Mapping):
                if any(not isinstance(nested_key, str) for nested_key in item):
                    return False
        return True

    def _body_is_httpish(value: Any) -> bool:
        return value is None or isinstance(
            value,
            (str, bytes, bytearray, memoryview, Mapping, list, tuple),
        )

    def predicate(value: Any) -> bool:
        if isinstance(value, Mapping):
            return _mapping_is_httpish(value)
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) == 2 and isinstance(items[0], Mapping):
                return _mapping_is_httpish(items[0]) and _body_is_httpish(items[1])
            if len(items) == 3 and isinstance(items[0], int) and isinstance(items[1], Mapping):
                return _mapping_is_httpish(items[1]) and _body_is_httpish(items[2])
        raise ContractNotApplicable(
            "http_shape only applies to HTTP-like mapping or response outputs"
        )

    return ContractCheck(
        name="http_shape",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="HTTP header/body shaping regression",
    )


def subprocess_argv_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a probe that checks subprocess argv tokens stay intact."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            raise ContractNotApplicable("subprocess_argv only applies to command-builder outputs")
        if not tokens or not isinstance(tokens[0], str) or not tokens[0]:
            return False
        for raw in _tracked_string_args(kwargs, tracked_params):
            if _tracked_token_count(tokens, raw) != 1:
                return False
        return True

    return ContractCheck(
        name="subprocess_argv",
        kwargs=dict(kwargs),
        predicate=predicate,
        summary="subprocess argv construction regression",
    )


def lifecycle_attempts_all_contract(
    *,
    kwargs: dict[str, Any],
    phase: str,
    fault: str = "raise_cleanup_handler",
    handler_name: str | None = None,
    contract_name: str = "lifecycle_attempts_all",
) -> ContractCheck:
    """Build a lifecycle probe that requires best-effort handler attempts."""

    def predicate(
        value: Any,
        *,
        lifecycle_probe: Mapping[str, Any] | None = None,
        **_extra: Any,
    ) -> bool:
        del value
        probe = dict(lifecycle_probe or {})
        attempts = list(probe.get("attempts", []))
        target_handlers = list(probe.get("target_handlers", []))
        if not target_handlers:
            return False
        return all(name in attempts for name in target_handlers)

    return ContractCheck(
        name=contract_name,
        kwargs=dict(kwargs),
        predicate=predicate,
        summary=f"all {phase} handlers should be attempted even if one fails",
        metadata={
            "kind": "lifecycle",
            "phase": phase,
            "fault": fault,
            "handler_name": handler_name,
        },
    )


def lifecycle_followup_contract(
    *,
    kwargs: dict[str, Any],
    phase: str,
    followup_phases: Sequence[str],
    fault: str = "raise_setup_hook",
    handler_name: str | None = None,
    contract_name: str = "lifecycle_followup",
) -> ContractCheck:
    """Build a lifecycle probe that requires follow-up phases after a fault."""

    def predicate(
        value: Any,
        *,
        lifecycle_probe: Mapping[str, Any] | None = None,
        teardown_called: bool | None = None,
        **_extra: Any,
    ) -> bool:
        del value
        probe = dict(lifecycle_probe or {})
        attempts = list(probe.get("attempts", []))
        followup_handlers = dict(probe.get("followup_handlers", {}))
        if not followup_handlers and teardown_called:
            return True
        saw_followup = False
        for followup_phase, names in followup_handlers.items():
            if followup_phase == "teardown" and teardown_called:
                saw_followup = True
                continue
            if not names:
                continue
            if any(name in attempts for name in names):
                saw_followup = True
                continue
            return False
        return saw_followup

    phases = [str(item) for item in followup_phases if str(item).strip()]
    summary = (
        f"{', '.join(phases)} handlers should still be attempted after {phase} fails"
        if phases
        else f"follow-up lifecycle handlers should still be attempted after {phase} fails"
    )
    return ContractCheck(
        name=contract_name,
        kwargs=dict(kwargs),
        predicate=predicate,
        summary=summary,
        metadata={
            "kind": "lifecycle",
            "phase": phase,
            "fault": fault,
            "handler_name": handler_name,
            "followup_phases": phases,
            "runtime_faults": (
                [fault]
                if fault in {"raise_setup_hook", "raise_teardown_hook", "cancel_rollout"}
                else []
            ),
        },
    )


def builtin_contract_check(
    name: str,
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
    protected_keys: Sequence[str] | None = None,
    env_param: str | None = None,
    phase: str | None = None,
    followup_phases: Sequence[str] | None = None,
    fault: str = "raise",
    handler_name: str | None = None,
) -> ContractCheck:
    """Build one built-in semantic contract probe by *name*."""
    match name:
        case "cleanup_attempts_all":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="cleanup",
                fault="raise",
                handler_name=handler_name,
            )
        case "teardown_attempts_all":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="teardown",
                fault="raise",
                handler_name=handler_name,
            )
        case "setup_failure_triggers_teardown":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="setup",
                followup_phases=["teardown"],
                fault="raise",
                handler_name=handler_name,
            )
        case "rollout_cancellation_triggers_cleanup":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="rollout",
                followup_phases=["cleanup", "teardown"],
                fault="cancel",
                handler_name=handler_name,
            )
        case "shell_safe":
            return shell_safe_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "shell_injection":
            return shell_injection_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "quoted_paths":
            return quoted_paths_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "command_arg_stability":
            return command_arg_stability_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "protected_env_keys":
            return protected_env_keys_contract(
                kwargs=kwargs,
                protected_keys=list(protected_keys or []),
                env_param=env_param,
            )
        case "json_roundtrip":
            return json_roundtrip_contract(kwargs=kwargs)
        case "http_shape":
            return http_shape_contract(kwargs=kwargs)
        case "subprocess_argv":
            return subprocess_argv_contract(kwargs=kwargs, tracked_params=tracked_params)
        case "all_cleanup_handlers_attempted":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="cleanup",
                fault="raise_cleanup_handler",
                handler_name=handler_name,
                contract_name="all_cleanup_handlers_attempted",
            )
        case "all_teardown_handlers_attempted":
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase="teardown",
                fault="raise_teardown_handler",
                handler_name=handler_name,
                contract_name="all_teardown_handlers_attempted",
            )
        case "cleanup_after_setup_failure":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="setup",
                followup_phases=list(followup_phases or ("cleanup", "teardown")),
                fault="raise_setup_hook",
                handler_name=handler_name,
                contract_name="cleanup_after_setup_failure",
            )
        case "cleanup_after_cancellation":
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase="rollout",
                followup_phases=list(followup_phases or ("cleanup", "teardown")),
                fault="cancel_rollout",
                handler_name=handler_name,
                contract_name="cleanup_after_cancellation",
            )
        case "lifecycle_attempts_all":
            resolved_phase = str(phase or "cleanup")
            return lifecycle_attempts_all_contract(
                kwargs=kwargs,
                phase=resolved_phase,
                fault=fault,
                handler_name=handler_name,
            )
        case "lifecycle_followup":
            resolved_phase = str(phase or "rollout")
            return lifecycle_followup_contract(
                kwargs=kwargs,
                phase=resolved_phase,
                followup_phases=list(followup_phases or ("cleanup", "teardown")),
                fault=fault,
                handler_name=handler_name,
            )
        case _:
            raise ValueError(f"unknown built-in contract check: {name}")


def _auto_contract_checks(
    func: Any,
    seed_examples: Sequence[SeedExample],
    *,
    auto_contracts: Sequence[str] | None,
    ignore_contracts: Sequence[str] | None = None,
    shell_injection_check: bool = False,
    security_focus: bool = False,
) -> tuple[list[ContractCheck], list[str]]:
    """Infer built-in sink-aware contract probes for *func* from source and seeds."""
    sink_categories = _infer_sink_categories(func, security_focus=security_focus)

    enabled = set(_expand_contract_names_ordered(auto_contracts or _DEFAULT_AUTO_CONTRACTS))
    if shell_injection_check:
        enabled.add("shell_injection")
    ignored = _expand_contract_names(ignore_contracts)
    probe_kwargs = dict(seed_examples[0].kwargs) if seed_examples else _contract_seed_kwargs(func)
    tracked_params = list(probe_kwargs)
    env_param = next(
        (name for name, value in probe_kwargs.items() if isinstance(value, Mapping)),
        None,
    )
    protected_keys = [
        key
        for key in ("PATH", "HOME", "PWD", "TMPDIR")
        if env_param is not None
        and isinstance(probe_kwargs.get(env_param), Mapping)
        and key in probe_kwargs.get(env_param, {})
    ]

    contract_names: list[str] = []
    if {"shell", "subprocess"} & set(sink_categories):
        contract_names.extend(["shell_safe", "command_arg_stability", "subprocess_argv"])
        if shell_injection_check:
            contract_names.append("shell_injection")
    if "path" in sink_categories:
        contract_names.append("quoted_paths")
    if "env" in sink_categories and protected_keys:
        contract_names.append("protected_env_keys")
    if "json_tool_call" in sink_categories:
        contract_names.append("json_roundtrip")
    if "http" in sink_categories:
        contract_names.append("http_shape")

    lifecycle_phase = getattr(func, "__ordeal_lifecycle_phase__", None)
    if (
        lifecycle_phase
        and getattr(func, "__ordeal_kind__", None) == "instance"
        and getattr(func, "__ordeal_factory__", None) is not None
    ):
        owner = getattr(func, "__ordeal_owner__", None)
        method_name = str(getattr(func, "__ordeal_method_name__", ""))
        handlers = _discover_lifecycle_handlers(owner, lifecycle_phase)
        if method_name in handlers and len(handlers) > 1:
            handlers = [name for name in handlers if name != method_name]
        if lifecycle_phase == "cleanup" and len(handlers) >= 1:
            contract_names.append("cleanup_attempts_all")
        if lifecycle_phase == "stop" and len(handlers) >= 1:
            contract_names.append("lifecycle_attempts_all")
        if lifecycle_phase == "teardown" and len(handlers) >= 1:
            contract_names.append("teardown_attempts_all")
        if lifecycle_phase in {"setup", "rollout"}:
            followup = [
                phase
                for phase in ("cleanup", "teardown", "stop")
                if _discover_lifecycle_handlers(owner, phase)
                or (phase == "teardown" and getattr(func, "__ordeal_teardown__", None) is not None)
            ]
            if followup:
                if lifecycle_phase == "setup":
                    contract_names.append("setup_failure_triggers_teardown")
                else:
                    contract_names.append("rollout_cancellation_triggers_cleanup")

    checks: list[ContractCheck] = []
    for name in dict.fromkeys(contract_names):
        if name not in enabled or name in ignored or not probe_kwargs:
            continue
        followup_phases: list[str] | None = None
        phase = None
        if name in {"lifecycle_attempts_all", "cleanup_attempts_all", "teardown_attempts_all"}:
            phase = str(lifecycle_phase or "cleanup")
            if name == "cleanup_attempts_all":
                phase = "cleanup"
            elif name == "teardown_attempts_all":
                phase = "teardown"
        elif name in {
            "lifecycle_followup",
            "setup_failure_triggers_teardown",
            "rollout_cancellation_triggers_cleanup",
        }:
            phase = str(lifecycle_phase or "rollout")
            if name == "setup_failure_triggers_teardown":
                phase = "setup"
                followup_phases = ["teardown"]
            elif name == "rollout_cancellation_triggers_cleanup":
                phase = "rollout"
                followup_phases = ["cleanup", "teardown"]
            else:
                followup_phases = [
                    phase_name
                    for phase_name in ("cleanup", "teardown", "stop")
                    if _discover_lifecycle_handlers(
                        getattr(func, "__ordeal_owner__", None), phase_name
                    )
                ]
        checks.append(
            builtin_contract_check(
                name,
                kwargs=probe_kwargs,
                tracked_params=tracked_params,
                protected_keys=protected_keys,
                env_param=env_param,
                phase=phase,
                followup_phases=followup_phases,
            )
        )
    return checks, sink_categories


def _get_public_functions(
    mod: ModuleType,
    *,
    include_private: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> list[tuple[str, Any]]:
    """Return (name, callable) pairs for testable callables.

    By default this includes public module functions and public class
    methods. Instance methods are wrapped only when a registered or
    explicit object factory is available; otherwise they are returned as
    placeholder callables that report a missing object factory.

    Discovery is based on the module's own ``__dict__`` and class
    ``__dict__`` entries for normal modules. Package targets also include
    public callable exports visible via ``dir()`` when they resolve back
    into the same package namespace, so lazy-exported APIs such as
    ``ordeal.scan_module`` remain discoverable from the package root.

    Decorated functions (``@ray.remote``, ``@functools.wraps``, etc.)
    are auto-unwrapped so that ``mine()``, ``fuzz()``, and ``scan_module()``
    can inspect signatures and call the real function.
    """
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

    results: list[tuple[str, Any]] = []
    package_prefix = f"{mod.__name__}."
    is_package = bool(getattr(mod, "__path__", None))
    items: dict[str, Any] = dict(sorted(vars(mod).items()))
    if is_package:
        for name in sorted(dir(mod)):
            if name.startswith("__"):
                continue
            if name.startswith("_") and not include_private:
                continue
            if name in items:
                continue
            try:
                obj = getattr(mod, name)
            except BaseException as exc:
                if _is_fatal_discovery_exception(exc):
                    raise
                continue
            obj_mod = getattr(obj, "__module__", None)
            if obj_mod == mod.__name__ or (
                isinstance(obj_mod, str) and obj_mod.startswith(package_prefix)
            ):
                items[name] = obj

    for name, obj in items.items():
        if name.startswith("__"):
            continue
        if name.startswith("_") and not include_private:
            continue
        if callable(obj) and not isinstance(obj, type):
            obj_mod = getattr(obj, "__module__", None)
            if (
                obj_mod
                and obj_mod != mod.__name__
                and not (
                    is_package and isinstance(obj_mod, str) and obj_mod.startswith(package_prefix)
                )
            ):
                continue
            target = _unwrap(obj)
            if inspect.iscoroutinefunction(target):
                results.append(
                    (
                        name,
                        _make_sync_callable(target, qualname=name, keep_wrapped=True),
                    )
                )
            else:
                results.append((name, target))
            continue
        obj_mod = getattr(obj, "__module__", None)
        if not inspect.isclass(obj) or obj_mod != mod.__name__:
            continue

        for meth_name, static_attr in sorted(vars(obj).items()):
            if meth_name.startswith("__"):
                continue
            if meth_name.startswith("_") and not include_private:
                continue
            if isinstance(static_attr, property):
                continue
            if not (
                isinstance(static_attr, (staticmethod, classmethod))
                or inspect.isfunction(static_attr)
            ):
                continue
            results.append(
                _resolve_method_callable(
                    obj,
                    meth_name,
                    static_attr,
                    object_factories=merged_factories,
                    object_setups=merged_setups,
                    object_scenarios=merged_scenarios,
                    object_state_factories=merged_state_factories,
                    object_teardowns=merged_teardowns,
                    object_harnesses=merged_harnesses,
                )
            )
    return results


_FIXTURE_REGISTRY_MODULES: set[str] = set()


def _load_fixture_registry_path(path: Path) -> str | None:
    """Import one fixture registry file and return a warning on failure."""
    resolved = path.resolve()
    key = str(resolved)
    if key in _FIXTURE_REGISTRY_MODULES:
        return None
    spec = importlib.util.spec_from_file_location(
        f"_ordeal_fixture_registry_{len(_FIXTURE_REGISTRY_MODULES)}",
        resolved,
    )
    if spec is None or spec.loader is None:
        return f"could not load fixture registry: {resolved}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _FIXTURE_REGISTRY_MODULES.add(key)
    return None


def load_fixture_registry_modules(modules: list[str]) -> list[str]:
    """Import explicit registry modules so ``register_fixture()`` takes effect."""
    warnings: list[str] = []
    for module_name in modules:
        if module_name in _FIXTURE_REGISTRY_MODULES:
            continue
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            warnings.append(f"fixture registry load failed for {module_name}: {exc}")
            continue
        _FIXTURE_REGISTRY_MODULES.add(module_name)
    return warnings


def load_project_fixture_registries(
    *,
    root: Path | None = None,
    extra_modules: list[str] | None = None,
) -> list[str]:
    """Import local registries so ``register_fixture()`` takes effect."""
    base = (root or Path.cwd()).resolve()
    warnings: list[str] = []
    candidates = [
        base / "conftest.py",
        base / "tests" / "conftest.py",
        base / "test" / "conftest.py",
        base / "src" / "tests" / "conftest.py",
    ]

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        try:
            warning = _load_fixture_registry_path(resolved)
            if warning:
                warnings.append(warning)
        except Exception as exc:
            warnings.append(f"fixture registry load failed for {resolved}: {exc}")
    if extra_modules:
        warnings.extend(load_fixture_registry_modules(list(extra_modules)))
    return warnings


def _infer_strategies(
    func: Any,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
) -> dict[str, st.SearchStrategy[Any]] | None:
    """Infer strategies from fixtures → name patterns → type hints.

    Resolution order per parameter:
    1. Explicit fixture (user-provided)
    2. Common name pattern (COMMON_NAME_STRATEGIES)
    3. Type hint (via strategy_for_type)
    4. Default value → skip
    5. None → can't infer, return None for entire function
    """
    target = _unwrap(func)
    if _callable_skip_reason(target) is not None:
        return None

    hints: dict[str, Any] = {}
    for candidate in (target, getattr(target, "__func__", None)):
        if candidate is None:
            continue
        hints = safe_get_annotations(candidate)
        if hints:
            break

    sig = inspect.signature(target)
    strategies: dict[str, st.SearchStrategy[Any]] = {}

    for name, param in sig.parameters.items():
        if name == "self" or name == "cls":
            continue
        has_default = param.default is not inspect.Parameter.empty
        # 1. Explicit fixture (always wins)
        if fixtures and name in fixtures:
            strategies[name] = fixtures[name]
        # 2. Default is None → sample both None and the typed value.
        #    Previously this skipped the param entirely, which blocked
        #    mine() on any function with Optional params.
        elif has_default and param.default is None:
            if name in hints:
                # Optional[T] → sample T | None.
                # If the type hint already includes None (e.g. Optional[str],
                # str | None), strategy_for_type handles it via the Union path.
                # Only add st.none() if the hint doesn't already include None.
                hint = hints[name]
                origin = get_origin(hint)
                args = get_args(hint)
                already_optional = (origin is Union and type(None) in args) or hint is type(None)
                strat = strategy_for_type(hint)
                if not already_optional:
                    strat = st.one_of(strat, st.none())
                strategies[name] = strat
            else:
                continue
        # 3. Common name pattern
        elif (name_strat := _strategy_for_name(name)) is not None:
            strategies[name] = name_strat
        # 4. Type hint
        elif name in hints:
            strategies[name] = strategy_for_type(hints[name])
        # 5. Has non-None default → let Python use it
        elif has_default:
            continue
        # 6. Can't infer
        else:
            return None

    return strategies


def _literal_seed_value(node: ast.AST) -> Any:
    """Return a Python literal from a small AST subset."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_literal_seed_value(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal_seed_value(item) for item in node.elts)
    if isinstance(node, ast.Set):
        return {_literal_seed_value(item) for item in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _literal_seed_value(key): _literal_seed_value(value)
            for key, value in zip(node.keys, node.values, strict=False)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = _literal_seed_value(node.operand)
        if isinstance(operand, (int, float)):
            return -operand
    raise ValueError("not a literal seed value")


def _is_simple_literal_node(node: ast.AST) -> bool:
    """Return True when *node* is safe to evaluate as a literal seed."""
    try:
        _literal_seed_value(node)
    except Exception:
        return False
    return True


def _test_search_roots(module_name: str) -> list[Path]:
    """Return likely roots containing valid example seeds for *module_name*."""
    roots = [Path.cwd() / "tests", Path.cwd()]
    try:
        module = importlib.import_module(module_name)
    except Exception:
        module = None
    module_file = getattr(module, "__file__", None)
    if module_file:
        module_dir = Path(module_file).resolve().parent
        if module_dir not in roots:
            roots.append(module_dir)
    return [root for root in roots if root.exists()]


def _callable_seed_files(module_name: str) -> list[Path]:
    """Return candidate Python files that may contain realistic seed examples."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in _test_search_roots(module_name):
        for pattern in ("test_*.py", "*_test.py", "conftest.py"):
            for path in root.rglob(pattern):
                resolved = path.resolve()
                if (
                    resolved in seen
                    or not resolved.is_file()
                    or not _is_project_discovery_path(resolved)
                ):
                    continue
                seen.add(resolved)
                candidates.append(resolved)
    return sorted(candidates)


def _camel_case_tokens(text: str) -> list[str]:
    """Split one identifier into coarse searchable tokens."""
    return [token.lower() for token in re.findall(r"[A-Z]?[a-z]+|[0-9]+", text) if token]


def _searchable_tokens(text: str) -> set[str]:
    """Return coarse word tokens for lightweight harness matching."""
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(text)):
        lowered = raw.lower()
        tokens.add(lowered)
        tokens.update(part for part in lowered.split("_") if part)
        tokens.update(_camel_case_tokens(raw))
    return {token for token in tokens if token}


def _harness_doc_files(module_name: str) -> list[Path]:
    """Return markdown files that may document lifecycle harness setup."""
    roots = [Path.cwd(), Path.cwd() / "docs"]
    try:
        module = importlib.import_module(module_name)
    except Exception:
        module = None
    module_file = getattr(module, "__file__", None)
    if module_file:
        module_root = Path(module_file).resolve().parent
        roots.extend([module_root, *module_root.parents[:2]])

    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("README*.md", "*.md", "docs/**/*.md"):
            for path in root.glob(pattern):
                resolved = path.resolve()
                if (
                    resolved in seen
                    or not resolved.is_file()
                    or not _is_project_discovery_path(resolved)
                ):
                    continue
                seen.add(resolved)
                candidates.append(resolved)
    return sorted(candidates)


_SCENARIO_PACK_ATTR_ALIASES: dict[str, str] = {
    "artifact_client": "upload_download",
    "cache": "state_store",
    "classifier": "model_inference",
    "command_runner": "subprocess",
    "downloader": "upload_download",
    "embedder": "model_inference",
    "embedding_store": "feature_store",
    "encoder": "model_inference",
    "executor": "subprocess",
    "feature_client": "feature_store",
    "feature_store": "feature_store",
    "http_client": "http",
    "model": "model_inference",
    "model_client": "model_inference",
    "model_registry": "upload_download",
    "model_store": "upload_download",
    "predictor": "model_inference",
    "process_runner": "subprocess",
    "reranker": "model_inference",
    "runner": "subprocess",
    "sandbox": "sandbox_client",
    "sandbox_client": "sandbox_client",
    "scorer": "model_inference",
    "session": "http",
    "session_state": "state_store",
    "state_store": "state_store",
    "storage_client": "upload_download",
    "store": "state_store",
    "subprocess": "subprocess",
    "transport": "http",
    "upload_download": "upload_download",
    "uploader": "upload_download",
    "vector_store": "feature_store",
    "weights_store": "upload_download",
}


def _constructor_aliases(
    tree: ast.AST,
    module_name: str,
    class_name: str,
) -> tuple[set[str], set[str]]:
    """Return direct and module aliases that may construct *class_name*."""
    direct = {class_name}
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            for alias in node.names:
                if imported_module == module_name and alias.name == class_name:
                    direct.add(alias.asname or alias.name)
                elif f"{imported_module}.{alias.name}" == module_name:
                    module_aliases.add(alias.asname or alias.name)
    return direct, module_aliases


def _call_looks_like_target_constructor(
    call: ast.Call,
    *,
    direct_aliases: set[str],
    module_aliases: set[str],
    class_name: str,
) -> bool:
    """Return whether *call* looks like ``ClassName(...)`` for the target."""
    func_name = _call_name(call.func)
    if func_name is None:
        return False
    if func_name in direct_aliases or func_name == class_name:
        return True
    if "." not in func_name:
        return False
    head, tail = func_name.rsplit(".", 1)
    return tail == class_name and head in module_aliases


def _names_returning_target_instance(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    direct_aliases: set[str],
    module_aliases: set[str],
    class_name: str,
) -> set[str]:
    """Return local names within *node* that hold a target instance."""
    names: set[str] = set()
    for child in ast.walk(node):
        value: ast.AST | None = None
        targets: list[ast.expr] = []
        if isinstance(child, ast.Assign):
            value = child.value
            targets = list(child.targets)
        elif isinstance(child, ast.AnnAssign):
            value = child.value
            targets = [child.target]
        if value is None or not isinstance(value, ast.Call):
            continue
        if not _call_looks_like_target_constructor(
            value,
            direct_aliases=direct_aliases,
            module_aliases=module_aliases,
            class_name=class_name,
        ):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _function_returns_target_instance(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    direct_aliases: set[str],
    module_aliases: set[str],
    class_name: str,
) -> tuple[bool, set[str]]:
    """Return whether *node* returns a target instance and the local instance names."""
    instance_names = _names_returning_target_instance(
        node,
        direct_aliases=direct_aliases,
        module_aliases=module_aliases,
        class_name=class_name,
    )
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        if isinstance(child.value, ast.Name) and child.value.id in instance_names:
            return True, instance_names
        if isinstance(child.value, ast.Call) and _call_looks_like_target_constructor(
            child.value,
            direct_aliases=direct_aliases,
            module_aliases=module_aliases,
            class_name=class_name,
        ):
            return True, instance_names
    return False, instance_names


def _instance_attr_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    instance_names: set[str],
) -> set[str]:
    """Return collaborator attr names assigned on local instance variables."""
    attrs: set[str] = set()
    if not instance_names:
        return attrs
    for child in ast.walk(node):
        targets: list[ast.expr] = []
        if isinstance(child, ast.Assign):
            targets = list(child.targets)
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in instance_names
            ):
                attrs.add(target.attr)
    return attrs


def _scenario_packs_for_attrs(attr_names: set[str]) -> list[str]:
    """Return built-in scenario packs implied by observed collaborator attrs."""
    packs: list[str] = []
    for attr_name in sorted(attr_names):
        pack = _SCENARIO_PACK_ATTR_ALIASES.get(attr_name)
        if pack is not None and pack not in packs:
            packs.append(pack)
    return packs


def _source_collaborator_attrs(
    module_name: str,
    class_name: str,
    method_name: str,
) -> tuple[set[str], str | None]:
    """Return ``self.<attr>`` names used in the target method source."""
    try:
        module = importlib.import_module(module_name)
        owner = getattr(module, class_name)
        method = getattr(owner, method_name)
        source_file = _source_file_for_callable(method)
        source_text = inspect.getsource(method)
    except Exception:
        return set(), None

    try:
        tree = ast.parse(textwrap.dedent(source_text))
    except SyntaxError:
        return set(), None

    attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"self", "instance", "env", "obj"}
    }
    if source_file is None:
        return attrs, None
    try:
        return attrs, str(source_file.relative_to(Path.cwd()))
    except ValueError:
        return attrs, str(source_file)


@functools.lru_cache(maxsize=128)
def _mine_object_harness_hints_cached(
    module_name: str,
    class_name: str,
    method_name: str,
    workspace_root: str,
) -> tuple[HarnessHint, ...]:
    """Mine likely factory/state/teardown/client hooks from tests and docs."""
    class_tokens = {class_name.lower(), *(_camel_case_tokens(class_name))}
    method_tokens = {method_name.lower(), *(_camel_case_tokens(method_name))}
    target_tokens = class_tokens | method_tokens
    state_hint_tokens = {"state", "context", "cache"}
    state_param_name: str | None = None
    collaborator_packs: list[str] = []
    collaborator_evidence: str | None = None
    with contextlib.suppress(Exception):
        module = importlib.import_module(module_name)
        owner = getattr(module, class_name)
        if inspect.getattr_static(owner, method_name, None) is not None:
            state_param_name = _state_param_name_for_callable(getattr(owner, method_name))
        source_attrs, source_path = _source_collaborator_attrs(
            module_name,
            class_name,
            method_name,
        )
        collaborator_packs = _scenario_packs_for_attrs(source_attrs)
        collaborator_evidence = source_path
    hints: list[HarnessHint] = []
    seen: set[tuple[str, str]] = set()

    def _add_hint(
        kind: str,
        suggestion: str,
        evidence: str,
        confidence: float,
        *,
        signals: Sequence[str] = (),
        config: dict[str, Any] | None = None,
    ) -> None:
        key = (kind, suggestion)
        if key in seen:
            return
        seen.add(key)
        normalized_signals = tuple(
            dict.fromkeys(
                [
                    *(str(item) for item in signals if str(item).strip()),
                    *_harness_hint_path_signals(evidence),
                ]
            )
        )
        hints.append(
            HarnessHint(
                kind=kind,
                suggestion=suggestion,
                evidence=evidence,
                confidence=confidence,
                score=_score_harness_hint(confidence, normalized_signals),
                signals=normalized_signals,
                config=dict(config or {}),
            )
        )

    support_files = list(_callable_seed_files(module_name))
    extra_patterns = ("*factory*.py", "*fixture*.py", "*support*.py", "conftest.py")
    for root in _test_search_roots(module_name):
        for pattern in extra_patterns:
            for path in root.rglob(pattern):
                resolved = path.resolve()
                if (
                    resolved.is_file()
                    and resolved not in support_files
                    and _is_project_discovery_path(resolved)
                ):
                    support_files.append(resolved)

    fixture_catalog = _pytest_fixture_catalog(
        tuple(str(path.resolve()) for path in support_files if path.exists())
    )

    for path in sorted(dict.fromkeys(support_files)):
        tree = _parse_python_source(str(path))
        if tree is None:
            continue
        direct_aliases, module_aliases = _constructor_aliases(tree, module_name, class_name)
        try:
            display_path = path.relative_to(Path.cwd())
        except ValueError:
            display_path = path
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name_lower = node.name.lower()
            doc_lower = (ast.get_docstring(node) or "").lower()
            returns_lower = (
                ast.unparse(node.returns).lower()
                if getattr(node, "returns", None) is not None
                else ""
            )
            text_lower = " ".join([name_lower, doc_lower, returns_lower])
            text_tokens = _searchable_tokens(" ".join([node.name, doc_lower, returns_lower]))
            name_tokens = _searchable_tokens(node.name)
            is_fixture = any(
                _call_name(decorator.func) == "pytest.fixture"
                if isinstance(decorator, ast.Call)
                else _call_name(decorator) == "pytest.fixture"
                for decorator in node.decorator_list
            )
            evidence = f"{display_path}:{getattr(node, 'lineno', '?')}"
            fixture_info = fixture_catalog.get(node.name, {})
            symbol_value = str(fixture_info.get("symbol") or _symbol_hint_value(path, node.name))
            fixture_values = list(fixture_info.get("values", ()))
            returns_mapping = any(isinstance(value, Mapping) for value in fixture_values) or (
                "dict" in returns_lower or "mapping" in returns_lower
            )
            mentions_target = bool(text_tokens & target_tokens)
            mentions_state = bool(text_tokens & state_hint_tokens)
            returns_target, instance_names = _function_returns_target_instance(
                node,
                direct_aliases=direct_aliases,
                module_aliases=module_aliases,
                class_name=class_name,
            )
            returns_target = returns_target or _matches_target_fixture(
                fixture_info,
                class_tokens=class_tokens,
            )
            attr_packs = _scenario_packs_for_attrs(
                _instance_attr_names(node, instance_names=instance_names)
            )
            state_like_name = bool(name_tokens & state_hint_tokens)
            state_param_tokens = (
                _searchable_tokens(state_param_name) if state_param_name is not None else set()
            )
            matches_state_param = bool(
                state_param_name is not None
                and (name_lower == state_param_name.lower() or name_tokens & state_param_tokens)
            )
            supports_state_factory = _callable_supports_optional_instance_call(node)
            looks_like_factory = (
                name_lower.startswith(("make_", "build_", "create_", "new_"))
                or "factory" in name_lower
            )

            if returns_target or (
                mentions_target
                and looks_like_factory
                and not state_like_name
                and not returns_mapping
            ):
                _add_hint(
                    "factory",
                    f"[[objects]] factory -> {evidence}:{node.name}",
                    evidence,
                    0.95 if returns_target else 0.9,
                    signals=(
                        *(("returns_target_instance",) if returns_target else ()),
                        *(("constructor_like",) if looks_like_factory else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "factory",
                        "value": symbol_value,
                    },
                )
            if supports_state_factory and (
                (returns_mapping and (mentions_target or mentions_state or matches_state_param))
                or matches_state_param
            ):
                _add_hint(
                    "state_factory",
                    f"[[objects]] state_factory -> {evidence}:{node.name}",
                    evidence,
                    0.9 if returns_mapping else 0.82,
                    signals=(
                        *(("state_compatible",) if state_param_name is not None else ()),
                        *(("returns_mapping",) if returns_mapping else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "state_factory",
                        "value": symbol_value,
                    },
                )
            if mentions_target and any(
                token in name_lower for token in ("setup", "prepare", "prime", "initialize")
            ):
                _add_hint(
                    "setup",
                    f"[[objects]] setup -> {evidence}:{node.name}",
                    evidence,
                    0.82,
                    signals=(
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        *(("returns_target_instance",) if returns_target else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "setup",
                        "value": symbol_value,
                    },
                )
            if returns_target and attr_packs:
                _add_hint(
                    "scenario_pack",
                    f"[[objects]] scenarios -> {attr_packs!r}",
                    evidence,
                    0.9,
                    signals=(
                        "returns_target_instance",
                        "collaborator_overlap",
                        *(("mentions_target_tokens",) if mentions_target else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": attr_packs,
                    },
                )
            if (
                mentions_target
                and any(token in text_lower for token in ("teardown", "cleanup", "close", "stop"))
            ) or bool(fixture_info.get("yield_cleanup")):
                _add_hint(
                    "teardown",
                    f"[[objects]] teardown -> {evidence}:{node.name}",
                    evidence,
                    0.9 if fixture_info.get("yield_cleanup") else 0.8,
                    signals=(
                        *(("lifecycle_cleanup",) if fixture_info.get("yield_cleanup") else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        *(("pytest_fixture",) if is_fixture else ()),
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "teardown",
                        "value": symbol_value,
                    },
                )
            if is_fixture and any(
                token in text_lower for token in ("client", "sandbox", "session", "transport")
            ):
                _add_hint(
                    "client_fixture",
                    f"[[objects]] scenarios -> [{evidence}:{node.name}]",
                    evidence,
                    0.75,
                    signals=(
                        *(("pytest_fixture",) if is_fixture else ()),
                        *(("mentions_target_tokens",) if mentions_target else ()),
                        "collaborator_overlap",
                    ),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": [symbol_value],
                    },
                )

    if collaborator_packs:
        _add_hint(
            "scenario_pack",
            f"[[objects]] scenarios -> {collaborator_packs!r}",
            collaborator_evidence or f"{module_name}.{class_name}.{method_name}",
            0.7,
            signals=("collaborator_overlap",),
            config={
                "section": "[[objects]]",
                "target": f"{module_name}:{class_name}",
                "method": method_name,
                "key": "scenarios",
                "value": collaborator_packs,
            },
        )

    for path in _harness_doc_files(module_name):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            display_path = path.relative_to(Path.cwd())
        except ValueError:
            display_path = path
        lowered = content.lower()
        if not any(token in lowered for token in target_tokens):
            continue
        for idx, line in enumerate(lowered.splitlines(), 1):
            if not any(token in line for token in target_tokens):
                continue
            evidence = f"{display_path}:{idx}"
            if "state" in line:
                _add_hint(
                    "state_factory",
                    "docs mention state setup for this target",
                    evidence,
                    0.55,
                    signals=("doc_evidence",),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "state_factory",
                        "value": "docs mention state setup for this target",
                    },
                )
            if any(token in line for token in ("setup", "prepare", "initialize", "prime")):
                _add_hint(
                    "setup",
                    "docs mention setup/prepare hooks for this target",
                    evidence,
                    0.55,
                    signals=("doc_evidence",),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "setup",
                        "value": "docs mention setup/prepare hooks for this target",
                    },
                )
            if any(token in line for token in ("teardown", "cleanup", "close", "stop")):
                _add_hint(
                    "teardown",
                    "docs mention lifecycle teardown/cleanup",
                    evidence,
                    0.55,
                    signals=("lifecycle_cleanup", "doc_evidence"),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "teardown",
                        "value": "docs mention lifecycle teardown/cleanup",
                    },
                )
            if any(token in line for token in ("client", "sandbox", "session")):
                _add_hint(
                    "client_fixture",
                    "docs mention a client/session collaborator",
                    evidence,
                    0.5,
                    signals=("collaborator_overlap", "doc_evidence"),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": ["docs mention a client/session collaborator"],
                    },
                )
            doc_packs = _scenario_packs_for_attrs(
                {attr_name for attr_name in _SCENARIO_PACK_ATTR_ALIASES if attr_name in line}
            )
            if doc_packs:
                _add_hint(
                    "scenario_pack",
                    f"[[objects]] scenarios -> {doc_packs!r}",
                    evidence,
                    0.5,
                    signals=("collaborator_overlap", "doc_evidence"),
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": doc_packs,
                    },
                )

    return tuple(
        sorted(
            hints,
            key=_hint_sort_key,
        )
    )


def _mine_object_harness_hints(
    module_name: str,
    class_name: str,
    method_name: str,
) -> tuple[HarnessHint, ...]:
    """Mine likely factory/state/teardown/client hooks from tests and docs."""
    return _mine_object_harness_hints_cached(
        module_name,
        class_name,
        method_name,
        str(Path.cwd().resolve()),
    )


_mine_object_harness_hints.cache_clear = _mine_object_harness_hints_cached.cache_clear  # type: ignore[attr-defined]


def _import_alias_maps(
    tree: ast.AST,
    module_name: str,
    leaf_name: str,
) -> tuple[set[str], set[str]]:
    """Return imported module aliases and direct-call aliases for a target."""
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            for alias in node.names:
                if imported_module == module_name and alias.name == leaf_name:
                    function_aliases.add(alias.asname or alias.name)
                elif f"{imported_module}.{alias.name}" == module_name:
                    module_aliases.add(alias.asname or alias.name)
    return module_aliases, function_aliases


def _call_matches_target(
    call: ast.Call,
    *,
    leaf_name: str,
    module_aliases: set[str],
    function_aliases: set[str],
) -> bool:
    """Return True when *call* looks like it invokes the target callable."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in function_aliases
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr == leaf_name and func.value.id in module_aliases
    return False


def _call_kwargs_from_ast(
    call: ast.Call,
    *,
    signature: inspect.Signature,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Convert a literal call site into concrete kwargs."""
    params = [
        param for param in signature.parameters.values() if param.name not in {"self", "cls"}
    ]
    positional_params = [
        param
        for param in params
        if param.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    ]
    if len(call.args) > len(positional_params):
        return None

    kwargs: dict[str, Any] = {}
    for param, arg in zip(positional_params, call.args, strict=False):
        value = _seed_value_from_node(arg, bindings=bindings)
        if value is _MISSING:
            return None
        kwargs[param.name] = value

    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        value = _seed_value_from_node(keyword.value, bindings=bindings)
        if value is _MISSING:
            return None
        kwargs[keyword.arg] = value

    for param in params:
        if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            return None
        if param.name not in kwargs:
            if param.default is inspect.Parameter.empty:
                return None
            kwargs[param.name] = param.default
    return kwargs


@functools.lru_cache(maxsize=128)
def _test_seed_examples_cached(
    module_name: str,
    leaf_name: str,
    workspace_root: str,
) -> tuple[SeedExample, ...]:
    """Extract literal call-site seeds for a top-level callable from test files."""
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, leaf_name)
        signature = _signature_without_first_context(func)
    except Exception:
        return ()

    seeds: list[SeedExample] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for path in _callable_seed_files(module_name):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        module_aliases, function_aliases = _import_alias_maps(tree, module_name, leaf_name)
        if not module_aliases and not function_aliases:
            continue
        scopes: list[tuple[ast.AST, list[dict[str, Any]]]] = [(tree, [{}])]
        scopes.extend(
            (node, _function_parametrize_bindings(node))
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for scope, bindings_list in scopes:
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                if not _call_matches_target(
                    node,
                    leaf_name=leaf_name,
                    module_aliases=module_aliases,
                    function_aliases=function_aliases,
                ):
                    continue
                for bindings in bindings_list or [{}]:
                    kwargs = _call_kwargs_from_ast(node, signature=signature, bindings=bindings)
                    if not kwargs:
                        continue
                    dedupe = tuple(sorted((key, repr(value)) for key, value in kwargs.items()))
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    seeds.append(
                        SeedExample(
                            kwargs=kwargs,
                            source="pytest_seed",
                            evidence=f"{path.name}:{getattr(node, 'lineno', '?')}",
                        )
                    )
    return tuple(seeds)


def _test_seed_examples(module_name: str, leaf_name: str) -> tuple[SeedExample, ...]:
    """Extract literal call-site seeds for a top-level callable from test files."""
    return _test_seed_examples_cached(module_name, leaf_name, str(Path.cwd().resolve()))


_test_seed_examples.cache_clear = _test_seed_examples_cached.cache_clear  # type: ignore[attr-defined]


def _source_boundary_candidates(func: Any) -> dict[str, list[Any]]:
    """Mine branch-edge constants from the function source."""
    try:
        source = inspect.getsource(func)
        tree = ast.parse(source)
        signature = _signature_without_first_context(func)
    except Exception:
        return {}

    param_names = {param.name for param in signature.parameters.values()}
    candidates: dict[str, list[Any]] = {name: [] for name in param_names}

    def _add(name: str, value: Any) -> None:
        bucket = candidates.setdefault(name, [])
        if value not in bucket:
            bucket.append(value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left_name = node.left.id if isinstance(node.left, ast.Name) else None
            if left_name in param_names:
                for comparator in node.comparators:
                    if _is_simple_literal_node(comparator):
                        value = _literal_seed_value(comparator)
                        _add(left_name, value)
                        if isinstance(value, int):
                            _add(left_name, value - 1)
                            _add(left_name, value + 1)
                        elif isinstance(value, float):
                            _add(left_name, value - 1.0)
                            _add(left_name, value + 1.0)
            if left_name in param_names and any(
                isinstance(comparator, ast.Constant) and comparator.value is None
                for comparator in node.comparators
            ):
                _add(left_name, None)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            if isinstance(node.operand, ast.Name) and node.operand.id in param_names:
                _add(node.operand.id, "")
                _add(node.operand.id, [])

    return {name: values for name, values in candidates.items() if values}


def _docstring_boundary_candidates(func: Any, hints: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Mine coarse boundary values from parameter-focused docstring hints."""
    doc = (inspect.getdoc(func) or "").lower()
    if not doc:
        return {}

    candidates: dict[str, list[Any]] = {}
    for name in hints:
        lowered = name.lower()
        if lowered not in doc:
            continue
        bucket: list[Any] = []
        if "non-empty" in doc or "nonempty" in doc:
            bucket.extend(["", "x"])
        if "positive" in doc:
            bucket.extend([0, 1])
        if "non-negative" in doc or "nonnegative" in doc:
            bucket.extend([0, 1])
        if "path" in lowered or "file" in lowered:
            bucket.extend(["demo.txt", "demo files/input.txt"])
        if bucket:
            candidates[name] = list(dict.fromkeys(bucket))
    return candidates


def _append_boundary_case(
    cases: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> None:
    """Append *candidate* when it is not already present."""
    if any(existing == candidate for existing in cases):
        return
    cases.append(candidate)


def _boundary_values_for_hint(hint: Any) -> list[Any]:
    """Return deterministic boundary values for common type hints."""
    import types as pytypes

    origin = get_origin(hint)
    if origin is Literal:
        return list(get_args(hint))

    if origin is Union or (hasattr(pytypes, "UnionType") and origin is pytypes.UnionType):
        values: list[Any] = []
        for arg in get_args(hint):
            if arg is type(None):
                values.append(None)
            else:
                values.extend(_boundary_values_for_hint(arg))
        return values

    if origin is list:
        return [[]]
    if origin is tuple:
        return [()]
    if origin is dict:
        return [{}]
    if origin is set:
        return [set()]
    if origin is frozenset:
        return [frozenset()]

    return list(_BOUNDARY_SMOKE_VALUES.get(hint, ()))


def _boundary_smoke_inputs(
    func: Any,
    *,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    seed_examples: Sequence[SeedExample] | None = None,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
) -> list[dict[str, Any]]:
    """Build deterministic boundary and observed inputs for one callable."""
    target = _unwrap(func)
    observed_examples = (
        list(seed_examples)
        if seed_examples is not None
        else list(
            _seed_examples_for_callable(
                target,
                seed_from_tests=seed_from_tests,
                seed_from_fixtures=seed_from_fixtures,
                seed_from_docstrings=seed_from_docstrings,
                seed_from_code=seed_from_code,
                seed_from_call_sites=seed_from_call_sites,
            )
        )
    )
    seeds: list[dict[str, Any]] = [dict(example.kwargs) for example in observed_examples]

    if fixtures and seeds:
        return list(seeds)
    if fixtures:
        return []

    try:
        sig = inspect.signature(target)
    except Exception:
        return seeds
    hints = safe_get_annotations(target)
    source_boundaries = _source_boundary_candidates(target)
    doc_boundaries = _docstring_boundary_candidates(target, hints)

    params = [param for name, param in sig.parameters.items() if name not in {"self", "cls"}]
    if not params:
        return seeds or [{}]

    base_kwargs: dict[str, Any] = {}
    per_param_values: list[tuple[str, list[Any]]] = []
    for param in params:
        values: list[Any] = []
        if param.default is not inspect.Parameter.empty:
            values.append(param.default)
        values.extend(source_boundaries.get(param.name, ()))
        values.extend(doc_boundaries.get(param.name, ()))
        if param.name in hints:
            values.extend(_boundary_values_for_hint(hints[param.name]))
        deduped_values: list[Any] = []
        for value in values:
            if any(existing == value for existing in deduped_values):
                continue
            deduped_values.append(value)
        values = deduped_values
        if not values:
            return seeds
        base_kwargs[param.name] = values[0]
        per_param_values.append((param.name, values))

    cases: list[dict[str, Any]] = list(seeds)
    _append_boundary_case(cases, dict(base_kwargs))
    for name, values in per_param_values:
        for value in values:
            candidate = dict(base_kwargs)
            candidate[name] = value
            _append_boundary_case(cases, candidate)
    return cases


def _candidate_inputs(
    func: Any,
    *,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    mutate_observed_inputs: bool = True,
    mode: ScanMode = "evidence",
    security_focus: bool = False,
    seed_examples: Sequence[SeedExample] | None = None,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
) -> list[CandidateInput]:
    """Return deterministic candidate inputs with provenance metadata."""
    mode = _normalize_scan_mode(mode)
    target = _unwrap(func)
    observed_examples = (
        list(seed_examples)
        if seed_examples is not None
        else list(
            _seed_examples_for_callable(
                target,
                seed_from_tests=seed_from_tests,
                seed_from_fixtures=seed_from_fixtures,
                seed_from_docstrings=seed_from_docstrings,
                seed_from_code=seed_from_code,
                seed_from_call_sites=seed_from_call_sites,
            )
        )
    )
    candidates: list[CandidateInput] = []
    seen: set[str] = set()

    for example in observed_examples:
        key = repr(sorted(example.kwargs.items()))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            CandidateInput(
                kwargs=dict(example.kwargs),
                origin=example.source,
                rationale=(example.evidence,),
            )
        )

    boundary_inputs = _boundary_smoke_inputs(
        target,
        fixtures=fixtures,
        seed_examples=observed_examples,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
    )

    def _append_boundary_inputs() -> None:
        for kwargs in boundary_inputs:
            key = repr(sorted(kwargs.items()))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(CandidateInput(kwargs=dict(kwargs), origin="boundary"))

    def _append_seed_mutations() -> None:
        if not mutate_observed_inputs:
            return
        try:
            from ordeal.mutagen import mutate_inputs

            rng = __import__("random").Random(42)
            for example in list(candidates):
                if example.origin not in {
                    "test",
                    "fixture",
                    "call_site",
                    "docstring",
                    "source_boundary",
                }:
                    continue
                mutated = mutate_inputs(example.kwargs, rng)
                key = repr(sorted(mutated.items()))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    CandidateInput(
                        kwargs=dict(mutated),
                        origin="seed_mutation",
                        rationale=(*(example.rationale), "mutated from observed test input"),
                    )
                )
        except Exception:
            pass

    if mode == "real_bug" and observed_examples:
        _append_seed_mutations()
        _append_boundary_inputs()
    else:
        _append_boundary_inputs()
        _append_seed_mutations()
    if security_focus:
        sink_categories = _infer_sink_categories(target, security_focus=True)
        for candidate in _security_candidate_inputs(target, boundary_inputs, sink_categories):
            key = repr(sorted(candidate.kwargs.items()))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        for candidate in _artifact_mutation_candidate_inputs(
            target,
            boundary_inputs,
            sink_categories,
        ):
            key = repr(sorted(candidate.kwargs.items()))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

    return candidates


def _type_matches(value: Any, expected: type) -> bool:
    """Check if value matches expected type, handling generics and unions."""
    import types as pytypes

    if expected is type(None):
        return value is None
    origin = get_origin(expected)
    if origin is Literal:
        return any(value == option for option in get_args(expected))
    # Union[str, None] or str | None — check each member
    is_union = origin is Union or (
        hasattr(pytypes, "UnionType") and isinstance(expected, pytypes.UnionType)
    )
    if is_union:
        args = get_args(expected)
        return any(_type_matches(value, a) for a in args)
    if origin is not None:
        # list[int] → check isinstance(value, list)
        return isinstance(value, origin)
    try:
        return isinstance(value, expected)
    except TypeError:
        return True  # can't check, assume ok


_DOC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "for",
    "from",
    "have",
    "into",
    "must",
    "same",
    "that",
    "the",
    "this",
    "when",
    "with",
}


def _documented_precondition_failure(
    func: Any,
    exc: Exception,
    kwargs: dict[str, Any],
    *,
    expected_patterns: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Return a detail dict when *exc* matches a documented precondition."""
    doc = inspect.getdoc(func) or ""
    lowered_doc = doc.lower()

    exc_name = type(exc).__name__
    exc_name_lower = exc_name.lower()

    message = str(exc)
    lowered_message = message.lower()
    message_tokens = {
        token
        for token in re.findall(r"[a-z_]{4,}", lowered_message)
        if token not in _DOC_STOPWORDS
    }
    doc_tokens = set(re.findall(r"[a-z_]{4,}", lowered_doc))
    param_names = {name.lower() for name in kwargs}
    explicit_patterns = [str(item).strip().lower() for item in expected_patterns or () if item]

    doc_match = (
        "raise" in lowered_doc
        and exc_name_lower in lowered_doc
        and ((message_tokens & doc_tokens) or (param_names & doc_tokens))
    )
    explicit_match = any(
        pattern == exc_name_lower
        or pattern in lowered_message
        or pattern in lowered_doc
        or pattern in param_names
        for pattern in explicit_patterns
    )

    if not doc_match and not explicit_match:
        return None

    summary = f"expected precondition failure: {exc_name}: {message}"
    return {
        "kind": "precondition",
        "category": "expected_precondition_failure",
        "summary": summary[:300],
        "error": message[:300],
        "error_type": exc_name,
        "failing_args": dict(kwargs),
        "source": "explicit_annotation" if explicit_match and not doc_match else "docstring",
    }


def _call_target_parts(func: Any) -> tuple[str, tuple[str, ...], str]:
    """Return ``(module_name, qualname_parts, leaf_name)`` for *func*."""
    target = _unwrap(func)
    module_name = getattr(target, "__module__", "")
    qualname = getattr(target, "__qualname__", getattr(target, "__name__", ""))
    parts = tuple(part for part in qualname.split(".") if part and part != "<locals>")
    if not parts:
        return module_name, (), getattr(target, "__name__", "")
    return module_name, parts[:-1], parts[-1]


def _semantic_bucket(name: str, hint: Any | None) -> str:
    """Infer a coarse semantic bucket for one parameter."""
    lowered = name.lower()
    if any(token in lowered for token in {"path", "file", "dir", "root"}):
        return "path"
    if any(token in lowered for token in {"cmd", "command", "argv", "shell"}):
        return "shell"
    if any(token in lowered for token in {"module", "plugin", "hook", "entrypoint"}):
        return "import"
    if any(
        token in lowered
        for token in {
            "config",
            "pickle",
            "checkpoint",
            "trace",
            "bundle",
            "artifact",
            "blob",
            "frame",
            "manifest",
            "resume",
            "session",
            "snapshot",
            "state",
            "toml",
        }
    ):
        return "serialized"
    if any(
        token in lowered
        for token in {
            "channel",
            "checkpoint",
            "descriptor",
            "fd",
            "mailbox",
            "pipe",
            "queue",
            "ring",
            "segment",
            "shared_memory",
            "shm",
            "sock",
            "topic",
        }
    ):
        return "ipc"
    if "link" in lowered or "symlink" in lowered:
        return "symlink"
    if lowered in {"env", "environ", "headers"} or lowered.endswith("_env"):
        return "mapping"
    if any(token in lowered for token in {"json", "payload", "body", "tool_call"}):
        return "json"
    if any(token in lowered for token in {"message", "response", "request"}):
        return "message"
    if any(token in lowered for token in {"timeout", "count", "size", "port", "index", "status"}):
        return "numeric"
    if hint in {int, float}:
        return "numeric"
    if hint in {dict, list, tuple, set, frozenset}:
        return "collection"
    return "generic"


def _semantic_value_score(bucket: str, value: Any) -> float:
    """Score whether *value* fits a coarse semantic bucket."""
    match bucket:
        case "path":
            return 1.0 if isinstance(value, (str, os.PathLike)) else 0.0
        case "shell":
            return (
                1.0
                if isinstance(value, str)
                or (
                    isinstance(value, (list, tuple))
                    and all(isinstance(item, (str, os.PathLike)) for item in value)
                )
                else 0.0
            )
        case "mapping" | "json":
            return 1.0 if isinstance(value, Mapping) else 0.0
        case "import":
            return (
                1.0 if isinstance(value, str) and ("." in value or value.isidentifier()) else 0.0
            )
        case "serialized":
            return 1.0 if isinstance(value, (bytes, bytearray, memoryview, str, Mapping)) else 0.0
        case "ipc":
            return 1.0 if isinstance(value, (str, bytes, bytearray, memoryview, Mapping)) else 0.0
        case "symlink":
            return 1.0 if isinstance(value, (str, os.PathLike)) else 0.0
        case "message":
            return 1.0 if isinstance(value, (str, Mapping, list, tuple)) else 0.0
        case "numeric":
            return 1.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        case "collection":
            return 1.0 if isinstance(value, (dict, list, tuple, set, frozenset)) else 0.0
        case _:
            return 0.5


def _callable_looks_like_security_shaper(func: Any) -> bool:
    """Return whether *func* looks like a pure shaper rather than a side-effect sink."""
    target = _unwrap(func)
    name = str(getattr(target, "__name__", "")).lower()
    source = ""
    with contextlib.suppress(OSError, TypeError):
        source = inspect.getsource(target).lower()
    tokens = set(re.findall(r"[a-z_]{3,}", f"{name} {source}"))
    if tokens & _SECURITY_SIDE_EFFECT_TOKENS or any(
        token in source for token in _SECURITY_SIDE_EFFECT_TOKENS
    ):
        return False
    return bool(tokens & _SECURITY_SHAPER_TOKENS)


def _security_candidate_inputs(
    func: Any,
    boundary_inputs: Sequence[Mapping[str, Any]],
    sink_categories: Sequence[str],
) -> list[CandidateInput]:
    """Build deterministic, low-side-effect security probes for shaper callables."""
    source_backed_sinks = _source_backed_sink_categories(func, security_focus=True)
    safe_probe_sinks = {"path", "symlink"}
    if (
        not boundary_inputs
        or not _callable_looks_like_security_shaper(func)
        or not set(source_backed_sinks)
        or not set(source_backed_sinks).issubset(safe_probe_sinks)
    ):
        return []
    try:
        sig = inspect.signature(_unwrap(func))
    except Exception:
        return []

    base_kwargs = dict(boundary_inputs[0])
    candidates: list[CandidateInput] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if name not in base_kwargs and param.default is inspect.Signature.empty:
            continue
        bucket = _semantic_bucket(name, safe_get_annotations(_unwrap(func)).get(name))
        if not _semantic_bucket_targets_sink(bucket, source_backed_sinks):
            continue
        probes = _SECURITY_PROBE_VALUES.get(bucket, ())
        for probe in probes:
            kwargs = dict(base_kwargs)
            kwargs[name] = probe
            candidates.append(
                CandidateInput(
                    kwargs=kwargs,
                    origin="security_probe",
                    rationale=(f"security probe for {bucket} trust-boundary handling",),
                )
            )
    return candidates


def _security_base_kwargs(func: Any) -> dict[str, Any] | None:
    """Return one conservative kwargs mapping for deterministic security probes."""
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except Exception:
        return None
    hints = safe_get_annotations(target)
    source_boundaries = _source_boundary_candidates(target)
    doc_boundaries = _docstring_boundary_candidates(target, hints)

    def _fallback_value(name: str, hint: Any | None) -> Any:
        values = list(source_boundaries.get(name, ())) or list(doc_boundaries.get(name, ()))
        if hint is not None:
            values.extend(_boundary_values_for_hint(hint))
        if values:
            return values[0]
        bucket = _semantic_bucket(name, hint)
        if hint in _BOUNDARY_SMOKE_VALUES:
            return _BOUNDARY_SMOKE_VALUES[hint][0]
        match bucket:
            case "path" | "symlink":
                return "artifact.txt"
            case "shell":
                return "echo ordeal"
            case "import":
                return "json"
            case "serialized":
                if hint is dict or get_origin(hint) is dict:
                    return {"checkpoint": "seed-1"}
                if hint in {bytes, bytearray, memoryview}:
                    return b"{}"
                return "{}"
            case "ipc":
                if hint is dict or get_origin(hint) is dict:
                    return {"channel": "ordeal-base"}
                if hint in {bytes, bytearray, memoryview}:
                    return b"ordeal-base"
                return "ordeal-base"
            case "mapping" | "json":
                return {}
            case "message" | "generic":
                return "ok"
            case "numeric":
                return 1
            case "collection":
                return {}
            case _:
                return "ok"

    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if param.default is not inspect.Signature.empty:
            kwargs[name] = param.default
            continue
        kwargs[name] = _fallback_value(name, hints.get(name))
    return kwargs


def _artifact_mutation_probe_values(
    *,
    bucket: str,
    name: str,
    hint: Any | None,
    current_value: Any,
) -> tuple[Any, ...]:
    """Return deterministic artifact/config mutations for one semantic bucket."""
    lowered = name.lower()
    if bucket == "import":
        return _SECURITY_ARTIFACT_MUTATION_VALUES["import_text"]
    if bucket == "serialized":
        if isinstance(current_value, Mapping) or hint is dict or get_origin(hint) is dict:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_mapping"]
        if isinstance(current_value, (bytes, bytearray, memoryview)) or hint in {
            bytes,
            bytearray,
            memoryview,
        }:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_bytes"]
        if any(token in lowered for token in {"config", "manifest", "settings", "toml"}):
            return (
                _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_text"][1],
                *_SECURITY_ARTIFACT_MUTATION_VALUES["serialized_mapping"],
            )
        return _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_text"]
    if bucket == "json":
        if isinstance(current_value, Mapping) or hint is dict or get_origin(hint) is dict:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["json_mapping"]
        return _SECURITY_ARTIFACT_MUTATION_VALUES["json_text"]
    if bucket == "ipc":
        if isinstance(current_value, Mapping) or hint is dict or get_origin(hint) is dict:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["ipc_mapping"]
        if isinstance(current_value, (bytes, bytearray, memoryview)) or hint in {
            bytes,
            bytearray,
            memoryview,
        }:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["ipc_bytes"]
        return _SECURITY_ARTIFACT_MUTATION_VALUES["ipc_text"]
    return ()


def _artifact_mutation_candidate_inputs(
    func: Any,
    boundary_inputs: Sequence[Mapping[str, Any]],
    sink_categories: Sequence[str],
) -> list[CandidateInput]:
    """Build deterministic artifact/config mutation candidates for risky data sinks."""
    source_backed_sinks = _source_backed_sink_categories(func, security_focus=True)
    if not ({"deserialization", "ipc", "import"} & set(source_backed_sinks)):
        return []
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except Exception:
        return []
    hints = safe_get_annotations(target)
    base_kwargs = (
        dict(boundary_inputs[0]) if boundary_inputs else (_security_base_kwargs(target) or {})
    )
    if not base_kwargs:
        return []

    candidates: list[CandidateInput] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if name not in base_kwargs and param.default is inspect.Signature.empty:
            continue
        hint = hints.get(name)
        bucket = _semantic_bucket(name, hint)
        if not _semantic_bucket_targets_sink(bucket, source_backed_sinks):
            continue
        for probe in _artifact_mutation_probe_values(
            bucket=bucket,
            name=name,
            hint=hint,
            current_value=base_kwargs.get(name),
        ):
            kwargs = dict(base_kwargs)
            kwargs[name] = probe
            candidates.append(
                CandidateInput(
                    kwargs=kwargs,
                    origin="artifact_mutation",
                    rationale=(f"artifact/config mutation for {bucket} trust-boundary handling",),
                )
            )
    return candidates


def _likely_contract_profile(
    func: Any,
    *,
    security_focus: bool = False,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
) -> dict[str, Any]:
    """Infer a weak contract profile from hints, docs, and observed seeds."""
    target = _unwrap(func)
    module_name, qual_parts, leaf_name = _call_target_parts(target)
    qualname = ".".join([*qual_parts, leaf_name]) if qual_parts else leaf_name
    hints = safe_get_annotations(target)
    doc = (inspect.getdoc(target) or "").lower()

    observed = tuple(
        _seed_examples_for_callable(
            target,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
        )
    )
    observed_types: dict[str, set[str]] = {}
    for example in observed:
        for name, value in example.kwargs.items():
            observed_types.setdefault(name, set()).add(type(value).__name__)

    comparisons = _source_boundary_candidates(target)
    profile_params: dict[str, dict[str, Any]] = {}
    for name in inspect.signature(target).parameters:
        if name in {"self", "cls"}:
            continue
        hint = hints.get(name)
        profile_params[name] = {
            "hint": hint,
            "weak_hint": (hint in {Any, object, None}) if treat_any_as_weak else False,
            "semantic": _semantic_bucket(name, hint),
            "observed_types": sorted(observed_types.get(name, set())),
            "comparison_values": list(comparisons.get(name, [])),
            "doc_mentions": int(name.lower() in doc),
        }

    return {
        "module": module_name,
        "qualname": qualname,
        "leaf_name": leaf_name,
        "params": profile_params,
        "seed_examples": list(observed),
        "treat_any_as_weak": treat_any_as_weak,
        "sink_categories": _infer_sink_categories(target, security_focus=security_focus),
        "security_focus": bool(security_focus),
    }


def _score_contract_fit(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> tuple[float, float, float, list[str]]:
    """Score how well a concrete input matches the inferred contract."""
    params = profile.get("params", {})
    if not kwargs:
        return 1.0, 1.0, 0.0, ["zero-arg callable"]

    fit_scores: list[float] = []
    realism_scores: list[float] = []
    sink_scores: list[float] = []
    reasons: list[str] = []
    seed_examples = list(profile.get("seed_examples", []))
    treat_any_as_weak = bool(profile.get("treat_any_as_weak", True))
    sink_categories = list(profile.get("sink_categories", ()))

    for name, value in kwargs.items():
        meta = params.get(name, {})
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        semantic = str(meta.get("semantic", "generic"))
        observed_types = list(meta.get("observed_types", []))
        comparison_values = list(meta.get("comparison_values", []))

        score = 0.0
        if hint is not None and hint is not Any and not weak_hint:
            if _type_matches(value, hint):
                score += 0.55
                reasons.append(f"{name}: matches type hint")
                if get_origin(hint) is Literal:
                    score += 0.1
                    reasons.append(f"{name}: matches a constrained Literal contract")
            else:
                score -= 0.35
                reasons.append(f"{name}: mismatches type hint")
        elif weak_hint and treat_any_as_weak:
            score += _WEAK_CONTRACT_FIT
            reasons.append(f"{name}: broad or missing type hint")

        if observed_types:
            if type(value).__name__ in observed_types:
                score += 0.25
                reasons.append(f"{name}: matches observed test shape")
            else:
                score -= 0.1
        if comparison_values and value in comparison_values:
            score += 0.1
            reasons.append(f"{name}: reaches boundary mined from code")
        if meta.get("doc_mentions"):
            score += 0.05

        semantic_score = _semantic_value_score(semantic, value)
        if hint is not None and not weak_hint and _type_matches(value, hint):
            semantic_score = max(
                semantic_score,
                0.75 if get_origin(hint) is Literal else 0.6,
            )
        realism_scores.append(semantic_score)
        sink_scores.append(_sink_signal_for_bucket(semantic, sink_categories))
        score += (semantic_score - 0.5) * 0.4
        fit_scores.append(min(max(score, 0.0), 1.0))

    contract_fit = sum(fit_scores) / len(fit_scores)
    if any(getattr(example, "kwargs", None) == dict(kwargs) for example in seed_examples):
        contract_fit = min(contract_fit + 0.15, 1.0)
        reasons.append("matches a concrete seed from tests/docs/code")
    realism = sum(realism_scores) / len(realism_scores) if realism_scores else 0.0
    sink_signal = max(sink_scores, default=0.0)
    return contract_fit, realism, sink_signal, reasons[:6]


def _looks_like_declared_contract_robustness(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    realism: float,
    reachability: float,
) -> bool:
    """Return whether a witness is near the declared contract but still outside it."""
    params = profile.get("params", {})
    strong_mismatch = False
    strong_match = False
    observed_shape = False
    for name, value in kwargs.items():
        meta = params.get(name, {})
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        if hint is not None and hint is not Any and not weak_hint:
            if _type_matches(value, hint):
                strong_match = True
            else:
                strong_mismatch = True
        if type(value).__name__ in list(meta.get("observed_types", [])):
            observed_shape = True
        if value in list(meta.get("comparison_values", [])):
            observed_shape = True
    return strong_mismatch and (
        strong_match or observed_shape or realism >= 0.55 or reachability >= 0.75
    )


def _aligned_security_sinks(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> list[str]:
    """Return sink categories supported by both the callable and the concrete input."""
    params = profile.get("params", {})
    sink_categories = {str(item) for item in profile.get("sink_categories", ())}
    aligned: set[str] = set()
    for name, value in kwargs.items():
        meta = params.get(name, {})
        bucket = str(meta.get("semantic", "generic"))
        if _semantic_value_score(bucket, value) < 0.6:
            continue
        aligned.update(
            sink for sink in _SECURITY_BUCKET_TO_SINKS.get(bucket, ()) if sink in sink_categories
        )
    return sorted(aligned, key=lambda item: (-_SECURITY_SINK_WEIGHTS.get(item, 0.0), item))


def _reachability_score(
    origin: str | None,
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> float:
    """Score whether the failing input came from a reachable, realistic source."""
    base = {
        "test": 1.0,
        "fixture": 0.95,
        "call_site": 0.85,
        "docstring": 0.75,
        "source_boundary": 0.75,
        "pytest_seed": 1.0,
        "seed_mutation": 0.8,
        "artifact_mutation": 0.8,
        "security_probe": 0.7,
        "boundary": 0.7,
        "random_fuzz": 0.45,
    }.get(origin or "", 0.4)
    params = profile.get("params", {})
    if any(
        value in list(params.get(name, {}).get("comparison_values", []))
        for name, value in kwargs.items()
    ):
        base = max(base, 0.75)
    return min(base, 1.0)


def _classify_crash(
    *,
    mode: ScanMode,
    replayable: bool,
    contract_fit: float,
    reachability: float,
    realism: float,
    robustness_case: bool,
    min_contract_fit: float,
    min_reachability: float,
    min_realism: float,
    require_replayable: bool = True,
) -> str:
    """Classify a crash for reporting and promotion."""
    if require_replayable and not replayable:
        return "speculative_crash"
    if not replayable:
        return "speculative_crash"
    if (
        contract_fit >= min_contract_fit
        and reachability >= min_reachability
        and realism >= min_realism
    ):
        return "likely_bug"
    if robustness_case:
        return "beyond_declared_contract_robustness"
    if contract_fit <= _WEAK_CONTRACT_FIT or realism < 0.35:
        return "invalid_input_crash"
    return "coverage_gap" if mode == "coverage_gap" else "speculative_crash"


def _verdict_for_crash(category: str) -> str:
    """Map one crash category to the coarse scan verdict bucket."""
    return {
        "likely_bug": "promoted_real_bug",
        "coverage_gap": "coverage_gap",
        "speculative_crash": "exploratory_crash",
        "invalid_input_crash": "invalid_input_crash",
        "beyond_declared_contract_robustness": "beyond_declared_contract_robustness",
    }.get(category, "exploratory_crash")


def _likely_impact(category: str, sink_signal: float) -> str:
    """Describe likely impact for a crash report."""
    if sink_signal >= 1.0:
        return "reaches a path/shell/json/env shaping sink with a contract-valid input."
    if category == "coverage_gap":
        return "the input looks partially valid, but current evidence points to missing coverage."
    if category == "beyond_declared_contract_robustness":
        return "the failure sits just beyond the declared contract and is best read as robustness."
    if category == "invalid_input_crash":
        return "the crash currently looks driven by out-of-contract input rather than a bug."
    return "the function crashes on an input that matches the inferred contract."


def _proof_target_qualname(qualname: str, profile: Mapping[str, Any]) -> str:
    """Return the fully qualified target name for a proof bundle."""
    module_name = str(profile.get("module") or "").strip()
    if not module_name or qualname.startswith(f"{module_name}."):
        return qualname
    return f"{module_name}.{qualname}"


def _proof_supporting_evidence(
    failing_args: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return structured contract evidence for the failing witness."""
    params = profile.get("params", {})
    evidence: list[dict[str, Any]] = []
    for name, value in failing_args.items():
        meta = params.get(name, {})
        parameter_evidence: list[dict[str, Any]] = []
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        observed_types = list(meta.get("observed_types", []))
        comparison_values = list(meta.get("comparison_values", []))
        semantic = str(meta.get("semantic", "generic"))
        if hint is not None and not weak_hint:
            parameter_evidence.append(
                {
                    "kind": "type_hint",
                    "detail": _json_ready_proof(hint),
                }
            )
        elif weak_hint:
            parameter_evidence.append(
                {
                    "kind": "weak_type_hint",
                    "detail": _json_ready_proof(hint),
                }
            )
        if observed_types:
            parameter_evidence.append(
                {
                    "kind": "observed_types",
                    "detail": list(observed_types),
                    "matched": type(value).__name__ in observed_types,
                }
            )
        if comparison_values and value in comparison_values:
            parameter_evidence.append(
                {
                    "kind": "boundary_value",
                    "detail": _json_ready_proof(value),
                }
            )
        if meta.get("doc_mentions"):
            parameter_evidence.append(
                {
                    "kind": "docstring",
                    "detail": f"{name} is mentioned in the callable docstring",
                }
            )
        if semantic != "generic":
            parameter_evidence.append(
                {
                    "kind": "semantic_shape",
                    "detail": semantic,
                }
            )
        evidence.append(
            {
                "parameter": name,
                "value": _json_ready_proof(value),
                "value_type": type(value).__name__,
                "checks": parameter_evidence,
            }
        )
    return evidence


def _profile_fixture_completeness(profile: Mapping[str, Any]) -> float:
    """Estimate how well the inferred profile covers the callable inputs."""
    params = profile.get("params", {})
    if not params:
        return 1.0
    scores: list[float] = []
    for meta in params.values():
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        has_runtime_evidence = bool(
            meta.get("observed_types") or meta.get("comparison_values") or meta.get("doc_mentions")
        )
        has_strong_hint = hint is not None and hint is not Any and not weak_hint
        if has_runtime_evidence:
            scores.append(1.0)
        elif has_strong_hint:
            scores.append(0.6)
        elif weak_hint:
            scores.append(0.2)
        else:
            scores.append(0.0)
    completeness = sum(scores) / len(scores)
    if any(
        getattr(example, "source", None) in {"test", "fixture", "pytest_seed", "call_site"}
        for example in profile.get("seed_examples", [])
    ):
        completeness = min(completeness + 0.1, 1.0)
    return completeness


def _bootstrap_failure_demotion(
    func: Any,
    *,
    category: str,
    call_context: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    """Demote mined harness bootstrap failures before they become promoted findings."""
    if category in {"invalid_input_crash", "beyond_declared_contract_robustness"}:
        return category, None
    if not getattr(func, "__ordeal_auto_harness__", False):
        return category, None
    if getattr(func, "__ordeal_harness_verified__", True):
        return category, None
    call_stage = str(
        (call_context or {}).get("failure_stage") or (call_context or {}).get("call_stage") or ""
    ).strip()
    if call_stage in {"", "invoke", "teardown"}:
        return category, None
    dry_run_error = str(getattr(func, "__ordeal_harness_dry_run_error__", "") or "").strip()
    reason = dry_run_error or (
        "auto-mined object harness needs a successful dry-run factory invocation before "
        "bound-method crashes can promote"
    )
    return "coverage_gap", reason


def _security_focus_demotion(
    *,
    category: str,
    mode: ScanMode,
    security_focus: bool,
    input_source: str | None,
    replayable: bool,
    fixture_completeness: float,
    aligned_sink_categories: Sequence[str],
) -> tuple[str, str | None]:
    """Apply stricter candidate-mode security-focus promotion gates."""
    if category != "likely_bug":
        return category, None
    if input_source == "artifact_mutation" and (not replayable or not aligned_sink_categories):
        return (
            "coverage_gap",
            "artifact mutation probes stay exploratory until they produce a "
            "replayable, sink-aligned failure",
        )
    if security_focus and mode == "real_bug":
        if fixture_completeness < _SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS:
            return (
                "coverage_gap",
                "fixture completeness stayed below the security-focus promotion bar "
                f"({fixture_completeness:.0%} < "
                f"{_SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS:.0%})",
            )
    return category, None


def _proof_demotion_reason(
    *,
    category: str,
    replayable: bool,
    contract_fit: float,
    reachability: float,
    realism: float,
    fixture_completeness: float | None = None,
    min_contract_fit: float,
    min_reachability: float,
    min_realism: float,
    min_fixture_completeness: float | None = None,
    forced_reason: str | None = None,
) -> str | None:
    """Return the concrete reason a finding was not promoted as a candidate issue."""
    if forced_reason:
        return forced_reason
    if category in {"likely_bug", "lifecycle_contract"}:
        return None
    if category == "expected_precondition_failure":
        return "the raised exception matches a documented precondition instead of a bug."
    reasons: list[str] = []
    if not replayable:
        reasons.append("replay did not confirm the failure")
    if contract_fit < min_contract_fit:
        reasons.append(
            "contract fit stayed below the promotion bar "
            f"({contract_fit:.0%} < {min_contract_fit:.0%})"
        )
    if reachability < min_reachability:
        reasons.append(
            "the witness did not come from a strong reachable seed "
            f"({reachability:.0%} < {min_reachability:.0%})"
        )
    if realism < min_realism:
        reasons.append(
            f"the input realism stayed below the promotion bar ({realism:.0%} < {min_realism:.0%})"
        )
    if (
        min_fixture_completeness is not None
        and fixture_completeness is not None
        and fixture_completeness < min_fixture_completeness
    ):
        reasons.append(
            "fixture completeness stayed below the promotion bar "
            f"({fixture_completeness:.0%} < {min_fixture_completeness:.0%})"
        )
    if category == "invalid_input_crash":
        reasons.append("the crash still looks driven by out-of-contract input")
    elif category == "beyond_declared_contract_robustness":
        reasons.append(
            "the witness sits beyond the declared contract, so treat this as robustness"
        )
    elif category == "coverage_gap":
        reasons.append("current evidence points to missing coverage more than a defect")
    elif category == "speculative_crash" and not replayable:
        reasons.append("the crash remains exploratory because it is not replayable")
    return "; ".join(dict.fromkeys(reasons)) or None


def _proof_minimal_reproduction(
    *,
    qualname: str,
    failing_args: Mapping[str, Any],
    profile: Mapping[str, Any],
    harness_mode: str | None,
    callable_kind: str | None,
    contract_check: str | None = None,
    security_focus: bool = False,
) -> dict[str, Any]:
    """Build a deterministic reproduction payload for reports and JSON bundles."""
    module_name = str(profile.get("module") or "").strip()
    explicit_target = f"{module_name}:{qualname}" if module_name else qualname
    direct_call_supported = callable_kind != "instance"
    snippet_lines = [
        "from importlib import import_module",
        f"mod = import_module({module_name!r})" if module_name else "mod = None",
        f"args = {pformat(_json_ready_proof(dict(failing_args)), width=88, sort_dicts=False)}",
    ]
    if direct_call_supported and module_name:
        expr = "mod"
        for part in [part for part in qualname.split(".") if part]:
            expr = f"{expr}.{part}"
        snippet_lines.append(f"{expr}(**args)")
    else:
        snippet_lines.append(
            "# This target requires the configured object harness before invoking the method."
        )
    if contract_check is not None:
        command = f"uv run ordeal check {explicit_target} --contract {contract_check}"
    elif module_name:
        command = (
            f"uv run ordeal scan {module_name} --mode candidate "
            f"{'--security-focus ' if security_focus else ''}"
            f"--targets {explicit_target} -n 1"
        )
    else:
        command = None
    note = None
    if not direct_call_supported:
        note = (
            "Bound instance method: replay requires the configured object factory/setup/scenario "
            f"(harness={harness_mode or 'fresh'})."
        )
    return {
        "target": explicit_target,
        "command": command,
        "python_snippet": "\n".join(snippet_lines),
        "direct_call_supported": direct_call_supported,
        "note": note,
    }


def _build_proof_bundle(
    *,
    qualname: str,
    error: Exception | None,
    failing_args: Mapping[str, Any],
    input_source: str | None,
    contract_fit: float,
    reachability: float,
    realism: float,
    rationale: Sequence[str],
    replayable: bool,
    replay_attempts: int,
    replay_matches: int,
    category: str,
    profile: Mapping[str, Any],
    sink_signal: float,
    sink_categories: Sequence[str] = (),
    aligned_sink_categories: Sequence[str] | None = None,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    min_fixture_completeness: float | None = None,
    harness_mode: str | None = None,
    callable_kind: str | None = None,
    contract_check: str | None = None,
    security_focus: bool = False,
    forced_demotion_reason: str | None = None,
    promoted: bool | None = None,
) -> dict[str, Any]:
    """Build the proof payload carried through reports and agent output."""
    evidence_class = (
        "candidate_issue"
        if category == "likely_bug"
        else "expected_precondition"
        if category == "expected_precondition_failure"
        else category
    )
    full_qualname = _proof_target_qualname(qualname, profile)
    matched_sources = [
        {
            "source": example.source,
            "evidence": example.evidence,
        }
        for example in profile.get("seed_examples", [])
        if getattr(example, "kwargs", None) == dict(failing_args)
    ]
    supporting_evidence = _proof_supporting_evidence(failing_args, profile)
    fixture_completeness = _profile_fixture_completeness(profile)
    replayability_score = (
        replay_matches / replay_attempts if replay_attempts > 0 else (1.0 if replayable else 0.0)
    )
    callable_sink_categories = [str(item) for item in sink_categories]
    impact_sink_categories = [str(item) for item in list(aligned_sink_categories or ())]
    demotion_reason = _proof_demotion_reason(
        category=category,
        replayable=replayable,
        contract_fit=contract_fit,
        reachability=reachability,
        realism=realism,
        fixture_completeness=fixture_completeness,
        min_contract_fit=min_contract_fit,
        min_reachability=min_reachability,
        min_realism=min_realism,
        min_fixture_completeness=min_fixture_completeness,
        forced_reason=forced_demotion_reason,
    )
    witness = {
        "input": _json_ready_proof(dict(failing_args)),
        "source": input_source,
        "seed_sources": matched_sources,
        "supporting_evidence": supporting_evidence,
    }
    contract_basis = {
        "category": category,
        "evidence_class": evidence_class,
        "fit": round(contract_fit, 4),
        "reachability": round(reachability, 4),
        "realism": round(realism, 4),
        "fixture_completeness": round(fixture_completeness, 4),
        "basis": list(rationale),
        "likely_contract": _json_ready_proof(profile.get("params", {})),
        "supporting_evidence": supporting_evidence,
        "input_source": input_source,
        "matched_seed_sources": matched_sources,
        "security_focus": bool(security_focus),
        "sink_categories": list(impact_sink_categories),
        "callable_sink_categories": list(callable_sink_categories),
        "critical_sinks": _critical_security_sinks(impact_sink_categories),
    }
    failure_path = {
        "target": full_qualname,
        "qualname": full_qualname,
    }
    if error is not None:
        failure_path["error_type"] = type(error).__name__
        failure_path["error"] = str(error)[:300]
        failure_path["traceback"] = _traceback_path(error)
    if contract_check is not None:
        failure_path["contract_check"] = contract_check
    impact_summary = (
        _sink_likely_impact(impact_sink_categories, error)
        if impact_sink_categories and error is not None
        else _likely_impact(category, sink_signal)
    )
    minimal_reproduction = _proof_minimal_reproduction(
        qualname=qualname,
        failing_args=failing_args,
        profile=profile,
        harness_mode=harness_mode,
        callable_kind=callable_kind,
        contract_check=contract_check,
        security_focus=security_focus,
    )
    critical_sinks = _critical_security_sinks(impact_sink_categories)
    promoted_verdict = (
        bool(promoted)
        if promoted is not None
        else category in {"likely_bug", "semantic_contract", "lifecycle_contract"}
    )
    return {
        "version": 2,
        "witness": witness,
        "valid_input_witness": {
            **witness,
            "contract_fit": round(contract_fit, 4),
            "reachability": round(reachability, 4),
            "realism": round(realism, 4),
            "rationale": list(rationale),
        },
        "contract_basis": contract_basis,
        "contract_validity": {
            "category": category,
            "evidence_class": evidence_class,
            "likely_contract": _json_ready_proof(profile.get("params", {})),
            "rationale": list(rationale),
            "supporting_evidence": supporting_evidence,
        },
        "confidence_breakdown": {
            "replayability": round(replayability_score, 4),
            "contract_fit": round(contract_fit, 4),
            "reachability": round(reachability, 4),
            "realism": round(realism, 4),
            "fixture_completeness": round(fixture_completeness, 4),
            "sink_signal": round(sink_signal, 4),
            "replay_attempts": replay_attempts,
            "replay_matches": replay_matches,
        },
        "failure_path": failure_path,
        "failing_path": failure_path,
        "minimal_reproduction": minimal_reproduction,
        "reproduction": {
            "replayable": replayable,
            "replay_attempts": replay_attempts,
            "replay_matches": replay_matches,
            "failing_args": _json_ready_proof(dict(failing_args)),
            **minimal_reproduction,
        },
        "impact": {
            "summary": impact_summary,
            "class": (
                "lifecycle"
                if category == "lifecycle_contract"
                else (list(impact_sink_categories)[0] if impact_sink_categories else category)
            ),
            "evidence_class": evidence_class,
            "sink_categories": list(impact_sink_categories),
            "callable_sink_categories": list(callable_sink_categories),
            "critical_sinks": critical_sinks,
            "trust_boundary_signal": round(sink_signal, 4),
            "security_focus": bool(security_focus),
        },
        "sink_categories": list(impact_sink_categories),
        "callable_sink_categories": list(callable_sink_categories),
        "likely_impact": impact_summary,
        "verdict": {
            "category": category,
            "evidence_class": evidence_class,
            "promoted": promoted_verdict,
            "demotion_reason": demotion_reason,
        },
    }


@contextlib.contextmanager
def _temporary_callable_attr(func: Any, name: str, value: Any) -> Any:
    """Temporarily set one attribute on *func* for a contract execution."""
    marker = object()
    previous = getattr(func, name, marker)
    setattr(func, name, value)
    try:
        yield
    finally:
        if previous is marker:
            with contextlib.suppress(AttributeError):
                delattr(func, name)
        else:
            setattr(func, name, previous)


def _lifecycle_contract_probe(func: Any, check: ContractCheck) -> Callable[..., Any] | None:
    """Build an instance probe that injects lifecycle faults for *check*."""
    metadata = dict(check.metadata)
    if metadata.get("kind") != "lifecycle":
        return None
    if getattr(func, "__ordeal_kind__", None) != "instance":
        return None

    phase = str(
        metadata.get("phase") or getattr(func, "__ordeal_lifecycle_phase__", None) or "cleanup"
    )
    fault = str(metadata.get("fault", "raise") or "raise")
    configured_handler = metadata.get("handler_name")
    followup_phases = [
        str(item) for item in list(metadata.get("followup_phases", []) or []) if str(item).strip()
    ]
    runtime_faults = [
        str(item) for item in list(metadata.get("runtime_faults", []) or []) if str(item).strip()
    ]

    def probe(*, instance: Any, owner: type | None, method_name: str) -> Any:
        target_handlers = _discover_lifecycle_handlers(instance, phase)
        if method_name in target_handlers and len(target_handlers) > 1:
            target_handlers = [name for name in target_handlers if name != method_name]
        followup_handlers = {
            item: _discover_lifecycle_handlers(instance, item) for item in followup_phases
        }
        combined = list(dict.fromkeys([*target_handlers, *sum(followup_handlers.values(), [])]))
        if not combined:
            return None, {
                "lifecycle_probe": {
                    "phase": phase,
                    "fault": fault,
                    "owner": getattr(owner, "__qualname__", None),
                    "method_name": method_name,
                    "target_handlers": [],
                    "followup_handlers": followup_handlers,
                    "attempts": [],
                    "injected_handler": None,
                    "runtime_faults": runtime_faults,
                }
            }

        attempts: list[str] = []
        patched: list[tuple[str, Any]] = []
        inject_via_probe = not runtime_faults
        injected_handler = (
            (
                str(configured_handler)
                if configured_handler and str(configured_handler) in combined
                else combined[0]
            )
            if inject_via_probe
            else None
        )

        def _make_wrapper(bound: Any, current_name: str, *, inject: bool) -> Any:
            is_async = inspect.iscoroutinefunction(getattr(bound, "__func__", bound))
            if is_async:

                @functools.wraps(bound)
                async def wrapped(*args: Any, **kwargs: Any) -> Any:
                    attempts.append(current_name)
                    if inject:
                        raise _lifecycle_fault_exception(fault)
                    result = bound(*args, **kwargs)
                    if inspect.isawaitable(result):
                        return await result
                    return result
            else:

                @functools.wraps(bound)
                def wrapped(*args: Any, **kwargs: Any) -> Any:
                    attempts.append(current_name)
                    if inject:
                        raise _lifecycle_fault_exception(fault)
                    return _call_sync(bound, *args, **kwargs)

            return wrapped

        for current_name in combined:
            bound = getattr(instance, current_name, None)
            if bound is None or not callable(bound):
                continue
            patched.append((current_name, bound))
            setattr(
                instance,
                current_name,
                _make_wrapper(bound, current_name, inject=current_name == injected_handler),
            )

        def cleanup() -> None:
            for current_name, bound in reversed(patched):
                setattr(instance, current_name, bound)

        return cleanup, {
            "lifecycle_probe": {
                "phase": phase,
                "fault": fault,
                "owner": getattr(owner, "__qualname__", None),
                "method_name": method_name,
                "target_handlers": list(target_handlers),
                "followup_handlers": {
                    key: list(value) for key, value in followup_handlers.items()
                },
                "attempts": attempts,
                "injected_handler": injected_handler,
                "runtime_faults": runtime_faults,
            }
        }

    return probe


def _call_contract_predicate(
    predicate: Callable[..., Any],
    value: Any,
    *,
    func: Any,
    call_context: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
    error: BaseException | None = None,
) -> bool:
    """Call a contract predicate with optional lifecycle-aware context."""
    supported = {
        "value": value,
        "result": value,
        "func": func,
        "kwargs": dict(kwargs),
        "error": error,
        "exception": error,
    }
    if call_context:
        supported.update(
            {
                "instance": call_context.get("instance"),
                "before_state": call_context.get("before_state"),
                "after_state": call_context.get("after_state"),
                "args": call_context.get("args"),
                "method_name": call_context.get("method_name"),
                "owner": call_context.get("owner"),
                "harness": call_context.get("harness"),
                "lifecycle_phase": call_context.get("lifecycle_phase"),
                "lifecycle_probe": call_context.get("lifecycle_probe"),
                "teardown_called": call_context.get("teardown_called"),
                "teardown_error": call_context.get("teardown_error"),
                "lifecycle_runtime": call_context.get("lifecycle_runtime"),
            }
        )

    try:
        signature = inspect.signature(predicate)
    except (TypeError, ValueError):
        return bool(predicate(value))

    kwargs_to_pass: dict[str, Any] = {}
    has_var_keywords = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
    )
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if name in supported:
            kwargs_to_pass[name] = supported[name]

    if has_var_keywords:
        for name, item in supported.items():
            kwargs_to_pass.setdefault(name, item)

    if kwargs_to_pass:
        return bool(predicate(**kwargs_to_pass))
    return bool(predicate(value))


def _resolve_contract_check_entries(
    checks: Sequence[Any] | None,
    *,
    probe_kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
    protected_keys: Sequence[str] | None = None,
    env_param: str | None = None,
    phase: str | None = None,
    followup_phases: Sequence[str] | None = None,
    fault: str = "raise",
    handler_name: str | None = None,
) -> list[ContractCheck]:
    """Resolve contract check objects, names, and named packs."""
    resolved: list[ContractCheck] = []
    resolved_env_param = env_param or next(
        (name for name, value in probe_kwargs.items() if isinstance(value, Mapping)),
        None,
    )
    resolved_protected_keys = list(protected_keys or [])
    if not resolved_protected_keys and resolved_env_param is not None:
        env_value = probe_kwargs.get(resolved_env_param)
        if isinstance(env_value, Mapping):
            resolved_protected_keys = [
                key for key in ("PATH", "HOME", "PWD", "TMPDIR") if key in env_value
            ]
    for raw in checks or ():
        if isinstance(raw, ContractCheck):
            resolved.append(raw)
            continue
        if isinstance(raw, Mapping):
            raw_name = str(
                raw.get("name") or raw.get("pack") or raw.get("contract") or raw.get("check") or ""
            ).strip()
            if not raw_name:
                raise ValueError("contract check spec needs a name or pack")
            merged_kwargs = dict(probe_kwargs)
            merged_kwargs.update(dict(raw.get("kwargs") or {}))
            for concrete_name in _expand_contract_names_ordered([raw_name]):
                resolved.append(
                    builtin_contract_check(
                        concrete_name,
                        kwargs=merged_kwargs,
                        tracked_params=tracked_params,
                        protected_keys=resolved_protected_keys,
                        env_param=resolved_env_param,
                        phase=str(raw.get("phase") or phase or "").strip() or None,
                        followup_phases=(
                            list(raw.get("followup_phases") or followup_phases or [])
                        ),
                        fault=str(raw.get("fault") or fault),
                        handler_name=str(raw.get("handler_name") or handler_name or "").strip()
                        or None,
                    )
                )
            continue
        if isinstance(raw, (str, bytes)):
            name = raw.decode() if isinstance(raw, bytes) else raw
            for concrete_name in _expand_contract_names_ordered([name]):
                resolved.append(
                    builtin_contract_check(
                        concrete_name,
                        kwargs=probe_kwargs,
                        tracked_params=tracked_params,
                        protected_keys=resolved_protected_keys,
                        env_param=resolved_env_param,
                        phase=phase,
                        followup_phases=followup_phases,
                        fault=fault,
                        handler_name=handler_name,
                    )
                )
            continue
        raise TypeError(f"unsupported contract check entry: {type(raw).__name__}")
    return resolved


def _replay_contract_failure(
    func: Any,
    check: ContractCheck,
    *,
    kwargs: Mapping[str, Any],
) -> tuple[bool, int, int]:
    """Replay one explicit contract failure and confirm it still fails."""
    attempts = 2
    matches = 0
    for _ in range(attempts):
        error_obj: BaseException | None = None
        call_context = None
        if _contract_check_is_static(check):
            value = None
        else:
            try:
                value = _call_sync(func, **dict(kwargs))
            except BaseException as exc:
                error_obj = exc
                value = None
            call_context = getattr(func, "__ordeal_last_call_context__", None)
        try:
            passed = _call_contract_predicate(
                check.predicate,
                value,
                func=func,
                call_context=call_context,
                kwargs=kwargs,
                error=error_obj,
            )
        except ContractNotApplicable:
            return False, attempts, matches
        except Exception:
            passed = False
        if not passed:
            matches += 1
    return matches == attempts, attempts, matches


def _semantic_contract_gate(
    *,
    func: Any,
    check: ContractCheck,
    value: Any,
    error_obj: BaseException | None,
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
    realism: float,
    fixture_completeness: float,
) -> tuple[bool, bool, int, int, str | None]:
    """Return promotion and replay evidence for one semantic contract failure."""
    if _contract_check_is_static(check):
        replayable, replay_attempts, replay_matches = _replay_contract_failure(
            func,
            check,
            kwargs=kwargs,
        )
        return True, replayable, replay_attempts, replay_matches, None
    skip_reason = _callable_skip_reason(func)
    if skip_reason is not None:
        return False, False, 0, 0, skip_reason
    if getattr(func, "__ordeal_auto_harness__", False) and not getattr(
        func, "__ordeal_harness_verified__", True
    ):
        return (
            False,
            False,
            0,
            0,
            str(
                getattr(func, "__ordeal_harness_dry_run_error__", None)
                or "auto-harness dry-run failed"
            ),
        )
    if fixture_completeness < _SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS:
        return (
            False,
            False,
            0,
            0,
            "fixture completeness stayed below the semantic-contract promotion bar "
            f"({fixture_completeness:.0%} < "
            f"{_SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS:.0%})",
        )

    replayable, replay_attempts, replay_matches = _replay_contract_failure(
        func,
        check,
        kwargs=kwargs,
    )
    aligned_sinks = _aligned_security_sinks(kwargs, profile)
    shaped_output_failure = error_obj is None and value is not None
    replay_demonstrates_impact = replayable and (
        shaped_output_failure or error_obj is not None or bool(aligned_sinks)
    )
    if shaped_output_failure:
        return True, replayable, replay_attempts, replay_matches, None
    if aligned_sinks and realism >= _SEMANTIC_CONTRACT_STRONG_REALISM:
        return True, replayable, replay_attempts, replay_matches, None
    if replay_demonstrates_impact:
        return True, replayable, replay_attempts, replay_matches, None
    return (
        False,
        replayable,
        replay_attempts,
        replay_matches,
        "semantic contract remains exploratory until it fails on a shaped output, "
        "a sink-aligned witness reaches stronger realism, or replay demonstrates impact",
    )


def _evaluate_contract_checks(
    func: Any,
    contract_checks: list[ContractCheck] | None,
    *,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    execute_calls: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run explicit contract probes against *func* and collect violations."""
    if not contract_checks:
        return [], []

    violations: list[str] = []
    details: list[dict[str, Any]] = []
    profile = _likely_contract_profile(
        func,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
    )
    fixture_completeness = _profile_fixture_completeness(profile)
    qualname = str(profile.get("qualname", getattr(func, "__qualname__", "?")))
    seed_examples = list(profile.get("seed_examples", []))
    probe_kwargs = dict(seed_examples[0].kwargs) if seed_examples else _contract_seed_kwargs(func)
    tracked_params = list(probe_kwargs)
    env_param = next(
        (name for name, value in probe_kwargs.items() if isinstance(value, Mapping)),
        None,
    )
    protected_keys = [
        key
        for key in ("PATH", "HOME", "PWD", "TMPDIR")
        if env_param is not None
        and isinstance(probe_kwargs.get(env_param), Mapping)
        and key in probe_kwargs.get(env_param, {})
    ]
    resolved_checks = _resolve_contract_check_entries(
        contract_checks,
        probe_kwargs=probe_kwargs,
        tracked_params=tracked_params,
        protected_keys=protected_keys,
        env_param=env_param,
    )
    for check in resolved_checks:
        kwargs = dict(check.kwargs)
        contract_fit, realism, sink_signal, rationale = _score_contract_fit(kwargs, profile)
        lifecycle_probe = _lifecycle_contract_probe(func, check)
        call_context: Mapping[str, Any] | None = None
        error_obj: BaseException | None = None
        metadata = dict(check.metadata)
        detail_category = (
            "lifecycle_contract"
            if str(metadata.get("kind")) == "lifecycle"
            else "semantic_contract"
        )
        input_source = (
            "static_contract" if _contract_check_is_static(check) else "explicit_contract"
        )
        runtime_faults = [
            str(item)
            for item in list(metadata.get("runtime_faults", []) or [])
            if str(item).strip()
        ]
        if _contract_check_is_static(check) and not execute_calls:
            value = None
            call_context = None
        else:
            try:
                with (
                    (
                        _active_instance_probe(func, lifecycle_probe)
                        if lifecycle_probe is not None
                        else contextlib.nullcontext()
                    ),
                    (
                        _active_contract_faults(func, runtime_faults)
                        if runtime_faults
                        else contextlib.nullcontext()
                    ),
                ):
                    value = _call_sync(func, **kwargs)
                call_context = getattr(func, "__ordeal_last_call_context__", None)
            except BaseException as exc:
                error_obj = exc
                call_context = getattr(func, "__ordeal_last_call_context__", None)
                value = None

        if call_context is None:
            call_context = getattr(func, "__ordeal_last_call_context__", None)

        try:
            passed = _call_contract_predicate(
                check.predicate,
                value,
                func=func,
                call_context=call_context,
                kwargs=kwargs,
                error=error_obj,
            )
        except ContractNotApplicable:
            continue
        except Exception as exc:
            passed = False
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None if error_obj is None else f"{type(error_obj).__name__}: {error_obj}"
        static_context = getattr(func, "__ordeal_last_static_contract_context__", None)

        if passed:
            continue

        summary = check.summary or f"explicit contract failed: {check.name}"
        promoted = True
        replayable = True
        replay_attempts = 1
        replay_matches = 1
        forced_demotion_reason: str | None = None
        if detail_category == "semantic_contract":
            (
                promoted,
                replayable,
                replay_attempts,
                replay_matches,
                forced_demotion_reason,
            ) = _semantic_contract_gate(
                func=func,
                check=check,
                value=value,
                error_obj=error_obj,
                kwargs=kwargs,
                profile=profile,
                realism=realism,
                fixture_completeness=fixture_completeness,
            )
        violations.append(summary)
        detail = {
            "kind": "contract",
            "category": detail_category,
            "name": check.name,
            "summary": summary,
            "failing_args": kwargs,
            "value": repr(value)[:300],
            "contract_fit": contract_fit,
            "reachability": 1.0,
            "realism": realism,
            "sink_signal": max(sink_signal, 1.0),
            "input_source": input_source,
            "replayable": replayable,
            "replay_attempts": replay_attempts,
            "replay_matches": replay_matches,
        }
        if call_context:
            detail["lifecycle_phase"] = call_context.get("lifecycle_phase")
            detail["lifecycle_probe"] = call_context.get("lifecycle_probe")
            detail["teardown_called"] = call_context.get("teardown_called")
            detail["teardown_error"] = call_context.get("teardown_error")
            detail["lifecycle_runtime"] = call_context.get("lifecycle_runtime")
        if isinstance(static_context, Mapping) and static_context:
            detail["static_analysis"] = dict(static_context)
        if error is not None:
            detail["error"] = error[:300]
            if error_obj is not None:
                detail["error_type"] = type(error_obj).__name__
        detail["proof_bundle"] = _build_proof_bundle(
            qualname=qualname,
            error=error_obj,
            failing_args=kwargs,
            input_source=input_source,
            contract_fit=contract_fit,
            reachability=1.0,
            realism=realism,
            rationale=rationale,
            replayable=replayable,
            replay_attempts=replay_attempts,
            replay_matches=replay_matches,
            category=detail_category,
            profile=profile,
            sink_signal=max(sink_signal, 1.0),
            sink_categories=profile.get("sink_categories", ()),
            min_contract_fit=0.0,
            min_reachability=0.0,
            min_realism=0.0,
            min_fixture_completeness=(
                _SEMANTIC_CONTRACT_MIN_FIXTURE_COMPLETENESS
                if detail_category == "semantic_contract"
                else None
            ),
            harness_mode=getattr(func, "__ordeal_harness__", None),
            callable_kind=getattr(func, "__ordeal_kind__", None),
            contract_check=check.name,
            forced_demotion_reason=forced_demotion_reason,
            promoted=promoted,
        )
        if call_context and call_context.get("lifecycle_probe") is not None:
            detail["proof_bundle"]["lifecycle"] = dict(call_context["lifecycle_probe"])
        if isinstance(static_context, Mapping) and static_context:
            detail["proof_bundle"]["static_analysis"] = dict(static_context)
        details.append(detail)

    return violations, details


# ============================================================================
# 1. scan_module
# ============================================================================


def scan_module(
    module: str | ModuleType,
    *,
    max_examples: int | dict[str, int] = 50,
    check_return_type: bool = True,
    targets: Sequence[str] | None = None,
    include_private: bool = False,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    expected_failures: list[str] | None = None,
    expected_preconditions: dict[str, list[str]] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    expected_properties: dict[str, list[str]] | None = None,
    expected_relations: dict[str, list[str]] | None = None,
    contract_checks: dict[str, list[ContractCheck]] | None = None,
    ignore_contracts: list[str] | None = None,
    contract_overrides: dict[str, list[str]] | None = None,
    mode: ScanMode = "evidence",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: Sequence[str] | None = None,
    shell_injection_check: bool = False,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    security_focus: bool = False,
) -> ScanResult:
    """Smoke-test every public callable in *module*.

    For each callable with type hints, generates random inputs and checks:

    - **No crash**: calling with valid inputs doesn't raise
    - **Return type**: if annotated, the return value matches (optional)

    Simple::

        result = scan_module("myapp.scoring")
        assert result.passed

    With fixtures for params that can't be inferred::

        result = scan_module("myapp", fixtures={"model": model_strategy})

    With per-function example budgets::

        result = scan_module("myapp", max_examples={
            "compute": 200,    # fuzz this one harder
            "__default__": 50, # everything else
        })

    With expected failures for known-broken functions::

        result = scan_module("myapp", expected_failures=["broken_func"])
        assert result.passed  # broken_func failure won't count

    Args:
        module: Module path or object to scan.
        max_examples: Hypothesis examples per function. Either a single int
            (same budget for all functions) or a dict mapping function names
            to budgets, with ``"__default__"`` as fallback (default: 50).
        check_return_type: Verify return type annotations.
        targets: Optional explicit callable targets within the module. Accepts
            local names like ``"Env.build_env_vars"`` or explicit targets like
            ``"pkg.mod:Env.build_env_vars"``.
        include_private: Also include single-underscore names.
        fixtures: Strategy overrides for specific parameter names.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_scenarios: Optional per-class collaborator scenarios run after setup.
        object_state_factories: Optional per-class state factories for methods that take
            a runtime ``state`` parameter.
        object_teardowns: Optional per-class teardown hooks for stateful harnesses.
        object_harnesses: Per-class harness mode (``fresh`` or ``stateful``).
        expected_failures: Function names that are expected to fail.
            Failures from these functions are tracked separately and
            do not cause ``result.passed`` to be ``False``.
        expected_preconditions: Per-function exception/docstring tokens that
            should count as expected contract preconditions instead of bugs.
        ignore_properties: Property names to suppress in mined warnings.
        ignore_relations: Relation names to suppress in mined warnings.
        property_overrides: Per-function property suppressions.
        relation_overrides: Per-function relation suppressions.
        expected_properties: Per-function expected property annotations. These
            are merged into ``property_overrides`` as suppressions.
        expected_relations: Per-function expected relation annotations. These
            are merged into ``relation_overrides`` as suppressions.
        contract_checks: Explicit semantic contract probes keyed by
            callable name. Each probe runs with explicit ``kwargs`` and
            reports a contract violation when its predicate fails.
        ignore_contracts: Auto-inferred contract probes to suppress globally.
        contract_overrides: Per-function auto-contract suppressions.
        mode: ``"evidence"`` surfaces broad exploratory findings;
            ``"candidate"`` keeps only stricter high-fit candidates.
            ``"coverage_gap"`` and ``"real_bug"`` remain compatibility aliases.
        seed_from_tests: Learn valid input shapes from adjacent pytest files.
        seed_from_fixtures: Mine literal pytest fixture returns as seed inputs.
        seed_from_docstrings: Mine doctest-like examples from docstrings.
        seed_from_code: Mine boundary values from code patterns.
        seed_from_call_sites: Mine literal examples from adjacent call sites.
        treat_any_as_weak: Penalize broad or missing hints instead of trusting them.
        proof_bundles: Attach structured proof payloads to crash findings.
        auto_contracts: Auto-enable sink-aware semantic checks for shell/path/env/json/http.
        shell_injection_check: Run a static shell-injection oracle before execution.
        require_replayable: Require replayability before promoting a bug candidate.
        min_contract_fit: Minimum inferred contract-fit score to promote.
        min_reachability: Minimum reachability score to promote.
        min_realism: Minimum semantic realism score to promote.
        security_focus: Opt into trust-boundary-biased sink detection, scoring,
            and deterministic low-side-effect security probes.
    """
    if mode not in _VALID_SCAN_MODES:
        raise ValueError(f"mode must be one of {_VALID_SCAN_MODES}, got {mode!r}")
    mode = _normalize_scan_mode(mode)
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__
    result = ScanResult(
        module=mod_name,
        expected_failure_names=list(expected_failures) if expected_failures else [],
    )

    # Resolve per-function example budgets
    if isinstance(max_examples, int):
        default_examples = max_examples
        examples_map: dict[str, int] = {}
    else:
        default_examples = max_examples.get("__default__", 50)
        examples_map = max_examples

    for name, func in _selected_public_functions(
        mod,
        targets=targets,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            reason = _callable_skip_reason(func) or "missing type hints"
            result.skipped.append((name, reason))
            continue

        return_type = safe_get_annotations(func).get("return")

        func_examples = examples_map.get(name, default_examples)
        func_result = _test_one_function(
            name,
            func,
            strategies,
            return_type,
            max_examples=func_examples,
            check_return_type=check_return_type,
            fixtures=fixtures,
            ignore_properties=sorted(
                {
                    *(ignore_properties or []),
                    *(expected_properties or {}).get("*", []),
                    *(property_overrides or {}).get(name, []),
                    *(expected_properties or {}).get(name, []),
                }
            ),
            ignore_relations=sorted(
                {
                    *(ignore_relations or []),
                    *(expected_relations or {}).get("*", []),
                    *(relation_overrides or {}).get(name, []),
                    *(expected_relations or {}).get(name, []),
                }
            ),
            property_overrides=property_overrides,
            relation_overrides=relation_overrides,
            contract_checks=(contract_checks or {}).get(name),
            expected_preconditions=sorted(
                {
                    *((expected_preconditions or {}).get("*", [])),
                    *((expected_preconditions or {}).get(name, [])),
                }
            ),
            ignore_contracts=sorted(
                {
                    *(ignore_contracts or []),
                    *(contract_overrides or {}).get("*", []),
                    *(contract_overrides or {}).get(name, []),
                }
            ),
            mode=mode,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            treat_any_as_weak=treat_any_as_weak,
            proof_bundles=proof_bundles,
            auto_contracts=auto_contracts,
            shell_injection_check=shell_injection_check,
            require_replayable=require_replayable,
            min_contract_fit=min_contract_fit,
            min_reachability=min_reachability,
            min_realism=min_realism,
            security_focus=security_focus,
        )
        result.functions.append(func_result)

    return result


def _test_one_function(
    name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    return_type: type | None,
    *,
    max_examples: int,
    check_return_type: bool,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    contract_checks: list[ContractCheck] | None = None,
    expected_preconditions: list[str] | None = None,
    ignore_contracts: list[str] | None = None,
    mode: ScanMode = "evidence",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: Sequence[str] | None = None,
    shell_injection_check: bool = False,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    security_focus: bool = False,
) -> FunctionResult:
    """Run no-crash + return-type + mined-property checks on a single function."""
    mode = _normalize_scan_mode(mode)

    def _replay_failure(exc: Exception) -> tuple[bool, int, int]:
        if not last_kwargs:
            return False, 0, 0
        attempts = 2
        matches = 0
        expected_type = type(exc)
        expected_text = str(exc)
        for _ in range(attempts):
            try:
                _call_sync(func, **dict(last_kwargs))
            except Exception as replay_exc:
                if type(replay_exc) is expected_type and str(replay_exc) == expected_text:
                    matches += 1
        return matches == attempts, attempts, matches

    last_kwargs: dict[str, Any] = {}
    last_input_source = "boundary"
    profile = _likely_contract_profile(
        func,
        security_focus=security_focus,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
    )
    fixture_completeness = _profile_fixture_completeness(profile)
    seed_examples = list(profile.get("seed_examples", []))
    strategies = _bias_strategies_with_seed_examples(strategies, seed_examples)
    auto_checks, sink_categories = _auto_contract_checks(
        func,
        seed_examples,
        auto_contracts=auto_contracts,
        ignore_contracts=ignore_contracts,
        shell_injection_check=shell_injection_check,
        security_focus=security_focus,
    )
    effective_contract_checks = [*(contract_checks or []), *auto_checks]
    static_contract_checks = [
        check for check in effective_contract_checks if _contract_check_is_static(check)
    ]
    runtime_contract_checks = [
        check for check in effective_contract_checks if not _contract_check_is_static(check)
    ]

    if static_contract_checks:
        contract_violations, contract_details = _evaluate_contract_checks(
            func,
            static_contract_checks,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            treat_any_as_weak=treat_any_as_weak,
            execute_calls=False,
        )
        if contract_violations:
            return FunctionResult(
                name=name,
                passed=False,
                execution_ok=True,
                verdict=(
                    "lifecycle_contract"
                    if any(
                        detail.get("category") == "lifecycle_contract"
                        for detail in contract_details
                    )
                    else "semantic_contract"
                ),
                contract_violations=contract_violations,
                contract_violation_details=contract_details,
                sink_categories=sink_categories,
                input_sources=[
                    {"source": example.source, "evidence": example.evidence}
                    for example in profile.get("seed_examples", [])
                ],
                input_source="static_contract",
            )

    def _origin_for_kwargs(kwargs: Mapping[str, Any], fallback: str) -> str:
        for example in profile.get("seed_examples", []):
            if dict(example.kwargs) == dict(kwargs):
                return example.source
        return fallback

    def _check_result(result: Any) -> None:
        if check_return_type and return_type is not None:
            if not _type_matches(result, return_type):
                raise AssertionError(
                    f"Expected return type {return_type}, got {type(result).__name__}: {result!r}"
                )

    def _run_one(kwargs: Mapping[str, Any], fallback: str) -> None:
        nonlocal last_kwargs
        nonlocal last_input_source
        last_kwargs = dict(kwargs)
        last_input_source = _origin_for_kwargs(kwargs, fallback)
        result = _call_sync(func, **dict(kwargs))
        _check_result(result)

    try:
        for candidate in _candidate_inputs(
            func,
            fixtures=fixtures,
            mutate_observed_inputs=any(
                (
                    seed_from_tests,
                    seed_from_fixtures,
                    seed_from_docstrings,
                    seed_from_call_sites,
                )
            ),
            mode=mode,
            security_focus=security_focus,
            seed_examples=seed_examples,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
        ):
            _run_one(candidate.kwargs, candidate.origin)

        # Hypothesis rejects @given() with no inferred arguments.
        if not strategies:
            _run_one({}, "random_fuzz")
        else:

            @given(**strategies)
            @settings(max_examples=max_examples, database=None)
            def test(**kwargs: Any) -> None:
                _run_one(kwargs, "random_fuzz")

            test()
    except Exception as e:
        call_context = getattr(func, "__ordeal_last_call_context__", None)
        precondition = _documented_precondition_failure(
            func,
            e,
            last_kwargs,
            expected_patterns=expected_preconditions,
        )
        if precondition is not None:
            return FunctionResult(
                name=name,
                passed=True,
                execution_ok=True,
                verdict="expected_precondition_failure",
                error_type=precondition["error_type"],
                failing_args=last_kwargs or None,
                contract_violations=[str(precondition["summary"])],
                contract_violation_details=[precondition],
                sink_categories=sink_categories,
                input_sources=[
                    {"source": example.source, "evidence": example.evidence}
                    for example in profile.get("seed_examples", [])
                ],
                input_source=last_input_source,
            )
        replayable, replay_attempts, replay_matches = _replay_failure(e)
        contract_fit, realism, sink_signal, rationale = _score_contract_fit(last_kwargs, profile)
        reachability = _reachability_score(last_input_source, last_kwargs, profile)
        aligned_sinks = _aligned_security_sinks(last_kwargs, profile)
        aligned_critical_sinks = _critical_security_sinks(aligned_sinks)
        effective_min_contract_fit = min_contract_fit
        effective_min_reachability = min_reachability
        effective_min_realism = min_realism
        effective_min_fixture_completeness: float | None = None
        if security_focus and aligned_critical_sinks:
            effective_min_contract_fit = max(0.45, min_contract_fit - 0.1)
            effective_min_reachability = max(0.35, min_reachability - 0.1)
            effective_min_realism = max(0.5, min_realism - 0.05)
        if security_focus and mode == "real_bug":
            effective_min_fixture_completeness = _SECURITY_FOCUS_MIN_FIXTURE_COMPLETENESS
        robustness_case = _looks_like_declared_contract_robustness(
            last_kwargs,
            profile,
            realism=realism,
            reachability=reachability,
        )
        crash_category = _classify_crash(
            mode=mode,
            replayable=replayable,
            contract_fit=contract_fit,
            reachability=reachability,
            realism=realism,
            robustness_case=robustness_case,
            min_contract_fit=effective_min_contract_fit,
            min_reachability=effective_min_reachability,
            min_realism=effective_min_realism,
            require_replayable=require_replayable,
        )
        crash_category, forced_demotion_reason = _bootstrap_failure_demotion(
            func,
            category=crash_category,
            call_context=call_context,
        )
        crash_category, security_focus_demotion_reason = _security_focus_demotion(
            category=crash_category,
            mode=mode,
            security_focus=security_focus,
            input_source=last_input_source,
            replayable=replayable,
            fixture_completeness=fixture_completeness,
            aligned_sink_categories=aligned_sinks,
        )
        if security_focus_demotion_reason is not None:
            forced_demotion_reason = security_focus_demotion_reason
        verdict = _verdict_for_crash(crash_category)
        proof_bundle = None
        if proof_bundles:
            proof_bundle = _build_proof_bundle(
                qualname=str(profile.get("qualname", name)),
                error=e,
                failing_args=last_kwargs,
                input_source=last_input_source,
                contract_fit=contract_fit,
                reachability=reachability,
                realism=realism,
                rationale=rationale,
                replayable=replayable,
                replay_attempts=replay_attempts,
                replay_matches=replay_matches,
                category=crash_category,
                profile=profile,
                sink_signal=sink_signal,
                sink_categories=sink_categories,
                aligned_sink_categories=aligned_sinks,
                min_contract_fit=effective_min_contract_fit,
                min_reachability=effective_min_reachability,
                min_realism=effective_min_realism,
                min_fixture_completeness=effective_min_fixture_completeness,
                harness_mode=getattr(func, "__ordeal_harness__", None),
                callable_kind=getattr(func, "__ordeal_kind__", None),
                security_focus=security_focus,
                forced_demotion_reason=forced_demotion_reason,
            )
            if call_context:
                lifecycle_details = {
                    "phase": call_context.get("lifecycle_phase"),
                    "probe": call_context.get("lifecycle_probe"),
                    "runtime": call_context.get("lifecycle_runtime"),
                    "teardown_called": call_context.get("teardown_called"),
                    "teardown_error": call_context.get("teardown_error"),
                }
                if any(value is not None for value in lifecycle_details.values()):
                    proof_bundle["lifecycle"] = lifecycle_details
        if crash_category == "likely_bug" and not _scan_crash_promoted(
            category=crash_category,
            replayable=replayable,
            proof_bundle=proof_bundle,
            sink_categories=sink_categories,
        ):
            verdict = "exploratory_crash"
            if isinstance(proof_bundle, dict):
                proof_verdict = dict(proof_bundle.get("verdict", {}))
                proof_verdict["promoted"] = False
                if not proof_verdict.get("demotion_reason") and (
                    _proof_bundle_critical_sinks(proof_bundle) is not None
                    or _critical_security_sinks(sink_categories)
                ):
                    proof_verdict["demotion_reason"] = (
                        "critical sink findings require a replayable proof bundle before promotion"
                    )
                proof_bundle["verdict"] = proof_verdict
        return FunctionResult(
            name=name,
            passed=verdict not in _PROMOTED_SCAN_VERDICTS,
            execution_ok=False,
            verdict=verdict,
            error=str(e)[:300],
            error_type=type(e).__name__,
            failing_args=last_kwargs or None,
            crash_category=crash_category,
            replayable=replayable,
            replay_attempts=replay_attempts,
            replay_matches=replay_matches,
            contract_fit=contract_fit,
            reachability=reachability,
            realism=realism,
            sink_signal=sink_signal,
            sink_categories=sink_categories,
            input_sources=[
                {"source": example.source, "evidence": example.evidence}
                for example in profile.get("seed_examples", [])
            ],
            input_source=last_input_source,
            proof_bundle=proof_bundle,
        )

    contract_violations, contract_details = _evaluate_contract_checks(
        func,
        runtime_contract_checks,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
    )
    violations: list[str] = []
    details: list[dict[str, Any]] = []
    if mode != "real_bug":
        try:
            from ordeal.mine import _is_suspicious_property, mine

            mine_result = mine(
                func,
                max_examples=min(max_examples, 30),
                ignore_properties=ignore_properties or [],
                ignore_relations=ignore_relations or [],
                property_overrides=property_overrides or {},
                relation_overrides=relation_overrides or {},
            )
            for prop in mine_result.properties:
                if _is_suspicious_property(prop):
                    label = f"{prop.name} ({prop.confidence:.0%})"
                    violations.append(label)
                    details.append(
                        {
                            "name": prop.name,
                            "summary": label,
                            "confidence": round(prop.confidence, 4),
                            "holds": prop.holds,
                            "total": prop.total,
                            "counterexample": prop.counterexample,
                        }
                    )
        except Exception:
            pass  # mining failed — still report crash-safety pass
    return FunctionResult(
        name=name,
        passed=not bool(contract_violations),
        execution_ok=True,
        verdict=(
            "lifecycle_contract"
            if any(detail.get("category") == "lifecycle_contract" for detail in contract_details)
            else "semantic_contract"
            if contract_violations
            else "exploratory_property"
            if violations
            else "clean"
        ),
        property_violations=violations,
        property_violation_details=details,
        contract_violations=contract_violations,
        contract_violation_details=contract_details,
        sink_categories=sink_categories,
        input_sources=[
            {"source": example.source, "evidence": example.evidence}
            for example in profile.get("seed_examples", [])
        ],
    )


# ============================================================================
# 2. fuzz
# ============================================================================


def fuzz(
    fn: Any,
    *,
    max_examples: int = 1000,
    check_return_type: bool = False,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> FuzzResult:
    """Deep-fuzz a single function with auto-inferred strategies.

    Simple::

        result = fuzz(myapp.scoring.compute)
        assert result.passed

    With fixture overrides (strategies or plain values)::

        result = fuzz(myapp.scoring.compute, model=model_strategy)
        result = fuzz(myapp.scoring.compute, max_tokens=5)  # auto-wrapped

    Args:
        fn: The function to fuzz.
        max_examples: Number of random inputs to try.
        check_return_type: Verify return type annotation.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_scenarios: Optional per-class collaborator scenarios run after setup.
        object_state_factories: Optional per-class state factories for methods that take
            a runtime ``state`` parameter.
        **fixtures: Strategy overrides or plain values (auto-wrapped in st.just).
    """
    fn_name = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    if isinstance(fn, str):
        fn_name, fn = _resolve_explicit_target(
            fn,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
        )

    # Auto-wrap plain values in st.just()
    normalized: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized = {}
        for k, v in fixtures.items():
            if isinstance(v, st.SearchStrategy):
                normalized[k] = v
            else:
                normalized[k] = st.just(v)
    strategies = _infer_strategies(fn, normalized)
    if strategies is None:
        reason = _callable_skip_reason(fn)
        if reason is not None:
            raise ValueError(f"Cannot fuzz {fn_name}: {reason}")
        raise ValueError(
            f"Cannot infer strategies for {fn_name}. Provide fixtures for untyped parameters."
        )

    return_type = safe_get_annotations(fn).get("return")

    failures: list[Exception] = []
    last_kwargs: dict[str, Any] = {}
    try:
        for kwargs in _boundary_smoke_inputs(fn, fixtures=normalized):
            last_kwargs = dict(kwargs)
            result = _call_sync(fn, **dict(kwargs))
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            nonlocal last_kwargs
            last_kwargs = dict(kwargs)
            result = _call_sync(fn, **kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(f"Expected {return_type}, got {type(result).__name__}")

        test()
    except Exception as e:
        failures.append(e)

    failing_args = last_kwargs if failures and last_kwargs else None
    return FuzzResult(
        function=fn.__qualname__ or fn.__name__,
        examples=max_examples,
        failures=failures,
        failing_args=failing_args,
    )


# ============================================================================
# 3. chaos_for — auto-infer faults + invariants
# ============================================================================

# Patterns in function ASTs that map to specific fault types.
# Keys are (module_attr, func_name) pairs found in ast.Call nodes.
_FAULT_PATTERNS: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
    # pattern → [(fault_module, fault_factory, kwargs), ...]
    "subprocess.run": [
        ("io", "subprocess_timeout", {}),
        ("io", "subprocess_delay", {}),
        ("io", "corrupt_stdout", {}),
    ],
    "subprocess.check_output": [
        ("io", "subprocess_timeout", {}),
    ],
    "subprocess.Popen": [
        ("io", "subprocess_timeout", {}),
    ],
    "open": [
        ("io", "disk_full", {}),
        ("io", "permission_denied", {}),
    ],
}


def _ml_data_fault_specs(call_str: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Infer ML/data seam faults from one dotted call target string."""
    parts = [part.lower() for part in call_str.split(".") if part]
    if not parts:
        return []
    leaf = parts[-1]
    has_model_token = bool(
        {
            "model",
            "model_client",
            "predictor",
            "scorer",
            "embedder",
            "encoder",
            "classifier",
            "reranker",
        }
        & set(parts)
    )
    has_feature_token = bool(
        {
            "feature_store",
            "vector_store",
            "embedding_store",
            "retriever",
            "feature_client",
        }
        & set(parts)
    )
    if has_model_token and leaf in {"predict", "infer", "run"}:
        return [
            ("numerical", "nan_injection", {}),
            ("numerical", "partial_batch", {}),
            ("numerical", "dtype_drift", {}),
        ]
    if has_model_token and leaf in {"predict_proba", "transform", "embed", "encode"}:
        return [
            ("numerical", "partial_batch", {}),
            ("numerical", "feature_order_drift", {}),
            ("numerical", "dtype_drift", {}),
        ]
    if has_feature_token and leaf in {
        "get",
        "fetch",
        "lookup",
        "get_features",
        "fetch_features",
        "lookup_features",
    }:
        return [
            ("numerical", "missing_feature", {}),
            ("numerical", "dtype_drift", {}),
        ]
    return []


def _infer_faults(
    mod: ModuleType,
    mod_name: str,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> list[Fault]:
    """Auto-discover faults by scanning function ASTs for risky calls.

    Detects subprocess, file I/O, and cross-function calls, then
    generates appropriate fault instances.
    """
    import ast
    import textwrap

    faults: list[Fault] = []
    seen: set[tuple[str, str, str, tuple[tuple[str, Any], ...]]] = set()

    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        try:
            source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Extract call target as string (e.g. "subprocess.run", "open")
            call_str = _call_to_string(node)
            if not call_str:
                continue

            # Check against known patterns
            for pattern, fault_specs in _FAULT_PATTERNS.items():
                if pattern not in call_str:
                    continue
                for fault_mod, fault_fn, kwargs in fault_specs:
                    key = (
                        name,
                        fault_mod,
                        fault_fn,
                        tuple(sorted(kwargs.items())),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    fault_module = importlib.import_module(f"ordeal.faults.{fault_mod}")
                    factory = getattr(fault_module, fault_fn)
                    # Faults that need a target get the module name
                    params = inspect.signature(factory).parameters
                    if "target" in params:
                        faults.append(factory(f"{mod_name}.{name}", **kwargs))
                    else:
                        faults.append(factory(**kwargs))

            for fault_mod, fault_fn, kwargs in _ml_data_fault_specs(call_str):
                key = (
                    name,
                    fault_mod,
                    fault_fn,
                    tuple(sorted(kwargs.items())),
                )
                if key in seen:
                    continue
                seen.add(key)
                fault_module = importlib.import_module(f"ordeal.faults.{fault_mod}")
                factory = getattr(fault_module, fault_fn)
                params = inspect.signature(factory).parameters
                if "target" in params:
                    faults.append(factory(f"{mod_name}.{name}", **kwargs))
                else:
                    faults.append(factory(**kwargs))

            # Cross-function calls → error_on_call
            if (
                call_str.startswith(mod_name + ".")
                and (
                    name,
                    "io",
                    call_str,
                    (),
                )
                not in seen
            ):
                seen.add((name, "io", call_str, ()))
                from ordeal.faults.io import error_on_call

                faults.append(error_on_call(call_str))

    return faults


def _call_to_string(node: Any) -> str | None:
    """Extract a dotted string from an ast.Call node's func attribute."""
    import ast

    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        current = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None


def _infer_invariants(
    mod: ModuleType,
    fixtures: dict[str, Any] | None,
    *,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> tuple[dict[str, list[Invariant]], list[Invariant]]:
    """Auto-discover invariants by mining function properties.

    Runs mine() on each function with a small example count,
    then maps universal properties to invariant objects.
    """
    from ordeal.invariants import bounded, finite, no_nan, non_empty
    from ordeal.mine import mine

    # Map mined property names to invariant constructors
    _PROPERTY_TO_INVARIANT: dict[str, Invariant | None] = {
        "no NaN": no_nan,
        "output >= 0": bounded(0, float("inf")),
        "output in [0, 1]": bounded(0, 1),
        "never empty": non_empty(),
    }

    invariant_map: dict[str, list[Invariant]] = {}
    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        strats = _infer_strategies(func, fixtures)
        if strats is None:
            continue
        try:
            result = mine(func, max_examples=30)
        except Exception:
            continue

        func_invs: list[Invariant] = []
        has_numeric = False
        for prop in result.universal:
            inv = _PROPERTY_TO_INVARIANT.get(prop.name)
            if inv is not None:
                func_invs.append(inv)
            if "output >= 0" in prop.name or "output in [" in prop.name:
                has_numeric = True

        # If function returns numeric values and no specific bound was found,
        # at least check for finite
        if has_numeric and not any(isinstance(i, type(finite)) for i in func_invs):
            func_invs.append(finite)

        if func_invs:
            invariant_map[name] = func_invs

    return invariant_map, []


def chaos_for(
    module: str | ModuleType,
    *,
    fixtures: dict[str, st.SearchStrategy] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    invariants: list[Invariant] | dict[str, Invariant] | None = None,
    faults: list[Fault] | None = None,
    max_examples: int = 50,
    stateful_step_count: int = 30,
    rule_timeout: float = 30.0,
) -> type:
    """Auto-generate a ChaosTest from a module's public API.

    Each public function becomes a ``@rule``.  The nemesis toggles
    *faults*.  After each step, *invariants* are checked on every
    return value.

    Zero-config — discovers everything automatically::

        TestScoring = chaos_for("myapp.scoring")
        # Scans code for subprocess/file/network calls → generates faults
        # Mines each function with random inputs → generates invariants

    With explicit overrides::

        TestScoring = chaos_for(
            "myapp.scoring",
            faults=[timing.timeout("myapp.db.query")],
            invariants={"compute": bounded(0, 1)},
        )

    Pass ``faults=[]`` or ``invariants=[]`` to disable auto-discovery.

    Returns a pytest-discoverable ``TestCase`` class.

    Args:
        module: Module path or object.
        fixtures: Strategy overrides for parameter names.
        object_factories: Factory overrides for class targets.
        object_setups: Optional per-class setup hooks run after factory creation.
        object_state_factories: Optional per-class state factories for methods that take
            a runtime ``state`` parameter.
        object_teardowns: Optional per-class teardown hooks run during ChaosTest teardown.
        object_harnesses: Per-class harness mode (``fresh`` or ``stateful``).
        invariants: ``None`` = auto-mine, list = global, dict = per-function.
        faults: ``None`` = auto-infer from code, list = explicit.
        max_examples: Hypothesis examples.
        stateful_step_count: Max rules per test case.
        rule_timeout: Per-rule timeout in seconds (default 30, 0 to disable).
    """
    mod = _resolve_module(module)
    mod_name = module if isinstance(module, str) else mod.__name__

    # Auto-discover faults from code analysis when not provided
    if faults is None:
        fault_list = _infer_faults(
            mod,
            mod_name,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
    else:
        fault_list = list(faults)

    # Auto-discover invariants from mine() when not provided
    if invariants is None:
        invariant_map, global_invs = _infer_invariants(
            mod,
            fixtures,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
    elif isinstance(invariants, dict):
        invariant_map: dict[str, list[Invariant]] = {
            k: [v] if isinstance(v, Invariant) else list(v) for k, v in invariants.items()
        }
        global_invs: list[Invariant] = []
    else:
        invariant_map = {}
        global_invs = list(invariants)

    # Collect rule methods
    rules_dict: dict[str, Any] = {}
    initialize_dict: dict[str, Any] = {}
    teardown_hooks: list[tuple[str, Any]] = []
    seen_stateful_owners: set[str] = set()
    for name, func in _get_public_functions(
        mod,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    ):
        strategies = _infer_strategies(func, fixtures)
        if strategies is None:
            continue
        # Per-function invariants override global; if neither, empty list
        func_invs = invariant_map.get(name, global_invs)
        if (
            getattr(func, "__ordeal_kind__", None) == "instance"
            and getattr(func, "__ordeal_harness__", "fresh") == "stateful"
            and getattr(func, "__ordeal_factory__", None) is not None
        ):
            owner = getattr(func, "__ordeal_owner__", None)
            method_name = str(getattr(func, "__ordeal_method_name__", name.rsplit(".", 1)[-1]))
            owner_key = re.sub(
                r"[^0-9a-zA-Z_]",
                "_",
                getattr(owner, "__qualname__", getattr(owner, "__name__", "owner")),
            ).lower()
            owner_attr = f"_ordeal_owner_{owner_key}"
            if owner_attr not in seen_stateful_owners:
                init_method = _make_stateful_initialize_method(
                    owner_attr,
                    factory=getattr(func, "__ordeal_factory__"),
                    setup=getattr(func, "__ordeal_setup__", None),
                    scenarios=tuple(getattr(func, "__ordeal_scenarios__", ()) or ()),
                )
                initialize_dict[init_method.__name__] = init_method
                teardown_hook = getattr(func, "__ordeal_teardown__", None)
                if teardown_hook is not None:
                    teardown_hooks.append((owner_attr, teardown_hook))
                seen_stateful_owners.add(owner_attr)
            method = _make_stateful_rule_method(
                name,
                func,
                strategies,
                func_invs,
                owner_attr=owner_attr,
                method_name=method_name,
            )
        else:
            method = _make_rule_method(name, func, strategies, func_invs)
        rules_dict[method.__name__] = method

    if not rules_dict:
        raise ValueError(
            f"No testable functions found in {mod_name}. "
            f"Ensure functions have type hints or provide fixtures."
        )

    # Build class
    namespace: dict[str, Any] = {
        "faults": fault_list,
        "rule_timeout": rule_timeout,
        **initialize_dict,
        **rules_dict,
    }
    if teardown_hooks:
        namespace["teardown"] = _make_stateful_teardown_method(teardown_hooks)

    class_name = f"AutoChaos_{mod_name.replace('.', '_')}"
    AutoChaos = type(class_name, (ChaosTest,), namespace)

    TestCase = AutoChaos.TestCase
    TestCase.settings = settings(
        max_examples=max_examples,
        stateful_step_count=stateful_step_count,
    )
    return TestCase


def _make_rule_method(
    func_name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    invariants: list[Invariant],
) -> Any:
    """Create a @rule method that calls func and checks invariants on the result."""
    safe_name = func_name.replace(".", "_")

    @rule(**strategies)
    def method(self: Any, **kwargs: Any) -> None:
        result = _call_sync(func, **kwargs)
        if result is not None:
            for inv in invariants:
                try:
                    inv(result)
                except TypeError:
                    pass  # invariant doesn't apply to this return type

    method.__name__ = f"call_{safe_name}"
    method.__qualname__ = f"AutoChaos.call_{safe_name}"
    return method


def _make_stateful_initialize_method(
    owner_attr: str,
    *,
    factory: Any,
    setup: Any | None = None,
    scenarios: Sequence[Any] | None = None,
) -> Any:
    """Create an ``@initialize`` hook that persists one owner instance."""

    @initialize()
    def method(self: Any) -> None:
        instance = _call_sync(factory)
        instance = _apply_instance_hook(instance, setup)
        instance = _apply_instance_hooks(instance, scenarios)
        setattr(self, owner_attr, instance)

    method.__name__ = f"setup_{owner_attr}"
    method.__qualname__ = f"AutoChaos.setup_{owner_attr}"
    return method


def _make_stateful_rule_method(
    func_name: str,
    func: Any,
    strategies: dict[str, st.SearchStrategy],
    invariants: list[Invariant],
    *,
    owner_attr: str,
    method_name: str,
) -> Any:
    """Create a rule that reuses one persistent object instance."""
    safe_name = func_name.replace(".", "_")

    @rule(**strategies)
    def method(self: Any, **kwargs: Any) -> None:
        instance = getattr(self, owner_attr, None)
        if instance is None:
            raise RuntimeError(f"stateful harness did not initialize {owner_attr}")
        probe_cleanup, probe_context = _instance_probe_result(
            getattr(func, "__ordeal_instance_probe__", None),
            instance=instance,
            owner=getattr(func, "__ordeal_owner__", None),
            method_name=method_name,
        )
        before_state = _snapshot_instance_state(instance)
        target = _unwrap(func)
        call_args, call_kwargs = _prepare_bound_method_call(
            target,
            (),
            kwargs,
            instance=instance,
            state_factory=getattr(func, "__ordeal_state_factory__", None),
            state_param=getattr(func, "__ordeal_state_param__", None),
        )
        result = _call_sync(getattr(instance, method_name), *call_args, **call_kwargs)
        func.__ordeal_last_call_context__ = {
            "instance": instance,
            "before_state": before_state,
            "after_state": _snapshot_instance_state(instance),
            "kwargs": dict(call_kwargs),
            "args": tuple(call_args),
            "method_name": method_name,
            "owner": getattr(func, "__ordeal_owner__", None),
            "harness": "stateful",
            "lifecycle_phase": getattr(func, "__ordeal_lifecycle_phase__", None),
            **probe_context,
        }
        if probe_cleanup is not None:
            probe_cleanup()
        if result is not None:
            for inv in invariants:
                try:
                    inv(result)
                except TypeError:
                    pass

    method.__name__ = f"call_{safe_name}"
    method.__qualname__ = f"AutoChaos.call_{safe_name}"
    return method


def _make_stateful_teardown_method(
    owner_hooks: Sequence[tuple[str, Any]],
) -> Any:
    """Create a teardown that attempts every configured owner cleanup hook."""

    def teardown(self: Any) -> None:
        errors: list[str] = []
        for owner_attr, hook in owner_hooks:
            instance = getattr(self, owner_attr, None)
            if instance is None or hook is None:
                continue
            try:
                _call_sync(hook, instance)
            except Exception as exc:
                errors.append(f"{owner_attr}: {type(exc).__name__}: {exc}")
        ChaosTest.teardown(self)
        if errors:
            raise AssertionError("; ".join(errors))

    teardown.__name__ = "teardown"
    teardown.__qualname__ = "AutoChaos.teardown"
    return teardown
