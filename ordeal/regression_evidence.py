"""Semantic integrity bindings for generated witness regressions."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from typing import Any

_REGRESSION_BINDING_SCHEMA = "ordeal.regression-binding/v1"
_COMPOSE_REGRESSION_BINDING_SCHEMA = "ordeal.compose-regression-binding/v1"


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
