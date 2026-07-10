"""Release gates for Evidence Closure recall, precision, and action validity."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_evidence_closure_corpus as corpus

pytestmark = pytest.mark.release_eval


def test_evidence_closure_corpus_is_substantially_paired() -> None:
    assert len(corpus.BUG_TARGETS) == 12
    assert len(corpus.FIXED_TARGETS) == 12
    assert len(set(corpus.BUG_TARGETS) | set(corpus.FIXED_TARGETS)) == 24


def test_evidence_closure_release_gates(tmp_path: Path) -> None:
    report = corpus.run_corpus(tmp_path / "evidence-closure-corpus.json")

    assert (
        report["baseline"]["supported_finding_recall"]
        < report["evidence_closure"]["supported_finding_recall"]
    )
    assert report["evidence_closure"]["supported_finding_recall"] >= 0.9
    assert report["evidence_closure"]["supported_finding_precision"] == 1.0
    assert report["tool_failures_misclassified"] == 0
    assert report["suggested_actions"]["invalid"] == []
    assert report["runtime_cell_transition"]["after"] == "PASS"
    assert report["runtime_cell_transition"]["observation"]["injection"]["hits"] > 0
    assert report["runtime_timeout_controls"]["recovery"]["status"] == "PASS"
    assert report["runtime_timeout_controls"]["bug"]["status"] == "FAIL"
    assert report["runtime_timeout_controls"]["recovery_cell"] == "PASS"
    assert report["runtime_timeout_controls"]["bug_cell"] == "FAIL"
    assert (tmp_path / "evidence-closure-corpus.json").is_file()
