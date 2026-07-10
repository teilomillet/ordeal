from __future__ import annotations
# ruff: noqa
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
                self._record_observation_hit()
                raise OSError(errno.ENOSPC, "No space left on device", str(file))
            return original_open(file, mode, *args, **kwargs)

        def failing_write(fd: int, data: bytes) -> int:
            self._record_observation_hit()
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
                self._record_observation_hit()
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
