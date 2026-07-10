"""Discoverability and scope checks for differential-testing documentation."""

from __future__ import annotations

import re
from pathlib import Path

from ordeal import catalog
from ordeal.cli import _diff_command_description

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "plain_language": ROOT / "docs/concepts/differential-testing.md",
    "quickstart": ROOT / "docs/guides/differential-quickstart.md",
    "state_and_effects": ROOT / "docs/guides/differential-state-and-effects.md",
    "evidence": ROOT / "docs/guides/differential-evidence.md",
}


def test_each_differential_audience_has_a_short_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.is_file(), f"missing {audience} differential page"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 40 <= len(lines) <= 130, f"{path.name} should stay focused: {len(lines)} lines"


def test_differential_pages_are_in_navigation() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES.values():
        relative = path.relative_to(ROOT / "docs").as_posix()
        assert relative in nav, f"{relative} is missing from the documentation nav"


def test_differential_pages_have_no_broken_local_links() -> None:
    for page in PAGES.values():
        text = page.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved.exists(), f"broken link in {page.name}: {raw_target}"


def test_layman_path_builds_to_the_full_soundness_contract() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values())
    lowered = corpus.lower()
    assert "imagine two cashiers" in lowered
    for field in (
        "return value",
        "exception type and message",
        "mutated arguments",
        "receiver state",
        "selected side effects",
    ):
        assert field in lowered
    for status in (
        "divergent",
        "no_divergence_observed",
        "proven_equivalent",
        "inconclusive",
    ):
        assert status in corpus
    assert "not proof of equivalence" in lowered
    assert "AssertionError" in corpus
    assert "private mismatch" in lowered


def test_docs_explain_one_minimized_replay_verified_witness() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values())
    lowered = corpus.lower()
    assert "only the final minimized candidate" in lowered
    assert re.search(r"intermediate candidates are\s+not collected", lowered)
    assert "replay_verified is True" in corpus
    assert "inconclusive" in lowered
    assert "expose no witness" in lowered


def test_human_and_agent_entrypoints_expose_the_quickstart() -> None:
    entrypoints = (
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/index.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
    )
    for relative in entrypoints:
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "differential-quickstart" in text, f"{relative} hides the quickstart"


def test_runtime_catalog_teaches_and_routes_the_diff_entrypoint() -> None:
    discovered = catalog()
    entries = discovered["diff"]
    diff_entry = next(entry for entry in entries if entry["name"] == "diff")
    assert "docs/concepts/differential-testing.md" in diff_entry["learn_more"]
    assert "docs/guides/differential-quickstart.md" in diff_entry["learn_more"]
    examples = "\n".join(diff_entry["examples"])
    assert "result.status" in examples
    assert "sequence=[Operation('read')]" in examples

    cli_entry = next(entry for entry in discovered["cli"] if entry["name"] == "diff")
    assert "docs/guides/revision-diff.md" in cli_entry["learn_more"]
    assert "docs/concepts/differential-testing.md" in cli_entry["learn_more"]


def test_revision_diff_help_routes_beginners_and_experienced_users() -> None:
    help_text = _diff_command_description()
    assert "https://docs.byordeal.com/guides/revision-diff/" in help_text
    assert "https://docs.byordeal.com/concepts/differential-testing/" in help_text
    assert "never as proven equivalence" in help_text
