"""Discoverability and scope checks for safe module migration documentation."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ordeal import catalog

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "mental_model": ROOT / "docs/concepts/safe-migrations.md",
    "workflow": ROOT / "docs/guides/migration-workflow.md",
}


def test_each_migration_audience_has_a_short_dedicated_page() -> None:
    for audience, path in PAGES.items():
        assert path.is_file(), f"missing {audience} migration page"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 50 <= len(lines) <= 130, f"{path.name} should stay focused: {len(lines)} lines"


def test_migration_pages_are_in_navigation() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for path in PAGES.values():
        relative = path.relative_to(ROOT / "docs").as_posix()
        assert relative in nav, f"{relative} is missing from the documentation nav"


def test_migration_pages_have_no_broken_local_links() -> None:
    for page in PAGES.values():
        text = page.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved.exists(), f"broken link in {page.name}: {raw_target}"


def test_layman_page_explains_why_parity_is_not_correctness() -> None:
    text = PAGES["mental_model"].read_text(encoding="utf-8")
    lowered = text.lower()
    assert "checkout calculator" in lowered
    assert "same behavior does not mean correct behavior" in lowered
    assert "clues, not rules" in lowered
    for stage in (
        "audit the base",
        "mine the candidate",
        "diff both modules",
        "classify changes",
        "save surprises",
        "mutate the tests",
        "scan the candidate",
    ):
        assert stage in lowered
    for invariant in (
        "risk score stays between 0 and 1",
        "refund never exceeds the original payment",
        "path never escapes its allowed directory",
    ):
        assert invariant in lowered
    for status in ("PROTECTIVE_WITHIN_MEASURED_SCOPE", "INCOMPLETE", "BLOCKED"):
        assert status in text


def test_workflow_is_copyable_and_states_the_exact_gate() -> None:
    text = PAGES["workflow"].read_text(encoding="utf-8")
    for required in (
        "ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml",
        "ContractCheck",
        "BLOCKED mutate resulting tests",
        "RESULT  INCOMPLETE",
        "PROTECTIVE_WITHIN_MEASURED_SCOPE",
        ".ordeal/migrations/",
        "tests/test_ordeal_migration_",
        "candidate-only scan",
        "evidence-only",
    ):
        assert required in text


def test_human_and_agent_entrypoints_expose_both_learning_steps() -> None:
    entrypoints = (
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/index.md",
        "docs/llms.txt",
        "ordeal/SKILL.md",
        "docs/guides/cli.md",
        "docs/core-concepts.md",
        "docs/reference/api.md",
    )
    for relative in entrypoints:
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "safe-migrations" in text, f"{relative} hides the layman page"
        assert "migration-workflow" in text, f"{relative} hides the workflow"


def test_runtime_catalog_routes_and_teaches_migration() -> None:
    discovered = catalog()
    migrate_entry = next(entry for entry in discovered["migration"] if entry["name"] == "migrate")
    assert "docs/concepts/safe-migrations.md" in migrate_entry["learn_more"]
    assert "docs/guides/migration-workflow.md" in migrate_entry["learn_more"]
    assert "docs/reference/api.md#migration-workflow" in migrate_entry["learn_more"]
    example = "\n".join(migrate_entry["examples"])
    assert "ContractCheck" in example
    assert "result = migrate(" in example

    cli_entry = next(entry for entry in discovered["cli"] if entry["name"] == "migrate")
    assert "docs/concepts/safe-migrations.md" in cli_entry["learn_more"]
    assert "docs/guides/migration-workflow.md" in cli_entry["learn_more"]


def test_migrate_help_starts_with_the_layman_risk_and_routes_onward() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "ordeal.cli", "migrate", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "perfect parity match can preserve an old bug" in process.stdout
    assert "https://docs.byordeal.com/concepts/safe-migrations/" in process.stdout
    assert "https://docs.byordeal.com/guides/migration-workflow/" in process.stdout
