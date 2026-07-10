"""Human-first guardrails for the repository README."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def test_readme_stays_short_enough_for_human_evaluation() -> None:
    text = README.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert len(lines) <= 180, f"README grew to {len(lines)} lines"
    assert len(text.split()) <= 900, "README is becoming a second documentation site"
    assert "## What's in the box" not in text
    assert "## Architecture — code map" not in text


def test_readme_proves_the_value_before_asking_for_adoption() -> None:
    text = README.read_text(encoding="utf-8")

    assert text.index("## See it find a bug") < text.index("## Run it on your project")
    for evidence in (
        "risky.average [supported]",
        'witness: input={"values": []}',
        "replay: verified (2/2 exact matches)",
        "same input reproduced the same",
    ):
        assert evidence in text


def test_readme_keeps_one_beginner_path_and_routes_depth_elsewhere() -> None:
    text = README.read_text(encoding="utf-8")
    beginner, advanced = text.split("<details>", maxsplit=1)

    for command in (
        "ordeal scan .",
        "ordeal scan . --save",
        "ordeal verify <finding-id>",
        "ordeal verify --ci",
    ):
        assert command in beginner
    assert "ordeal mine" not in beginner
    assert "ordeal audit" not in beginner

    for route in (
        "Test Protection",
        "Service Evidence Loop",
        "Differential Quickstart",
        "System Differential Testing",
        "Safe Migrations",
        "CLI reference",
    ):
        assert route in advanced


def test_readme_local_links_resolve() -> None:
    text = README.read_text(encoding="utf-8")
    for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        target = raw_target.split("#", maxsplit=1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (ROOT / target).resolve().exists(), f"broken README link: {raw_target}"
