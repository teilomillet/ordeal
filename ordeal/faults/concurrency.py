"""Concurrency fault injections.

Faults for testing thread-safety, resource contention, and concurrent
access patterns — common in any Python library that uses threading,
connection pools, or shared mutable state.

    from ordeal.faults.concurrency import contended_call, delayed_release
    faults = [
        contended_call("myapp.pool.acquire", contention=0.1),
        delayed_release("myapp.pool.release", delay=0.5),
    ]
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any

from . import Fault, LambdaFault, PatchFault


def contended_call(
    target: str,
    contention: float = 0.05,
    mode: str = "simulate",
) -> PatchFault:
    """Add artificial contention to *target* via a shared lock.

    Every call to *target* acquires a global lock, simulating
    resource contention.  *contention* seconds of hold time per call.

    Args:
        target: Dotted path to the function.
        contention: Seconds to hold the lock (simulated work).
        mode: ``"simulate"`` (no actual sleep) or ``"real"``
            (calls ``time.sleep``).
    """
    lock = threading.Lock()

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def contended(*args: Any, **kwargs: Any) -> Any:
            with lock:
                if mode == "real":
                    time.sleep(contention)
                return original(*args, **kwargs)

        return contended

    return PatchFault(target, wrapper, name=f"contended_call({target}, {contention}s, {mode})")


def delayed_release(
    target: str,
    delay: float = 0.5,
    mode: str = "simulate",
) -> PatchFault:
    """Add a delay *after* *target* returns, before the caller gets the result.

    Simulates slow resource release (e.g. connection pool return,
    lock release, file handle close).  Useful for surfacing bugs where
    callers assume instant cleanup.

    Args:
        target: Dotted path to the function.
        delay: Seconds of post-call delay.
        mode: ``"simulate"`` (no actual sleep) or ``"real"``.
    """

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if mode == "real":
                time.sleep(delay)
            return result

        return delayed

    return PatchFault(target, wrapper, name=f"delayed_release({target}, {delay}s, {mode})")


class _ThreadBoundaryFault(PatchFault):
    """Executes *target* on a background thread, returning the result.

    Surfaces thread-safety issues: if the function or its caller
    assumes it runs on the calling thread, this breaks that assumption.
    """

    def __init__(self, target: str, timeout: float = 5.0) -> None:
        self._timeout = timeout

        def wrapper(original: Any) -> Any:
            @functools.wraps(original)
            def on_other_thread(*args: Any, **kwargs: Any) -> Any:
                result_box: list = []
                error_box: list = []

                def run() -> None:
                    try:
                        result_box.append(original(*args, **kwargs))
                    except Exception as exc:
                        error_box.append(exc)

                t = threading.Thread(target=run, daemon=True)
                t.start()
                t.join(timeout=self._timeout)
                if error_box:
                    raise error_box[0]
                if result_box:
                    return result_box[0]
                raise TimeoutError(f"Thread boundary timeout after {self._timeout}s in {target}")

            return on_other_thread

        super().__init__(target, wrapper, name=f"thread_boundary({target})")


def thread_boundary(target: str, timeout: float = 5.0) -> Fault:
    """Execute *target* on a background thread instead of the calling thread.

    Useful for finding thread-local state bugs, non-thread-safe access
    patterns, and implicit thread affinity assumptions.
    """
    return _ThreadBoundaryFault(target, timeout)


def stale_state(
    obj: Any,
    attr: str,
    stale_value: Any,
) -> Fault:
    """Overwrite *obj.attr* with *stale_value* while active (simulates stale cache/config).

    Simulates stale caches, expired tokens, outdated config — any
    scenario where an in-memory value drifts from the source of truth.

    Args:
        obj: The object whose attribute to corrupt.
        attr: The attribute name.
        stale_value: The stale value to inject.
    """
    original: list = []  # box to hold the original value

    def on_activate() -> None:
        original.append(getattr(obj, attr))
        setattr(obj, attr, stale_value)

    def on_deactivate() -> None:
        if original:
            setattr(obj, attr, original.pop())

    return LambdaFault(
        name=f"stale_state({type(obj).__name__}.{attr})",
        on_activate=on_activate,
        on_deactivate=on_deactivate,
    )
