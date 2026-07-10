"""Discoverability and scope checks for scan documentation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "quickstart": ROOT / "docs/guides/scan-quickstart.md",
    "harnesses": ROOT / "docs/guides/scan-object-harnesses.md",
    "troubleshooting": ROOT / "docs/guides/scan-troubleshooting.md",
    "schema": ROOT / "docs/reference/scan-evidence-schema.md",
}


def test_scan_docs_exist_and_stay_short() -> None:
    for audience, path in PAGES.items():
        assert path.is_file(), f"missing {audience} scan page"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 130, f"{path.name} grew to {len(lines)} lines"


def test_scan_docs_are_in_navigation_and_entrypoints() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES.values():
        relative = path.relative_to(ROOT / "docs").as_posix()
        assert relative in nav, f"{relative} is missing from mkdocs nav"

    for relative in ("README.md", "docs/index.md", "AGENTS.md", "ordeal/SKILL.md"):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "scan-quickstart" in text, f"{relative} does not expose scan quickstart"


def test_scan_docs_cover_plain_and_technical_boundaries() -> None:
    quickstart = PAGES["quickstart"].read_text(encoding="utf-8")
    assert "Plain meaning" in quickstart
    assert "does not prove the root cause" in quickstart
    assert "--list-targets" in quickstart

    harnesses = PAGES["harnesses"].read_text(encoding="utf-8")
    assert "factory → setup → scenarios → state injection → method → teardown" in harnesses
    assert 'harness = "stateful"' in harnesses
    assert "<locals>" in harnesses

    schema = PAGES["schema"].read_text(encoding="utf-8")
    assert "terminal source location" in schema
    assert "harness_replay_supported" in schema
    assert "subject.source_sha256" in schema


def test_scan_examples_use_the_real_selector_flag() -> None:
    documented = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values())
    assert "--target " in documented
    assert "--targets" not in documented

    implementation = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "ordeal/parts/auto").glob("*.py"))
    )
    assert 'f"--target {explicit_target} -n 1"' in implementation


def test_human_entrypoints_teach_one_path_before_advanced_tools() -> None:
    for relative in ("README.md", "docs/index.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        beginner = text.split("<details>", maxsplit=1)[0]
        assert "ordeal scan ." in beginner
        assert "ordeal scan . --save" in beginner
        assert "ordeal verify <finding-id>" in beginner
        assert "ordeal verify --ci" in beginner
        assert "ordeal mine" not in beginner
        assert "ordeal audit" not in beginner


def test_navigation_is_grouped_by_user_journey() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert nav.index("- Start Here:") < nav.index("- Advanced Workflows:")
    assert nav.index("- Advanced Workflows:") < nav.index("- Concepts:")
    assert nav.index("- Concepts:") < nav.index("- Reference:")


def test_agent_entrypoints_also_start_with_scan() -> None:
    sections = {
        "AGENTS.md": "**Specialized commands",
        "docs/llms.txt": "## Key capabilities",
        "ordeal/SKILL.md": "Specialized CLI:",
    }
    for relative, boundary in sections.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        beginner = text.split(boundary, maxsplit=1)[0]
        assert "ordeal scan ." in beginner
        assert "ordeal mine" not in beginner
        assert "ordeal audit" not in beginner
