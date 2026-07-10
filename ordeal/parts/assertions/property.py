from __future__ import annotations
# ruff: noqa
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

    def counter_snapshot(self) -> dict[str, tuple[str, int, int, int]]:
        """Return compact property counters for a later event delta.

        This avoids allocating ``Property`` result lists when a hot caller
        only needs to know which properties changed since one rule step.
        """
        with self._lock:
            return {
                name: (prop.type, prop.hits, prop.passes, prop.failures)
                for name, prop in self._properties.items()
            }

    def counter_delta(
        self,
        before: dict[str, tuple[str, int, int, int]],
    ) -> list[dict[str, int | str]]:
        """Return counter changes since :meth:`counter_snapshot`.

        The event-shaped result lets Explorer update behavior telemetry
        without constructing a second full counter snapshot.
        """
        with self._lock:
            events: list[dict[str, int | str]] = []
            for name, prop in self._properties.items():
                _, old_hits, old_passes, old_failures = before.get(
                    name,
                    (prop.type, 0, 0, 0),
                )
                delta_hits = prop.hits - old_hits
                delta_passes = prop.passes - old_passes
                delta_failures = prop.failures - old_failures
                if delta_hits <= 0 and delta_passes <= 0 and delta_failures <= 0:
                    continue
                events.append(
                    {
                        "name": name,
                        "type": prop.type,
                        "delta_hits": delta_hits,
                        "delta_passes": delta_passes,
                        "delta_failures": delta_failures,
                    }
                )
            return events

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
