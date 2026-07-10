"""CLI integration tests for evidence closure and automatic deepening."""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

import ordeal.cli as ordeal_cli
from ordeal.cli import main


def _write_module(root: Path) -> None:
    (root / "deep_fixture.py").write_text(
        "def persist(path: str) -> None:\n"
        "    try:\n"
        "        with open(path, 'w') as handle:\n"
        "            handle.write('ok')\n"
        "    except (OSError, ValueError):\n"
        "        return None\n",
        encoding="utf-8",
    )


def _write_fault_module(root: Path) -> None:
    (root / "fault_closure_fixture.py").write_text(
        "def persist(path: str) -> None:\n"
        "    try:\n"
        "        with open(path, 'w') as handle:\n"
        "            handle.write('ok')\n"
        "    except (OSError, ValueError):\n"
        "        return None\n",
        encoding="utf-8",
    )


def test_deepen_requires_an_explicit_time_budget(capsys) -> None:
    rc = main(["scan", "ordeal.demo", "--deepen", "--json", "-n", "1"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "blocked"
    assert "explicit --time-limit" in payload["blocking_reason"]


def test_invalid_base_ref_blocks_changed_code_prioritization(capsys) -> None:
    rc = main(["scan", "ordeal.demo", "--base-ref", "missing-base", "--json", "-n", "1"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "blocked"
    assert payload["raw_details"]["base_ref"] == "missing-base"
    assert "unavailable" in payload["blocking_reason"]


def test_budgeted_child_uses_a_group_and_cleans_the_tree_on_timeout(monkeypatch) -> None:
    class FakeProcess:
        pid = 4242
        returncode = -1

        def __init__(self) -> None:
            self.calls = 0
            self.killed = False

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["child"], timeout)
            return "partial-out", "partial-err"

        def poll(self):
            return None if not self.killed else self.returncode

        def wait(self, timeout=None):
            self.killed = True
            return self.returncode

        def terminate(self):
            self.killed = True

        def kill(self):
            self.killed = True

    process = FakeProcess()
    popen_kwargs = {}

    def fake_popen(command, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(ordeal_cli.subprocess, "Popen", fake_popen)
    if os.name == "nt":
        taskkill = []
        monkeypatch.setattr(
            ordeal_cli.subprocess,
            "run",
            lambda command, **kwargs: taskkill.append(command),
        )
    else:
        signals = []
        monkeypatch.setattr(
            ordeal_cli.os,
            "killpg",
            lambda pid, sig: signals.append((pid, sig)),
        )

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        ordeal_cli._run_budgeted_child(["child"], timeout=0.01)

    assert raised.value.output == "partial-out"
    assert raised.value.stderr == "partial-err"
    if os.name == "nt":
        assert popen_kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert taskkill == [["taskkill", "/PID", "4242", "/T", "/F"]]
    else:
        assert popen_kwargs["start_new_session"] is True
        assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_deepen_executes_one_safe_planned_scan_within_budget(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    rc = main(
        [
            "scan",
            "deep_fixture",
            "--deepen",
            "--time-limit",
            "15",
            "--json",
            "-n",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    deepening = payload["raw_details"]["reliability_map"]["deepening"]
    observations = payload["raw_details"]["reliability_observations"]
    assert rc == 0
    assert deepening["status"] == "completed"
    assert deepening["engine"] == "scan"
    assert deepening["exit_code"] in {0, 1}
    assert isinstance(deepening["findings"], list)
    assert deepening["findings_truncated"] is False
    assert deepening["service_faults_executed"] is False
    assert deepening["elapsed_seconds"] <= 15
    assert observations and observations[0]["status"] == "PASS"
    assert observations[0]["fault"] == "io_error"
    assert observations[0]["injection"]["hits"] > 0


def test_deepen_merges_a_real_fault_specific_pass_into_the_map(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_fault_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    rc = main(
        [
            "scan",
            "fault_closure_fixture",
            "--deepen",
            "--time-limit",
            "15",
            "--json",
            "-n",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    reliability_map = payload["raw_details"]["reliability_map"]
    observations = payload["raw_details"]["reliability_observations"]
    deepening = reliability_map["deepening"]
    assert rc == 0
    assert deepening["status"] == "completed"
    assert observations and observations[0]["status"] == "PASS"
    assert observations[0]["injection"]["hits"] > 0
    assert reliability_map["summary"]["pass"] >= 1


def test_save_persists_reliability_map_without_inventing_a_regression(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_module(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    rc = main(["scan", "deep_fixture", "--save", "--json", "-n", "2"])

    payload = json.loads(capsys.readouterr().out)
    artifact_kinds = {artifact["kind"] for artifact in payload["artifacts"]}
    path = tmp_path / ".ordeal" / "evidence-plans" / "deep_fixture.json"
    assert rc == 0
    assert path.is_file()
    assert "reliability-map" in artifact_kinds
    assert "regression" not in artifact_kinds
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema"] == "ordeal.reliability-map/v1"
    assert persisted["summary"]["cells"] >= 1


def test_import_failure_is_blocked_and_never_reported_as_a_target_crash(capsys) -> None:
    rc = main(["scan", "definitely_missing_ordeal_target", "--json", "-n", "1"])

    payload = json.loads(capsys.readouterr().out)
    evidence = payload["raw_details"]["evidence"]
    assert rc == 1
    assert payload["status"] == "blocked"
    assert evidence["schema"] == "ordeal.scan-limitation/v1"
    assert evidence["limitation"]["kind"] == "import"
    assert "target behavior was not observed" in evidence["boundaries"]["establishes"]
    assert payload["findings"] == []
    assert payload["suggested_test_file"] is None
