"""Timing fault injections — 4 faults.

- timeout(target) — raise TimeoutError instantly (no sleep)
- slow(target, delay) — add latency (simulate or real sleep)
- intermittent_crash(target, every_n) — crash every Nth call
- jitter(target, magnitude) — add numeric jitter to return value

::

    from ordeal.faults.timing import timeout, slow
    faults = [timeout("myapp.db.query"), slow("myapp.api.call", delay=2.0)]
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any

from . import Fault, PatchFault


def timeout(
    target: str,
    delay: float = 30.0,
    error: type[Exception] = TimeoutError,
) -> PatchFault:
    """Make *target* raise ``TimeoutError`` (no actual sleep — instant failure)."""

    def wrapper(original):
        @functools.wraps(original)
        def timed_out(*args: Any, **kwargs: Any) -> Any:
            raise error(f"Simulated timeout after {delay}s in {target}")

        return timed_out

    return PatchFault(target, wrapper, name=f"timeout({target}, {delay}s)")


def slow(
    target: str,
    delay: float = 1.0,
    mode: str = "simulate",
) -> PatchFault:
    """Add *delay* seconds to every call of *target*.

    Args:
        target: Dotted path to the function to slow down.
        delay: Delay in seconds.
        mode: ``"simulate"`` (default) records the delay without sleeping —
            safe for Explorer and fast tests.  ``"real"`` calls
            ``time.sleep(delay)`` for production fault injection.
    """
    if mode not in ("simulate", "real"):
        raise ValueError(f"mode must be 'simulate' or 'real', got {mode!r}")

    def wrapper(original):
        @functools.wraps(original)
        def slowed(*args: Any, **kwargs: Any) -> Any:
            if mode == "real":
                time.sleep(delay)
            # In simulate mode: no sleep, just call through
            return original(*args, **kwargs)

        return slowed

    return PatchFault(target, wrapper, name=f"slow({target}, {delay}s, {mode})")


class _IntermittentCrashFault(PatchFault):
    """Crashes every *every_n* calls to *target*."""

    def __init__(
        self,
        target: str,
        every_n: int = 3,
        error: type[Exception] = RuntimeError,
    ) -> None:
        self._call_count = 0
        self._every_n = every_n
        self._error = error
        self._counter_lock = threading.Lock()

        def wrapper(original):
            @functools.wraps(original)
            def crashing(*args: Any, **kwargs: Any) -> Any:
                with self._counter_lock:
                    self._call_count += 1
                    count = self._call_count
                if count % self._every_n == 0:
                    raise self._error(f"Simulated crash in {target} (call #{count})")
                return original(*args, **kwargs)

            return crashing

        super().__init__(target, wrapper, name=f"intermittent_crash({target}, every {every_n})")

    def reset(self) -> None:
        with self._counter_lock:
            self._call_count = 0
        super().reset()


def intermittent_crash(
    target: str,
    every_n: int = 3,
    error: type[Exception] = RuntimeError,
) -> Fault:
    """Crash *target* every *every_n* calls. Call count resets on ``reset()``."""
    return _IntermittentCrashFault(target, every_n, error)


class _JitterFault(PatchFault):
    """Adds random jitter to a function's return value (numeric)."""

    def __init__(self, target: str, magnitude: float = 0.01) -> None:
        self._magnitude = magnitude
        self._counter = 0
        self._counter_lock = threading.Lock()

        def wrapper(original):
            @functools.wraps(original)
            def jittered(*args: Any, **kwargs: Any) -> Any:
                result = original(*args, **kwargs)
                if isinstance(result, (int, float)):
                    with self._counter_lock:
                        self._counter += 1
                        count = self._counter
                    sign = 1 if count % 2 == 0 else -1
                    return result + sign * self._magnitude * abs(result or 1)
                return result

            return jittered

        super().__init__(target, wrapper, name=f"jitter({target}, {magnitude})")

    def reset(self) -> None:
        with self._counter_lock:
            self._counter = 0
        super().reset()


def jitter(target: str, magnitude: float = 0.01) -> Fault:
    """Add deterministic numeric jitter to *target*'s return value."""
    return _JitterFault(target, magnitude)
