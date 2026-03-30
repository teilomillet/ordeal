"""Simulation primitives — no-mock, fast, deterministic testing.

Inject these instead of mocking real infrastructure.  They are instant
(no real I/O, no real sleeps), deterministic, fully introspectable, and
support injectable faults for chaos testing.

    from ordeal.simulate import Clock, FileSystem

    clock = Clock()
    service = MyService(clock=clock)
    clock.advance(60)           # instant — no real waiting
    assert service.is_healthy()

    fs = FileSystem()
    fs.write("/data.json", '{"ok": true}')
    fs.inject_fault("/data.json", "corrupt")
    # service.load(fs) now reads corrupted data

Integrates with ChaosTest — the nemesis can advance clocks, partition
networks, corrupt files.
"""
from __future__ import annotations

import heapq
import os
import unittest.mock
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Callable


# ============================================================================
# Clock
# ============================================================================

class Clock:
    """Deterministic, controllable clock.

    Drop-in replacement for ``time.time()`` and ``time.sleep()``.
    Supports timers that fire when time advances past their deadline.
    """

    def __init__(self, start: float = 0.0):
        self._now = start
        self._timers: list[tuple[float, int, Callable]] = []
        self._timer_id = 0

    def time(self) -> float:
        """Current simulated time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance time by *seconds* (instant — no real waiting)."""
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Advance the clock, firing any timers whose deadline has passed."""
        if seconds < 0:
            raise ValueError("Cannot advance time backwards")
        target = self._now + seconds
        while self._timers and self._timers[0][0] <= target:
            deadline, _, callback = heapq.heappop(self._timers)
            self._now = deadline
            callback()
        self._now = target

    def set_timer(self, delay: float, callback: Callable[[], None]) -> int:
        """Schedule *callback* to fire after *delay* seconds.

        Returns a timer id (for future cancellation support).
        """
        self._timer_id += 1
        heapq.heappush(self._timers, (self._now + delay, self._timer_id, callback))
        return self._timer_id

    @property
    def pending_timers(self) -> int:
        """Number of timers that haven't fired yet."""
        return len(self._timers)

    @contextmanager
    def patch(self) -> Generator[Clock, None, None]:
        """Patch ``time.time`` and ``time.sleep`` with this clock.

        Use when the code under test calls ``time.time()`` directly::

            clock = Clock()
            with clock.patch():
                assert time.time() == 0.0
                time.sleep(10)
                assert time.time() == 10.0
        """
        with unittest.mock.patch("time.time", side_effect=self.time), \
             unittest.mock.patch("time.sleep", side_effect=self.sleep):
            yield self


# ============================================================================
# FileSystem
# ============================================================================

class FileSystem:
    """In-memory filesystem — no disk I/O, injectable faults.

    Faults:
        ``"readonly"``  — writes raise ``PermissionError``
        ``"full"``      — writes raise ``OSError(ENOSPC)``
        ``"corrupt"``   — reads return random bytes (same length)
        ``"missing"``   — reads raise ``FileNotFoundError``
    """

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._faults: dict[str, str] = {}

    def write(self, path: str, data: str | bytes) -> None:
        """Write *data* to *path*, respecting any injected write-faults."""
        fault = self._faults.get(path)
        if fault == "readonly":
            raise PermissionError(f"Read-only: {path}")
        if fault == "full":
            raise OSError(28, "No space left on device", path)
        self._files[path] = data.encode() if isinstance(data, str) else data

    def read(self, path: str) -> bytes:
        """Read raw bytes from *path*, respecting any injected read-faults."""
        fault = self._faults.get(path)
        if fault == "missing":
            raise FileNotFoundError(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        data = self._files[path]
        if fault == "corrupt":
            return os.urandom(len(data)) if data else b""
        return data

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read and decode the file at *path* as a string."""
        return self.read(path).decode(encoding)

    def exists(self, path: str) -> bool:
        """Return True if *path* exists and has no ``"missing"`` fault."""
        return path in self._files and self._faults.get(path) != "missing"

    def delete(self, path: str) -> None:
        """Remove *path* from the filesystem. No-op if it does not exist."""
        self._files.pop(path, None)

    def list_dir(self, prefix: str = "/") -> list[str]:
        """Return sorted paths that start with *prefix*."""
        return sorted(p for p in self._files if p.startswith(prefix))

    def inject_fault(self, path: str, fault: str) -> None:
        """Inject a fault on *path*.  See class docstring for fault types."""
        self._faults[path] = fault

    def clear_fault(self, path: str) -> None:
        """Remove the injected fault on *path*, if any."""
        self._faults.pop(path, None)

    def clear_all_faults(self) -> None:
        """Remove all injected faults from every path."""
        self._faults.clear()

    def reset(self) -> None:
        """Remove all files and faults."""
        self._files.clear()
        self._faults.clear()
