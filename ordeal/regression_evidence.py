"""Semantic integrity bindings for generated witness regressions."""

from __future__ import annotations

import ast
import hashlib
import json
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_REGRESSION_BINDING_SCHEMA = "ordeal.regression-binding/v1"
_COMPOSE_REGRESSION_BINDING_SCHEMA = "ordeal.compose-regression-binding/v1"


def _encode_replay_value(value: object) -> object:
    """Encode common values without losing insertion order or shared aliases."""
    memo: dict[int, int] = {}

    def encode(item: object) -> object:
        """Encode one value into the current reference graph."""
        if item is None or type(item) in {bool, int, str}:
            return item
        if type(item) is float:
            return {
                "__ordeal_type__": "float",
                "bits": struct.pack(">d", item).hex(),
            }
        if type(item) is bytes:
            return {"__ordeal_type__": "bytes", "hex": item.hex()}
        if type(item) is type(Path()):
            return {"__ordeal_type__": "path", "value": item.as_posix()}

        identity = id(item)
        if identity in memo:
            return {"__ordeal_type__": "ref", "id": memo[identity]}
        reference = len(memo)
        memo[identity] = reference

        if type(item) is list:
            return {
                "__ordeal_type__": "list",
                "id": reference,
                "items": [encode(child) for child in item],
            }
        if type(item) is tuple:
            return {
                "__ordeal_type__": "tuple",
                "id": reference,
                "items": [encode(child) for child in item],
            }
        if type(item) in {set, frozenset}:
            ordered = sorted(
                item,
                key=lambda child: json.dumps(
                    _encode_replay_value(child),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return {
                "__ordeal_type__": ("frozenset" if type(item) is frozenset else "set"),
                "id": reference,
                "items": [encode(child) for child in ordered],
            }
        if type(item) is dict:
            return {
                "__ordeal_type__": "dict",
                "id": reference,
                "items": [[encode(key), encode(child)] for key, child in item.items()],
            }
        raise TypeError(
            f"{type(item).__module__}.{type(item).__qualname__} is not a replayable literal"
        )

    encoded = encode(value)
    try:
        _decode_replay_value(encoded)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "value contains a reference cycle that cannot be reconstructed exactly"
        ) from exc
    return encoded


def _decode_replay_value(value: object) -> object:
    """Decode data written by :func:`_encode_replay_value`."""
    memo: dict[int, object] = {}
    pending = object()

    def reference_id(payload: Mapping[str, object]) -> int | None:
        """Validate and return an optional graph reference identifier."""
        if "id" not in payload:
            return None
        identifier = payload["id"]
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0:
            raise ValueError("ordeal replay reference IDs must be non-negative integers")
        if identifier in memo:
            raise ValueError(f"duplicate ordeal replay reference ID: {identifier}")
        return identifier

    def decode(item: object) -> object:
        """Decode one value from the current reference graph."""
        if isinstance(item, list):
            return [decode(child) for child in item]
        if not isinstance(item, dict) or "__ordeal_type__" not in item:
            return item
        kind = item["__ordeal_type__"]
        if kind == "ref":
            identifier = item.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, int):
                raise ValueError("ordeal replay references require an integer ID")
            if identifier not in memo or memo[identifier] is pending:
                raise ValueError(f"unknown or cyclic ordeal replay reference: {identifier}")
            return memo[identifier]
        if kind == "bytes":
            return bytes.fromhex(str(item["hex"]))
        if kind == "path":
            return Path(str(item["value"]))
        if kind == "float":
            if "bits" in item:
                raw = bytes.fromhex(str(item["bits"]))
                if len(raw) != 8:
                    raise ValueError("ordeal replay floats require exactly 8 bytes")
                return struct.unpack(">d", raw)[0]
            # Decode pre-bit-exact artifacts written by older Ordeal releases.
            return float(str(item["value"]))

        items = list(item.get("items", []))
        identifier = reference_id(item)
        if kind == "list":
            result: list[object] = []
            if identifier is not None:
                memo[identifier] = result
            result.extend(decode(child) for child in items)
            return result
        if kind == "dict":
            result_dict: dict[object, object] = {}
            if identifier is not None:
                memo[identifier] = result_dict
            for pair in items:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("ordeal replay dictionaries require key/value pairs")
                result_dict[decode(pair[0])] = decode(pair[1])
            return result_dict
        if kind == "set":
            result_set: set[object] = set()
            if identifier is not None:
                memo[identifier] = result_set
            result_set.update(decode(child) for child in items)
            return result_set
        if kind in {"tuple", "frozenset"}:
            if identifier is not None:
                memo[identifier] = pending
            decoded_items = [decode(child) for child in items]
            result_immutable: object = (
                tuple(decoded_items) if kind == "tuple" else frozenset(decoded_items)
            )
            if identifier is not None:
                memo[identifier] = result_immutable
            return result_immutable
        raise ValueError(f"unknown ordeal replay value type: {kind!r}")

    return decode(value)


def _ast_sha256(node: ast.AST) -> str:
    """Hash semantic Python structure without formatting or location noise."""
    payload = ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _global_load_names(test: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return names the test resolves from module globals or builtins."""
    local_names = {
        node.id
        for node in ast.walk(test)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    local_names.update(arg.arg for arg in test.args.posonlyargs)
    local_names.update(arg.arg for arg in test.args.args)
    local_names.update(arg.arg for arg in test.args.kwonlyargs)
    if test.args.vararg is not None:
        local_names.add(test.args.vararg.arg)
    if test.args.kwarg is not None:
        local_names.add(test.args.kwarg.arg)
    return {
        node.id
        for node in ast.walk(test)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id not in local_names
    }


def _directly_bound_names(node: ast.stmt) -> set[str]:
    """Return names directly bound by one top-level statement."""
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.partition(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    targets: list[ast.AST] = []
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
    return {
        child.id
        for target in targets
        for child in ast.walk(target)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _is_dynamic_top_level(node: ast.stmt) -> bool:
    """Return whether a statement may mutate globals without a direct binding."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return False
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return bool(node.decorator_list or node.args.defaults or any(node.args.kw_defaults))
    return not isinstance(node, ast.Expr) or not (
        isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    )


def _global_binding_hashes(
    tree: ast.Module,
    test: ast.FunctionDef | ast.AsyncFunctionDef,
    global_names: set[str],
) -> list[str]:
    """Hash ordered module statements that can change the test's global resolution."""
    hashes: list[str] = []
    for node in tree.body:
        if node is test:
            continue
        bound_names = _directly_bound_names(node)
        if "*" in bound_names or bound_names & global_names or _is_dynamic_top_level(node):
            hashes.append(_ast_sha256(node))
    return hashes


def _regression_binding(source: str, test_name: str) -> dict[str, Any] | None:
    """Bind one generated test and its imports to stable AST hashes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    tests = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name
    ]
    if len(tests) != 1:
        return None
    test = tests[0]
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    global_names = _global_load_names(test)
    return {
        "schema": _REGRESSION_BINDING_SCHEMA,
        "test_name": test_name,
        "test_ast_sha256": _ast_sha256(test),
        "import_ast_sha256": sorted(_ast_sha256(node) for node in imports),
        "global_names": sorted(global_names),
        "global_binding_ast_sha256": _global_binding_hashes(tree, test, global_names),
    }


def _regression_binding_matches(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> bool:
    """Return whether an observed file preserves the bound test and imports."""
    if expected.get("schema") != _REGRESSION_BINDING_SCHEMA:
        return False
    if observed.get("schema") != _REGRESSION_BINDING_SCHEMA:
        return False
    if expected.get("test_name") != observed.get("test_name"):
        return False
    if expected.get("test_ast_sha256") != observed.get("test_ast_sha256"):
        return False
    if expected.get("global_names") != observed.get("global_names"):
        return False
    if expected.get("global_binding_ast_sha256") != observed.get("global_binding_ast_sha256"):
        return False
    expected_imports = {str(value) for value in expected.get("import_ast_sha256", ())}
    observed_imports = {str(value) for value in observed.get("import_ast_sha256", ())}
    return expected_imports <= observed_imports


def _register_python_regression(
    *,
    manifest_path: Path,
    finding_id: str,
    change_kind: str,
    target: str,
    test_path: Path,
    test_name: str,
    evidence_path: Path,
    change_artifact_ids: list[str],
    test_basis: str,
    extra: Mapping[str, object] | None = None,
    active: bool = True,
) -> tuple[Path | None, str | None]:
    """Upsert one source-bound Python regression in the shared CI manifest."""
    payload: dict[str, object] = {
        "schema": "ordeal.regression-manifest/v1",
        "regressions": [],
    }
    try:
        if manifest_path.is_file():
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(loaded, dict)
                or loaded.get("schema") != payload["schema"]
                or not isinstance(loaded.get("regressions"), list)
            ):
                raise ValueError(
                    "existing regression manifest does not use the supported v1 schema"
                )
            payload["regressions"] = list(loaded["regressions"])
        records = {
            str(item.get("finding_id")): item
            for item in payload["regressions"]
            if isinstance(item, Mapping) and item.get("finding_id")
        }
        if not active:
            records.pop(finding_id, None)
        else:
            binding = _regression_binding(test_path.read_text(encoding="utf-8"), test_name)
            if binding is None:
                raise ValueError("generated regression could not be source-bound")
            resolved_manifest = manifest_path.resolve()
            workspace = (
                resolved_manifest.parent.parent
                if resolved_manifest.parent.name == "tests"
                else Path.cwd().resolve()
            )
            try:
                test_file = test_path.resolve().relative_to(workspace).as_posix()
                evidence_file = evidence_path.resolve().relative_to(workspace).as_posix()
            except ValueError as exc:
                raise ValueError(
                    "manifest, regression, and evidence must share one workspace"
                ) from exc
            record: dict[str, object] = {
                "finding_id": finding_id,
                "runner": "python",
                "change_kind": change_kind,
                "target": target,
                "test_file": test_file,
                "test_name": test_name,
                "binding": binding,
                "evidence_file": evidence_file,
                "change_artifact_ids": list(change_artifact_ids),
                "test_basis": test_basis,
            }
            if extra is not None:
                record.update(dict(extra))
            records[finding_id] = record
        payload["regressions"] = [records[key] for key in sorted(records)]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)
    return manifest_path, None


def _compose_regression_binding(trace_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an exact, redacted Compose trace to a canonical JSON digest."""
    canonical = json.dumps(trace_payload, sort_keys=True, separators=(",", ":"), default=str)
    actions = trace_payload.get("actions", [])
    return {
        "schema": _COMPOSE_REGRESSION_BINDING_SCHEMA,
        "trace_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "failure_signature": trace_payload.get("failure_signature"),
        "action_count": len(actions) if isinstance(actions, list) else 0,
    }


def _compose_regression_binding_matches(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> bool:
    """Return whether a Compose trace preserves its exact canonical binding."""
    return (
        expected.get("schema") == _COMPOSE_REGRESSION_BINDING_SCHEMA
        and observed.get("schema") == _COMPOSE_REGRESSION_BINDING_SCHEMA
        and expected.get("trace_sha256") == observed.get("trace_sha256")
        and expected.get("failure_signature") == observed.get("failure_signature")
        and expected.get("action_count") == observed.get("action_count")
    )
