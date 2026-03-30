"""I/O fault injections.

Targeted faults (patch a specific function):

    from ordeal.faults.io import error_on_call, return_empty, corrupt_output
    faults = [error_on_call("mymodule.read_file")]

Environment faults (system-wide, use with caution):

    from ordeal.faults.io import disk_full, permission_denied
    faults = [disk_full()]
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
    """Make *target* raise *error* on every call while active."""

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
    """Simulate disk-full errors on write operations.

    **Warning**: patches ``builtins.open`` and ``os.write`` globally.
    """
    return _DiskFullFault()


def permission_denied() -> Fault:
    """Simulate permission-denied errors on write operations.

    **Warning**: patches ``builtins.open`` globally.
    """
    return _PermissionDeniedFault()
