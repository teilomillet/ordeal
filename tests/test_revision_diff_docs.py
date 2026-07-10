"""Discoverability and contract checks for revision-diff documentation."""

from __future__ import annotations

import re
from pathlib import Path

from ordeal import catalog
from ordeal._revision_diff import (
    RevisionDiffResult,
    RevisionFunctionDiff,
    RevisionRuntime,
)
from ordeal.cli import command_catalog

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "quickstart": ROOT / "docs/guides/revision-diff.md",
    "troubleshooting": ROOT / "docs/guides/revision-diff-troubleshooting.md",
    "schema": ROOT / "docs/reference/revision-diff-schema.md",
}


def test_revision_diff_audiences_have_short_dedicated_pages() -> None:
    for audience, path in PAGES.items():
        assert path.is_file(), f"missing {audience} revision-diff page"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 40 <= len(lines) <= 130, f"{path.name} should stay focused: {len(lines)} lines"


def test_revision_diff_pages_are_in_navigation_and_have_valid_links() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for page in PAGES.values():
        relative = page.relative_to(ROOT / "docs").as_posix()
        assert relative in nav, f"{relative} is missing from the documentation nav"
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", page.read_text()):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            assert (page.parent / target).resolve().exists(), (
                f"broken link in {page.name}: {raw_target}"
            )


def test_human_and_agent_entrypoints_expose_the_revision_path() -> None:
    entrypoints = (
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/index.md",
        "docs/getting-started.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
    )
    for relative in entrypoints:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "revision-diff.md" in text or "/revision-diff/" in text
        assert "revision-diff-troubleshooting" in text
        assert "revision-diff-schema" in text


def test_quickstart_is_layman_first_and_keeps_claims_bounded() -> None:
    text = PAGES["quickstart"].read_text(encoding="utf-8")
    lowered = text.lower()
    assert "two sealed rooms" in lowered
    assert "exact same inputs" in lowered
    assert "useful evidence, not proof of equivalence" in text
    assert "does not decide which version is correct" in text
    assert "`head` means committed files only" in lowered
    for status in ("DIVERGENT", "NO DIVERGENCE OBSERVED", "INCONCLUSIVE"):
        assert status in text


def test_documented_flags_match_the_live_diff_parser() -> None:
    entry = next(item for item in command_catalog() if item["name"] == "diff")
    live_flags = {flag for argument in entry["arguments"] for flag in argument.get("flags", [])}
    corpus = "\n".join(page.read_text(encoding="utf-8") for page in PAGES.values())
    for flag in (
        "--base-ref",
        "--candidate-ref",
        "--save-artifacts",
        "--fixture-registry",
        "--replay-attempts",
        "--max-examples",
    ):
        assert flag in live_flags
        assert flag in corpus
    assert "--targets" not in corpus


def test_cli_catalog_routes_to_copyable_examples_and_all_revision_docs() -> None:
    raw_entry = next(item for item in command_catalog() if item["name"] == "diff")
    assert raw_entry["examples"][0].startswith("ordeal diff mypkg.scoring")
    for path in (
        "docs/guides/revision-diff.md",
        "docs/guides/revision-diff-troubleshooting.md",
        "docs/reference/revision-diff-schema.md",
    ):
        assert path in raw_entry["learn_more"]

    runtime_entry = next(item for item in catalog()["cli"] if item["name"] == "diff")
    assert "docs/reference/revision-diff-schema.md" in runtime_entry["learn_more"]


def test_schema_documents_live_result_and_function_fields() -> None:
    runtime = RevisionRuntime(ref="HEAD", commit="a" * 40, pid=1, worktree="/tmp/wt")
    function = RevisionFunctionDiff(
        name="score",
        base_signature="(x: int) -> int",
        candidate_signature="(x: int) -> int",
        total=1,
        mismatch_count=0,
    )
    result = RevisionDiffResult(
        target="mypkg.scoring",
        base=runtime,
        candidate=runtime,
        functions=(function,),
    )
    schema = PAGES["schema"].read_text(encoding="utf-8")
    for field in result.to_dict():
        assert f"`{field}`" in schema, f"top-level field is undocumented: {field}"
    for field in function.to_dict():
        assert f"`{field}`" in schema, f"function field is undocumented: {field}"


def test_troubleshooting_covers_fail_closed_boundaries() -> None:
    text = PAGES["troubleshooting"].read_text(encoding="utf-8")
    lowered = " ".join(text.lower().split())
    for phrase in (
        "uncommitted",
        "could not infer a base revision",
        "cannot infer strategies",
        "object factory/harness",
        "inconclusive",
        "untrusted pull request",
    ):
        assert phrase in lowered
