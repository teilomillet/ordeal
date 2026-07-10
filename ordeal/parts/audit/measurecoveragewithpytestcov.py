from __future__ import annotations
# ruff: noqa
def _measure_coverage_with_pytest_cov(
    module_name: str,
    pytest_args: list[str],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage via pytest-cov when the plugin is available."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *pytest_args,
        f"--cov={module_name}",
        f"--cov-report=json:{json_path}",
        "--cov-report=",  # suppress terminal output
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
        return CoverageMeasurement(
            Status.FAILED,
            error=f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        return CoverageMeasurement(Status.FAILED, error="pytest not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return CoverageMeasurement(
            Status.FAILED,
            error=f"coverage report not generated. stderr: {stderr_hint}",
        )
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200] if result else ""
        json_path.unlink(missing_ok=True)
        return CoverageMeasurement(
            Status.FAILED,
            error=f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else ""),
        )
    finally:
        json_path.unlink(missing_ok=True)

    return _coverage_measurement_from_json(raw, module_name, source="coverage.py JSON")
def _measure_coverage_with_trace(
    module_name: str,
    pytest_args: list[str],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage without external plugins by tracing a pytest subprocess."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_trace_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import ast
import importlib
import inspect
import json
import os
import sys
import threading
from pathlib import Path

import pytest

module_name = sys.argv[1]
json_path = sys.argv[2]
pytest_args = sys.argv[3:]

module = importlib.import_module(module_name)
module_file = getattr(module, "__file__", None)
source_file = inspect.getsourcefile(module) or module_file
if source_file is None:
    err = {"error": f"{module_name} has no source file"}
    Path(json_path).write_text(json.dumps(err), encoding="utf-8")
    raise SystemExit(0)

target_file = os.path.realpath(source_file)
hits = set()

def tracer(frame, event, arg):
    if event == "line" and os.path.realpath(frame.f_code.co_filename) == target_file:
        hits.add(frame.f_lineno)
    return tracer

return_code = 0

class _CoveragePlugin:
    def pytest_runtest_setup(self, item):
        sys.settrace(tracer)
        threading.settrace(tracer)

    def pytest_runtest_teardown(self, item, nextitem):
        sys.settrace(None)
        threading.settrace(None)

return_code = pytest.main(pytest_args, plugins=[_CoveragePlugin()])

source_text = Path(target_file).read_text(encoding="utf-8")
tree = ast.parse(source_text, filename=target_file)
stmt_lines = sorted(
    {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and hasattr(node, "lineno")
    }
)
measured_hits = sorted(set(stmt_lines) & hits)
missing = sorted(set(stmt_lines) - set(measured_hits))
total = len(stmt_lines)
percent = ((total - len(missing)) / total * 100.0) if total else 100.0

payload = {
    "return_code": int(return_code),
    "module_file": target_file,
    "num_statements": total,
    "executed_lines": measured_hits,
    "missing_lines": missing,
    "percent_covered": percent,
}
Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_trace_cov_",
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
        *pytest_args,
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
        return CoverageMeasurement(Status.FAILED, error="pytest not found")

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

    if "error" in raw:
        return CoverageMeasurement(Status.FAILED, error=str(raw["error"]))
    return_code = int(raw.get("return_code", 0))
    if return_code not in (0, 1):
        return CoverageMeasurement(
            Status.FAILED,
            error=f"pytest exited with code {return_code} during trace coverage",
        )

    return _coverage_measurement_from_trace_payload(raw)
def _measure_generated_coverage_with_trace(
    module_name: str,
    test_files: list[Path],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage for generated ordeal tests without invoking pytest."""
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_direct_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import ast
import importlib
import importlib.util
import inspect
import json
import os
import sys
import threading
from pathlib import Path

module_name = sys.argv[1]
json_path = sys.argv[2]
test_files = sys.argv[3:]

module = importlib.import_module(module_name)
module_file = getattr(module, "__file__", None)
source_file = inspect.getsourcefile(module) or module_file
if source_file is None:
    err = {"error": f"{module_name} has no source file"}
    Path(json_path).write_text(json.dumps(err), encoding="utf-8")
    raise SystemExit(0)

target_file = os.path.realpath(source_file)
hits = set()

def tracer(frame, event, arg):
    if event == "line" and os.path.realpath(frame.f_code.co_filename) == target_file:
        hits.add(frame.f_lineno)
    return tracer

return_code = 0
error = None
try:
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
            if not callable(test_fn):
                continue
            sys.settrace(tracer)
            threading.settrace(tracer)
            try:
                test_fn()
            finally:
                sys.settrace(None)
                threading.settrace(None)
except Exception as exc:
    return_code = 1
    error = f"{type(exc).__name__}: {exc}"

source_text = Path(target_file).read_text(encoding="utf-8")
tree = ast.parse(source_text, filename=target_file)
stmt_lines = sorted(
    {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and hasattr(node, "lineno")
    }
)
measured_hits = sorted(set(stmt_lines) & hits)
missing = sorted(set(stmt_lines) - set(measured_hits))
total = len(stmt_lines)
percent = ((total - len(missing)) / total * 100.0) if total else 100.0

payload = {
    "return_code": int(return_code),
    "error": error,
    "module_file": target_file,
    "num_statements": total,
    "executed_lines": measured_hits,
    "missing_lines": missing,
    "percent_covered": percent,
}
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
            error=f"generated test runner exited with code {return_code}",
        )

    return _coverage_measurement_from_trace_payload(raw)
