"""Tests for source-backed executable bug evidence records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import ordeal.evidence as evidence


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_test_record(
    tmp_path: Path,
    *,
    exception_message: str = "boom",
    requires_python: str = ">=3.12",
    undeclared_dependency: bool = False,
) -> Path:
    source = b"authoritative regression marker\n"
    buggy = (
        b"from helper import fail\n\ndef reproduce(value):\n    fail()\n"
        if undeclared_dependency
        else b'def reproduce(value):\n    raise ValueError("boom")\n'
    )
    fixed = b'def reproduce(value):\n    return {"value": value}\n'
    (tmp_path / "buggy.py").write_bytes(buggy)
    (tmp_path / "fixed.py").write_bytes(fixed)
    if undeclared_dependency:
        (tmp_path / "helper.py").write_text(
            'def fail():\n    raise ValueError("boom")\n',
            encoding="utf-8",
        )
    kwargs = {"value": 1}
    kwargs_json = json.dumps(kwargs, separators=(",", ":"), sort_keys=True)
    record = tmp_path / "evidence.toml"
    record.write_text(
        f'''
schema_version = 1
evidence_id = "test-bug-1"
claim = "The buggy side raises and the fixed side returns."
scope = "One exact input."
online_sources_required = true
limitations = ["Synthetic verifier test."]

[upstream]
project = "test"
bug_id = "1"
fixed_commit = "abcdef1"

[[sources]]
name = "source"
url = "https://example.test/source"
sha256 = "{_sha256(source)}"
bytes = {len(source)}

[[content_checks]]
name = "regression_marker"
source = "source"
contains = "authoritative regression marker"

[reproduction]
workspace = "."
buggy_module = "buggy"
fixed_module = "fixed"
callable = "reproduce"
kwargs_json = '{kwargs_json}'
kwargs_sha256 = "{_sha256(kwargs_json.encode())}"
repetitions = 2
timeout_seconds = 2.0
requires_python = "{requires_python}"

[expected.buggy]
outcome = "exception"
exception_type = "ValueError"
exception_message = "{exception_message}"

[expected.fixed]
outcome = "return"
result_json = '{{"value":1}}'

[[artifacts]]
name = "buggy"
path = "buggy.py"
sha256 = "{_sha256(buggy)}"
bytes = {len(buggy)}

[[artifacts]]
name = "fixed"
path = "fixed.py"
sha256 = "{_sha256(fixed)}"
bytes = {len(fixed)}
''',
        encoding="utf-8",
    )
    return record


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_checked_in_httpie_evidence_replays_exact_oracle_offline(monkeypatch) -> None:
    root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(root)

    result = evidence.verify_bug_evidence("benchmarks/evidence/httpie-3.toml")

    assert result.local_verified is True
    assert result.sources_verified is False
    assert result.verified is False
    assert result.record_path == "benchmarks/evidence/httpie-3.toml"
    assert result.backing_values["oracle"]["kwargs_sha256"] == (
        "bdd0c82f4c058188fce09292f1530bfa3665fb9b544189251ec6c3942a65d296"
    )
    assert result.backing_values["oracle"]["buggy"]["matching_replays"] == 5
    assert result.backing_values["oracle"]["fixed"]["matching_replays"] == 5
    assert sum(check.passed for check in result.checks) == 16
    assert len(result.checks) == 30
    assert result.backing_values["oracle"]["runtime"]["version"].startswith("3.")
    assert result.backing_values["oracle"]["workspace_isolation"] == ("declared_artifacts_only")


def test_pinned_evidence_files_force_lf_on_every_platform() -> None:
    attributes = (Path(__file__).resolve().parent.parent / ".gitattributes").read_text(
        encoding="utf-8"
    )

    assert "benchmarks/*.toml text eol=lf" in attributes
    assert "benchmarks/evidence/*.toml text eol=lf" in attributes
    assert "benchmarks/fixtures/**/*.py text eol=lf" in attributes


@pytest.mark.parametrize("literal", ["0", '"false"'])
def test_evidence_rejects_non_boolean_online_requirement(
    tmp_path: Path,
    literal: str,
) -> None:
    record = _write_test_record(tmp_path)
    record.write_text(
        record.read_text(encoding="utf-8").replace(
            "online_sources_required = true",
            f"online_sources_required = {literal}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a TOML boolean"):
        evidence.verify_bug_evidence(str(record))


def test_checked_in_pysnooper_evidence_replays_exact_oracle_offline(monkeypatch) -> None:
    root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(root)

    result = evidence.verify_bug_evidence("benchmarks/evidence/pysnooper-3.toml")

    assert result.local_verified is True
    assert result.sources_verified is False
    assert result.verified is False
    assert result.backing_values["oracle"]["kwargs_sha256"] == (
        "aa8a92b273e3465d2712da546a934467fc4de7a635d79465989775853efc993d"
    )
    assert result.backing_values["oracle"]["buggy"]["matching_replays"] == 5
    assert result.backing_values["oracle"]["fixed"]["matching_replays"] == 5
    assert result.backing_values["oracle"]["buggy"]["expected"] == {
        "outcome": "exception",
        "exception_type": "NameError",
        "exception_message": "name 'output_path' is not defined",
    }
    assert sum(check.passed for check in result.checks) == 16
    assert len(result.checks) == 30


def test_checked_in_tornado_evidence_replays_exact_oracle_offline(monkeypatch) -> None:
    root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(root)

    result = evidence.verify_bug_evidence("benchmarks/evidence/tornado-14.toml")

    assert result.local_verified is True
    assert result.sources_verified is False
    assert result.verified is False
    assert result.backing_values["oracle"]["kwargs_sha256"] == (
        "c49dbffe64d4cfcab1f753a0c6fade4928fef28f66bda199d3845e900dfa6972"
    )
    assert result.backing_values["oracle"]["buggy"]["matching_replays"] == 5
    assert result.backing_values["oracle"]["fixed"]["matching_replays"] == 5
    assert result.backing_values["oracle"]["fixed"]["expected"] == {
        "outcome": "return",
        "result": {
            "current_preserved": True,
            "second_error": "current IOLoop already exists",
        },
    }
    assert sum(check.passed for check in result.checks) == 16
    assert len(result.checks) == 30


def test_evidence_verifies_only_when_sources_artifacts_and_replays_match(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _write_test_record(tmp_path)
    monkeypatch.setattr(
        evidence.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(b"authoritative regression marker\n"),
    )

    result = evidence.verify_bug_evidence(str(record), online_sources=True)

    assert result.verified is True
    assert result.local_verified is True
    assert result.sources_verified is True
    assert all(check.passed for check in result.checks)
    assert result.backing_values["oracle"]["buggy"]["matching_replays"] == 2
    assert result.backing_values["oracle"]["fixed"]["matching_replays"] == 2


def test_evidence_fails_closed_when_a_hashed_artifact_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _write_test_record(tmp_path)
    (tmp_path / "buggy.py").write_text(
        "def reproduce(value):\n    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        evidence.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(b"authoritative regression marker\n"),
    )

    result = evidence.verify_bug_evidence(str(record), online_sources=True)

    assert result.verified is False
    assert result.local_verified is False
    artifact_check = next(check for check in result.checks if check.name == "artifact:buggy")
    assert artifact_check.passed is False


def test_evidence_fails_closed_when_authoritative_source_bytes_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _write_test_record(tmp_path)
    monkeypatch.setattr(
        evidence.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(b"changed authoritative bytes\n"),
    )

    result = evidence.verify_bug_evidence(str(record), online_sources=True)

    assert result.verified is False
    assert result.local_verified is True
    assert result.sources_verified is False
    source_check = next(check for check in result.checks if check.name == "source:source")
    assert source_check.passed is False


def test_evidence_fails_closed_when_declared_oracle_is_wrong(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _write_test_record(tmp_path, exception_message="not the observed error")
    monkeypatch.setattr(
        evidence.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(b"authoritative regression marker\n"),
    )

    result = evidence.verify_bug_evidence(str(record), online_sources=True)

    assert result.verified is False
    assert result.backing_values["oracle"]["buggy"]["matching_replays"] == 0
    assert not all(
        check.passed for check in result.checks if check.name.startswith("oracle:buggy:")
    )


def test_evidence_fails_closed_on_unsupported_replay_interpreter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _write_test_record(tmp_path, requires_python=">=99")
    monkeypatch.setattr(
        evidence.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(b"authoritative regression marker\n"),
    )

    result = evidence.verify_bug_evidence(str(record), online_sources=True)

    assert result.verified is False
    runtime_check = next(
        check for check in result.checks if check.name == "oracle:python_requirement"
    )
    assert runtime_check.passed is False
    assert runtime_check.expected == ">=99"


def test_evidence_replay_cannot_execute_an_undeclared_local_dependency(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _write_test_record(tmp_path, undeclared_dependency=True)
    monkeypatch.setattr(
        evidence.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(b"authoritative regression marker\n"),
    )

    result = evidence.verify_bug_evidence(str(record), online_sources=True)

    assert result.verified is False
    assert result.backing_values["oracle"]["buggy"]["matching_replays"] == 0
    assert all(
        observation["outcome"] == "runner_error"
        for observation in result.backing_values["oracle"]["buggy"]["observations"]
    )


def test_evidence_rejects_unknown_schema(tmp_path: Path) -> None:
    record = tmp_path / "invalid.toml"
    record.write_text("schema_version = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version must be 1"):
        evidence.verify_bug_evidence(str(record))
