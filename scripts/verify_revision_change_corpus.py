"""Run revision diff against twelve pinned, real Ordeal repository changes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ordeal._revision_diff import RevisionDiffError, run_revision_diff

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ChangeCase:
    """One immutable candidate commit and stable observed target."""

    commit: str
    target: str
    expected_status: Literal["divergent", "no_divergence_observed"]


CASES = (
    ChangeCase(
        "d4850b7561d00d445a1fdb0d08fa1d93905adbdf",
        "ordeal.cli.command_catalog",
        "no_divergence_observed",
    ),
    ChangeCase(
        "e768706b7969656747a62229886839e2bce304c8",
        "ordeal.cli.command_catalog",
        "no_divergence_observed",
    ),
    ChangeCase(
        "ccfbb500c6c8c10d030d8d52f75af88907bbfe5f",
        "ordeal.catalog",
        "divergent",
    ),
    ChangeCase(
        "5aa91124aeba48aa14b4659239830f40bb4cb597",
        "ordeal.catalog",
        "divergent",
    ),
    ChangeCase(
        "57643174986b284b7f2d74d56399b5fa4a7749c6",
        "ordeal.cli.command_catalog",
        "no_divergence_observed",
    ),
    ChangeCase(
        "6bc429927cafb81b93ab662eb6a8d295e5f7a55b",
        "ordeal.cli.command_catalog",
        "no_divergence_observed",
    ),
    ChangeCase(
        "23a4c4a256f95e79a334317f424606d270caea6f",
        "ordeal.cli.command_catalog",
        "divergent",
    ),
    ChangeCase(
        "c98be9ee9c07ddf5229077879735cb2d7737af08",
        "ordeal.cli.command_catalog",
        "divergent",
    ),
    ChangeCase(
        "8230a97b31992e45e21e7b211e629774152599e5",
        "ordeal.catalog",
        "divergent",
    ),
    ChangeCase(
        "d150793011f0962352241d3d2586448737beed54",
        "ordeal.catalog",
        "divergent",
    ),
    ChangeCase(
        "0f44a60454d7197dbbdd59c84bd0054588d246c0",
        "ordeal.cli.command_catalog",
        "no_divergence_observed",
    ),
    ChangeCase(
        "94d9e1786a80b45949b741adb9d8ca248db85ff4",
        "ordeal.cli.command_catalog",
        "divergent",
    ),
)


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def run_corpus(output: Path) -> dict[str, Any]:
    """Execute every pinned change and persist a bounded result index."""
    if not 10 <= len(CASES) <= 20:
        raise AssertionError("revision change corpus must contain 10 to 20 cases")
    records: list[dict[str, Any]] = []
    for case in CASES:
        resolved = _git("rev-parse", "--verify", f"{case.commit}^{{commit}}")
        if resolved != case.commit:
            raise AssertionError(f"candidate commit did not resolve exactly: {case.commit}")
        base_commit = _git("rev-parse", f"{case.commit}^")
        changed_files = _git("diff", "--name-only", base_commit, case.commit).splitlines()
        if not any(path.startswith("ordeal/") and path.endswith(".py") for path in changed_files):
            raise AssertionError(f"case has no real Ordeal Python change: {case.commit}")
        result = run_revision_diff(
            case.target,
            base_ref=base_commit,
            candidate_ref=case.commit,
            repo=ROOT,
            max_examples=1,
            replay_attempts=1,
        )
        if result.status != case.expected_status:
            raise AssertionError(
                f"historical change {case.commit} returned {result.status}; "
                f"expected {case.expected_status}"
            )
        if result.status == "divergent" and result.supported_mismatch_count == 0:
            raise AssertionError(f"historical divergence lacked replay support: {case.commit}")
        records.append(
            {
                "candidate_commit": case.commit,
                "base_commit": base_commit,
                "subject": _git("show", "-s", "--format=%s", case.commit),
                "changed_files": changed_files,
                "target": case.target,
                "status": result.status,
                "isolated": result.isolated,
                "examples": result.total,
                "mismatches": result.mismatch_count,
                "supported_mismatches": result.supported_mismatch_count,
                "artifact_ids": [
                    str(artifact.get("artifact_id")) for artifact in result.artifacts
                ],
            }
        )
    report = {
        "schema": "ordeal.revision-change-corpus/v1",
        "repository": ROOT.as_posix(),
        "case_count": len(records),
        "decisive_count": sum(record["status"] != "inconclusive" for record in records),
        "divergent_count": sum(record["status"] == "divergent" for record in records),
        "no_divergence_observed_count": sum(
            record["status"] == "no_divergence_observed" for record in records
        ),
        "cases": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the real-change corpus and write its result index."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_corpus(args.output)
    except (AssertionError, OSError, RevisionDiffError, RuntimeError) as exc:
        print(f"Revision change corpus failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Verified {report['case_count']} real repository changes: "
        f"{report['divergent_count']} divergent, "
        f"{report['no_divergence_observed_count']} bounded controls."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
