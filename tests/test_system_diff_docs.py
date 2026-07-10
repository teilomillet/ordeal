"""Learning-path and discoverability checks for system differential testing."""

from __future__ import annotations

import re
from pathlib import Path

import ordeal

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "mental_model": ROOT / "docs/concepts/system-differential.md",
    "first_run": ROOT / "docs/guides/system-differential.md",
    "recipes": ROOT / "docs/guides/system-differential-recipes.md",
    "troubleshooting": ROOT / "docs/guides/system-differential-troubleshooting.md",
    "reference": ROOT / "docs/reference/system-differential.md",
}


def test_each_audience_has_a_focused_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.exists(), f"missing {audience} documentation: {path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 50 <= len(lines) <= 130, f"{path} should stay focused: {len(lines)} lines"


def test_navigation_exposes_the_complete_system_learning_path() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES.values():
        relative = path.relative_to(ROOT / "docs").as_posix()
        assert relative in nav, f"{relative} is missing from MkDocs navigation"


def test_local_links_from_system_pages_resolve() -> None:
    for page in PAGES.values():
        text = page.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved.exists(), f"broken link in {page}: {raw_target}"


def test_docs_explain_every_system_contract_in_plain_language() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.values()).lower()
    for phrase in (
        "public",
        "signature",
        "return",
        "exception",
        "state",
        "side effect",
        "operation",
        "fault",
        "recovery",
        "replay",
        "performance",
        "no_divergence_observed",
    ):
        assert phrase in corpus
    assert "not prove" in corpus or "not a proof" in corpus
    assert "performance" in corpus and "separate" in corpus


def test_human_and_agent_entrypoints_expose_system_differential_docs() -> None:
    for relative in (
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/index.md",
        "docs/core-concepts.md",
        "docs/getting-started.md",
        "docs/troubleshooting.md",
        "docs/llms.txt",
        "docs/reference/api.md",
        "ordeal/SKILL.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "system-differential" in text, f"{relative} hides the system learning path"


def test_docs_keep_python_system_mode_distinct_from_revision_cli() -> None:
    corpus = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "AGENTS.md",
            "ordeal/SKILL.md",
            "docs/guides/system-differential.md",
            "docs/guides/system-differential-troubleshooting.md",
        )
    )
    assert "diff(Old, New, sequence=" in corpus
    assert "ordeal diff" in corpus
    assert "separate" in corpus.lower() or "distinct" in corpus.lower()


def test_first_system_comparison_is_executable() -> None:
    guide = PAGES["first_run"].read_text(encoding="utf-8")
    match = re.search(r"```python\n(.*?)\n```", guide, flags=re.DOTALL)
    assert match is not None
    namespace = {"__name__": "system_diff_docs_example"}
    exec(compile(match.group(1), str(PAGES["first_run"]), "exec"), namespace)


def test_runtime_catalog_teaches_the_system_surface() -> None:
    items = {item["name"]: item for item in ordeal.catalog()["diff"]}
    expected = {
        "Operation": "both systems",
        "FaultEvent": "both system versions",
        "PerformanceBudget": "latency limit",
        "SystemDiffResult": "recovery",
    }
    for name, phrase in expected.items():
        assert name in items
        assert phrase in items[name]["doc"].lower()

    learn_more = set(items["diff"]["learn_more"])
    for path in (
        "docs/concepts/system-differential.md",
        "docs/guides/system-differential.md",
        "docs/guides/system-differential-recipes.md",
        "docs/guides/system-differential-troubleshooting.md",
        "docs/reference/system-differential.md",
    ):
        assert path in learn_more
    assert any("sequence=" in example for example in items["diff"]["examples"])


def test_runtime_catalog_preserves_top_level_diff_callable() -> None:
    ordeal.catalog()

    assert callable(ordeal.diff)
