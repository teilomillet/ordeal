"""Contextual capability suggestions — auto-derived from results.

Every ordeal result (MineResult, MutationResult, ScanResult, etc.)
can ask: "given what I found, what capabilities are relevant next?"

This module answers that question by inspecting result attributes
and matching them to catalog capabilities.  When new capabilities
are added to ordeal, they appear in suggestions automatically if
they match the result context.

The suggestions are informational, not prescriptive.  They tell the
AI what EXISTS and is RELEVANT, not what to DO.
"""

from __future__ import annotations

from typing import Any


def suggest(result: Any) -> list[str]:
    """Return contextual capability suggestions for a result object.

    Inspects the result's attributes to determine which ordeal
    capabilities are relevant.  Returns a list of short descriptions.

    Works with any result type — MineResult, MutationResult,
    ScanResult, FuzzResult, ExplorationState, etc.  Unknown types
    return an empty list.

    This is auto-derived from the result, not hardcoded per type.
    When new capabilities are added, they appear here if they match
    the result's attributes.
    """
    suggestions: list[str] = []

    # Saturation → concolic can crack new branches
    if getattr(result, "saturated", False):
        suggestions.append("concolic (crack saturated branches: pip install crosshair-tool)")

    # Has properties with < 100% confidence → mutation testing verifies them
    properties = getattr(result, "properties", [])
    if properties:
        suspicious = [
            p for p in properties if hasattr(p, "confidence") and 0.85 < p.confidence < 1.0
        ]
        if suspicious:
            suggestions.append("mutate (verify suspicious properties catch real bugs)")

    # Has survived mutants → equivalence detection + test stubs
    survived = getattr(result, "survived", [])
    if survived:
        suggestions.append("equivalence (detect if survivors are equivalent mutants)")
        suggestions.append("generate_test_stubs() (auto-generate tests for gaps)")

    # Mutation preset not thorough → more operators available
    preset = getattr(result, "preset_used", None)
    if preset and preset != "thorough":
        suggestions.append("preset='thorough' (test all 14+ operators)")

    # Has functions (scan/explore result) → deeper analysis available
    functions = getattr(result, "functions", None)
    if isinstance(functions, dict):
        # ExplorationState
        any_not_mined = any(not getattr(f, "mined", True) for f in functions.values())
        any_not_mutated = any(not getattr(f, "mutated", True) for f in functions.values())
        if any_not_mined:
            suggestions.append("mine (discover properties for untested functions)")
        if any_not_mutated:
            suggestions.append("mutate (test code change detection)")
    elif isinstance(functions, list):
        # ScanResult
        has_failures = any(not getattr(f, "passed", True) for f in functions)
        has_warnings = any(getattr(f, "property_violations", []) for f in functions)
        if has_failures:
            suggestions.append("chaos_for (stateful fault injection testing)")
        if has_warnings:
            suggestions.append("mine (deeper property analysis)")

    # Has edges but few → more examples might help
    edges = getattr(result, "edges_discovered", 0)
    if edges and not getattr(result, "saturated", False):
        suggestions.append("more max_examples (still finding new edges)")

    # Has branch points → CMPLOG is working
    branch_points = getattr(result, "branch_points", {})
    if branch_points and not getattr(result, "saturated", False):
        cracked = getattr(result, "branches_cracked", 0)
        total = sum(len(v) for v in branch_points.values())
        if cracked < total:
            suggestions.append(f"CMPLOG cracked {cracked}/{total} branches")

    # patch_io not active → available for I/O-heavy code
    supervisor_info = getattr(result, "supervisor_info", {})
    if supervisor_info and not supervisor_info.get("patch_io"):
        suggestions.append("patch_io (deterministic file/network/subprocess I/O)")

    # State tree available
    tree = getattr(result, "tree", None)
    if tree and getattr(tree, "size", 0) > 1:
        suggestions.append(f"state tree rollback ({tree.size} checkpoints available)")

    # Source-hash refresh — functions re-explored due to code changes
    refreshed = getattr(result, "refreshed", [])
    if refreshed:
        names = ", ".join(refreshed[:5])
        tail = f" +{len(refreshed) - 5} more" if len(refreshed) > 5 else ""
        suggestions.append(f"refreshed ({len(refreshed)} source changed: {names}{tail})")

    return suggestions


def format_suggestions(result: Any) -> str:
    """Format suggestions as a single line for summary output.

    Returns empty string if no suggestions.
    """
    items = suggest(result)
    if not items:
        return ""
    return f"  available: {', '.join(items)}"
