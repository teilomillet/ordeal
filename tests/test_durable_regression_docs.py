"""Discoverability and scope checks for durable-regression documentation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "plain_language": ROOT / "docs/concepts/durable-regressions.md",
    "workflow": ROOT / "docs/guides/durable-regressions.md",
    "ci": ROOT / "docs/guides/durable-regressions-ci.md",
    "faq": ROOT / "docs/guides/durable-regressions-faq.md",
    "schema": ROOT / "docs/reference/durable-regression-schema.md",
}


def test_each_audience_has_a_short_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.exists(), f"missing {audience} documentation: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 20 <= len(lines) <= 130, f"{path} should stay focused: {len(lines)} lines"


def test_navigation_exposes_every_durable_regression_page() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES.values():
        relative = path.relative_to(ROOT / "docs").as_posix()
        assert relative in nav


def test_local_links_from_durable_regression_pages_resolve() -> None:
    for page in PAGES.values():
        text = page.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved.exists(), f"broken link in {page}: {raw_target}"


def test_docs_cover_the_complete_bounded_workflow() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values())
    for stage in (
        "discover",
        "reproduce",
        "minimize",
        "save regression",
        "verify fix",
        "guard ci",
    ):
        assert stage in corpus.lower()
    assert "not a whole-project correctness certificate" in corpus
    assert "ordeal verify --ci" in corpus
    assert "tests/ordeal-regressions.json" in corpus


def test_schema_reference_names_every_machine_contract() -> None:
    schema = PAGES["schema"].read_text(encoding="utf-8")
    assert "ordeal.finding-evidence/v1" in schema
    assert "ordeal.divergence-evidence/v1" in schema
    assert "ordeal.regression-binding/v1" in schema
    assert "ordeal.regression-manifest/v1" in schema


def test_human_and_agent_entrypoints_link_to_durable_regressions() -> None:
    for relative in (
        "README.md",
        "AGENTS.md",
        "docs/index.md",
        "docs/getting-started.md",
        "docs/troubleshooting.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "durable regression" in text, f"{relative} does not expose durable regressions"
