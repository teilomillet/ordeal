from __future__ import annotations
# ruff: noqa
import ast
import contextlib
import copy
import functools
import hashlib
import importlib
import importlib.machinery
import inspect
import json
import os
import pkgutil
import sys
import textwrap
import time
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    Sequence,
    Union,
    get_args,
    get_origin,
)
from ordeal.faults import PatchFault
from ordeal.introspection import annotation_is_none, safe_get_annotations
if TYPE_CHECKING:
    from ordeal.mine import MinedProperty, MineResult
# ============================================================================
# Helpers
# ============================================================================

ValidationMode = Literal["fast", "deep"]
def _normalize_validation_mode(validation_mode: str) -> ValidationMode:
    """Validate how mined properties should be checked against mutants."""
    match validation_mode:
        case "fast" | "deep":
            return validation_mode
        case _:
            raise ValueError(
                f"validation_mode must be 'fast' or 'deep', got {validation_mode!r}",
            )
@contextmanager
def _timed_phase(timings: dict[str, float], name: str) -> Callable[[], None]:
    """Accumulate elapsed wall time for a named mutation phase."""
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - start)
@contextmanager
def _disable_seed_replay() -> Callable[[], None]:
    """Suppress seed replay while preserving an enclosing pytest identity."""
    previous = os.environ.get("ORDEAL_DISABLE_SEED_REPLAY")
    previous_pytest_item = os.environ.get("PYTEST_CURRENT_TEST")
    os.environ["ORDEAL_DISABLE_SEED_REPLAY"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ORDEAL_DISABLE_SEED_REPLAY", None)
        else:
            os.environ["ORDEAL_DISABLE_SEED_REPLAY"] = previous
        if previous_pytest_item is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = previous_pytest_item
def _unwrap_func(func: object) -> object:
    """Unwrap decorated/wrapped functions to reach the original source.

    Handles ``inspect.unwrap`` (follows ``__wrapped__`` chains),
    Ray's ``@ray.remote`` (stores the real function in ``._function``),
    Celery-style ``task.run`` patterns, staticmethod/classmethod
    (``__func__``), property (``fget``), and ``functools.partial``.
    """
    # Ray @ray.remote stores the original in ._function
    if hasattr(func, "_function"):
        func = func._function
    # staticmethod / classmethod → __func__
    if hasattr(func, "__func__"):
        func = func.__func__
    # property → fget
    if isinstance(func, property) and func.fget is not None:
        func = func.fget
    # functools.partial → .func
    if hasattr(func, "func") and isinstance(func, functools.partial):
        func = func.func
    # Celery-style task.run
    if hasattr(func, "run") and callable(getattr(func, "run", None)):
        candidate = func.run
        if hasattr(candidate, "__code__"):
            func = candidate
    # Standard unwrap (__wrapped__ chains from functools.wraps)
    try:
        func = inspect.unwrap(func)
    except (ValueError, TypeError):
        pass
    return func
@dataclass(frozen=True)
class _ResolvedMutationTarget:
    """Resolved callable target used by mutation execution paths."""

    target: str
    module: types.ModuleType
    owner: object
    attr_name: str | None
    qualname_parts: tuple[str, ...] = ()

    @property
    def module_name(self) -> str:
        return self.module.__name__

    @property
    def is_module(self) -> bool:
        return isinstance(self.owner, types.ModuleType)

    @property
    def leaf_name(self) -> str | None:
        return self.attr_name
def _target_to_normalized_dotted(target: str) -> str:
    """Normalize explicit module:Class.method targets to dotted form."""
    return target.replace(":", ".", 1) if ":" in target else target
def _local_module_exists(module_name: str) -> bool:
    """Return whether *module_name* exists in the current project layout."""
    parts = module_name.split(".")
    roots = [Path.cwd(), Path.cwd() / "src"]
    for root in roots:
        module_path = root.joinpath(*parts)
        if module_path.with_suffix(".py").exists():
            return True
        if (module_path / "__init__.py").exists():
            return True
    return False
def _find_spec_from_sys_path(module_name: str) -> importlib.machinery.ModuleSpec | None:
    """Resolve a module spec from ``sys.path`` without trusting ``sys.modules``."""
    parts = [part for part in module_name.split(".") if part]
    if not parts:
        return None

    fullname = parts[0]
    spec = importlib.machinery.PathFinder.find_spec(fullname)
    for part in parts[1:]:
        if spec is None or spec.submodule_search_locations is None:
            return None
        fullname = f"{fullname}.{part}"
        spec = importlib.machinery.PathFinder.find_spec(
            fullname,
            spec.submodule_search_locations,
        )
    return spec
def _normalized_module_origin(origin: object) -> Path | None:
    """Return a normalized module origin path when available."""
    if not isinstance(origin, str) or origin in {"built-in", "frozen"}:
        return None
    try:
        return Path(origin).resolve()
    except OSError:
        return None
def _purge_module_family(module_name: str) -> None:
    """Drop a module and its package family from ``sys.modules``."""
    parts = [part for part in module_name.split(".") if part]
    prefixes = [".".join(parts[:idx]) for idx in range(1, len(parts) + 1)]
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(name, None)
def _import_module_current(module_name: str) -> types.ModuleType:
    """Import a module, refreshing stale local modules shadowed on ``sys.path``."""
    importlib.invalidate_caches()
    parts = [part for part in module_name.split(".") if part]
    for index in range(1, len(parts) + 1):
        prefix = ".".join(parts[:index])
        cached_prefix = sys.modules.get(prefix)
        if cached_prefix is None:
            continue
        desired_prefix = _find_spec_from_sys_path(prefix)
        desired_origin = _normalized_module_origin(getattr(desired_prefix, "origin", None))
        cached_origin = _normalized_module_origin(getattr(cached_prefix, "__file__", None))
        if desired_origin is not None and cached_origin != desired_origin:
            _purge_module_family(module_name)
            break

    cached = sys.modules.get(module_name)
    spec = _find_spec_from_sys_path(module_name)
    desired_origin = _normalized_module_origin(getattr(spec, "origin", None))
    cached_origin = _normalized_module_origin(getattr(cached, "__file__", None))

    if cached is not None and desired_origin is not None and cached_origin != desired_origin:
        _purge_module_family(module_name)
        cached = None

    if cached is not None:
        return cached
    return importlib.import_module(module_name)
def _resolve_mutation_owner(target: str) -> tuple[Any, str]:
    """Resolve the owner object for a function or method mutation target."""
    if "." not in target:
        raise ValueError(f"Mutation target must be dotted, got {target!r}")

    parent_path, attr_name = target.rsplit(".", 1)
    try:
        parent = _import_module_current(parent_path)
        return parent, attr_name
    except ImportError:
        pass

    parts = parent_path.split(".")
    for idx in range(len(parts), 0, -1):
        try:
            obj: Any = _import_module_current(".".join(parts[:idx]))
            for part in parts[idx:]:
                obj = getattr(obj, part)
            return obj, attr_name
        except (ImportError, AttributeError):
            continue

    raise ImportError(f"Cannot resolve target: {target!r}")
def _resolve_mutation_target(target: str) -> _ResolvedMutationTarget:
    """Resolve a module, function, or method mutation target explicitly."""
    normalized = _target_to_normalized_dotted(target)
    try:
        module = _import_module_current(normalized)
    except ImportError:
        parent, attr_name = _resolve_mutation_owner(normalized)
        if isinstance(parent, types.ModuleType):
            return _ResolvedMutationTarget(target, parent, parent, attr_name)

        obj = getattr(parent, attr_name)
        if inspect.isclass(obj):
            raise ValueError(
                f"Mutation target must be a function or method, got class target {target!r}"
            )
        if not callable(obj):
            raise ValueError(
                f"Mutation target must resolve to a callable, got {type(obj).__name__}"
            )

        module = inspect.getmodule(parent)
        if module is None:
            module_name = getattr(parent, "__module__", None)
            if not module_name:
                raise ImportError(f"Cannot resolve module for mutation target {target!r}")
            module = importlib.import_module(module_name)

        qualname = getattr(parent, "__qualname__", "")
        qual_parts = tuple(part for part in qualname.split(".") if part and part != "<locals>")
        return _ResolvedMutationTarget(target, module, parent, attr_name, qual_parts)

    return _ResolvedMutationTarget(target, module, module, None)
def _resolved_target_callable(target_spec: _ResolvedMutationTarget) -> Any:
    """Return the actual callable identified by *target_spec*."""
    if target_spec.leaf_name is None:
        return target_spec.module
    return getattr(target_spec.owner, target_spec.leaf_name)
def _mutation_target_display(target: str) -> tuple[str, bool]:
    """Render a mutation target with method/class context when available."""
    try:
        target_spec = _resolve_mutation_target(target)
    except Exception:
        return target, False

    if target_spec.leaf_name is None:
        return target_spec.module_name, False

    qualified = ".".join([*target_spec.qualname_parts, target_spec.leaf_name])
    return f"{target_spec.module_name}.{qualified}", bool(target_spec.qualname_parts)
def _resolve_dotted_attr(obj: Any, dotted: str) -> Any | None:
    """Resolve a dotted attribute path against *obj*."""
    current: Any = obj
    for part in dotted.split("."):
        try:
            current = getattr(current, part)
        except AttributeError:
            return None
    return current
def _get_source(func: object) -> str:
    """Extract source code for *func*, with file-based fallback.

    Tries ``inspect.getsource`` first.  When that fails (common for
    decorated callables whose wrapper is defined in C or lacks source
    metadata), falls back to reading the source file directly using
    ``__code__`` attributes.
    """
    try:
        return textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        pass

    # Fallback: read from __code__.co_filename / co_firstlineno
    code = getattr(func, "__code__", None)
    if code is None:
        raise OSError(
            f"Cannot retrieve source for {func!r}: "
            "inspect.getsource failed and object has no __code__ attribute"
        )

    filename = code.co_filename
    first_line = code.co_firstlineno  # 1-based

    try:
        with open(filename) as fh:
            lines = fh.readlines()
    except (OSError, TypeError) as exc:
        raise OSError(
            f"Cannot retrieve source for {func!r}: "
            f"inspect.getsource failed and could not read {filename!r}"
        ) from exc

    # Walk from the def/async def line until dedent signals end of function
    start = first_line - 1  # 0-based
    if start >= len(lines):
        raise OSError(f"Source line {first_line} is past end of {filename}")

    func_lines = [lines[start]]
    # Determine the indentation of the def line
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            func_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        func_lines.append(line)

    return textwrap.dedent("".join(func_lines))
class NoTestsFoundError(RuntimeError):
    """Raised when auto-discovery finds no tests for a mutation target.

    Attributes:
        target: The dotted path that was being tested.
        suggested_file: Recommended filename to save starter tests to.
    """

    def __init__(self, message: str, *, target: str = "", suggested_file: str = ""):
        super().__init__(message)
        self.target = target
        self.suggested_file = suggested_file
# ============================================================================
# Data structures
# ============================================================================


_REMEDIATION: dict[str, str] = {
    "arithmetic": (
        "Add an assertion that checks the exact numeric result of this expression.\n"
        "    Example: assert compute(3, 4) == 7  # catches + -> -\n"
        "    The surviving mutant changes the arithmetic operator, so a test that\n"
        "    verifies the precise output value (not just sign or range) will kill it."
    ),
    "comparison": (
        "Add a boundary test using the exact threshold value.\n"
        "    Example: test with x == boundary to distinguish < from <=\n"
        "    The surviving mutant shifts a comparison boundary, so test the\n"
        "    value exactly at the boundary where < and <= differ."
    ),
    "negate": (
        "Add a test that exercises the opposite branch of this condition.\n"
        "    The surviving mutant flips an if-condition; add a test case where\n"
        "    the condition is True and verify different behavior from when False."
    ),
    "return_none": (
        "Add an assertion that checks the return value is not None.\n"
        "    Example: result = func(...); assert result is not None\n"
        "    Also verify the return value's type or contents."
    ),
    "boundary": (
        "Add a test using the exact integer constant and its neighbors.\n"
        "    Example: if the code uses limit=10, test with 9, 10, and 11.\n"
        "    The surviving mutant shifts an integer by ±1."
    ),
    "constant": (
        "Add a test that verifies the exact constant value matters.\n"
        "    The surviving mutant replaces a number with 0, 1, or -1.\n"
        "    Test with inputs where the original constant produces a\n"
        "    meaningfully different result from the replacement."
    ),
    "delete_statement": (
        "Add a test that depends on the side effect of this statement.\n"
        "    The surviving mutant removes the statement entirely.\n"
        "    Verify the observable effect: updated state, return value,\n"
        "    or accumulated result that this statement contributes to."
    ),
    "logical": (
        "Add a test where exactly one of the two conditions is True.\n"
        "    The surviving mutant swaps 'and' with 'or' (or vice versa).\n"
        "    When both are True or both False, and/or are equivalent;\n"
        "    test with mixed True/False to distinguish them."
    ),
    "swap_if_else": (
        "Add a test that verifies the if-branch produces different output\n"
        "    from the else-branch, then assert the correct one is taken.\n"
        "    The surviving mutant swaps the two branches."
    ),
    "remove_not": (
        "Add a test where the negation changes the outcome.\n"
        "    The surviving mutant removes a 'not' operator.\n"
        "    Test with a value where the condition is True, ensuring the\n"
        "    negated version (False) produces different behavior."
    ),
    "exception_swallow": (
        "Add a test that verifies the except handler's body executes.\n"
        "    The surviving mutant replaces the handler body with 'pass'.\n"
        "    Assert on any side effect of the error handling logic."
    ),
    "argument_swap": (
        "Add a test where the first two arguments are different values\n"
        "    and the function is not commutative.\n"
        "    Example: assert f(a, b) != f(b, a), then check the correct one."
    ),
    "break_continue_swap": (
        "Add a test that verifies the loop exits (break) or continues\n"
        "    at the right point. Check the number of iterations or the\n"
        "    accumulated result to distinguish break from continue."
    ),
    "unary_negate": (
        "Add a test where the sign of the value matters.\n"
        "    The surviving mutant removes a unary minus.\n"
        "    Assert the exact (negative) value, not just its magnitude."
    ),
    "extra": (
        "This mutant was provided externally (by an AI assistant or human).\n"
        "    It may represent a subtle logic error, missed edge case, or wrong\n"
        "    variable usage that rule-based operators cannot produce.\n"
        "    Read the mutant description for the specific change, then add a test\n"
        "    that exercises the affected code path with an input that distinguishes\n"
        "    the original behavior from the mutated version."
    ),
    "llm": (
        "This mutant was generated by an LLM to mimic a realistic developer bug.\n"
        "    It may represent a subtle logic error, missed edge case, or wrong\n"
        "    variable usage that rule-based operators cannot produce.\n"
        "    Read the mutant description for the specific change, then add a test\n"
        "    that exercises the affected code path with an input that distinguishes\n"
        "    the original behavior from the mutated version."
    ),
}
@dataclass
class Mutant:
    """A single code mutation."""

    operator: str
    description: str
    line: int
    col: int
    killed: bool = False
    error: str | None = None
    source_line: str = ""
    killed_by: str | None = None
    qualname: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _mutant_source: str | None = field(default=None, repr=False)

    @property
    def location(self) -> str:
        """Source location as ``L<line>:<col>``."""
        return f"L{self.line}:{self.col}"

    @property
    def site_summary(self) -> str:
        """Return the exact source site for this mutant."""
        if self.source_line:
            return f"{self.location} | {self.source_line}"
        return self.location

    @property
    def report_label(self) -> str:
        """Return a compact, review-friendly label for this mutant."""
        return f"{self.site_summary} [{self.operator}] {self.description}"

    @property
    def remediation(self) -> str:
        """Actionable guidance for killing this mutant."""
        advice = _REMEDIATION.get(self.operator, "")
        if not advice:
            return f"Add a test that distinguishes the original from: {self.description}"
        return advice
def _target_semantic_tags(target: str) -> list[str]:
    """Infer semantic boundary tags from the explicit mutation target."""
    lowered = target.lower()
    tags: list[str] = []
    if any(token in lowered for token in {"cleanup", "teardown", "rollout", "setup", "stop"}):
        tags.append("lifecycle")
    if any(token in lowered for token in {"build_env", "env_vars", "sandbox", "shell", "argv"}):
        tags.append("contract_boundary")
    return tags
