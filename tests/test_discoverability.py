"""Discoverability tests — ensure an AI assistant can find ordeal's features.

An AI assistant that runs ``uv add ordeal`` has three discovery paths:

1. ``from ordeal import X`` — top-level imports
2. ``catalog()`` — runtime introspection of all capabilities
3. ``dir(ordeal)`` — tab completion

These tests verify that every major public class and function is
reachable through all three paths.  If a new feature ships without
appearing here, these tests fail — preventing invisible features.
"""

from __future__ import annotations

import ordeal

# ============================================================================
# Top-level imports: ``from ordeal import X`` must work
# ============================================================================

# Every name that an AI assistant should be able to import directly.
# Add new public classes/functions here when they ship.
_TOP_LEVEL_IMPORTS = [
    # Core stateful testing
    "ChaosTest",
    "chaos_test",
    "rule",
    "invariant",
    "initialize",
    "precondition",
    "Bundle",
    # Assertions
    "always",
    "sometimes",
    "reachable",
    "unreachable",
    "report",
    # Inline faults
    "buggify",
    "buggify_value",
    # Discoverability
    "catalog",
    "auto_configure",
    # Mining
    "mine",
    "mine_pair",
    "mine_module",
    # Scanning
    "scan_module",
    "chaos_for",
    "fuzz",
    # Mutation testing
    "mutate",
    "mutate_function_and_test",
    "MutationResult",
    "PRESETS",
    "OPERATORS",
    "NoTestsFoundError",
    "generate_starter_tests",
    "init_project",
    # Exploration — the core value prop
    "explore",
    "ExplorationState",
    "Explorer",
    "ExplorationResult",
    "CoverageCollector",
    # Supervisor
    "DeterministicSupervisor",
    "StateTree",
    # Metamorphic
    "discover_relations",
    # Differential
    "diff",
    # Scaling
    "fit_usl",
    "scales_linearly",
    # Mutagen
    "mutate_value",
    "mutate_inputs",
    # CMPLOG
    "extract_comparison_values",
]


def test_top_level_imports():
    """Every major feature must be importable via ``from ordeal import X``."""
    missing = []
    for name in _TOP_LEVEL_IMPORTS:
        try:
            obj = getattr(ordeal, name)
            assert obj is not None, f"ordeal.{name} is None"
        except AttributeError:
            missing.append(name)
    assert not missing, (
        f"These names are not importable from ordeal: {missing}\n"
        f"Add them to _LAZY_SUBMODULES in ordeal/__init__.py or export them directly."
    )


def test_dir_includes_top_level():
    """``dir(ordeal)`` must include all top-level names for tab completion."""
    available = set(dir(ordeal))
    missing = [name for name in _TOP_LEVEL_IMPORTS if name not in available]
    assert not missing, (
        f"These names are missing from dir(ordeal): {missing}\n"
        f"Check __dir__() in ordeal/__init__.py."
    )


# ============================================================================
# catalog() completeness: every subsystem must be present and non-empty
# ============================================================================

_EXPECTED_CATALOG_SECTIONS = [
    "faults",
    "invariants",
    "assertions",
    "strategies",
    "mutations",
    "integrations",
    "mining",
    "audit",
    "auto",
    "metamorphic",
    "diff",
    "scaling",
    "exploration",
    "supervisor",
    "mutagen",
    "cmplog",
    "equivalence",
]


def test_catalog_sections_present():
    """catalog() must have all expected subsystem keys."""
    c = ordeal.catalog()
    missing = [s for s in _EXPECTED_CATALOG_SECTIONS if s not in c]
    assert not missing, f"Missing catalog sections: {missing}"


def test_catalog_sections_non_empty():
    """Every catalog section must contain at least one item."""
    c = ordeal.catalog()
    empty = [s for s in _EXPECTED_CATALOG_SECTIONS if s in c and len(c[s]) == 0]
    assert not empty, f"Empty catalog sections: {empty}"


def test_catalog_items_have_required_fields():
    """Every catalog item must have at least ``name`` and ``doc``.

    Different subsystems use slightly different schemas (faults have
    ``module`` and ``parameters``, mutations have ``type``), but ``name``
    and ``doc`` are universal — they're what an AI reads first.
    """
    c = ordeal.catalog()
    required = {"name", "doc"}
    bad = []
    for section, items in c.items():
        if section == "skill":
            continue  # skill entries have a different schema (SKILL.md)
        for item in items:
            missing_fields = required - set(item.keys())
            if missing_fields:
                bad.append(f"{section}/{item.get('name', '?')}: missing {missing_fields}")
    assert not bad, "Catalog items with missing fields:\n" + "\n".join(bad)


def test_catalog_docs_are_not_empty():
    """Every catalog item should have a non-empty doc string (first line of docstring)."""
    c = ordeal.catalog()
    undocumented = []
    for section, items in c.items():
        for item in items:
            if not item.get("doc", "").strip():
                undocumented.append(f"{section}/{item['name']}")
    assert not undocumented, "Undocumented items in catalog (add a docstring):\n" + "\n".join(
        f"  {u}" for u in undocumented
    )


# ============================================================================
# Exploration discoverability: the most important features for AI assistants
# ============================================================================

_EXPLORATION_MUST_CONTAIN = [
    "Explorer",
    "ExplorationResult",
    "ExplorationState",
    "Checkpoint",
    "explore",
]


def test_catalog_exploration_contains_key_items():
    """The exploration catalog section must include Explorer, ExplorationState, etc."""
    c = ordeal.catalog()
    names = {item["name"] for item in c["exploration"]}
    missing = [n for n in _EXPLORATION_MUST_CONTAIN if n not in names]
    assert not missing, f"Missing from catalog['exploration']: {missing}\nPresent: {sorted(names)}"


def test_explorer_docstring_mentions_seed_mutation():
    """Explorer's docstring must mention seed mutation — it's the key differentiator."""
    from ordeal.explore import Explorer

    doc = Explorer.__doc__ or ""
    assert "seed mutation" in doc.lower() or "seed_mutation" in doc.lower(), (
        "Explorer docstring must mention seed mutation for discoverability.\n"
        f"First 200 chars: {doc[:200]}"
    )


def test_explorer_docstring_mentions_checkpoint():
    """Explorer's docstring must mention checkpoints."""
    from ordeal.explore import Explorer

    doc = Explorer.__doc__ or ""
    assert "checkpoint" in doc.lower(), (
        "Explorer docstring must mention checkpoints for discoverability."
    )


def test_mutagen_docstrings_reference_users():
    """mutagen functions should say where they're used (mine, Explorer)."""
    from ordeal.mutagen import mutate_inputs, mutate_value

    for fn in [mutate_value, mutate_inputs]:
        doc = fn.__doc__ or ""
        assert "mine" in doc.lower() or "explorer" in doc.lower(), (
            f"{fn.__name__} docstring should reference mine() or Explorer "
            f"so AI assistants understand the connection.\n"
            f"First 200 chars: {doc[:200]}"
        )
