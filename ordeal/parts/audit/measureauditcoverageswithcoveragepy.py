from __future__ import annotations
# ruff: noqa
def _measure_audit_coverages_with_coverage_py(
    module_name: str,
    current_test_files: list[Path],
    generated_test_files: list[Path],
) -> tuple[CoverageMeasurement, CoverageMeasurement]:
    """Measure current and migrated coverage in one coverage.py subprocess."""
    cwd, env, current_pytest_args, _generated_only = _coverage_runtime_context(
        current_test_files,
        module_name,
    )

    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_dual_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import importlib.util
import contextlib
import inspect
import json
import sys
import tempfile
from types import SimpleNamespace
from typing import Literal, Union, get_args, get_origin
from pathlib import Path

import coverage
import pytest

module_name = sys.argv[1]
json_path = sys.argv[2]
current_count = int(sys.argv[3])
current_args = sys.argv[4 : 4 + current_count]
generated_files = sys.argv[4 + current_count :]

def coverage_payload(cov):
    spec = importlib.util.find_spec(module_name)
    source_file = getattr(spec, "origin", None) if spec is not None else None
    if not source_file:
        raise RuntimeError(f"cannot locate source for {module_name!r}")
    filename, statements, _excluded, missing, _formatted = cov.analysis2(source_file)
    total = len(statements)
    percent = (100.0 * (total - len(missing)) / total) if total else 100.0
    return {
        "files": {
            filename: {
                "summary": {
                    "percent_covered": percent,
                    "num_statements": total,
                    "missing_lines": len(missing),
                },
                "missing_lines": list(missing),
            }
        }
    }

def run_pytest_suite(pytest_args):
    payload = {"return_code": 0, "coverage": None, "error": None}
    cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)
    try:
        cov.start()
        payload["return_code"] = int(pytest.main(pytest_args))
    except Exception as exc:
        payload["return_code"] = 2
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cov.stop()
        except Exception:
            pass

    try:
        payload["coverage"] = coverage_payload(cov)
    except Exception as exc:
        payload["error"] = payload["error"] or f"coverage analysis failed: {type(exc).__name__}: {exc}"
    return payload

def run_generated_suite(test_files):
    payload = {"return_code": 0, "coverage": None, "error": None}
    cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)
    original_fuzz = None
    original_auto_fuzz = None
    original_quickcheck = None
    ordeal = None
    ordeal_auto = None
    ordeal_quickcheck = None
    try:
        import ordeal
        import ordeal.auto as ordeal_auto
        import ordeal.quickcheck as ordeal_quickcheck

        original_fuzz = ordeal.fuzz
        original_auto_fuzz = ordeal_auto.fuzz
        original_quickcheck = ordeal_quickcheck.quickcheck

        def example_value(annotation):
            if annotation in {inspect._empty, None, object}:
                return None
            origin = get_origin(annotation)
            if origin is Literal:
                values = get_args(annotation)
                return values[0] if values else None
            if origin is Union:
                for option in get_args(annotation):
                    if option is type(None):
                        continue
                    return example_value(option)
                return None
            if origin is list:
                return []
            if origin is tuple:
                return ()
            if origin is dict:
                return {}
            if origin is set:
                return set()
            if annotation is int:
                return 1
            if annotation is float:
                return 1.0
            if annotation is str:
                return "x"
            if annotation is bool:
                return True
            if annotation is bytes:
                return b"x"
            return None

        def call_kwargs(fn):
            kwargs = {}
            for name, param in inspect.signature(fn).parameters.items():
                if param.default is not inspect._empty:
                    kwargs[name] = param.default
                else:
                    kwargs[name] = example_value(param.annotation)
            return kwargs

        def fuzz_smoke(target, *args, **kwargs):
            try:
                target(**call_kwargs(target))
            except Exception as exc:
                return SimpleNamespace(
                    passed=False,
                    summary=lambda: f"{type(exc).__name__}: {exc}",
                )
            return SimpleNamespace(passed=True, summary=lambda: "smoke")

        def quickcheck_smoke(*decorator_args, **decorator_kwargs):
            def decorator(fn):
                def wrapped(*args, **kwargs):
                    return fn(**call_kwargs(fn))

                return wrapped

            if decorator_args and callable(decorator_args[0]) and not decorator_kwargs:
                return decorator(decorator_args[0])
            return decorator

        ordeal.fuzz = fuzz_smoke
        ordeal_auto.fuzz = fuzz_smoke
        ordeal_quickcheck.quickcheck = quickcheck_smoke

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
        with contextlib.suppress(Exception):
            ordeal.fuzz = original_fuzz
        with contextlib.suppress(Exception):
            ordeal_auto.fuzz = original_auto_fuzz
        with contextlib.suppress(Exception):
            ordeal_quickcheck.quickcheck = original_quickcheck

    try:
        payload["coverage"] = coverage_payload(cov)
    except Exception as exc:
        payload["error"] = payload["error"] or f"coverage analysis failed: {type(exc).__name__}: {exc}"
    return payload

payload = {
    "current": run_pytest_suite(current_args),
    "generated": run_generated_suite(generated_files),
}
Path(json_path).write_text(json.dumps(payload), encoding="utf-8")
"""

    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="ordeal_dual_cov_",
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
        str(len(current_pytest_args)),
        *current_pytest_args,
        *[str(f) for f in generated_test_files],
    ]

    def _failed(error: str) -> tuple[CoverageMeasurement, CoverageMeasurement]:
        failed = CoverageMeasurement(Status.FAILED, error=error)
        return failed, failed

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
        return _failed(f"timed out after {SUBPROCESS_TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        return _failed("python not found")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stderr_hint = (result.stderr or "")[:200]
        return _failed(f"coverage report not generated. stderr: {stderr_hint}")
    except json.JSONDecodeError as exc:
        stderr_hint = (result.stderr or "")[:200]
        return _failed(
            f"invalid JSON: {exc}"
            + (f" (subprocess stderr: {stderr_hint})" if stderr_hint else "")
        )
    finally:
        json_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)

    def _measurement_from_payload(
        payload: object,
        *,
        source: str,
        error_prefix: str,
    ) -> CoverageMeasurement:
        if not isinstance(payload, dict):
            return CoverageMeasurement(Status.FAILED, error=f"{error_prefix}: missing payload")
        if payload.get("error"):
            return CoverageMeasurement(Status.FAILED, error=str(payload["error"]))
        return_code = int(payload.get("return_code", 0))
        if return_code not in (0, 1):
            return CoverageMeasurement(
                Status.FAILED,
                error=f"{error_prefix}: runner exited with code {return_code}",
            )
        coverage_json = payload.get("coverage")
        if not isinstance(coverage_json, dict):
            return CoverageMeasurement(Status.FAILED, error=f"{error_prefix}: coverage missing")
        return _coverage_measurement_from_json(coverage_json, module_name, source=source)

    current = _measurement_from_payload(
        raw.get("current"),
        source="coverage.py API",
        error_prefix="current coverage",
    )
    migrated = _measurement_from_payload(
        raw.get("generated"),
        source="coverage.py API direct",
        error_prefix="generated coverage",
    )
    return current, migrated
def _measure_audit_coverages(
    current_test_files: list[Path],
    generated_test_files: list[Path],
    module_name: str,
) -> tuple[CoverageMeasurement, CoverageMeasurement]:
    """Measure current and migrated audit coverage with shared fast paths."""
    existing_generated_files = [path for path in generated_test_files if path.exists()]
    generated_test_count = sum(_count_tests_in_file(path)[0] for path in existing_generated_files)
    if generated_test_files and existing_generated_files and generated_test_count == 0:
        current = (
            _measure_coverage(current_test_files, module_name)
            if current_test_files
            else CoverageMeasurement(Status.FAILED, error="no test files found")
        )
        return current, CoverageMeasurement(Status.FAILED, error="no generated tests")

    cached_pair = _load_audit_coverage_evidence(
        module_name,
        current_test_files,
        generated_test_files,
    )
    if cached_pair is not None:
        return cached_pair

    if (
        current_test_files
        and generated_test_files
        and importlib.util.find_spec("coverage") is not None
        and all(_is_generated_test_file(f) for f in generated_test_files)
    ):
        current, migrated = _measure_audit_coverages_with_coverage_py(
            module_name,
            current_test_files,
            generated_test_files,
        )
        with contextlib.suppress(Exception):
            _save_audit_coverage_evidence(
                module_name,
                current_test_files,
                generated_test_files,
                current,
                migrated,
            )
        return current, migrated

    current = (
        _measure_coverage(current_test_files, module_name)
        if current_test_files
        else CoverageMeasurement(Status.FAILED, error="no test files found")
    )
    migrated = _measure_coverage(generated_test_files, module_name)
    with contextlib.suppress(Exception):
        _save_audit_coverage_evidence(
            module_name,
            current_test_files,
            generated_test_files,
            current,
            migrated,
        )
    return current, migrated
def _measure_coverage_with_coverage_py(
    module_name: str,
    pytest_args: list[str],
    cwd: str,
    env: dict[str, str],
) -> CoverageMeasurement:
    """Measure coverage via the ``coverage.py`` API when available.

    This avoids the pytest-cov plugin dependency while still using
    coverage.py's much faster tracer instead of Python-level ``sys.settrace``.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".json",
        prefix="ordeal_cov_",
        delete=False,
    ) as tmp:
        json_path = Path(tmp.name)

    script = """
import json
import sys
import tempfile
from pathlib import Path

import coverage
import pytest

module_name = sys.argv[1]
json_path = sys.argv[2]
pytest_args = sys.argv[3:]

payload = {"return_code": 0, "coverage": None, "error": None}
cov_json = Path(tempfile.mkstemp(prefix="ordeal_cov_raw_", suffix=".json")[1])
cov = coverage.Coverage(source=[module_name], config_file=False, data_file=None)

try:
    cov.start()
    payload["return_code"] = int(pytest.main(pytest_args))
except Exception as exc:
    payload["return_code"] = 2
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
        prefix="ordeal_cov_runner_",
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
            error=f"pytest exited with code {return_code} during coverage measurement",
        )
    coverage_json = raw.get("coverage")
    if not isinstance(coverage_json, dict):
        return CoverageMeasurement(Status.FAILED, error="coverage report missing payload")

    return _coverage_measurement_from_json(
        coverage_json,
        module_name,
        source="coverage.py API",
    )
