"""Discoverability and scope checks for test-protection documentation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "plain_language": ROOT / "docs/concepts/test-meaningfulness.md",
    "workflow": ROOT / "docs/guides/test-protection.md",
    "ci": ROOT / "docs/guides/test-protection-ci.md",
    "faq": ROOT / "docs/guides/test-protection-faq.md",
    "schema": ROOT / "docs/reference/test-protection-schema.md",
}


def test_each_audience_has_a_short_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.exists(), f"missing {audience} documentation: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 20 <= len(lines) <= 130, f"{path} should stay focused: {len(lines)} lines"


def test_navigation_exposes_every_test_protection_page() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES.values():
        relative = path.relative_to(ROOT / "docs").as_posix()
        assert relative in nav


def test_docs_preserve_the_epistemic_boundary() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values())
    assert "100% line coverage" in corpus
    assert "protective_within_measured_scope" in corpus
    assert "not a universal correctness proof" in corpus
    assert "generated/migrated checks" in corpus
    assert "selected existing" in corpus


def test_human_and_agent_entrypoints_link_to_test_protection() -> None:
    for relative in ("README.md", "docs/index.md", "AGENTS.md", "ordeal/SKILL.md"):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "test protection" in text, f"{relative} does not expose test protection"
