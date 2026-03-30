"""Tests for ordeal.cli — replay, audit, reporting, progress."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ordeal.cli import (
    _print_report,
    _ProgressPrinter,
    _write_json_report,
    main,
)
from ordeal.explore import ExplorationResult, ProgressSnapshot
from ordeal.trace import Trace, TraceFailure, TraceStep

# ============================================================================
# Helpers
# ============================================================================


def _write_trace(tmp_path: Path, *, fail: bool = True) -> Path:
    trace = Trace(
        run_id=1,
        seed=42,
        test_class="tests.test_trace:_BombAt3",
        from_checkpoint=None,
        steps=[TraceStep(kind="rule", name="tick") for _ in range(3)],
        failure=TraceFailure("ValueError", "boom", 2) if fail else None,
    )
    path = tmp_path / "trace.json"
    trace.save(path)
    return path


def _write_non_failing_trace(tmp_path: Path) -> Path:
    trace = Trace(
        run_id=1,
        seed=42,
        test_class="tests.test_trace:_BombAt3",
        from_checkpoint=None,
        steps=[TraceStep(kind="rule", name="tick") for _ in range(2)],
    )
    path = tmp_path / "trace_ok.json"
    trace.save(path)
    return path


# ============================================================================
# ordeal replay
# ============================================================================


class TestReplayCommand:
    def test_replay_reproduces(self, tmp_path):
        path = _write_trace(tmp_path, fail=True)
        assert main(["replay", str(path)]) == 1

    def test_replay_no_reproduce(self, tmp_path):
        path = _write_non_failing_trace(tmp_path)
        assert main(["replay", str(path)]) == 0

    def test_replay_missing_file(self):
        assert main(["replay", "/nonexistent/trace.json"]) == 1

    def test_replay_with_shrink(self, tmp_path):
        path = _write_trace(tmp_path, fail=True)
        assert main(["replay", str(path), "--shrink"]) == 1

    def test_replay_with_shrink_and_output(self, tmp_path):
        path = _write_trace(tmp_path, fail=True)
        out = tmp_path / "shrunk.json"
        assert main(["replay", str(path), "--shrink", "--output", str(out)]) == 1
        assert out.exists()
        loaded = Trace.load(out)
        assert len(loaded.steps) <= 3

    def test_replay_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        assert main(["replay", str(bad)]) == 1

    def test_replay_gzip_trace(self, tmp_path):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="tick") for _ in range(3)],
            failure=TraceFailure("ValueError", "boom", 2),
        )
        path = tmp_path / "trace.json.gz"
        trace.save(path)
        assert main(["replay", str(path)]) == 1


# ============================================================================
# ordeal audit
# ============================================================================


class TestAuditCommand:
    def test_audit_basic(self, capsys):
        code = main(["audit", "ordeal.invariants", "--max-examples", "5"])
        assert code == 0

    def test_audit_multiple_modules(self, capsys):
        code = main(["audit", "ordeal.invariants", "ordeal.buggify", "--max-examples", "5"])
        assert code == 0


# ============================================================================
# _ProgressPrinter
# ============================================================================


class TestProgressPrinter:
    def test_prints_on_interval(self):
        printer = _ProgressPrinter(interval=0.0)
        snap = ProgressSnapshot(
            elapsed=5.0,
            total_runs=100,
            total_steps=500,
            unique_edges=42,
            checkpoints=3,
            failures=1,
            runs_per_second=20.0,
        )
        with patch("ordeal.cli._stderr") as mock:
            printer(snap)
            mock.assert_called_once()
            assert "runs=100" in mock.call_args[0][0]

    def test_rate_limits(self):
        printer = _ProgressPrinter(interval=999.0)
        snap = ProgressSnapshot(
            elapsed=1.0,
            total_runs=1,
            total_steps=1,
            unique_edges=1,
            checkpoints=0,
            failures=0,
            runs_per_second=1.0,
        )
        with patch("ordeal.cli._stderr") as mock:
            printer(snap)
            printer(snap)
            assert mock.call_count == 1


# ============================================================================
# _print_report
# ============================================================================


class TestPrintReport:
    def _result(self, failures=None):
        return ExplorationResult(
            total_runs=100,
            total_steps=500,
            unique_edges=42,
            checkpoints_saved=5,
            duration_seconds=10.0,
            failures=failures or [],
            traces=[],
        )

    def _cfg(self, fmt="text"):
        cfg = MagicMock()
        cfg.report.format = fmt
        cfg.report.verbose = False
        return cfg

    def test_text_report(self, capsys):
        _print_report([("test:Class", self._result())], self._cfg("text"))
        out = capsys.readouterr().out
        assert "100 runs" in out
        assert "No failures" in out

    def test_text_report_with_failures(self, capsys):
        failure = MagicMock()
        failure.error = ValueError("boom")
        failure.trace = None
        _print_report([("test:Class", self._result([failure]))], self._cfg("text"))
        out = capsys.readouterr().out
        assert "FAILURES" in out

    def test_json_format_skips_text(self, capsys):
        _print_report([("test:Class", self._result())], self._cfg("json"))
        assert capsys.readouterr().out == ""


# ============================================================================
# _write_json_report
# ============================================================================


class TestWriteJsonReport:
    def _result(self, failures=None):
        return ExplorationResult(
            total_runs=50,
            total_steps=200,
            unique_edges=30,
            checkpoints_saved=3,
            duration_seconds=5.0,
            failures=failures or [],
            traces=[],
        )

    def test_writes_valid_json(self, tmp_path):
        cfg = MagicMock()
        cfg.report.output = str(tmp_path / "report.json")
        _write_json_report([("mod:Cls", self._result())], cfg)
        with open(tmp_path / "report.json") as f:
            data = json.load(f)
        assert data["results"][0]["total_runs"] == 50

    def test_writes_failures(self, tmp_path):
        failure = MagicMock()
        failure.error = ValueError("boom")
        failure.step = 5
        failure.run_id = 1
        failure.active_faults = ["f1"]
        failure.trace = MagicMock()
        failure.trace.steps = [MagicMock()] * 3
        cfg = MagicMock()
        cfg.report.output = str(tmp_path / "report.json")
        _write_json_report([("mod:Cls", self._result([failure]))], cfg)
        with open(tmp_path / "report.json") as f:
            data = json.load(f)
        assert data["results"][0]["failures"][0]["error_type"] == "ValueError"

    def test_creates_parent_dirs(self, tmp_path):
        cfg = MagicMock()
        cfg.report.output = str(tmp_path / "nested" / "dir" / "report.json")
        _write_json_report([("mod:Cls", self._result())], cfg)
        assert (tmp_path / "nested" / "dir" / "report.json").exists()


# ============================================================================
# main() edge cases
# ============================================================================


class TestMainEdgeCases:
    def test_no_command_shows_help(self):
        assert main([]) == 0

    def test_mine_pair_second_bad(self):
        assert main(["mine-pair", "json.loads", "nodot"]) == 1

    def test_explore_no_tests_in_config(self, tmp_path):
        config = tmp_path / "ordeal.toml"
        config.write_text('[explorer]\ntarget_modules = ["ordeal"]\nmax_time = 1\n')
        assert main(["explore", "--config", str(config)]) == 1

    def test_explore_config_error(self, tmp_path):
        config = tmp_path / "ordeal.toml"
        config.write_text('[explorer]\ntarget_modules = ["ordeal"]\nunknown_xyz = true\n')
        assert main(["explore", "--config", str(config)]) == 1
