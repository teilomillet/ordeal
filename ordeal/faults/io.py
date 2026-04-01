"""I/O fault injections — 6 faults.

Targeted (patch a specific function):
- error_on_call(target) — raise IOError on every call
- return_empty(target) — return None on every call
- corrupt_output(target) — replace output with random bytes
- truncate_output(target, fraction) — cut output to fraction of length

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


def subprocess_timeout(target: str, *, timeout_sec: float = 0.001) -> PatchFault:
    """Make ``subprocess.run``/``subprocess.check_output`` calls matching *target* time out.

    Patches ``subprocess.run`` — when the command string contains *target*,
    raises ``subprocess.TimeoutExpired`` instead of running the process.
    Useful for testing Python↔Rust/C/Go bridges under chaos.

    Best used with regular pytest + ``always()`` (not ChaosTest, since
    subprocess lifecycle doesn't compose with stateful rules)::

        from ordeal.faults.io import subprocess_timeout

        def test_kernel_timeout(chaos_enabled):
            with subprocess_timeout("cargo run"):
                result = run_kernel()
            always(result is not None, "handles timeout gracefully")

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


def corrupt_stdout(target: str) -> PatchFault:
    """Replace the stdout of subprocess calls matching *target* with random bytes.

    Replaces ``stdout`` in the ``CompletedProcess`` with random bytes,
    simulating garbled FFI output.

    Best used with regular pytest + ``always()`` (not ChaosTest, since
    subprocess lifecycle doesn't compose with stateful rules)::

        from ordeal.faults.io import corrupt_stdout

        def test_garbled_output(chaos_enabled):
            with corrupt_stdout("my_binary"):
                result = parse_binary_output()
            always(result is not None, "handles corrupt output")

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

    Best used with regular pytest + ``always()`` (not ChaosTest, since
    subprocess lifecycle doesn't compose with stateful rules)::

        from ordeal.faults.io import subprocess_delay

        def test_kernel_slow(chaos_enabled):
            with subprocess_delay("cargo run", delay=5.0):
                result = run_kernel()
            always(result.completed, "completes despite delay")

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
