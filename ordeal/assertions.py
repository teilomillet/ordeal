"""Property assertions inspired by Antithesis.

Four assertion types, keyed by name (message string):

- always(condition, name):  must hold every time called
- sometimes(condition, name): must hold at least once across all calls
- reachable(name): code path must execute at least once
- unreachable(name): code path must never execute

In production (tracker inactive), these are zero-cost no-ops.
In testing, they accumulate results and raise on violation (always/unreachable).

Each function is simple by default and unlocks depth through parameters:

    sometimes(is_cached, "cache hit")                          # deferred
    sometimes(lambda: cache.hit_rate() > 0, "cache", attempts=100)  # immediate
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Property:
    """A tracked property and its accumulated results."""

    name: str
    type: str  # always | sometimes | reachable | unreachable
    hits: int = 0
    passes: int = 0
    failures: int = 0
    first_failure_details: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        """Whether this property passed, according to its type's semantics."""
        match self.type:
            case "always":
                return self.hits > 0 and self.failures == 0
            case "sometimes":
                return self.passes > 0
            case "reachable":
                return self.hits > 0
            case "unreachable":
                return self.hits == 0
        return False

    @property
    def summary(self) -> str:
        """Human-readable one-line summary: ``PASS name (type: N hits)`` or ``FAIL ...``."""
        if self.passed:
            return f"PASS {self.name} ({self.type}: {self.hits} hits)"
        match self.type:
            case "always":
                return f"FAIL {self.name} (always: {self.failures}/{self.hits} violations)"
            case "sometimes":
                return f"FAIL {self.name} (sometimes: never true in {self.hits} hits)"
            case "reachable":
                return f"FAIL {self.name} (reachable: never reached)"
            case "unreachable":
                return f"FAIL {self.name} (unreachable: reached {self.hits} times)"
        return f"UNKNOWN {self.name}"


class PropertyTracker:
    """Thread-safe accumulator for property assertion results.

    All access to ``active`` and ``_properties`` is guarded by a lock,
    making this safe for free-threaded Python 3.13+.
    """

    def __init__(self) -> None:
        self._properties: dict[str, Property] = {}
        self._lock = threading.Lock()
        self._active = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @active.setter
    def active(self, value: bool) -> None:
        with self._lock:
            self._active = value

    def reset(self) -> None:
        with self._lock:
            self._properties.clear()

    def record(
        self,
        name: str,
        prop_type: str,
        condition: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if not self._active:
                return
            if name not in self._properties:
                self._properties[name] = Property(name=name, type=prop_type)
            p = self._properties[name]
            p.hits += 1
            if condition:
                p.passes += 1
            else:
                p.failures += 1
                if p.first_failure_details is None and details:
                    p.first_failure_details = details

    def record_hit(self, name: str, prop_type: str) -> None:
        with self._lock:
            if not self._active:
                return
            if name not in self._properties:
                self._properties[name] = Property(name=name, type=prop_type)
            self._properties[name].hits += 1

    @property
    def results(self) -> list[Property]:
        """All tracked properties (both passed and failed)."""
        with self._lock:
            return list(self._properties.values())

    @property
    def failures(self) -> list[Property]:
        """Only the properties that have not passed."""
        with self._lock:
            return [p for p in self._properties.values() if not p.passed]


# ---------------------------------------------------------------------------
# Global tracker — one per process, activated by the pytest plugin or
# auto_configure().
# ---------------------------------------------------------------------------
tracker = PropertyTracker()


def always(condition: bool, name: str, **details: Any) -> None:
    """Assert *condition* is ``True`` every time this line executes.

    Raises ``AssertionError`` immediately on violation (triggers Hypothesis
    shrinking when used inside a ``ChaosTest``).
    """
    # record() checks active internally under the lock
    tracker.record(name, "always", condition, details or None)
    if not condition and tracker.active:
        msg = f"always violated: {name}"
        if details:
            msg += f" | {details}"
        raise AssertionError(msg)


def sometimes(
    condition: bool | Callable[[], bool],
    name: str,
    *,
    attempts: int | None = None,
    **details: Any,
) -> None:
    """Assert *condition* is ``True`` at least once.

    Simple — deferred, checked at session end::

        sometimes(score > 0.5, "high scores exist")

    With ``attempts`` — immediate, standalone, no tracker needed::

        sometimes(lambda: cache.hit_rate() > 0, "cache warms up", attempts=100)

    When *attempts* is set and *condition* is callable, the function is
    called up to *attempts* times.  Succeeds on the first ``True``.
    Raises ``AssertionError`` immediately if never ``True``.
    """
    if attempts is not None and callable(condition):
        for _ in range(attempts):
            if condition():
                tracker.record(name, "sometimes", True, details or None)
                return
        raise AssertionError(f"sometimes: never true in {attempts} attempts: {name}")

    cond = condition() if callable(condition) else condition
    tracker.record(name, "sometimes", cond, details or None)


def reachable(name: str, **details: Any) -> None:
    """Assert this code path executes at least once during the run."""
    tracker.record_hit(name, "reachable")


def unreachable(name: str, **details: Any) -> None:
    """Assert this code path *never* executes.

    Raises ``AssertionError`` immediately on violation.
    """
    tracker.record_hit(name, "unreachable")
    if tracker.active:
        msg = f"unreachable code reached: {name}"
        if details:
            msg += f" | {details}"
        raise AssertionError(msg)
