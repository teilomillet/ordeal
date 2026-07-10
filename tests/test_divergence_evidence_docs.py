"""Discoverability and scope checks for divergence-evidence documentation."""

from __future__ import annotations

import re
from pathlib import Path

from ordeal import catalog

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "concept": ROOT / "docs/concepts/divergence-evidence.md",
    "workflow": ROOT / "docs/guides/divergence-evidence.md",
    "troubleshooting": ROOT / "docs/guides/divergence-evidence-troubleshooting.md",
    "schema": ROOT / "docs/reference/divergence-evidence-schema.md",
}


def test_each_audience_has_a_short_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.is_file(), f"missing {audience} page: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 45 <= len(lines) <= 130, f"{path.name} should stay focused: {len(lines)}"


def test_navigation_and_entrypoints_expose_divergence_evidence() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for page in PAGES.values():
        assert page.relative_to(ROOT / "docs").as_posix() in nav

    for relative in (
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/index.md",
        "docs/getting-started.md",
        "docs/guides/cli.md",
        "docs/reference/api.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "divergence evidence" in text, f"{relative} hides divergence evidence"


def test_local_links_resolve() -> None:
    for page in PAGES.values():
        text = page.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (page.parent / target).resolve().exists(), (
                f"broken link in {page.name}: {raw_target}"
            )


def test_docs_induct_from_story_to_machine_contract() -> None:
    concept = PAGES["concept"].read_text(encoding="utf-8")
    assert "Imagine you rewrote" in concept
    assert "same input" in concept
    assert "comparator" in concept and "normalizer" in concept
    assert "no divergence observed" in concept.lower()
    assert "does **not** establish" in concept

    workflow = PAGES["workflow"].read_text(encoding="utf-8")
    assert "ordeal diff mypkg.pricing" in workflow
    assert 'artifact_dir=".ordeal/divergences"' in workflow
    assert "--save-artifacts" in workflow
    assert "--replay-attempts 3" in workflow


def test_schema_names_every_required_divergence_field() -> None:
    schema = PAGES["schema"].read_text(encoding="utf-8")
    for field in (
        "ordeal.divergence-evidence/v1",
        "revisions",
        "comparison",
        "witness",
        "observations",
        "replay",
        "boundaries",
        "source_sha256",
        "original_input",
        "exact_matches",
    ):
        assert field in schema
    assert "supported" in schema and "exploratory" in schema


def test_runtime_catalog_routes_people_to_the_artifact_learning_path() -> None:
    discovered = catalog()
    diff_entry = next(entry for entry in discovered["diff"] if entry["name"] == "diff")
    cli_entry = next(entry for entry in discovered["cli"] if entry["name"] == "diff")
    for entry in (diff_entry, cli_entry):
        assert "docs/concepts/divergence-evidence.md" in entry["learn_more"]
        assert "docs/guides/divergence-evidence.md" in entry["learn_more"]
        assert "docs/reference/divergence-evidence-schema.md" in entry["learn_more"]
