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

Use ``declare()`` to register deferred properties up front so they can
fail even when never observed::

    declare("timeout handler runs", "reachable")
    declare("cache warms up", "sometimes")

Each function is simple by default and unlocks depth through parameters::

    always(x > 0, "positive")                                     # fatal
    always(x > 0, "positive", mute=True)                          # tracked, not fatal
    sometimes(is_cached, "cache hit")                              # deferred
    sometimes(lambda: cache.hit_rate() > 0, "cache", attempts=100)# immediate

Add ``operation`` and ``fault`` to record reliability coverage without learning
a second assertion API::

    always(
        charge_count == 1,
        "no_duplicate_charge",
        operation="create_order",
        fault="timeout",
    )

The dimensions are evidence labels; they do not inject a fault.  Contextual
``declare()`` calls register expected cells so zero observations appear as
``NOT EXERCISED``.  ``report()["reliability_coverage"]`` exposes the same
PASS / NOT EXERCISED / FAIL matrix as JSON-safe rows and summary counts.
"""

from __future__ import annotations

import copy
import threading
import warnings
from collections.abc import Callable, Iterable, Mapping
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

    @property
    def evidence_status(self) -> str:
        """Return ``observed``, ``violated``, or ``unexercised`` evidence status."""
        if self.hits == 0:
            return "unexercised"
        if not self.passed:
            return "violated"
        return "observed"


@dataclass
class ReliabilityCell:
    """One operation × fault × property reliability-coverage cell.

    Status is always derived from assertion type and observation counters.
    Zero hits means ``NOT EXERCISED``; observed cells become ``PASS`` or
    ``FAIL`` according to the normal assertion semantics.
    """

    operation: str
    fault: str
    property: str
    type: str
    hits: int = 0
    passes: int = 0
    failures: int = 0
    first_failure_details: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        """Return ``PASS``, ``FAIL``, or ``NOT EXERCISED`` from observations."""
        if self.hits == 0:
            return "NOT EXERCISED"
        match self.type:
            case "always":
                return "PASS" if self.failures == 0 else "FAIL"
            case "sometimes":
                return "PASS" if self.passes > 0 else "FAIL"
            case "reachable":
                return "PASS"
            case "unreachable":
                return "FAIL"
        return "FAIL"

    def as_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-serializable representation of this cell."""
        return {
            "operation": self.operation,
            "fault": self.fault,
            "property": self.property,
            "type": self.type,
            "status": self.status,
            "hits": self.hits,
            "passes": self.passes,
            "failures": self.failures,
        }


class PropertyTracker:
    """Thread-safe accumulator for property assertion results.

    All access to ``active``, properties, and reliability cells is guarded by
    a lock, making this safe for free-threaded Python 3.13+.
    """

    def __init__(self) -> None:
        self._properties: dict[str, Property] = {}
        self._reliability: dict[tuple[str, str, str], ReliabilityCell] = {}
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
            self._reliability.clear()

    def snapshot(
        self,
    ) -> tuple[
        bool,
        dict[str, Property],
        dict[tuple[str, str, str], ReliabilityCell],
    ]:
        """Return a deep-copied snapshot for temporary lifecycle overrides."""
        with self._lock:
            return (
                self._active,
                copy.deepcopy(self._properties),
                copy.deepcopy(self._reliability),
            )

    def restore(
        self,
        snapshot: tuple[
            bool,
            dict[str, Property],
            dict[tuple[str, str, str], ReliabilityCell],
        ],
    ) -> None:
        """Restore a snapshot previously returned by ``snapshot()``."""
        active, properties, reliability = snapshot
        with self._lock:
            self._properties = copy.deepcopy(properties)
            self._reliability = copy.deepcopy(reliability)
            self._active = active

    @staticmethod
    def _reliability_key(
        name: str,
        operation: str | None,
        fault: str | None,
    ) -> tuple[str, str, str] | None:
        if operation is None and fault is None:
            return None
        if operation is None or fault is None:
            raise ValueError("operation and fault must be provided together")
        if not operation.strip() or not fault.strip():
            raise ValueError("operation and fault must be non-empty")
        return operation, fault, name

    def _cell(
        self,
        key: tuple[str, str, str],
        prop_type: str,
    ) -> ReliabilityCell:
        cell = self._reliability.get(key)
        if cell is None:
            operation, fault, name = key
            cell = ReliabilityCell(
                operation=operation,
                fault=fault,
                property=name,
                type=prop_type,
            )
            self._reliability[key] = cell
        elif cell.type != prop_type:
            raise ValueError(
                f"Reliability property {key[2]!r} for {key[0]!r} × {key[1]!r} "
                f"already uses type {cell.type!r}, not {prop_type!r}"
            )
        return cell

    def declare_reliability(
        self,
        name: str,
        prop_type: str,
        *,
        operation: str,
        fault: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Declare an expected matrix cell without claiming it was exercised."""
        key = self._reliability_key(name, operation, fault)
        assert key is not None
        with self._lock:
            if not self._active:
                return False
            cell = self._cell(key, prop_type)
            if cell.first_failure_details is None and details:
                cell.first_failure_details = details
            return True

    def merge_reliability(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Merge JSON-safe reliability rows produced by another test worker."""
        with self._lock:
            for row in rows:
                operation = str(row["operation"])
                fault = str(row["fault"])
                name = str(row["property"])
                prop_type = str(row["type"])
                key = self._reliability_key(name, operation, fault)
                assert key is not None
                counts = tuple(int(row.get(field, 0)) for field in ("hits", "passes", "failures"))
                if any(count < 0 for count in counts):
                    raise ValueError("reliability counters must be non-negative")
                cell = self._cell(key, prop_type)
                cell.hits += counts[0]
                cell.passes += counts[1]
                cell.failures += counts[2]

    def record(
        self,
        name: str,
        prop_type: str,
        condition: bool,
        details: dict[str, Any] | None = None,
        *,
        operation: str | None = None,
        fault: str | None = None,
    ) -> bool:
        """Record a property observation. Returns True if tracker was active."""
        key = self._reliability_key(name, operation, fault)
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
            if key is not None:
                cell = self._cell(key, prop_type)
                cell.hits += 1
                if condition:
                    cell.passes += 1
                else:
                    cell.failures += 1
                    if cell.first_failure_details is None and details:
                        cell.first_failure_details = details
            return True

    def record_hit(
        self,
        name: str,
        prop_type: str,
        *,
        operation: str | None = None,
        fault: str | None = None,
    ) -> bool:
        """Record a code-path hit. Returns True if tracker was active."""
        key = self._reliability_key(name, operation, fault)
        with self._lock:
            if not self._active:
                return False
            if name not in self._properties:
                self._properties[name] = Property(name=name, type=prop_type)
            self._properties[name].hits += 1
            if key is not None:
                self._cell(key, prop_type).hits += 1
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

    @property
    def reliability_results(self) -> list[ReliabilityCell]:
        """Reliability cells sorted by operation, fault, then property."""
        with self._lock:
            return [self._reliability[key] for key in sorted(self._reliability)]


# ---------------------------------------------------------------------------
# Global tracker — one per process, activated by the pytest plugin or
# auto_configure().
# ---------------------------------------------------------------------------
tracker = PropertyTracker()
_DEFERRED_PROPERTY_TYPES = frozenset({"sometimes", "reachable"})


def _stderr(msg: str) -> None:
    import sys

    sys.stderr.write(msg)
    sys.stderr.flush()


def report() -> dict[str, Any]:
    """Structured summary of all tracked property assertions.

    Returns a dict with ``passed`` and ``failed`` lists.  When reliability
    dimensions were supplied, the result also has ``reliability_coverage``
    with JSON-serializable rows, explicit dimensions, and status counts.  The
    optional key is absent when no dimensional cells were declared or observed.

    Reliability rows are sorted by operation, fault, then property.  Each row
    includes the assertion type, derived status, and hit/pass/failure counters.
    Under pytest-xdist, worker rows are merged before the controller renders
    its terminal summary.

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
            "evidence_status": p.evidence_status,
            "summary": p.summary,
        }
        if p.passed:
            passed.append(entry)
        else:
            failed.append(entry)
    result: dict[str, Any] = {"passed": passed, "failed": failed}
    cells = tracker.reliability_results
    if cells:
        rows = [cell.as_dict() for cell in cells]
        result["reliability_coverage"] = {
            "dimensions": ["operation", "fault", "property"],
            "rows": rows,
            "summary": {
                "pass": sum(row["status"] == "PASS" for row in rows),
                "not_exercised": sum(row["status"] == "NOT EXERCISED" for row in rows),
                "fail": sum(row["status"] == "FAIL" for row in rows),
                "total": len(rows),
            },
        }
    return result


def declare(
    name: str,
    prop_type: str,
    *,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Register a deferred property or reliability expectation before observations.

    ``sometimes()`` and ``reachable()`` are observational by default: if the
    call site is never reached, there is nothing to track.  ``declare()``
    makes the expectation explicit so the property can fail at session end
    even when it was never observed.  With ``operation`` and ``fault``, all
    four property types can be declared as reliability cells; an unseen cell
    reports ``NOT EXERCISED`` rather than pretending it passed or failed.

    Typical use::

        declare("timeout handler runs", "reachable")
        declare("cache warms up", "sometimes")
        declare(
            "eventual_commit",
            "always",
            operation="create_order",
            fault="worker_restart",
        )

    Then, elsewhere in the code under test::

        reachable("timeout handler runs")
        sometimes(cache_hit, "cache warms up")

    Args:
        name: Human-readable property label.
        prop_type: ``"reachable"`` or ``"sometimes"`` for a plain
            declaration; any assertion type for a reliability cell.
        operation: Operation dimension for reliability coverage.
        fault: Fault dimension for reliability coverage. Must be provided
            together with ``operation``.
        **details: Optional metadata stored with the property.
    """
    contextual = operation is not None or fault is not None
    if not contextual and prop_type not in _DEFERRED_PROPERTY_TYPES:
        raise ValueError(
            "declare() only supports deferred property types: 'sometimes' or 'reachable'"
        )
    if contextual and prop_type not in {"always", "sometimes", "reachable", "unreachable"}:
        raise ValueError(
            "contextual declare() type must be 'always', 'sometimes', 'reachable', "
            "or 'unreachable'"
        )
    if contextual:
        if operation is None or fault is None:
            raise ValueError("operation and fault must be provided together")
        was_active = tracker.declare_reliability(
            name,
            prop_type,
            operation=operation,
            fault=fault,
            details=details or None,
        )
    else:
        with tracker._lock:
            if not tracker._active:
                was_active = False
            else:
                prop = tracker._properties.get(name)
                if prop is None:
                    tracker._properties[name] = Property(
                        name=name,
                        type=prop_type,
                        first_failure_details=details or None,
                    )
                elif prop.type != prop_type:
                    raise ValueError(
                        f"Property {name!r} already declared as {prop.type!r}, "
                        f"cannot redeclare as {prop_type!r}"
                    )
                elif prop.first_failure_details is None and details:
                    prop.first_failure_details = details
                was_active = True
    if not was_active:
        warnings.warn(
            f"declare({name!r}, {prop_type!r}) called but tracker is inactive — this is a no-op. "
            "Run with --chaos or call auto_configure() to enable property tracking.",
            stacklevel=2,
        )


def always(
    condition: bool,
    name: str,
    *,
    mute: bool = False,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
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

    Args:
        condition: The boolean condition that must hold.
        name: Human-readable label for this assertion.
        mute: If ``True``, record violation without raising.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context included in the error message.
    """
    tracker.record(
        name,
        "always",
        condition,
        details or None,
        operation=operation,
        fault=fault,
    )
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
    operation: str | None = None,
    fault: str | None = None,
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
        condition: Boolean or callable condition that should eventually hold.
        name: Human-readable label for this assertion.
        attempts: Polling attempts for immediate standalone use.
        warn: If True, print to stderr even when tracker is inactive.
            Useful for pre-flight checklists where findings should be
            visible in normal test runs.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context stored with a first failure.
    """
    if attempts is not None and callable(condition):
        for _ in range(attempts):
            if condition():
                tracker.record(
                    name,
                    "sometimes",
                    True,
                    details or None,
                    operation=operation,
                    fault=fault,
                )
                return
        tracker.record(
            name,
            "sometimes",
            False,
            details or None,
            operation=operation,
            fault=fault,
        )
        raise AssertionError(f"sometimes: never true in {attempts} attempts: {name}")

    cond = condition() if callable(condition) else condition
    was_active = tracker.record(
        name,
        "sometimes",
        cond,
        details or None,
        operation=operation,
        fault=fault,
    )
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


def reachable(
    name: str,
    *,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Assert this code path executes at least once — deferred, checked at session end.

    Args:
        name: Human-readable label for this reachability assertion.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context for the property report.
    """
    was_active = tracker.record_hit(
        name,
        "reachable",
        operation=operation,
        fault=fault,
    )
    if not was_active:
        warnings.warn(
            f"reachable({name!r}) called but tracker is inactive — this is a no-op. "
            f"Run with --chaos or call auto_configure() to enable property tracking.",
            stacklevel=2,
        )


def unreachable(
    name: str,
    *,
    mute: bool = False,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Assert this code path never executes — raises immediately if reached.

    Raises ``AssertionError`` immediately — whether or not the tracker
    is active.  Violations are never silent by default.

    Pass ``mute=True`` to record the hit without raising.  The hit
    still appears in the property report — it is tracked, not hidden.

    Args:
        name: Human-readable label for this assertion.
        mute: If ``True``, record the hit without raising.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context included in the error message.
    """
    tracker.record_hit(
        name,
        "unreachable",
        operation=operation,
        fault=fault,
    )
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
