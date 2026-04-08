"""Tests for subprocess / native-boundary faults."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ordeal.faults.io import (
    subprocess_exit,
    subprocess_signal,
    subprocess_truncate_stderr,
    subprocess_truncate_stdout,
)

TARGET = Path(sys.executable).name or "python"


class TestSubprocessExitFault:
    def test_nonzero_exit_surfaces_through_popen_run_and_check_output(self):
        with subprocess_exit(TARGET, returncode=7, stdout="boom", stderr="err"):
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('ignored')"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.poll() == 7
            assert proc.wait() == 7
            assert proc.communicate() == ("boom", "err")

            completed = subprocess.run(
                [sys.executable, "-c", "print('ignored')"],
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 7
            assert completed.stdout == "boom"
            assert completed.stderr == "err"

            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                subprocess.run(
                    [sys.executable, "-c", "print('ignored')"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            assert exc_info.value.returncode == 7
            assert exc_info.value.output == "boom"
            assert exc_info.value.stderr == "err"

            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                subprocess.check_output([sys.executable, "-c", "print('ignored')"], text=True)
            assert exc_info.value.returncode == 7
            assert exc_info.value.output == "boom"


class TestSubprocessSignalFault:
    def test_signal_death_is_reported_as_negative_returncode(self):
        with subprocess_signal(TARGET, signum=signal.SIGTERM, stdout="oops", stderr="boom"):
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('ignored')"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.returncode == -signal.SIGTERM
            assert proc.communicate() == ("oops", "boom")

            completed = subprocess.run(
                [sys.executable, "-c", "print('ignored')"],
                capture_output=True,
                text=True,
            )
            assert completed.returncode == -signal.SIGTERM
            assert completed.stdout == "oops"
            assert completed.stderr == "boom"

            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                subprocess.run(
                    [sys.executable, "-c", "print('ignored')"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            assert exc_info.value.returncode == -signal.SIGTERM


class TestSubprocessTruncationFaults:
    def test_truncate_stdout_preserves_bytes_type(self):
        with subprocess_truncate_stdout(TARGET, fraction=0.5):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'abcdef')",
                ],
                capture_output=True,
            )
            assert completed.stdout == b"abc"
            assert isinstance(completed.stdout, bytes)

    def test_truncate_stderr_preserves_bytes_type(self):
        with subprocess_truncate_stderr(TARGET, fraction=0.5):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.buffer.write(b'uvwxyz')",
                ],
                capture_output=True,
            )
            assert completed.stderr == b"uvw"
            assert isinstance(completed.stderr, bytes)
