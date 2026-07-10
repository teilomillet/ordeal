from __future__ import annotations
# ruff: noqa
def _measure_generated_coverage_with_coverage_py(
    module_name: str,
    test_files: list[Path],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure generated ordeal tests directly under the ``coverage.py`` API."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_direct_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import importlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import coverage

module_name = sys.argv[1]
json_path = sys.argv[2]
test_files = sys.argv[3:]

payload = {"return_code": 0, "coverage": None, "error": None}
cov_json = Path(tempfile.mkstemp(prefix="ordeal_cov_raw_", suffix=".json")[1])
cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)

try:
    cov.start()
    for index, test_path in enumerate(test_files):
        spec = importlib.util.spec_from_file_location(f"_ordeal_generated_{index}", test_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import generated test file: {test_path}")
        test_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test_module)
        for name in sorted(dir(test_module)):
            if not name.startswith("test_"):
                continue
            test_fn = getattr(test_module, name)
            if callable(test_fn):
                test_fn()
except Exception as exc:
    payload["return_code"] = 1
    payload["error"] = f"{type(exc).__name__}: {exc}"
finally:
    try:
        cov.stop()
    except Exception:
        pass

try:
    cov.json_report(outfile=str(cov_json))
    payload["coverage"] = json.loads(cov_json.read_text(encoding="utf-8"))
except Exception as exc:
    payload["error"] = payload["error"] or f"coverage JSON failed: {type(exc).__name__}: {exc}"
finally:
    cov_json.unlink(missing_ok=True)

Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_direct_cov_",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)

    cmd = [
        sys.executable,
        str(script_path),
        module_name,
        str(json_path),
        *[str(f) for f in test_files],
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="python not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    if raw.get("error"):
        return CoverageMeasurement(Status.FAILED, error=str(raw["error"]))
    return_code = int(raw.get("return_code", 0))
    if return_code not in (0, 1):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"generated coverage runner exited with code {return_code}",
        )
    coverage_json = raw.get("coverage")
    if not isinstance(coverage_json, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing payload")

    return _coverage_measurement_from_json(
        coverage_json,
        module_name,
        source="coverage.py API direct",
    )
def _coverage_measurement_from_json(
    raw: dict[str, object],
    module_name: str,
    *,
    source: str,
) -> CoverageMeasurement:
    """Build a CoverageMeasurement from coverage.py JSON output."""
    files_data = raw.get("files", {})
    if not isinstance(files_data, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing files section")

    mod_path = module_name.replace(".", "/")

    file_data = None
    for file_key, data in files_data.items():
        if mod_path in str(file_key):
            file_data = data
            break

    if not isinstance(file_data, dict):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"module {module_name} not found in coverage report",
        )

    summary = file_data.get("summary", {})
    if not isinstance(summary, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing summary")

    percent = float(summary.get("percent_covered", 0.0))
    total_stmts = int(summary.get("num_statements", 0))
    missing_raw = file_data.get("missing_lines", [])
    missing_lines = (
        frozenset(int(x) for x in missing_raw) if isinstance(missing_raw, list) else frozenset()
    )
    missing_count = summary.get("missing_lines", 0)

    return _build_verified_coverage(
        percent=percent,
        total_stmts=total_stmts,
        missing_lines=missing_lines,
        missing_count=int(missing_count) if isinstance(missing_count, int) else len(missing_lines),
        source=source,
    )
def _coverage_measurement_from_trace_payload(raw: dict[str, object]) -> CoverageMeasurement:
    """Build a CoverageMeasurement from the internal trace fallback payload."""
    percent = float(raw.get("percent_covered", 0.0))
    total_stmts = int(raw.get("num_statements", 0))
    missing_raw = raw.get("missing_lines", [])
    missing_lines = (
        frozenset(int(x) for x in missing_raw) if isinstance(missing_raw, list) else frozenset()
    )

    return _build_verified_coverage(
        percent=percent,
        total_stmts=total_stmts,
        missing_lines=missing_lines,
        missing_count=len(missing_lines),
        source="stdlib trace fallback",
    )
def _build_verified_coverage(
    *,
    percent: float,
    total_stmts: int,
    missing_lines: frozenset[int],
    missing_count: int,
    source: str,
) -> CoverageMeasurement:
    """Cross-check a coverage payload and wrap it as a verified result."""
    if total_stmts > 0:
        computed = (total_stmts - len(missing_lines)) / total_stmts * 100
        if abs(computed - percent) > 1.0:
            return CoverageMeasurement(
                Status.FAILED,
                error=(
                    f"coverage data inconsistent: reported {percent:.1f}% "
                    f"but computed {computed:.1f}% from "
                    f"{total_stmts} stmts - {len(missing_lines)} missing"
                ),
            )

    return CoverageMeasurement(
        Status.VERIFIED,
        result=CoverageResult(
            percent=percent,
            total_statements=total_stmts,
            missing_count=missing_count,
            missing_lines=missing_lines,
            source=source,
        ),
    )
# ============================================================================
# Test suggestions — from coverage gap analysis
# ============================================================================


def _suggest_tests(
    module_name: str,
    current_missing: frozenset[int],
    migrated_missing: frozenset[int],
) -> list[str]:
    """Generate test suggestions for lines covered by current but not migrated.

    Reads the source at each gap line and describes what to test.

    **How it works:**

    1. Compute ``gap = migrated_missing - current_missing``
       (lines that current tests cover but migrated tests don't)
    2. Group consecutive gap lines into blocks (within LINE_BLOCK_GAP)
    3. For each block, read the source and identify the construct
       (if/elif, return, raise, for, assignment)
    4. Find the enclosing function name by scanning backwards

    **Limitations:**

    - Uses string matching on source, not AST. Can match keywords
      inside strings or comments.
    - Assumes line numbers are still valid (source hasn't changed
      since coverage was measured).
    - Capped at MAX_SUGGESTIONS to avoid overwhelming the user.
    """
    gap_lines = migrated_missing - current_missing
    if not gap_lines:
        return []

    try:
        mod = _resolve_module(module_name)
        source_file = getattr(mod, "__file__", None)
        if source_file is None:
            return [f"cannot suggest: {module_name} has no __file__"]
        source = Path(source_file).read_text(encoding="utf-8").splitlines()
    except (ImportError, OSError) as exc:
        return [f"cannot suggest: {exc}"]

    suggestions: list[str] = []
    sorted_lines = sorted(gap_lines)

    # Group consecutive lines into blocks
    blocks: list[list[int]] = []
    current_block: list[int] = []
    for ln in sorted_lines:
        if current_block and ln > current_block[-1] + LINE_BLOCK_GAP:
            blocks.append(current_block)
            current_block = [ln]
        else:
            current_block.append(ln)
    if current_block:
        blocks.append(current_block)

    for block in blocks[:MAX_SUGGESTIONS]:
        first = block[0]
        if first - 1 >= len(source):
            suggestions.append(f"L{first}: line number out of range (source may have changed)")
            continue

        line_text = source[first - 1].strip()

        # Find enclosing function by scanning backwards for "def "
        func_name = "<module>"
        for i in range(first - 1, -1, -1):
            stripped = source[i].strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                func_name = stripped.split("(")[0].replace("def ", "").replace("async ", "")
                break

        # Describe the construct
        if "if " in line_text or "elif " in line_text:
            condition = line_text.split("if ", 1)[-1].rstrip(":")
            suggestions.append(f"L{first} in {func_name}(): test when {condition}")
        elif "return " in line_text:
            suggestions.append(f"L{first} in {func_name}(): test input that triggers this return")
        elif "raise " in line_text:
            exc_type = line_text.split("raise ", 1)[-1].split("(")[0]
            suggestions.append(f"L{first} in {func_name}(): test that {exc_type} is raised")
        elif "for " in line_text:
            suggestions.append(f"L{first} in {func_name}(): test with non-empty input for loop")
        else:
            suggestions.append(
                f"L{first} in {func_name}(): cover '{line_text[:SOURCE_TRUNCATION]}'"
            )

    return suggestions
# ============================================================================
# Migrated test generation
# ============================================================================


def _type_expr(
    tp: object,
    *,
    current_module: str,
    imports: set[str],
) -> str | None:
    """Render a type annotation and collect any imports it needs."""
    import types as pytypes

    if isinstance(tp, str):
        return tp
    if tp is type(None):
        return "None"
    if tp is Any:
        imports.add("from typing import Any")
        return "Any"

    origin = get_origin(tp)
    if origin is Literal:
        imports.add("from typing import Literal")
        return f"Literal[{', '.join(repr(arg) for arg in get_args(tp))}]"

    if origin is Union or (hasattr(pytypes, "UnionType") and origin is pytypes.UnionType):
        parts = []
        for arg in get_args(tp):
            part = _type_expr(arg, current_module=current_module, imports=imports)
            if part is None:
                return None
            parts.append(part)
        return " | ".join(parts)

    if origin in {list, set, frozenset}:
        args = get_args(tp)
        if len(args) != 1:
            return origin.__name__
        inner = _type_expr(args[0], current_module=current_module, imports=imports)
        if inner is None:
            return None
        return f"{origin.__name__}[{inner}]"

    if origin is dict:
        args = get_args(tp)
        if len(args) != 2:
            return "dict"
        key = _type_expr(args[0], current_module=current_module, imports=imports)
        value = _type_expr(args[1], current_module=current_module, imports=imports)
        if key is None or value is None:
            return None
        return f"dict[{key}, {value}]"

    if origin is tuple:
        rendered: list[str] = []
        for arg in get_args(tp):
            if arg is Ellipsis:
                rendered.append("...")
                continue
            part = _type_expr(arg, current_module=current_module, imports=imports)
            if part is None:
                return None
            rendered.append(part)
        return f"tuple[{', '.join(rendered)}]"

    if origin is not None:
        origin_expr = _type_expr(origin, current_module=current_module, imports=imports)
        if origin_expr is None:
            return None
        rendered_args: list[str] = []
        for arg in get_args(tp):
            part = _type_expr(arg, current_module=current_module, imports=imports)
            if part is None:
                return None
            rendered_args.append(part)
        if not rendered_args:
            return origin_expr
        return f"{origin_expr}[{', '.join(rendered_args)}]"

    module = getattr(tp, "__module__", None)
    qualname = getattr(tp, "__qualname__", None) or getattr(tp, "__name__", None)
    if qualname is None:
        return None
    if module in {None, "builtins"}:
        return qualname
    imports.add(f"import {module}")
    return f"{module}.{qualname}"
def _func_sig_for_codegen(
    func: object,
) -> tuple[list[str], list[str], str, list[str]] | None:
    """Extract param info for generated ``@quickcheck`` test, or *None*.

    Returns ``(param_names, param_decls_with_types, call_args_str, imports)``
    only when every required parameter has a renderable type hint.
    """
    try:
        hints = safe_get_annotations(func)
    except Exception:
        return None

    sig = inspect.signature(func)
    names: list[str] = []
    decls: list[str] = []
    imports: set[str] = set()
    current_module = getattr(func, "__module__", "")
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        if pname not in hints:
            return None
        type_str = _type_expr(
            hints[pname],
            current_module=current_module,
            imports=imports,
        )
        if type_str is None:
            return None
        names.append(pname)
        decls.append(f"{pname}: {type_str}")

    return (names, decls, ", ".join(names), sorted(imports)) if names else None
