"""Property assertions inspired by Antithesis.

Four assertion types, keyed by name (message string):

- ``always(condition, name)``   — must hold every time called
- ``sometimes(condition, name)``— must hold at least once across all calls
- ``reachable(name)``           — code path must execute at least once
- ``unreachable(name)``         — code path must never execute

**Violation behavior:**

- ``always`` and ``unreachable`` raise ``AssertionError`` immediately on
  violation — whether or not ``--chaos`` / the tracker is active.
  Violations are never silent.  Pass ``mute=True`` to record without
  raising (tracked in the property report, not hidden).
- ``sometimes`` and ``reachable`` are deferred: they only track when
  the ``PropertyTracker`` is active (``--chaos`` or ``auto_configure()``).
  Without it, they are no-ops.

**Tracker (--chaos) adds:**

- Property report at the end of the session (hit counts, pass/fail).
- Deferred checking for ``sometimes`` and ``reachable``.
- Does NOT control whether ``always``/``unreachable`` raise — they
  always raise on violation regardless.

Each function is simple by default and unlocks depth through parameters::

    always(x > 0, "positive")                                     # fatal
    always(x > 0, "positive", mute=True)                          # tracked, not fatal
    sometimes(is_cached, "cache hit")                              # deferred
    sometimes(lambda: cache.hit_rate() > 0, "cache", attempts=100)# immediate
"""

from __future__ import annotations

import threading
import warnings
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
    ) -> bool:
        """Record a property observation. Returns True if tracker was active."""
        with self._lock:
            if not self._active:
                return False
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
            return True

    def record_hit(self, name: str, prop_type: str) -> bool:
        """Record a code-path hit. Returns True if tracker was active."""
        with self._lock:
            if not self._active:
                return False
            if name not in self._properties:
                self._properties[name] = Property(name=name, type=prop_type)
            self._properties[name].hits += 1
            return True

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


def _stderr(msg: str) -> None:
    import sys

    sys.stderr.write(msg)
    sys.stderr.flush()


def report() -> dict[str, list[dict[str, Any]]]:
    """Structured summary of all tracked property assertions.

    Returns a dict with ``passed`` and ``failed`` lists.  Each entry
    has ``name``, ``type``, ``hits``, ``status``, and ``summary``.

    Call at the end of a test session for a preflight checklist::

        from ordeal.assertions import report
        r = report()
        for p in r["failed"]:
            print(p["summary"])
    """
    passed = []
    failed = []
    for p in tracker.results:
        entry = {
            "name": p.name,
            "type": p.type,
            "hits": p.hits,
            "passes": p.passes,
            "failures": p.failures,
            "status": "PASS" if p.passed else "FAIL",
            "summary": p.summary,
        }
        if p.passed:
            passed.append(entry)
        else:
            failed.append(entry)
    return {"passed": passed, "failed": failed}


def always(condition: bool, name: str, *, mute: bool = False, **details: Any) -> None:
    """Assert *condition* every time — raises immediately on violation.

    Raises ``AssertionError`` immediately on violation — whether or not
    the tracker is active.  Violations are never silent by default.

    Pass ``mute=True`` to record the violation without raising.  The
    violation still appears in the property report (when ``--chaos`` is
    active) — it is tracked, not hidden.  Use this when a known issue
    is too loud and you need to focus on something else::

        always(not math.isnan(x), "no NaN", mute=True)  # tracked, not fatal

    When the tracker IS active (``--chaos``), the result is also recorded
    for the property report regardless of ``mute``.
    """
    tracker.record(name, "always", condition, details or None)
    if not condition and not mute:
        msg = f"always violated: {name}"
        if details:
            msg += f" | {details}"
        raise AssertionError(msg)


def sometimes(
    condition: bool | Callable[[], bool],
    name: str,
    *,
    attempts: int | None = None,
    warn: bool = False,
    **details: Any,
) -> None:
    """Assert *condition* at least once — deferred, checked at session end.

    Simple — deferred, checked at session end::

        sometimes(score > 0.5, "high scores exist")

    With ``warn=True`` — visible in normal pytest (no --chaos needed)::

        sometimes(score > 0.5, "high scores exist", warn=True)

    With ``attempts`` — immediate, standalone, no tracker needed::

        sometimes(lambda: cache.hit_rate() > 0, "cache warms up", attempts=100)

    Args:
        warn: If True, print to stderr even when tracker is inactive.
            Useful for pre-flight checklists where findings should be
            visible in normal test runs.
    """
    if attempts is not None and callable(condition):
        for _ in range(attempts):
            if condition():
                tracker.record(name, "sometimes", True, details or None)
                return
        raise AssertionError(f"sometimes: never true in {attempts} attempts: {name}")

    cond = condition() if callable(condition) else condition
    was_active = tracker.record(name, "sometimes", cond, details or None)
    if not was_active:
        if warn:
            # Use print (stdout) so pytest captures and shows it visibly
            status = "PASS" if cond else "OBSERVE"
            detail_str = f" | {details}" if details else ""
            print(f"  ordeal: sometimes({name!r}): {status}{detail_str}")
        else:
            warnings.warn(
                f"sometimes({name!r}) called but tracker is inactive — this is a no-op. "
                "Run with --chaos or call auto_configure() to enable property tracking.",
                stacklevel=2,
            )


def reachable(name: str, **details: Any) -> None:
    """Assert this code path executes at least once — deferred, checked at session end."""
    was_active = tracker.record_hit(name, "reachable")
    if not was_active:
        warnings.warn(
            f"reachable({name!r}) called but tracker is inactive — this is a no-op. "
            f"Run with --chaos or call auto_configure() to enable property tracking.",
            stacklevel=2,
        )


def unreachable(name: str, *, mute: bool = False, **details: Any) -> None:
    """Assert this code path never executes — raises immediately if reached.

    Raises ``AssertionError`` immediately — whether or not the tracker
    is active.  Violations are never silent by default.

    Pass ``mute=True`` to record the hit without raising.  The hit
    still appears in the property report — it is tracked, not hidden.
    """
    tracker.record_hit(name, "unreachable")
    if not mute:
        msg = f"unreachable code reached: {name}"
        if details:
            msg += f" | {details}"
        raise AssertionError(msg)


def catalog() -> list[dict[str, str]]:
    """Discover all assertion functions via runtime introspection.

    Returns a list of dicts with ``name``, ``signature``, and ``doc``.
    Fully automatic — scans all public functions in this module.
    New assertion functions appear without registration.
    """
    import inspect as _inspect
    import sys

    mod = sys.modules[__name__]
    entries: list[dict[str, str]] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_") or attr_name in ("catalog", "report"):
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or _inspect.isclass(obj):
            continue
        # Only include functions defined in this module
        if getattr(obj, "__module__", None) != __name__:
            continue
        try:
            sig = str(_inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(...)"
        entries.append(
            {
                "name": attr_name,
                "qualname": f"ordeal.assertions.{attr_name}",
                "signature": sig,
                "doc": (_inspect.getdoc(obj) or "").split("\n")[0],
            }
        )
    return entries
