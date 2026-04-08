"""I/O fault injections — 10 faults.

Targeted (patch a specific function):
- error_on_call(target) — raise IOError on every call
- return_empty(target) — return None on every call
- corrupt_output(target) — replace output with random bytes
- truncate_output(target, fraction) — cut output to fraction of length
- subprocess_timeout(target) — make matching subprocess calls time out
- subprocess_exit(target) — make matching subprocesses exit nonzero
- subprocess_signal(target) — make matching subprocesses die by signal
- subprocess_truncate_stdout(target, fraction) — truncate subprocess stdout
- subprocess_truncate_stderr(target, fraction) — truncate subprocess stderr

Environment (system-wide, use with caution):
- disk_full() — fail all write-mode open() and os.write()
- permission_denied() — fail all write-mode open()

::

    from ordeal.faults.io import error_on_call, disk_full
    faults = [error_on_call("myapp.db.read"), disk_full()]
"""

from __future__ import annotations

import errno
import functools
import os
import signal
import subprocess as _sp
from dataclasses import dataclass
from typing import Any

from . import Fault, PatchFault

# ---------------------------------------------------------------------------
# Targeted faults (safe — scope to a single function)
# ---------------------------------------------------------------------------


def error_on_call(
    target: str,
    error: type[Exception] = IOError,
    message: str = "Simulated I/O error",
) -> PatchFault:
    """Make *target* raise *error* on every call — simulates database/cache/service failure."""

    def wrapper(original):
        @functools.wraps(original)
        def raising(*args: Any, **kwargs: Any) -> Any:
            raise error(message)

        return raising

    return PatchFault(target, wrapper, name=f"error_on_call({target})")


def return_empty(target: str) -> PatchFault:
    """Make *target* return ``None`` on every call while active."""

    def wrapper(original):
        @functools.wraps(original)
        def empty(*args: Any, **kwargs: Any) -> None:
            return None

        return empty

    return PatchFault(target, wrapper, name=f"return_empty({target})")


def corrupt_output(target: str) -> PatchFault:
    """Replace the output of *target* with random bytes (same length)."""

    def wrapper(original):
        @functools.wraps(original)
        def corrupted(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if isinstance(result, bytes):
                return os.urandom(len(result))
            if isinstance(result, str):
                return os.urandom(len(result)).hex()[: len(result)]
            return result

        return corrupted

    return PatchFault(target, wrapper, name=f"corrupt_output({target})")


def truncate_output(target: str, fraction: float = 0.5) -> PatchFault:
    """Truncate the output of *target* to *fraction* of its length."""

    def wrapper(original):
        @functools.wraps(original)
        def truncated(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if isinstance(result, (bytes, str)):
                cut = max(0, int(len(result) * fraction))
                return result[:cut]
            if isinstance(result, (list, tuple)):
                cut = max(0, int(len(result) * fraction))
                return type(result)(result[:cut])
            return result

        return truncated

    return PatchFault(target, wrapper, name=f"truncate_output({target}, {fraction})")


# ---------------------------------------------------------------------------
# Environment faults (system-wide — use carefully)
# ---------------------------------------------------------------------------


class _DiskFullFault(Fault):
    """Simulates disk full by making write-mode ``open()`` and ``os.write()`` fail."""

    def __init__(self) -> None:
        super().__init__(name="disk_full")
        self._original_open: Any = None
        self._original_write: Any = None

    def _do_activate(self) -> None:
        import builtins

        self._original_open = builtins.open
        self._original_write = os.write

        original_open = self._original_open

        def failing_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(c in str(mode) for c in "wax"):
                raise OSError(errno.ENOSPC, "No space left on device", str(file))
            return original_open(file, mode, *args, **kwargs)

        def failing_write(fd: int, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        builtins.open = failing_open  # type: ignore[assignment]
        os.write = failing_write  # type: ignore[assignment]

    def _do_deactivate(self) -> None:
        import builtins

        if self._original_open is not None:
            builtins.open = self._original_open  # type: ignore[assignment]
        if self._original_write is not None:
            os.write = self._original_write  # type: ignore[assignment]


class _PermissionDeniedFault(Fault):
    """Simulates permission denied on write-mode ``open()``."""

    def __init__(self) -> None:
        super().__init__(name="permission_denied")
        self._original_open: Any = None

    def _do_activate(self) -> None:
        import builtins

        self._original_open = builtins.open
        original = self._original_open

        def denied(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(c in str(mode) for c in "wax"):
                raise PermissionError(errno.EACCES, "Permission denied", str(file))
            return original(file, mode, *args, **kwargs)

        builtins.open = denied  # type: ignore[assignment]

    def _do_deactivate(self) -> None:
        import builtins

        if self._original_open is not None:
            builtins.open = self._original_open  # type: ignore[assignment]


def disk_full() -> Fault:
    """Simulate disk full — all write-mode open() and os.write() fail with ENOSPC.

    **Warning**: patches ``builtins.open`` and ``os.write`` globally.
    """
    return _DiskFullFault()


def permission_denied() -> Fault:
    """Simulate permission-denied errors on write operations.

    **Warning**: patches ``builtins.open`` globally.
    """
    return _PermissionDeniedFault()


# ---------------------------------------------------------------------------
# Subprocess / FFI boundary faults
# ---------------------------------------------------------------------------


def _subprocess_cmd_string(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Return the command string passed to ``subprocess``."""
    cmd = args[0] if args else kwargs.get("args", "")
    return " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)


def _subprocess_text_mode(kwargs: dict[str, Any]) -> bool:
    """Return whether subprocess output should be text rather than bytes."""
    return bool(
        kwargs.get("text")
        or kwargs.get("universal_newlines")
        or kwargs.get("encoding") is not None
        or kwargs.get("errors") is not None
    )


def _truncate_stream(value: bytes | str | None, fraction: float) -> bytes | str | None:
    """Truncate a subprocess stream while preserving its type."""
    if value is None:
        return None
    cut = max(0, int(len(value) * fraction))
    return value[:cut]


@dataclass
class _SyntheticSubprocess:
    """Minimal subprocess object used for synthetic child-process failures."""

    args: Any
    returncode: int
    stdout: bytes | str | None
    stderr: bytes | str | None
    _stdout_pipe: bool
    _stderr_pipe: bool

    def __enter__(self) -> "_SyntheticSubprocess":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def communicate(
        self,
        input: object = None,
        timeout: float | None = None,
    ) -> tuple[bytes | str | None, bytes | str | None]:
        del input, timeout
        return (
            self.stdout if self._stdout_pipe else None,
            self.stderr if self._stderr_pipe else None,
        )

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL

    def terminate(self) -> None:
        self.returncode = -signal.SIGTERM

    def send_signal(self, sig: int) -> None:
        self.returncode = -abs(sig)


def _subprocess_failure_factory(
    target: str,
    *,
    returncode: int,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
) -> Any:
    """Build a ``PatchFault`` factory that returns a synthetic child process."""

    def _factory(original: object) -> object:
        def _patched_popen(*args: object, **kwargs: object) -> object:
            cmd_str = _subprocess_cmd_string(args, kwargs)
            if target not in cmd_str:
                return original(*args, **kwargs)  # type: ignore[operator]

            text_mode = _subprocess_text_mode(kwargs)
            stdout_pipe = kwargs.get("stdout") is _sp.PIPE
            stderr_pipe = kwargs.get("stderr") is _sp.PIPE
            empty_stdout: bytes | str = "" if text_mode else b""
            empty_stderr: bytes | str = "" if text_mode else b""
            result_stdout = stdout if stdout is not None else empty_stdout
            result_stderr = stderr if stderr is not None else empty_stderr
            return _SyntheticSubprocess(
                args=args[0] if args else kwargs.get("args", ""),
                returncode=returncode,
                stdout=result_stdout,
                stderr=result_stderr,
                _stdout_pipe=stdout_pipe,
                _stderr_pipe=stderr_pipe,
            )

        return _patched_popen

    return _factory


def _subprocess_output_truncation_factory(
    target: str,
    *,
    stream: str,
    fraction: float,
) -> Any:
    """Build a ``PatchFault`` factory that truncates subprocess output."""

    def _factory(original: object) -> object:
        def _patched_run(*args: object, **kwargs: object) -> object:
            cmd_str = _subprocess_cmd_string(args, kwargs)
            result = original(*args, **kwargs)  # type: ignore[operator]
            if target not in cmd_str:
                return result

            if stream == "stdout" and hasattr(result, "stdout"):
                result.stdout = _truncate_stream(result.stdout, fraction)
            elif stream == "stderr" and hasattr(result, "stderr"):
                result.stderr = _truncate_stream(result.stderr, fraction)
            return result

        return _patched_run

    return _factory


def subprocess_timeout(target: str, *, timeout_sec: float = 0.001) -> PatchFault:
    """Make ``subprocess.run``/``subprocess.check_output`` calls matching *target* time out.

    Patches ``subprocess.run`` — when the command string contains *target*,
    raises ``subprocess.TimeoutExpired`` instead of running the process.
    Useful for testing Python↔Rust/C/Go bridges under chaos.

    Works in ChaosTest (nemesis toggles the fault) or as a context manager::

        # In ChaosTest — nemesis toggles automatically
        class KernelChaos(ChaosTest):
            faults = [subprocess_timeout("cargo run")]

            @rule()
            def run_episode(self):
                result = run_kernel(steps=10)  # calls subprocess.run
                always(result.exit_code == 0, "clean exit")

        # As context manager — scoped activation
        with subprocess_timeout("cargo run"):
            result = run_kernel()

    Args:
        target: Substring to match in the command (e.g. ``"cargo run"``).
        timeout_sec: Fake timeout duration for the error.
    """
    import subprocess as _sp

    def _factory(original: object) -> object:
        def _timeout_run(*args: object, **kwargs: object) -> object:
            cmd = args[0] if args else kwargs.get("args", "")
            cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if target in cmd_str:
                raise _sp.TimeoutExpired(cmd_str, timeout_sec)
            return original(*args, **kwargs)  # type: ignore[operator]

        return _timeout_run

    return PatchFault("subprocess.run", _factory, name=f"subprocess_timeout({target!r})")


def subprocess_exit(
    target: str,
    *,
    returncode: int = 1,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
) -> PatchFault:
    """Make matching subprocesses exit with *returncode*.

    Patches ``subprocess.Popen`` so the child never starts.  This makes
    ``subprocess.run()``, ``subprocess.check_output()``, and direct
    ``Popen`` callers observe a nonzero exit as if the child had failed.

    Args:
        target: Substring to match in the command string.
        returncode: Exit status to report.  Use a negative value only if
            you want to model signal-style death directly.
        stdout: Optional synthetic stdout for the completed process.
        stderr: Optional synthetic stderr for the completed process.
    """
    return PatchFault(
        "subprocess.Popen",
        _subprocess_failure_factory(
            target,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
        name=f"subprocess_exit({target!r}, returncode={returncode})",
    )


def subprocess_signal(
    target: str,
    *,
    signum: int = signal.SIGTERM,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
) -> PatchFault:
    """Make matching subprocesses die from *signum*.

    The synthetic process reports ``returncode == -signum``, matching
    Python's standard subprocess convention for signal death.

    Args:
        target: Substring to match in the command string.
        signum: Signal number to report as the death cause.
        stdout: Optional synthetic stdout for the completed process.
        stderr: Optional synthetic stderr for the completed process.
    """
    return PatchFault(
        "subprocess.Popen",
        _subprocess_failure_factory(
            target,
            returncode=-abs(signum),
            stdout=stdout,
            stderr=stderr,
        ),
        name=f"subprocess_signal({target!r}, signum={signum})",
    )


def subprocess_truncate_stdout(target: str, fraction: float = 0.5) -> PatchFault:
    """Truncate matching subprocess stdout to *fraction* of its length.

    Patches ``subprocess.run`` so the child process still executes, but
    the captured stdout is shortened after the fact.  Useful for testing
    parsers that assume the child always returns a full payload.
    """
    return PatchFault(
        "subprocess.run",
        _subprocess_output_truncation_factory(target, stream="stdout", fraction=fraction),
        name=f"subprocess_truncate_stdout({target!r}, {fraction})",
    )


def subprocess_truncate_stderr(target: str, fraction: float = 0.5) -> PatchFault:
    """Truncate matching subprocess stderr to *fraction* of its length.

    Patches ``subprocess.run`` so the child process still executes, but
    the captured stderr is shortened after the fact.
    """
    return PatchFault(
        "subprocess.run",
        _subprocess_output_truncation_factory(target, stream="stderr", fraction=fraction),
        name=f"subprocess_truncate_stderr({target!r}, {fraction})",
    )


def corrupt_stdout(target: str) -> PatchFault:
    """Replace the stdout of subprocess calls matching *target* with random bytes.

    Replaces ``stdout`` in the ``CompletedProcess`` with random bytes,
    simulating garbled FFI output.

    Works in ChaosTest (nemesis toggles the fault) or as a context manager::

        # In ChaosTest
        faults = [corrupt_stdout("my_binary")]

        # As context manager
        with corrupt_stdout("my_binary"):
            result = parse_binary_output()

    Args:
        target: Substring to match in the command.
    """
    import os as _os

    def _factory(original: object) -> object:
        def _corrupt_run(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)  # type: ignore[operator]
            cmd = args[0] if args else kwargs.get("args", "")
            cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if target in cmd_str and hasattr(result, "stdout") and result.stdout:
                n = len(result.stdout)
                result.stdout = _os.urandom(n)
            return result

        return _corrupt_run

    return PatchFault("subprocess.run", _factory, name=f"corrupt_stdout({target!r})")


def subprocess_delay(target: str, *, delay: float = 1.0) -> PatchFault:
    """Add *delay* seconds to subprocess calls matching *target*.

    Simulates slow FFI responses — tests timeout handling and
    progress reporting in Python↔Rust/C/Go bridges.

    Works in ChaosTest (nemesis toggles the fault) or as a context manager::

        # In ChaosTest
        faults = [subprocess_delay("cargo run", delay=5.0)]

        # As context manager
        with subprocess_delay("cargo run", delay=5.0):
            result = run_kernel()

    Args:
        target: Substring to match in the command.
        delay: Seconds to sleep before returning the real result.
    """
    import time as _time

    def _factory(original: object) -> object:
        def _slow_run(*args: object, **kwargs: object) -> object:
            cmd = args[0] if args else kwargs.get("args", "")
            cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            result = original(*args, **kwargs)  # type: ignore[operator]
            if target in cmd_str:
                _time.sleep(delay)
            return result

        return _slow_run

    return PatchFault("subprocess.run", _factory, name=f"subprocess_delay({target!r})")
