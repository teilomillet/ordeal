"""Source-backed evidence-closure planning for :command:`ordeal scan`.

The reliability map joins static seams and candidate properties with the
bounded runtime evidence already held by :mod:`ordeal.state`.  Static
inferences are hypotheses; only executed fault/property cells may PASS or FAIL.
"""

from __future__ import annotations

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


def _operation_records(
    module: str,
    state: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    base_ref: str | None,
    allow_service_faults: bool,
) -> list[dict[str, Any]]:
    """Build source-backed operations, seams, hypotheses, and cells."""
    from ordeal.auto import _source_bound_subprocess_match

    changed_files = _changed_files(base_ref)
    sources = _module_sources(module)
    test_roots = sorted({root for _, _, root in sources})
    operations: list[dict[str, Any]] = []
    for source_module, path, _ in sources:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path.as_posix())
        except (OSError, UnicodeError, SyntaxError):
            continue
        schema_definitions = _schema_definitions(tree, path)
        parents: list[str] = []

        def visit(body: Sequence[ast.stmt]) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    if node.name.startswith("_"):
                        continue
                    parents.append(node.name)
                    visit(node.body)
                    parents.pop()
                    continue
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                qualname = ".".join((*parents, node.name))
                selector = qualname
                target = f"{source_module}.{qualname}"
                function_source = _function_source(source, node)
                lowered = function_source.lower()
                calls = {_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)}
                source_bound_subprocesses = sum(
                    _call_name(call) == "subprocess.run"
                    and _source_bound_subprocess_match(call) is not None
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                )
                writes_file = any(
                    _write_mode_open(call) for call in ast.walk(node) if isinstance(call, ast.Call)
                )
                searchable = " ".join((lowered, *sorted(calls)))
                seams = [
                    {
                        "kind": kind,
                        "evidence": f"{_relative_path(path)}:{node.lineno}",
                        "faults": list(faults),
                    }
                    for kind, (tokens, faults) in _SEAM_RULES.items()
                    if any(token in searchable for token in tokens)
                ]
                annotation = _annotation_text(node)
                profiles = [
                    {
                        "kind": kind,
                        "evidence": f"{_relative_path(path)}:{node.lineno}",
                        "fault": fault,
                        "property": property_name,
                    }
                    for kind, (tokens, fault, property_name) in _ML_PROFILE_RULES.items()
                    if any(token in f"{searchable} {annotation}" for token in tokens)
                ]
                if not seams and not profiles:
                    continue
                docstring = ast.get_docstring(node) or ""
                test_evidence = _test_provenance(node.name, test_roots)
                provenance: list[dict[str, Any]] = [
                    {
                        "kind": "source",
                        "evidence": f"{_relative_path(path)}:{node.lineno}",
                    }
                ]
                if annotation:
                    provenance.append({"kind": "types", "evidence": annotation[:300]})
                    for schema_name, location in schema_definitions.items():
                        if re.search(rf"\b{re.escape(schema_name)}\b", annotation):
                            provenance.append({"kind": "schema", "evidence": location})
                if docstring:
                    provenance.append(
                        {
                            "kind": "documentation",
                            "evidence": f"{_relative_path(path)}:{node.lineno}",
                        }
                    )
                if any(isinstance(item, ast.Assert) for item in ast.walk(node)):
                    provenance.append(
                        {
                            "kind": "assertion",
                            "evidence": f"{_relative_path(path)}:{node.lineno}",
                        }
                    )
                provenance.extend(test_evidence)
                function_state = _matching_state(state, qualname, node.name)
                blocker = _surface_blocker(rows, qualname, node.name)
                if function_state is not None and getattr(
                    function_state, "scan_limitation_kind", None
                ):
                    blocker = str(
                        getattr(function_state, "scan_blocking_reason", None)
                        or getattr(function_state, "scan_limitation_kind")
                    )
                relative = _relative_path(path)
                changed = relative in changed_files
                hypotheses: list[dict[str, Any]] = []
                cells: list[dict[str, Any]] = []

                def add_runtime_fault_cell(kind: str, fault: str) -> None:
                    if not _source_fault_probe_supported(
                        fault,
                        calls,
                        writes_file=writes_file,
                        source_bound_subprocesses=source_bound_subprocesses,
                    ):
                        return
                    property_name = _runtime_fault_property(fault)
                    hypotheses.append(
                        {
                            "name": property_name,
                            "epistemic_status": "hypothesis",
                            "profile": "runtime_fault_probe",
                            "provenance": provenance,
                        }
                    )
                    observation = _runtime_cell_status(
                        state,
                        target=target,
                        fault=fault,
                        property_name=property_name,
                    )
                    status, observed_blocker = observation or ("NOT EXERCISED", None)
                    effective_blocker = blocker or observed_blocker
                    experiment = _next_experiment(
                        module=source_module,
                        target=target,
                        selector=selector,
                        seam=kind,
                        status=status,
                        blocker=effective_blocker,
                        base_ref=base_ref,
                        changed=changed,
                        has_tests=bool(test_evidence),
                        allow_service_faults=allow_service_faults,
                        fault=fault,
                        automatable=True,
                    )
                    cells.append(
                        {
                            "id": hashlib.sha256(
                                f"{target}|{kind}|{fault}|{property_name}".encode()
                            ).hexdigest()[:16],
                            "operation": target,
                            "seam": kind,
                            "fault": fault,
                            "property": property_name,
                            "status": status,
                            "blocking_reason": effective_blocker,
                            "next_experiment": experiment,
                        }
                    )

                for seam in seams:
                    property_name = _SEAM_PROPERTIES[seam["kind"]]
                    hypotheses.append(
                        {
                            "name": property_name,
                            "epistemic_status": "hypothesis",
                            "provenance": provenance,
                        }
                    )
                    for fault in seam["faults"]:
                        status, measured_blocker = _cell_status(function_state, fault)
                        effective_blocker = blocker or measured_blocker
                        experiment = _next_experiment(
                            module=source_module,
                            target=target,
                            selector=selector,
                            seam=seam["kind"],
                            status=status,
                            blocker=effective_blocker,
                            base_ref=base_ref,
                            changed=changed,
                            has_tests=bool(test_evidence),
                            allow_service_faults=allow_service_faults,
                        )
                        cells.append(
                            {
                                "id": hashlib.sha256(
                                    f"{target}|{seam['kind']}|{fault}|{property_name}".encode()
                                ).hexdigest()[:16],
                                "operation": target,
                                "seam": seam["kind"],
                                "fault": fault,
                                "property": property_name,
                                "status": status,
                                "blocking_reason": effective_blocker,
                                "next_experiment": experiment,
                            }
                        )
                        add_runtime_fault_cell(str(seam["kind"]), str(fault))
                for profile in profiles:
                    property_name = str(profile["property"])
                    hypotheses.append(
                        {
                            "name": property_name,
                            "epistemic_status": "hypothesis",
                            "profile": profile["kind"],
                            "provenance": provenance,
                        }
                    )
                    status, measured_blocker = _cell_status(function_state, str(profile["fault"]))
                    effective_blocker = blocker or measured_blocker
                    experiment = _next_experiment(
                        module=source_module,
                        target=target,
                        selector=selector,
                        seam=str(profile["kind"]),
                        status=status,
                        blocker=effective_blocker,
                        base_ref=base_ref,
                        changed=changed,
                        has_tests=bool(test_evidence),
                        allow_service_faults=allow_service_faults,
                    )
                    cells.append(
                        {
                            "id": hashlib.sha256(
                                f"{target}|{profile['kind']}|{profile['fault']}|{property_name}".encode()
                            ).hexdigest()[:16],
                            "operation": target,
                            "seam": profile["kind"],
                            "fault": profile["fault"],
                            "property": property_name,
                            "status": status,
                            "blocking_reason": effective_blocker,
                            "next_experiment": experiment,
                        }
                    )
                    add_runtime_fault_cell(str(profile["kind"]), str(profile["fault"]))
                unique_hypotheses = {
                    (item["name"], item.get("profile")): item for item in hypotheses
                }
                operations.append(
                    {
                        "target": target,
                        "selector": selector,
                        "source": f"{relative}:{node.lineno}",
                        "source_sha256": hashlib.sha256(function_source.encode()).hexdigest(),
                        "changed_since_base": changed,
                        "priority": len(cells) + (5 if changed else 0) + (2 if blocker else 0),
                        "seams": seams,
                        "ml_data_profiles": profiles,
                        "candidate_properties": list(unique_hypotheses.values()),
                        "cells": cells,
                    }
                )

        visit(tree.body)
    return sorted(
        operations,
        key=lambda item: (-int(item["priority"]), str(item["target"])),
    )


def _plan_diff(current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare cell identity and status with a previous persisted plan."""
    current_cells = {cell["id"]: cell for cell in current.get("cells", ())}
    previous_cells = {
        cell["id"]: cell for cell in (previous or {}).get("cells", ()) if cell.get("id")
    }
    shared = set(current_cells) & set(previous_cells)
    new_cells = sorted(set(current_cells) - set(previous_cells))
    removed_cells = sorted(set(previous_cells) - set(current_cells))
    status_changes = [
        {
            "id": cell_id,
            "before": previous_cells[cell_id].get("status"),
            "after": current_cells[cell_id].get("status"),
        }
        for cell_id in sorted(shared)
        if previous_cells[cell_id].get("status") != current_cells[cell_id].get("status")
    ]
    current_operations = {
        operation["id"]: operation
        for operation in current.get("operations", ())
        if operation.get("id")
    }
    previous_operations = {
        operation["id"]: operation
        for operation in (previous or {}).get("operations", ())
        if operation.get("id")
    }
    shared_operations = set(current_operations) & set(previous_operations)
    new_operations = sorted(set(current_operations) - set(previous_operations))
    removed_operations = sorted(set(previous_operations) - set(current_operations))
    source_changes = [
        {
            "id": operation_id,
            "target": current_operations[operation_id].get("target"),
            "before_sha256": previous_operations[operation_id].get("source_sha256"),
            "after_sha256": current_operations[operation_id].get("source_sha256"),
        }
        for operation_id in sorted(shared_operations)
        if previous_operations[operation_id].get("source_sha256")
        != current_operations[operation_id].get("source_sha256")
    ]
    bounded_lists = (
        new_cells,
        removed_cells,
        status_changes,
        new_operations,
        removed_operations,
        source_changes,
    )
    return {
        "new_cell_count": len(new_cells),
        "new_cells": new_cells[:50],
        "removed_cell_count": len(removed_cells),
        "removed_cells": removed_cells[:50],
        "status_change_count": len(status_changes),
        "status_changes": status_changes[:50],
        "new_operation_count": len(new_operations),
        "new_operations": new_operations[:50],
        "removed_operation_count": len(removed_operations),
        "removed_operations": removed_operations[:50],
        "source_change_count": len(source_changes),
        "source_changes": source_changes[:50],
        "truncated": any(len(items) > 50 for items in bounded_lists),
        "retained_cells": len(shared),
    }


def _merge_productive_hints(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> dict[str, list[Any]]:
    """Carry prior seed/config hints into the current plan without duplicates."""
    prior = (previous or {}).get("productive_hints", {})
    merged: dict[str, list[Any]] = {}
    for key in ("input_sources", "config_suggestions"):
        values: list[Any] = []
        seen: set[str] = set()
        for value in (*prior.get(key, ()), *current.get(key, ())):
            identity = json.dumps(value, sort_keys=True, default=str)
            if identity in seen:
                continue
            seen.add(identity)
            values.append(value)
        merged[key] = values
    return merged


def _default_reliability_map_path(module: str) -> Path:
    """Return the default persisted plan path for one module."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", module).strip("._") or "project"
    return Path(".ordeal") / "evidence-plans" / f"{safe}.json"


def _load_reliability_map(path: Path) -> dict[str, Any] | None:
    """Read one prior map if it has the supported schema."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if payload.get("schema") == RELIABILITY_MAP_SCHEMA else None


def _write_reliability_map(path: Path, payload: Mapping[str, Any]) -> Path:
    """Persist a deterministic reliability map and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run_fault_probe(
    module: str,
    selector: str,
    fault: str,
    *,
    max_examples: int,
    scan_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one exact fault/no-uncaught-exception observation."""
    from ordeal.auto import (
        _infer_faults,
        _resolve_module,
        _selected_public_functions,
        _source_bound_subprocess_match,
        _unwrap,
        scan_module,
    )

    property_name = _runtime_fault_property(fault)
    target = f"{module}.{selector}"
    observation: dict[str, Any] = {
        "target": target,
        "fault": fault,
        "property": property_name,
        "status": "NOT EXERCISED",
        "blocking_reason": None,
    }
    if not _fault_probe_supported(fault):
        observation["blocking_reason"] = f"no safe Python fault probe is registered for {fault}"
        return observation

    kwargs = dict(scan_kwargs or {})
    try:
        mod = _resolve_module(module)
        selected = _selected_public_functions(
            mod,
            targets=[selector],
            object_factories=kwargs.get("object_factories"),
            object_setups=kwargs.get("object_setups"),
            object_scenarios=kwargs.get("object_scenarios"),
            object_state_factories=kwargs.get("object_state_factories"),
            object_teardowns=kwargs.get("object_teardowns"),
            object_harnesses=kwargs.get("object_harnesses"),
        )
        resolved = _unwrap(selected[0][1]) if len(selected) == 1 else None
        if resolved is not None:
            target = f"{resolved.__module__}.{resolved.__qualname__}"
            observation["target"] = target
        if fault == "timeout" and resolved is not None:
            from ordeal.faults.io import subprocess_timeout

            source = textwrap.dedent(inspect.getsource(inspect.unwrap(resolved)))
            tree = ast.parse(source)
            inferred = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _call_name(node) != "subprocess.run":
                    continue
                command_match = _source_bound_subprocess_match(node)
                if command_match is None:
                    continue
                inferred_fault = subprocess_timeout(command_match)
                setattr(inferred_fault, "__ordeal_operation__", selector)
                setattr(inferred_fault, "__ordeal_fault_kind__", "subprocess_timeout")
                setattr(inferred_fault, "__ordeal_source_match__", command_match)
                setattr(
                    inferred_fault,
                    "__ordeal_source_location__",
                    (node.lineno, node.col_offset),
                )
                inferred.append(inferred_fault)
        else:
            inferred = _infer_faults(
                mod,
                module,
                object_factories=kwargs.get("object_factories"),
                object_setups=kwargs.get("object_setups"),
                object_scenarios=kwargs.get("object_scenarios"),
                object_state_factories=kwargs.get("object_state_factories"),
                object_teardowns=kwargs.get("object_teardowns"),
                object_harnesses=kwargs.get("object_harnesses"),
            )
    except Exception as exc:
        observation["blocking_reason"] = (f"fault discovery failed: {type(exc).__name__}: {exc}")[
            :500
        ]
        return observation

    aliases = _FAULT_PROBE_ALIASES[fault]
    candidates = [
        item
        for item in inferred
        if getattr(item, "__ordeal_operation__", None) == selector
        and getattr(item, "__ordeal_fault_kind__", None) in aliases
    ]
    if not candidates:
        observation["blocking_reason"] = (
            f"source analysis found no injectable {fault} boundary for {target}"
        )
        return observation
    if len(candidates) != 1:
        observation["blocking_reason"] = (
            f"source analysis found {len(candidates)} injectable {fault} boundaries for "
            f"{target}; automatic closure requires exactly one"
        )
        return observation

    injected = candidates[0]
    try:
        injected.reset()
        injected.activate()
        if not injected.active:
            observation["blocking_reason"] = f"the {fault} injection boundary could not activate"
            return observation
        result = scan_module(
            mod,
            targets=[selector],
            max_examples=max_examples,
            **kwargs,
        )
        hits = int(getattr(injected, "observation_hits", 0))
    except Exception as exc:
        observation["blocking_reason"] = (
            f"fault probe failed inside Ordeal: {type(exc).__name__}: {exc}"
        )[:500]
        return observation
    finally:
        injected.deactivate()

    observation["injection"] = {
        "kind": str(getattr(injected, "__ordeal_fault_kind__", fault)),
        "name": str(getattr(injected, "name", fault)),
        "hits": hits,
    }
    if hits == 0:
        observation["blocking_reason"] = (
            "the fault activated but the target did not reach its injection boundary"
        )
        return observation
    if len(result.functions) != 1:
        observation["blocking_reason"] = (
            f"targeted probe returned {len(result.functions)} function results instead of one"
        )
        return observation

    function = result.functions[0]
    observation["evidence"] = {
        "verdict": function.verdict,
        "error_type": function.error_type,
        "error": function.error,
        "replay_attempts": function.replay_attempts,
        "replay_matches": function.replay_matches,
    }
    if function.limitation_kind is not None:
        observation["blocking_reason"] = function.blocking_reason or function.limitation_kind
    elif function.execution_ok and function.verdict != "expected_precondition_failure":
        observation["status"] = "PASS"
    elif function.verdict == "expected_precondition_failure":
        observation["blocking_reason"] = (
            "the operation raised an expected precondition exception; it did not return cleanly"
        )
    elif function.replayable:
        observation["status"] = "FAIL"
    else:
        observation["blocking_reason"] = (
            "the injected fault produced an unreplayed outcome; no cell verdict was promoted"
        )
    return observation


def _build_reliability_map(
    module: str,
    state: Any,
    surface_rows: Sequence[Mapping[str, Any]],
    *,
    base_ref: str | None = None,
    allow_service_faults: bool = False,
    previous_path: Path | None = None,
) -> dict[str, Any]:
    """Build the source-backed reliability map consumed by scan reports."""
    operations = _operation_records(
        module,
        state,
        surface_rows,
        base_ref=base_ref,
        allow_service_faults=allow_service_faults,
    )
    cells = [cell for operation in operations for cell in operation["cells"]]
    counts = {
        status.lower().replace(" ", "_"): sum(1 for cell in cells if cell["status"] == status)
        for status in ("PASS", "NOT EXERCISED", "FAIL")
    }
    counts["blocked"] = sum(1 for cell in cells if cell.get("blocking_reason"))
    safe_experiments = [
        cell["next_experiment"]
        for cell in cells
        if cell["status"] == "NOT EXERCISED"
        and cell["next_experiment"].get("safety") == "safe"
        and cell["next_experiment"].get("auto_runnable")
    ]
    experiment_catalog: dict[str, dict[str, Any]] = {}
    property_catalog: dict[str, dict[str, str]] = {}
    for operation in operations:
        operation_id = hashlib.sha256(str(operation["target"]).encode()).hexdigest()[:16]
        operation["id"] = operation_id
        operation_provenance: set[str] = set()
        test_evidence: set[str] = set()
        property_ids: set[str] = set()
        for candidate in operation["candidate_properties"]:
            for record in candidate.pop("provenance", []):
                kind = str(record.get("kind") or "source")
                operation_provenance.add(kind)
                if kind == "test" and record.get("evidence"):
                    test_evidence.add(str(record["evidence"]))
            property_name = str(candidate["name"])
            property_id = hashlib.sha256(property_name.encode()).hexdigest()[:16]
            property_catalog[property_id] = {
                "id": property_id,
                "name": property_name,
                "epistemic_status": "hypothesis",
            }
            property_ids.add(property_id)
        operation["property_ids"] = sorted(property_ids)
        operation["provenance"] = sorted(operation_provenance)
        operation["test_evidence"] = sorted(test_evidence)
        operation.pop("candidate_properties", None)
        operation["seams"] = sorted({str(item["kind"]) for item in operation["seams"]})
        operation["ml_data_profiles"] = sorted(
            {str(item["kind"]) for item in operation["ml_data_profiles"]}
        )
        for cell in operation["cells"]:
            property_name = str(cell.pop("property"))
            property_id = hashlib.sha256(property_name.encode()).hexdigest()[:16]
            property_catalog[property_id] = {
                "id": property_id,
                "name": property_name,
                "epistemic_status": "hypothesis",
            }
            experiment = dict(cell.pop("next_experiment"))
            encoded = json.dumps(experiment, sort_keys=True)
            experiment_id = hashlib.sha256(encoded.encode()).hexdigest()[:16]
            experiment_catalog[experiment_id] = {
                "id": experiment_id,
                **{
                    key: value
                    for key, value in experiment.items()
                    if key not in {"module", "target", "selector"}
                },
            }
            cell.pop("operation", None)
            cell["operation_id"] = operation_id
            cell["property_id"] = property_id
            cell["next_experiment_id"] = experiment_id
    compact_operations = [
        {key: value for key, value in operation.items() if key != "cells"}
        for operation in operations
    ]
    payload: dict[str, Any] = {
        "schema": RELIABILITY_MAP_SCHEMA,
        "module": module,
        "base_ref": base_ref,
        "service_faults_enabled": allow_service_faults,
        "summary": {
            "operations": len(operations),
            "cells": len(cells),
            **counts,
        },
        "operations": compact_operations,
        "cells": cells,
        "properties": sorted(property_catalog.values(), key=lambda item: item["id"]),
        "experiments": sorted(experiment_catalog.values(), key=lambda item: item["id"]),
        "next_experiment": safe_experiments[0] if safe_experiments else None,
        "reliability_observations": list(
            getattr(state, "supervisor_info", {}).get("reliability_observations", ())
        ),
        "productive_hints": {
            "input_sources": sorted(
                {
                    str(item.get("source"))
                    for function in (getattr(state, "functions", {}) or {}).values()
                    for item in getattr(function, "scan_input_sources", ())
                    if item.get("source")
                }
            ),
            "config_suggestions": list(
                getattr(state, "supervisor_info", {}).get("config_suggestions", ())
            ),
        },
    }
    previous = _load_reliability_map(previous_path) if previous_path is not None else None
    payload["continuity"] = _plan_diff(payload, previous)
    if previous is not None:
        payload["continuity"]["carried_forward_hints"] = previous.get("productive_hints", {})
        payload["productive_hints"] = _merge_productive_hints(
            payload["productive_hints"], previous
        )
    return payload
