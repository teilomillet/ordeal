"""Documentation gates for the Evidence Closure workflow."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    ROOT / "docs/concepts/evidence-closure.md",
    ROOT / "docs/guides/evidence-closure.md",
    ROOT / "docs/reference/evidence-closure-schema.md",
)


def test_evidence_closure_pages_exist_stay_focused_and_are_in_navigation() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES:
        assert path.is_file()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 40 <= len(lines) <= 130
        assert path.relative_to(ROOT / "docs").as_posix() in nav


def test_evidence_closure_docs_state_the_epistemic_and_safety_contracts() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES)
    for phrase in (
        "PASS",
        "NOT EXERCISED",
        "FAIL",
        "hypothesis",
        "--deepen",
        "--time-limit",
        "--allow-service-faults",
        "ordeal.reliability-map/v1",
        "target behavior was not observed",
    ):
        assert phrase in corpus


def test_evidence_closure_is_exposed_on_human_and_agent_surfaces() -> None:
    for relative in (
        "README.md",
        "docs/index.md",
        "AGENTS.md",
        "docs/guides/cli.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "reliability map" in text or "evidence closure" in text
