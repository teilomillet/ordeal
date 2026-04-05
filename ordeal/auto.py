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
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from pprint import pformat
from types import ModuleType
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

    def __str__(self) -> str:
        if self.passed and not self.property_violations and not self.contract_violations:
            return f"  PASS  {self.name}"
        if not self.passed:
            label = (
                "WARN"
                if self.crash_category in {"speculative_crash", "invalid_input_crash"}
                else "FAIL"
            )
            return f"  {label}  {self.name}: {self.error}"
        if self.contract_violations:
            viols = "; ".join(self.contract_violations)
            return f"  NOTE  {self.name}: {viols}"
        viols = "; ".join(self.property_violations)
        return f"  WARN  {self.name}: {viols}"


ScanMode = Literal["coverage_gap", "real_bug"]


@dataclass(frozen=True)
class ContractCheck:
    """Explicit semantic contract probe for a scanned callable."""

    name: str
    predicate: Callable[[Any], bool] = field(repr=False)
    kwargs: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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

    def summary(self) -> str:
        lines = [f"scan_module({self.module!r}): {self.total} functions, {self.failed} failed"]
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
_VALID_SCAN_MODES: set[str] = {"coverage_gap", "real_bug"}
_WEAK_CONTRACT_FIT = 0.35


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
    config: dict[str, Any] = field(default_factory=dict)


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


def _candidate_seed_files(module_name: str) -> tuple[list[Path], list[Path]]:
    """Return likely test files and project call-site files for *module_name*."""
    test_files: list[Path] = []
    project_files: list[Path] = []

    tests_dir = Path.cwd() / "tests"
    if tests_dir.is_dir():
        with contextlib.suppress(Exception):
            from ordeal.audit import _find_test_file_evidence

            test_files = [
                Path(item.path)
                for item in _find_test_file_evidence(module_name, tests_dir)
                if Path(item.path).is_file()
            ]

    module_path_parts = module_name.split(".")
    roots = [Path.cwd(), Path.cwd() / "src"]
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
        return test_files, project_files

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
    return test_files, project_files


def _fixture_literals_for_params(
    param_names: set[str],
    files: Sequence[Path],
) -> dict[str, list[Any]]:
    """Return literal pytest fixture values keyed by matching parameter name."""
    fixtures: dict[str, list[Any]] = {name: [] for name in param_names}
    for path in files:
        tree = _parse_python_source(str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name not in param_names:
                continue
            if not any(
                isinstance(decorator, ast.Call)
                and _call_name(decorator.func) == "pytest.fixture"
                or _call_name(decorator) == "pytest.fixture"
                for decorator in node.decorator_list
            ):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    value = _literal_ast_value(stmt.value)
                    if value is not _MISSING and value not in fixtures[name]:
                        fixtures[name].append(value)
    return {name: values for name, values in fixtures.items() if values}


def _call_seed_examples_from_files(
    func: Any,
    files: Sequence[Path],
    *,
    source: str,
) -> list[SeedExample]:
    """Extract literal call examples for *func* from Python files."""
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except Exception:
        return []

    module_name = getattr(target, "__module__", "")
    leaf_name = getattr(target, "__name__", "")
    if not module_name or not leaf_name:
        return []

    param_names = [name for name in sig.parameters if name not in {"self", "cls"}]
    if not param_names:
        return []

    examples: list[SeedExample] = []
    for path in files:
        tree = _parse_python_source(str(path))
        if tree is None:
            continue

        module_aliases: set[str] = set()
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name:
                        module_aliases.add(alias.asname or alias.name.split(".")[-1])
            elif isinstance(node, ast.ImportFrom):
                if node.module == module_name:
                    for alias in node.names:
                        imported_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
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
            if "." not in func_name and imported_names and func_name not in imported_names:
                continue

            kwargs: dict[str, Any] = {}
            positional_params = iter(param_names)
            supported = True
            for arg in node.args:
                param_name = next(positional_params, None)
                if param_name is None:
                    supported = False
                    break
                value = _literal_ast_value(arg)
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
                value = _literal_ast_value(kw.value)
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


def _infer_sink_categories(func: Any) -> list[str]:
    """Infer semantic sink categories from source and parameter names."""
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

    categories: list[str] = []
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
            ("json", "tool_call", "tool_calls", "normalize"),
            {"message", "messages", "response", "tool_call"},
        ),
        (
            "http",
            ("headers", "request", "response", "body", "http"),
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
    for category, tokens, param_names in checks:
        if any(token in source for token in tokens) or params & param_names:
            categories.append(category)
    return categories


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


def _scenario_hook_from_spec(spec: Mapping[str, Any]) -> Callable[[Any], Any]:
    """Compile one TOML-friendly collaborator scenario spec into a hook."""
    kind = str(spec.get("kind") or spec.get("action") or spec.get("op") or "").strip().lower()
    if not kind:
        if spec.get("error") is not None or spec.get("exception") is not None:
            kind = "stub_raise"
        elif spec.get("path") is not None or spec.get("target") is not None:
            kind = "setattr"
    path = spec.get("path") or spec.get("target") or spec.get("attr") or spec.get("name")

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

    match kind:
        case "setattr" | "assign" | "set":
            return _setattr_hook
        case "stub_return" | "return" | "returns":
            return _stub_return_hook
        case "stub_raise" | "raise" | "raises":
            return _stub_raise_hook
        case _:
            raise ValueError(f"unsupported scenario kind {kind!r}")


def _expand_object_scenario_hooks(hook: Any) -> tuple[Any, ...]:
    """Normalize a scenario entry into one or more executable hooks."""
    if hook is None:
        return ()
    if callable(hook) or isinstance(hook, (str, bytes)):
        return (hook,)
    if isinstance(hook, Mapping):
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
    result = _call_sync(hook, instance)
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
                ),
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
) -> Any:
    """Build a sync wrapper that creates a fresh object per invocation."""
    target = _unwrap(method)
    lifecycle_phase = _lifecycle_phase(method_name, target)

    @functools.wraps(target)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        instance = _call_sync(factory)
        result: Any = None
        error: BaseException | None = None
        before_state = _snapshot_instance_state(instance)
        call_args = tuple(args)
        call_kwargs = dict(kwargs)
        teardown_called = False
        teardown_error: str | None = None
        probe_cleanup: Callable[[], None] | None = None
        probe_context: dict[str, Any] = {}
        fault_names = tuple(getattr(wrapped, "__ordeal_contract_faults__", ()))
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
                probe_cleanup, probe_context = _instance_probe_result(
                    getattr(wrapped, "__ordeal_instance_probe__", None),
                    instance=instance,
                    owner=owner,
                    method_name=method_name,
                )
                instance = _apply_instance_hook(instance, runtime_setup)
                instance = _apply_instance_hooks(instance, scenarios)
                before_state = _snapshot_instance_state(instance)
                bound = getattr(instance, method_name)
                call_args, call_kwargs = _prepare_bound_method_call(
                    target,
                    args,
                    kwargs,
                    instance=instance,
                    state_factory=state_factory,
                    state_param=state_param,
                )
                result = _call_sync(bound, *call_args, **call_kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                if runtime_teardown is not None:
                    teardown_called = True
                    try:
                        _call_with_optional_instance_arg(runtime_teardown, instance)
                    except BaseException as exc:
                        teardown_error = f"{type(exc).__name__}: {exc}"
                        if error is None:
                            error = exc
                            raise
                wrapped.__ordeal_last_call_context__ = {
                    "instance": instance,
                    "before_state": before_state,
                    "after_state": _snapshot_instance_state(instance),
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
    wrapped.__ordeal_setup__ = setup
    wrapped.__ordeal_scenario__ = (scenarios or (None,))[0]
    wrapped.__ordeal_scenarios__ = tuple(scenarios or ())
    wrapped.__ordeal_state_factory__ = state_factory
    wrapped.__ordeal_state_param__ = state_param
    wrapped.__ordeal_teardown__ = teardown
    wrapped.__ordeal_harness__ = harness
    wrapped.__ordeal_kind__ = "instance"
    wrapped.__ordeal_lifecycle_phase__ = lifecycle_phase
    wrapped.__ordeal_keep_wrapped__ = True
    wrapped.__ordeal_instance_probe__ = None
    wrapped.__ordeal_harness_hints__ = tuple(
        _mine_object_harness_hints(owner.__module__, owner.__name__, method_name)
    )
    return wrapped


def _make_unbound_method_placeholder(
    owner: type,
    method_name: str,
    method: Any,
    *,
    state_param: str | None = None,
    state_factory: Any | None = None,
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
    wrapped.__ordeal_state_factory__ = state_factory
    wrapped.__ordeal_state_param__ = state_param
    wrapped.__ordeal_lifecycle_phase__ = _lifecycle_phase(method_name, target)
    wrapped.__ordeal_skip_reason__ = "missing object factory"
    wrapped.__ordeal_keep_wrapped__ = True
    wrapped.__ordeal_instance_probe__ = None
    wrapped.__ordeal_harness_hints__ = tuple(
        _mine_object_harness_hints(owner.__module__, owner.__name__, method_name)
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
    if not targets:
        return discovered

    discovered_map = {name: func for name, func in discovered}
    selected: list[tuple[str, Any]] = []
    seen: set[str] = set()

    for raw_target in targets:
        target = str(raw_target).strip()
        if not target:
            continue
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
    variants = (
        name,
        f"{module_name}.{name}",
        f"{module_name}:{name}",
    )
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


def shell_safe_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a shell-safety probe for command construction helpers."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            return False
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


def quoted_paths_contract(
    *,
    kwargs: dict[str, Any],
    tracked_params: Sequence[str] | None = None,
) -> ContractCheck:
    """Build a path-quoting probe for command builders."""

    def predicate(value: Any) -> bool:
        tokens = _command_tokens(value)
        if tokens is None:
            return False
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
            return False
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

    def predicate(value: Any) -> bool:
        if isinstance(value, Mapping):
            return _mapping_is_httpish(value)
        return isinstance(value, (str, bytes, tuple, list))

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
) -> tuple[list[ContractCheck], list[str]]:
    """Infer built-in sink-aware contract probes for *func* from source and seeds."""
    sink_categories = _infer_sink_categories(func)

    enabled = set(auto_contracts or _DEFAULT_AUTO_CONTRACTS)
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
        if name not in enabled or not probe_kwargs:
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
            except Exception:
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

    return strategies if strategies else None


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
                if resolved in seen or not resolved.is_file():
                    continue
                seen.add(resolved)
                candidates.append(resolved)
    return sorted(candidates)


def _camel_case_tokens(text: str) -> list[str]:
    """Split one identifier into coarse searchable tokens."""
    return [token.lower() for token in re.findall(r"[A-Z]?[a-z]+|[0-9]+", text) if token]


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
                if resolved in seen or not resolved.is_file():
                    continue
                seen.add(resolved)
                candidates.append(resolved)
    return sorted(candidates)


@functools.lru_cache(maxsize=128)
def _mine_object_harness_hints(
    module_name: str,
    class_name: str,
    method_name: str,
) -> tuple[HarnessHint, ...]:
    """Mine likely factory/state/teardown/client hooks from tests and docs."""
    class_tokens = {class_name.lower(), *(_camel_case_tokens(class_name))}
    method_tokens = {method_name.lower(), *(_camel_case_tokens(method_name))}
    target_tokens = class_tokens | method_tokens
    hints: list[HarnessHint] = []
    seen: set[tuple[str, str]] = set()

    def _add_hint(
        kind: str,
        suggestion: str,
        evidence: str,
        confidence: float,
        *,
        config: dict[str, Any] | None = None,
    ) -> None:
        key = (kind, suggestion)
        if key in seen:
            return
        seen.add(key)
        hints.append(
            HarnessHint(
                kind=kind,
                suggestion=suggestion,
                evidence=evidence,
                confidence=confidence,
                config=dict(config or {}),
            )
        )

    support_files = list(_callable_seed_files(module_name))
    extra_patterns = ("*factory*.py", "*fixture*.py", "*support*.py", "conftest.py")
    for root in _test_search_roots(module_name):
        for pattern in extra_patterns:
            for path in root.rglob(pattern):
                resolved = path.resolve()
                if resolved.is_file() and resolved not in support_files:
                    support_files.append(resolved)

    for path in sorted(dict.fromkeys(support_files)):
        tree = _parse_python_source(str(path))
        if tree is None:
            continue
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
            is_fixture = any(
                _call_name(decorator.func) == "pytest.fixture"
                if isinstance(decorator, ast.Call)
                else _call_name(decorator) == "pytest.fixture"
                for decorator in node.decorator_list
            )
            evidence = f"{display_path}:{getattr(node, 'lineno', '?')}"
            mentions_target = any(token in text_lower for token in target_tokens)

            if mentions_target and (
                name_lower.startswith(("make_", "build_", "create_", "new_"))
                or "factory" in name_lower
            ):
                _add_hint(
                    "factory",
                    f"[[objects]] factory -> {evidence}:{node.name}",
                    evidence,
                    0.9,
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "factory",
                        "value": f"{evidence}:{node.name}",
                    },
                )
            if mentions_target and "state" in text_lower:
                _add_hint(
                    "state_factory",
                    f"[[objects]] state_factory -> {evidence}:{node.name}",
                    evidence,
                    0.85,
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "state_factory",
                        "value": f"{evidence}:{node.name}",
                    },
                )
            if mentions_target and any(
                token in text_lower for token in ("teardown", "cleanup", "close", "stop")
            ):
                _add_hint(
                    "teardown",
                    f"[[objects]] teardown -> {evidence}:{node.name}",
                    evidence,
                    0.8,
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "teardown",
                        "value": f"{evidence}:{node.name}",
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
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": [f"{evidence}:{node.name}"],
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
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "state_factory",
                        "value": "docs mention state setup for this target",
                    },
                )
            if any(token in line for token in ("teardown", "cleanup", "close", "stop")):
                _add_hint(
                    "teardown",
                    "docs mention lifecycle teardown/cleanup",
                    evidence,
                    0.55,
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
                    config={
                        "section": "[[objects]]",
                        "target": f"{module_name}:{class_name}",
                        "method": method_name,
                        "key": "scenarios",
                        "value": ["docs mention a client/session collaborator"],
                    },
                )

    return tuple(
        sorted(
            hints,
            key=lambda item: (-float(item.confidence), item.kind, item.suggestion),
        )
    )


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
        if not _is_simple_literal_node(arg):
            return None
        kwargs[param.name] = _literal_seed_value(arg)

    for keyword in call.keywords:
        if keyword.arg is None or not _is_simple_literal_node(keyword.value):
            return None
        kwargs[keyword.arg] = _literal_seed_value(keyword.value)

    for param in params:
        if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            return None
        if param.name not in kwargs:
            if param.default is inspect.Parameter.empty:
                return None
            kwargs[param.name] = param.default
    return kwargs


@functools.lru_cache(maxsize=128)
def _test_seed_examples(module_name: str, leaf_name: str) -> tuple[SeedExample, ...]:
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
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _call_matches_target(
                node,
                leaf_name=leaf_name,
                module_aliases=module_aliases,
                function_aliases=function_aliases,
            ):
                continue
            kwargs = _call_kwargs_from_ast(node, signature=signature)
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
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
) -> list[dict[str, Any]]:
    """Build deterministic boundary and observed inputs for one callable."""
    target = _unwrap(func)
    seeds: list[dict[str, Any]] = [
        dict(example.kwargs)
        for example in _seed_examples_for_callable(
            target,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
        )
    ]

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
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
) -> list[CandidateInput]:
    """Return deterministic candidate inputs with provenance metadata."""
    target = _unwrap(func)
    candidates: list[CandidateInput] = []
    seen: set[str] = set()

    for example in _seed_examples_for_callable(
        target,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
    ):
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

    for kwargs in _boundary_smoke_inputs(
        target,
        fixtures=fixtures,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
    ):
        key = repr(sorted(kwargs.items()))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(CandidateInput(kwargs=dict(kwargs), origin="boundary"))

    if mutate_observed_inputs:
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
) -> dict[str, Any] | None:
    """Return a detail dict when *exc* matches a documented precondition."""
    doc = inspect.getdoc(func) or ""
    lowered_doc = doc.lower()
    if "raise" not in lowered_doc:
        return None

    exc_name = type(exc).__name__
    exc_name_lower = exc_name.lower()
    if exc_name_lower not in lowered_doc:
        return None

    message = str(exc)
    message_tokens = {
        token
        for token in re.findall(r"[a-z_]{4,}", message.lower())
        if token not in _DOC_STOPWORDS
    }
    doc_tokens = set(re.findall(r"[a-z_]{4,}", lowered_doc))
    param_names = {name.lower() for name in kwargs}

    if not (message_tokens & doc_tokens) and not (param_names & doc_tokens):
        return None

    summary = f"expected precondition failure: {exc_name}: {message}"
    return {
        "kind": "precondition",
        "category": "expected_precondition_failure",
        "summary": summary[:300],
        "error": message[:300],
        "error_type": exc_name,
        "failing_args": dict(kwargs),
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
        case "message":
            return 1.0 if isinstance(value, (str, Mapping, list, tuple)) else 0.0
        case "numeric":
            return 1.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        case "collection":
            return 1.0 if isinstance(value, (dict, list, tuple, set, frozenset)) else 0.0
        case _:
            return 0.5


def _likely_contract_profile(
    func: Any,
    *,
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
        "sink_categories": _infer_sink_categories(target),
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
        realism_scores.append(semantic_score)
        sink_scores.append(1.0 if semantic in {"path", "shell", "json", "mapping"} else 0.0)
        score += (semantic_score - 0.5) * 0.4
        fit_scores.append(min(max(score, 0.0), 1.0))

    contract_fit = sum(fit_scores) / len(fit_scores)
    if any(getattr(example, "kwargs", None) == dict(kwargs) for example in seed_examples):
        contract_fit = min(contract_fit + 0.15, 1.0)
        reasons.append("matches a concrete seed from tests/docs/code")
    realism = sum(realism_scores) / len(realism_scores) if realism_scores else 0.0
    sink_signal = max(sink_scores, default=0.0)
    return contract_fit, realism, sink_signal, reasons[:6]


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
    if contract_fit <= _WEAK_CONTRACT_FIT or realism < 0.35:
        return "invalid_input_crash"
    return "coverage_gap" if mode == "coverage_gap" else "speculative_crash"


def _likely_impact(category: str, sink_signal: float) -> str:
    """Describe likely impact for a crash report."""
    if sink_signal >= 1.0:
        return "reaches a path/shell/json/env shaping sink with a contract-valid input."
    if category == "coverage_gap":
        return "the input looks partially valid, but current evidence points to missing coverage."
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


def _proof_demotion_reason(
    *,
    category: str,
    replayable: bool,
    contract_fit: float,
    reachability: float,
    realism: float,
    min_contract_fit: float,
    min_reachability: float,
    min_realism: float,
) -> str | None:
    """Return the concrete reason a finding was not promoted as a real bug."""
    if category in {"likely_bug", "semantic_contract", "lifecycle_contract"}:
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
    if category == "invalid_input_crash":
        reasons.append("the crash still looks driven by out-of-contract input")
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
            f"uv run ordeal scan {module_name} --mode real_bug --targets {explicit_target} -n 1"
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
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    harness_mode: str | None = None,
    callable_kind: str | None = None,
    contract_check: str | None = None,
) -> dict[str, Any]:
    """Build the proof payload carried through reports and agent output."""
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
    demotion_reason = _proof_demotion_reason(
        category=category,
        replayable=replayable,
        contract_fit=contract_fit,
        reachability=reachability,
        realism=realism,
        min_contract_fit=min_contract_fit,
        min_reachability=min_reachability,
        min_realism=min_realism,
    )
    witness = {
        "input": _json_ready_proof(dict(failing_args)),
        "source": input_source,
        "seed_sources": matched_sources,
        "supporting_evidence": supporting_evidence,
    }
    contract_basis = {
        "category": category,
        "fit": round(contract_fit, 4),
        "reachability": round(reachability, 4),
        "realism": round(realism, 4),
        "fixture_completeness": round(fixture_completeness, 4),
        "basis": list(rationale),
        "likely_contract": _json_ready_proof(profile.get("params", {})),
        "supporting_evidence": supporting_evidence,
        "input_source": input_source,
        "matched_seed_sources": matched_sources,
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
        _sink_likely_impact(sink_categories, error)
        if sink_categories and error is not None
        else _likely_impact(category, sink_signal)
    )
    minimal_reproduction = _proof_minimal_reproduction(
        qualname=qualname,
        failing_args=failing_args,
        profile=profile,
        harness_mode=harness_mode,
        callable_kind=callable_kind,
        contract_check=contract_check,
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
                else (list(sink_categories)[0] if sink_categories else category)
            ),
            "sink_categories": list(sink_categories),
        },
        "sink_categories": list(sink_categories),
        "likely_impact": impact_summary,
        "verdict": {
            "category": category,
            "promoted": category in {"likely_bug", "semantic_contract", "lifecycle_contract"},
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


def _evaluate_contract_checks(
    func: Any,
    contract_checks: list[ContractCheck] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run explicit contract probes against *func* and collect violations."""
    if not contract_checks:
        return [], []

    violations: list[str] = []
    details: list[dict[str, Any]] = []
    profile = _likely_contract_profile(func)
    qualname = str(profile.get("qualname", getattr(func, "__qualname__", "?")))
    for check in contract_checks:
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
        runtime_faults = [
            str(item)
            for item in list(metadata.get("runtime_faults", []) or [])
            if str(item).strip()
        ]
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
        except Exception as exc:
            passed = False
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None if error_obj is None else f"{type(error_obj).__name__}: {error_obj}"

        if passed:
            continue

        summary = check.summary or f"explicit contract failed: {check.name}"
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
            "input_source": "explicit_contract",
        }
        if call_context:
            detail["lifecycle_phase"] = call_context.get("lifecycle_phase")
            detail["lifecycle_probe"] = call_context.get("lifecycle_probe")
            detail["teardown_called"] = call_context.get("teardown_called")
            detail["teardown_error"] = call_context.get("teardown_error")
            detail["lifecycle_runtime"] = call_context.get("lifecycle_runtime")
        if error is not None:
            detail["error"] = error[:300]
            if error_obj is not None:
                detail["error_type"] = type(error_obj).__name__
        detail["proof_bundle"] = _build_proof_bundle(
            qualname=qualname,
            error=error_obj,
            failing_args=kwargs,
            input_source="explicit_contract",
            contract_fit=contract_fit,
            reachability=1.0,
            realism=realism,
            rationale=rationale,
            replayable=True,
            replay_attempts=1,
            replay_matches=1,
            category=detail_category,
            profile=profile,
            sink_signal=max(sink_signal, 1.0),
            sink_categories=profile.get("sink_categories", ()),
            min_contract_fit=0.0,
            min_reachability=0.0,
            min_realism=0.0,
            harness_mode=getattr(func, "__ordeal_harness__", None),
            callable_kind=getattr(func, "__ordeal_kind__", None),
            contract_check=check.name,
        )
        if call_context and call_context.get("lifecycle_probe") is not None:
            detail["proof_bundle"]["lifecycle"] = dict(call_context["lifecycle_probe"])
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
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    contract_checks: dict[str, list[ContractCheck]] | None = None,
    mode: ScanMode = "coverage_gap",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: Sequence[str] | None = None,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
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
        ignore_properties: Property names to suppress in mined warnings.
        ignore_relations: Relation names to suppress in mined warnings.
        property_overrides: Per-function property suppressions.
        relation_overrides: Per-function relation suppressions.
        contract_checks: Explicit semantic contract probes keyed by
            callable name. Each probe runs with explicit ``kwargs`` and
            reports a contract violation when its predicate fails.
        mode: ``"coverage_gap"`` promotes plausible crashes plus gaps;
            ``"real_bug"`` promotes only high-fit bug candidates.
        seed_from_tests: Learn valid input shapes from adjacent pytest files.
        seed_from_fixtures: Mine literal pytest fixture returns as seed inputs.
        seed_from_docstrings: Mine doctest-like examples from docstrings.
        seed_from_code: Mine boundary values from code patterns.
        seed_from_call_sites: Mine literal examples from adjacent call sites.
        treat_any_as_weak: Penalize broad or missing hints instead of trusting them.
        proof_bundles: Attach structured proof payloads to crash findings.
        auto_contracts: Auto-enable sink-aware semantic checks for shell/path/env/json/http.
        require_replayable: Require replayability before promoting a bug candidate.
        min_contract_fit: Minimum inferred contract-fit score to promote.
        min_reachability: Minimum reachability score to promote.
        min_realism: Minimum semantic realism score to promote.
    """
    if mode not in _VALID_SCAN_MODES:
        raise ValueError(f"mode must be one of {_VALID_SCAN_MODES}, got {mode!r}")
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
                    *(property_overrides or {}).get(name, []),
                }
            ),
            ignore_relations=sorted(
                {
                    *(ignore_relations or []),
                    *(relation_overrides or {}).get(name, []),
                }
            ),
            property_overrides=property_overrides,
            relation_overrides=relation_overrides,
            contract_checks=(contract_checks or {}).get(name),
            mode=mode,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            treat_any_as_weak=treat_any_as_weak,
            proof_bundles=proof_bundles,
            auto_contracts=auto_contracts,
            require_replayable=require_replayable,
            min_contract_fit=min_contract_fit,
            min_reachability=min_reachability,
            min_realism=min_realism,
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
    mode: ScanMode = "coverage_gap",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: Sequence[str] | None = None,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
) -> FunctionResult:
    """Run no-crash + return-type + mined-property checks on a single function."""

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
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
    )
    seed_examples = list(profile.get("seed_examples", []))
    strategies = _bias_strategies_with_seed_examples(strategies, seed_examples)
    auto_checks, sink_categories = _auto_contract_checks(
        func,
        seed_examples,
        auto_contracts=auto_contracts,
    )
    effective_contract_checks = [*(contract_checks or []), *auto_checks]

    def _origin_for_kwargs(kwargs: Mapping[str, Any], fallback: str) -> str:
        for example in profile.get("seed_examples", []):
            if dict(example.kwargs) == dict(kwargs):
                return example.source
        return fallback

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
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
        ):
            last_kwargs = dict(candidate.kwargs)
            last_input_source = _origin_for_kwargs(candidate.kwargs, candidate.origin)
            result = _call_sync(func, **dict(candidate.kwargs))
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(
                        f"Expected return type {return_type}, "
                        f"got {type(result).__name__}: {result!r}"
                    )

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def test(**kwargs: Any) -> None:
            nonlocal last_kwargs
            nonlocal last_input_source
            last_kwargs = dict(kwargs)
            last_input_source = _origin_for_kwargs(kwargs, "random_fuzz")
            result = _call_sync(func, **kwargs)
            if check_return_type and return_type is not None:
                if not _type_matches(result, return_type):
                    raise AssertionError(
                        f"Expected return type {return_type}, "
                        f"got {type(result).__name__}: {result!r}"
                    )

        test()
    except Exception as e:
        precondition = _documented_precondition_failure(func, e, last_kwargs)
        if precondition is not None:
            return FunctionResult(
                name=name,
                passed=True,
                error_type=precondition["error_type"],
                failing_args=last_kwargs or None,
                contract_violations=[str(precondition["summary"])],
                contract_violation_details=[precondition],
            )
        replayable, replay_attempts, replay_matches = _replay_failure(e)
        contract_fit, realism, sink_signal, rationale = _score_contract_fit(last_kwargs, profile)
        reachability = _reachability_score(last_input_source, last_kwargs, profile)
        crash_category = _classify_crash(
            mode=mode,
            replayable=replayable,
            contract_fit=contract_fit,
            reachability=reachability,
            realism=realism,
            min_contract_fit=min_contract_fit,
            min_reachability=min_reachability,
            min_realism=min_realism,
            require_replayable=require_replayable,
        )
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
                min_contract_fit=min_contract_fit,
                min_reachability=min_reachability,
                min_realism=min_realism,
                harness_mode=getattr(func, "__ordeal_harness__", None),
                callable_kind=getattr(func, "__ordeal_kind__", None),
            )
            call_context = getattr(func, "__ordeal_last_call_context__", None)
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
        return FunctionResult(
            name=name,
            passed=False,
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

    # Mine properties to detect semantic anomalies (not just crashes)
    violations: list[str] = []
    details: list[dict[str, Any]] = []
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

    contract_violations, contract_details = _evaluate_contract_checks(
        func,
        effective_contract_checks,
    )
    return FunctionResult(
        name=name,
        passed=True,
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
    seen: set[str] = set()

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
                    key = f"{fault_mod}.{fault_fn}"
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

            # Cross-function calls → error_on_call
            if call_str.startswith(mod_name + ".") and call_str not in seen:
                seen.add(call_str)
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
