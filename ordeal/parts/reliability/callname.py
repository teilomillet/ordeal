from __future__ import annotations
# ruff: noqa
import ast
import hashlib
import importlib
import inspect
import json
import re
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
RELIABILITY_MAP_SCHEMA = "ordeal.reliability-map/v1"
RUNTIME_FAULT_PROPERTY_PREFIX = "operation completes without an uncaught exception under"
_FAULT_PROBE_ALIASES: dict[str, frozenset[str]] = {
    # Automatic closure is deliberately narrower than the fault catalog. These
    # implementations record a hit only at the branch that actually raises.
    "io_error": frozenset({"permission_denied"}),
    "disk_full": frozenset({"disk_full"}),
    "timeout": frozenset({"subprocess_timeout"}),
}
_SEAM_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "retry": (("retry", "attempt", "backoff", "sleep"), ("timeout", "connection_reset")),
    "fallback": (("fallback", "default", "degraded"), ("primary_failure",)),
    "recovery": (("recover", "restore", "restart", "rollback", "cleanup"), ("restart",)),
    "cache": (("cache", "memo", "redis", "lru_cache"), ("stale_state", "cache_miss")),
    "file": (
        ("open", "read_text", "write_text", "read_bytes", "write_bytes"),
        ("io_error", "disk_full"),
    ),
    "http": (
        ("http", "requests", "httpx", "aiohttp", "urllib", "urlopen"),
        ("timeout", "http_503"),
    ),
    "subprocess": (
        ("subprocess", "popen", "check_output", "os.system"),
        ("timeout", "nonzero_exit"),
    ),
    "transaction": (("transaction", "commit", "rollback", "atomic", "begin"), ("commit_failure",)),
    "model_loading": (
        ("from_pretrained", "load_model", "load_state_dict", "torch.load", "joblib.load"),
        ("missing_artifact", "stale_artifact"),
    ),
}
_ML_PROFILE_RULES: dict[str, tuple[tuple[str, ...], str, str]] = {
    "shape_drift": (
        (".shape", "reshape", "tensor", "ndarray", "array"),
        "wrong_shape",
        "output shape follows declared batch and feature dimensions",
    ),
    "dtype_drift": (
        (".dtype", "astype", "float16", "float32", "bfloat16"),
        "wrong_dtype",
        "dtype conversions preserve the supported numeric domain",
    ),
    "non_finite_values": (
        ("isfinite", "isnan", "isinf", "nan_to_num"),
        "nan_or_inf",
        "outputs remain finite for contract-valid inputs",
    ),
    "partial_batches": (
        ("batch", "dataloader", "drop_last"),
        "partial_batch",
        "partial batches preserve output cardinality",
    ),
    "stale_artifacts": (
        ("checkpoint", "artifact", "model_revision", "etag"),
        "stale_artifact",
        "artifact identity and version match the active model",
    ),
    "feature_order": (
        ("feature_names", "columns", "vectorizer", "feature_order"),
        "feature_reordering",
        "feature ordering is stable across fit, load, and predict",
    ),
}
_SEAM_PROPERTIES = {
    "retry": "retries do not duplicate committed effects",
    "fallback": "fallback behavior preserves the declared result contract",
    "recovery": "recovery restores a usable state",
    "cache": "cache hits preserve uncached behavior",
    "file": "failed file I/O does not expose partial or corrupt state",
    "http": "transient HTTP failures do not duplicate committed effects",
    "subprocess": "timeouts and nonzero exits remain bounded and reported",
    "transaction": "failed transactions do not leave partially committed state",
    "model_loading": "missing or stale model artifacts fail safely",
}
def _call_name(node: ast.Call) -> str:
    """Return a dotted best-effort name for one call expression."""
    parts: list[str] = []
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts)).lower()
def _write_mode_open(node: ast.Call) -> bool:
    """Return whether one source call explicitly opens a file for writing."""
    name = _call_name(node)
    if not (name == "open" or name.endswith(".open")):
        return False
    mode_node: ast.AST | None = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
            break
    return bool(
        isinstance(mode_node, ast.Constant)
        and isinstance(mode_node.value, str)
        and any(flag in mode_node.value for flag in "wax")
    )
def _annotation_text(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return normalized annotation text for one function."""
    annotations = [
        arg.annotation for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    ]
    if node.args.vararg is not None:
        annotations.append(node.args.vararg.annotation)
    if node.args.kwarg is not None:
        annotations.append(node.args.kwarg.annotation)
    annotations.append(node.returns)
    return " ".join(ast.unparse(item).lower() for item in annotations if item is not None)
def _schema_definitions(tree: ast.Module, path: Path) -> dict[str, str]:
    """Return schema-like class names and their source locations.

    Dataclasses, ``TypedDict`` declarations, and common model/schema base
    classes are structural contract evidence. They remain hypotheses until a
    runtime experiment exercises the corresponding property.
    """
    definitions: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {ast.unparse(base).rsplit(".", maxsplit=1)[-1].lower() for base in node.bases}
        decorators = {
            ast.unparse(decorator).split("(", maxsplit=1)[0].rsplit(".", maxsplit=1)[-1].lower()
            for decorator in node.decorator_list
        }
        schema_base = any(
            base in {"typeddict", "basemodel", "schema", "model", "struct"}
            or base.endswith("schema")
            for base in bases
        )
        if schema_base or "dataclass" in decorators:
            definitions[node.name.lower()] = f"{_relative_path(path)}:{node.lineno}"
    return definitions
def _function_source(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the source segment for *node* without failing on parser offsets."""
    segment = ast.get_source_segment(source, node)
    return segment if segment is not None else ast.unparse(node)
def _module_sources(module_name: str) -> list[tuple[str, Path, Path]]:
    """Return ``(module, file, root)`` triples for an importable target."""
    module = importlib.import_module(module_name)
    module_paths = [Path(item).resolve() for item in getattr(module, "__path__", ())]
    if module_paths:
        root = module_paths[0]
        records: list[tuple[str, Path, Path]] = []
        for path in sorted(root.rglob("*.py")):
            if any(part in {"__pycache__", ".venv", "venv"} for part in path.parts):
                continue
            relative = path.relative_to(root).with_suffix("")
            suffix = ".".join(relative.parts)
            if suffix.endswith(".__init__"):
                suffix = suffix[: -len(".__init__")]
            qualified = module_name if not suffix else f"{module_name}.{suffix}"
            records.append((qualified.rstrip("."), path, root.parent))
        return records
    path = Path(str(getattr(module, "__file__", ""))).resolve()
    return [(module_name, path, path.parent)] if path.is_file() else []
def _test_provenance(simple_name: str, roots: Sequence[Path]) -> list[dict[str, Any]]:
    """Return bounded textual test references for one callable name."""
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots:
        for test_root in (root / "tests", root / "test"):
            if not test_root.is_dir():
                continue
            for path in sorted(test_root.rglob("test_*.py")):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    continue
                for line_number, line in enumerate(lines, start=1):
                    if re.search(rf"\b{re.escape(simple_name)}\b", line):
                        records.append(
                            {
                                "kind": "test",
                                "evidence": f"{_relative_path(path)}:{line_number}",
                            }
                        )
                        break
                if records:
                    return records
    return records
def _changed_files(base_ref: str | None) -> set[str]:
    """Return committed, staged, unstaged, and untracked files since *base_ref*."""
    if not base_ref:
        return set()
    error = _base_ref_error(base_ref)
    if error is not None:
        raise ValueError(error)
    completed = subprocess.run(
        ["git", "diff", "--name-only", base_ref],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git diff could not compare the requested revision"
        raise ValueError(f"Git revision {base_ref!r} could not be compared: {detail}")
    changed = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        text=True,
        capture_output=True,
        check=False,
    )
    if untracked.returncode == 0:
        changed.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    return changed
def _base_ref_error(base_ref: str) -> str | None:
    """Return a blocking reason when *base_ref* is not a committed Git revision."""
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return None
    detail = completed.stderr.strip() or "revision was not found in this Git checkout"
    return f"Git revision {base_ref!r} is unavailable: {detail}"
def _relative_path(path: Path) -> str:
    """Return a stable workspace-relative path where possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()
def _matching_state(state: Any, qualname: str, simple_name: str) -> Any | None:
    """Return the closest runtime function state for one static operation."""
    functions = getattr(state, "functions", {}) or {}
    for candidate in (qualname, qualname.split(".", maxsplit=1)[-1], simple_name):
        if candidate in functions:
            return functions[candidate]
    suffix = f".{simple_name}"
    matches = [
        value for key, value in functions.items() if key.endswith(suffix) or key == simple_name
    ]
    return matches[0] if len(matches) == 1 else None
def _surface_blocker(
    rows: Sequence[Mapping[str, Any]], qualname: str, simple_name: str
) -> str | None:
    """Return a callable-discovery blocker for one operation."""
    for row in rows:
        name = str(row.get("name") or "")
        target = str(row.get("target") or "")
        if name not in {qualname, simple_name} and not target.endswith(f".{qualname}"):
            continue
        execution = row.get("execution")
        if not isinstance(execution, Mapping):
            surface = row.get("surface")
            execution = surface.get("execution") if isinstance(surface, Mapping) else None
        runnable = row.get("runnable")
        if runnable is None and isinstance(execution, Mapping):
            runnable = execution.get("can_execute_now")
        if runnable is not False:
            return None
        return str(
            row.get("skip_reason")
            or (execution or {}).get("blocking_reason")
            or "callable has no runnable harness"
        )
    if "." in qualname:
        return "bound method has no verified runnable harness in the scanned surface"
    return None
def _runtime_fault_property(fault: str) -> str:
    """Return the exact operational property checked by a fault probe."""
    return f"{RUNTIME_FAULT_PROPERTY_PREFIX} {fault}"
def _fault_probe_supported(fault: str) -> bool:
    """Return whether Ordeal can safely attempt this fault in a child scan."""
    return fault in _FAULT_PROBE_ALIASES
def _source_fault_probe_supported(
    fault: str,
    calls: set[str],
    *,
    writes_file: bool,
    source_bound_subprocesses: int,
) -> bool:
    """Return whether source analysis will construct this exact fault kind."""
    if not _fault_probe_supported(fault):
        return False
    from ordeal.auto import _FAULT_PATTERNS, _ml_data_fault_specs

    aliases = _FAULT_PROBE_ALIASES[fault]
    if fault in {"disk_full", "io_error"} and not writes_file:
        return False
    if fault == "timeout" and source_bound_subprocesses != 1:
        return False
    for call in calls:
        if fault == "timeout" and call.lower() != "subprocess.run":
            continue
        for pattern, specs in _FAULT_PATTERNS.items():
            if pattern.lower() in call.lower() and any(spec[1] in aliases for spec in specs):
                return True
        if any(spec[1] in aliases for spec in _ml_data_fault_specs(call)):
            return True
    return False
def _runtime_cell_status(
    state: Any,
    *,
    target: str,
    fault: str,
    property_name: str,
) -> tuple[str, str | None] | None:
    """Return the newest exact runtime observation for one normalized cell."""
    observations = getattr(state, "supervisor_info", {}).get("reliability_observations", ())
    for observation in reversed(list(observations)):
        if (
            observation.get("target") == target
            and observation.get("fault") == fault
            and observation.get("property") == property_name
        ):
            status = str(observation.get("status") or "NOT EXERCISED")
            if status not in {"PASS", "NOT EXERCISED", "FAIL"}:
                status = "NOT EXERCISED"
            return status, (
                str(observation["blocking_reason"]) if observation.get("blocking_reason") else None
            )
    return None
def _cell_status(function_state: Any | None, fault: str) -> tuple[str, str | None]:
    """Return bounded coverage status and blocker for one reliability cell."""
    if function_state is None:
        return "NOT EXERCISED", None
    limitation = getattr(function_state, "scan_limitation_kind", None)
    if limitation:
        return "NOT EXERCISED", str(
            getattr(function_state, "scan_blocking_reason", None) or limitation
        )

    def detail_faults(detail: Mapping[str, Any]) -> set[str]:
        """Return explicit fault identities attached to one runtime violation."""
        identities: set[str] = set()
        for key in ("fault", "injected_fault", "active_faults", "runtime_faults", "faults"):
            value = detail.get(key)
            if isinstance(value, str):
                identities.add(value)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                identities.update(str(item) for item in value)
        return identities

    if any(
        detail.get("category") in {"lifecycle_contract", "semantic_contract"}
        and fault in detail_faults(detail)
        for detail in getattr(function_state, "contract_violation_details", ())
    ):
        return "FAIL", None
    return "NOT EXERCISED", None
def _next_experiment(
    *,
    module: str,
    target: str,
    selector: str,
    seam: str,
    status: str,
    blocker: str | None,
    base_ref: str | None,
    changed: bool,
    has_tests: bool,
    allow_service_faults: bool,
    fault: str | None = None,
    automatable: bool = False,
) -> dict[str, Any]:
    """Choose the cheapest executable experiment for one evidence gap."""
    if blocker:
        return {
            "engine": "scan",
            "command": f"ordeal scan {module} --list-targets",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "safe",
            "reason": "blocked_harness_review",
            "auto_runnable": False,
        }
    if status == "FAIL":
        return {
            "engine": "scan",
            "command": f"ordeal scan {module} --target {selector} --save",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "safe",
            "reason": "save_observed_witness",
            "auto_runnable": False,
        }
    if automatable and fault is not None and _fault_probe_supported(fault):
        return {
            "engine": "scan",
            "command": (
                f"ordeal scan {module} --target {selector} --evidence-fault {fault} -n 30"
            ),
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "safe",
            "reason": "fault_specific_runtime_probe",
            "auto_runnable": True,
        }
    if changed and base_ref:
        return {
            "engine": "differential",
            "command": f"ordeal diff {target} --base-ref {base_ref} --candidate-ref HEAD",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "safe",
            "reason": "changed_callable_diff",
            "auto_runnable": False,
        }
    compose_configured = False
    config_path = Path("ordeal.toml")
    config_exists = config_path.is_file()
    if allow_service_faults and config_exists:
        try:
            compose_configured = "[compose]" in config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            compose_configured = False
    if seam in {"http", "recovery", "transaction"} and compose_configured:
        return {
            "engine": "compose",
            "command": "ordeal explore --runner compose -c ordeal.toml",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "service_faults_opted_in",
            "reason": "opted_in_service_faults",
            "auto_runnable": True,
        }
    if has_tests and seam in {"cache", "fallback", "transaction"}:
        return {
            "engine": "mutation",
            "command": f"ordeal mutate {target}",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "review_required",
            "reason": "measure_test_strength",
            "auto_runnable": False,
        }
    if seam in {"subprocess", "http", "recovery", "transaction"} and config_exists:
        return {
            "engine": "exploration",
            "command": "ordeal explore -c ordeal.toml",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "review_required",
            "reason": "review_stateful_workload",
            "auto_runnable": False,
        }
    if seam in {"subprocess", "http", "recovery", "transaction"}:
        return {
            "engine": "scan",
            "command": f"ordeal scan {module} --target {selector} -n 100",
            "module": module,
            "target": target,
            "selector": selector,
            "safety": "review_required",
            "reason": "review_side_effecting_target",
            "auto_runnable": False,
        }
    return {
        "engine": "scan",
        "command": f"ordeal scan {module} --target {selector} -n 100",
        "module": module,
        "target": target,
        "selector": selector,
        "safety": "review_required",
        "reason": "review_candidate_property",
        "auto_runnable": False,
    }
