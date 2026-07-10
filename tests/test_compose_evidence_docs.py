"""Discoverability and scope checks for the Compose service evidence loop."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "mental_model": ROOT / "docs/concepts/service-evidence-loop.md",
    "workflow": ROOT / "docs/guides/compose-evidence-loop.md",
    "fixture": ROOT / "tests/fixtures/compose_e2e/README.md",
}


def test_each_audience_has_a_short_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.exists(), f"missing {audience} documentation: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 20 <= len(lines) <= 130, f"{path} should stay focused: {len(lines)} lines"


def test_navigation_exposes_the_mental_model_and_workflow() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for page in (PAGES["mental_model"], PAGES["workflow"]):
        relative = page.relative_to(ROOT / "docs").as_posix()
        assert relative in nav


def test_local_links_resolve() -> None:
    for page in PAGES.values():
        text = page.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved.exists(), f"broken link in {page}: {raw_target}"


def test_docs_cover_the_complete_service_evidence_loop() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values()).lower()
    for stage in (
        "explore",
        "coverage",
        "exact replay",
        "bounded finding",
        "portable regression",
        "verify --ci",
        "workload-strength control",
    ):
        assert stage in corpus
    assert "attempted 3 / reproduced 3" in corpus
    assert "buggy" in corpus and "fixed" in corpus
    assert "not exercised" in corpus
    assert "universal correctness" in corpus


def test_human_and_agent_entrypoints_expose_the_loop() -> None:
    for relative in (
        "README.md",
        "AGENTS.md",
        "docs/index.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "evidence loop" in text, f"{relative} does not expose the service evidence loop"


def test_fixture_readme_and_ci_point_to_the_executable_proof() -> None:
    fixture = PAGES["fixture"].read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for text in (fixture, workflow):
        assert "verify_compose_evidence_loop.py" in text
        assert "compose-evidence-loop.json" in text
