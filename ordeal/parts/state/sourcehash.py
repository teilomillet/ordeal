from __future__ import annotations
# ruff: noqa
import hashlib
import inspect
import json
from dataclasses import asdict, dataclass, field
from typing import Any
_ALL_EXPLORATION_DIMENSIONS = ("mine", "scan", "mutate", "chaos")
def _source_hash(func: Any) -> str | None:
    """Hash a function's source code.  Returns ``None`` if unavailable."""
    try:
        source = inspect.getsource(func)
        return hashlib.sha256(source.encode()).hexdigest()
    except (OSError, TypeError):
        return None
def _json_ready(value: Any) -> Any:
    """Convert nested state values into JSON-friendly structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_ready(item) for item in value]
    return repr(value)
def _crash_summary(name: str, category: str, replayable: bool | None) -> str:
    """Return the human-facing summary for one scan crash category."""
    if category == "likely_bug":
        return f"{name}: strong candidate issue on contract-valid inputs"
    if category == "coverage_gap":
        return f"{name}: crash evidence still looks like a coverage gap"
    if category == "beyond_declared_contract_robustness":
        return f"{name}: robustness issue just beyond the declared contract"
    if category == "invalid_input_crash":
        return f"{name}: robustness issue currently looks driven by invalid input"
    if replayable:
        return f"{name}: replayable crash on semi-valid inputs, still exploratory"
    return f"{name}: unreplayed crash on random inputs"
def _evidence_class(category: str) -> str:
    """Return the user-facing evidence class for one internal category."""
    return {
        "likely_bug": "candidate_issue",
        "expected_precondition_failure": "expected_precondition",
    }.get(category, category)
@dataclass
class FunctionState:
    """Exploration state for a single function."""

    name: str
    source_hash: str | None = None

    # mine() results
    mined: bool = False
    properties: list[dict[str, Any]] = field(default_factory=list)
    property_violations: list[str] = field(default_factory=list)
    property_violation_details: list[dict[str, Any]] = field(default_factory=list)
    edges_discovered: int = 0
    saturated: bool = False

    # mutate() results
    mutated: bool = False
    mutation_score: float | None = None
    survived_mutants: int = 0
    killed_mutants: int = 0

    # harden() results — verified tests that close mutation gaps
    hardened: bool = False
    hardened_kills: int = 0

    # scan/fuzz results
    scanned: bool = False
    crash_free: bool | None = None
    scan_error: str | None = None
    failing_args: dict[str, Any] | None = None
    scan_crash_category: str | None = None
    scan_replayable: bool | None = None
    scan_replay_attempts: int = 0
    scan_replay_matches: int = 0
    scan_minimization: dict[str, Any] | None = None
    scan_contract_fit: float | None = None
    scan_reachability: float | None = None
    scan_realism: float | None = None
    scan_sink_signal: float | None = None
    scan_sink_categories: list[str] = field(default_factory=list)
    scan_input_sources: list[dict[str, str]] = field(default_factory=list)
    scan_input_source: str | None = None
    scan_proof_bundle: dict[str, Any] | None = None
    scan_limitation_kind: str | None = None
    scan_blocking_reason: str | None = None
    fuzz_examples: int = 0
    contract_violations: list[str] = field(default_factory=list)
    contract_violation_details: list[dict[str, Any]] = field(default_factory=list)

    # chaos testing
    chaos_tested: bool = False
    faults_tested: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return self.confidence_for(_ALL_EXPLORATION_DIMENSIONS)

    def confidence_for(self, dimensions: set[str] | tuple[str, ...] | list[str]) -> float:
        """Per-function exploration confidence [0, 1].

        Combines coverage across dimensions. Each dimension contributes
        independently — more exploration = higher confidence.
        """
        enabled = set(dimensions)
        scores: list[float] = []
        if "mine" in enabled and self.mined:
            # High confidence if many properties hold universally
            total = len(self.properties)
            universal = sum(1 for p in self.properties if p.get("universal", False))
            scores.append(universal / total if total > 0 else 0.5)
        if "mutate" in enabled and self.mutated and self.mutation_score is not None:
            # Hardening boosts effective mutation score: verified kills
            # close gaps that the original test suite missed.
            effective = self.mutation_score
            if self.hardened and self.survived_mutants > 0:
                total = self.killed_mutants + self.survived_mutants
                effective = (self.killed_mutants + self.hardened_kills) / total
            scores.append(min(effective, 1.0))
        if "scan" in enabled and self.scanned:
            scores.append(1.0 if self.crash_free else 0.0)
        if "chaos" in enabled and self.chaos_tested:
            scores.append(min(1.0, len(self.faults_tested) / 3))
        return sum(scores) / max(len(scores), 1)

    def reset(self) -> None:
        """Clear all exploration results.  Called when source changes."""
        self.source_hash = None
        self.mined = False
        self.properties = []
        self.property_violations = []
        self.property_violation_details = []
        self.edges_discovered = 0
        self.saturated = False
        self.mutated = False
        self.mutation_score = None
        self.survived_mutants = 0
        self.killed_mutants = 0
        self.hardened = False
        self.hardened_kills = 0
        self.scanned = False
        self.crash_free = None
        self.scan_error = None
        self.failing_args = None
        self.scan_crash_category = None
        self.scan_replayable = None
        self.scan_replay_attempts = 0
        self.scan_replay_matches = 0
        self.scan_minimization = None
        self.scan_contract_fit = None
        self.scan_reachability = None
        self.scan_realism = None
        self.scan_sink_signal = None
        self.scan_sink_categories = []
        self.scan_input_sources = []
        self.scan_input_source = None
        self.scan_proof_bundle = None
        self.scan_limitation_kind = None
        self.scan_blocking_reason = None
        self.fuzz_examples = 0
        self.contract_violations = []
        self.contract_violation_details = []
        self.chaos_tested = False
        self.faults_tested = []

    @property
    def frontier(self) -> list[str]:
        return self.frontier_for(_ALL_EXPLORATION_DIMENSIONS)

    def frontier_for(self, dimensions: set[str] | tuple[str, ...] | list[str]) -> list[str]:
        """What's unexplored for this function."""
        enabled = set(dimensions)
        gaps: list[str] = []
        if "mine" in enabled and not self.mined:
            gaps.append("not mined")
        elif "mine" in enabled and self.saturated:
            gaps.append(f"mining saturated ({self.edges_discovered} edges)")
        if "mutate" in enabled and not self.mutated:
            gaps.append("not mutation-tested")
        elif "mutate" in enabled and self.mutation_score is not None and self.mutation_score < 0.8:
            gaps.append(f"mutation score {self.mutation_score:.0%}")
            unhardened = self.survived_mutants - self.hardened_kills
            if unhardened > 0:
                gaps.append(f"{unhardened} unhardened survivor(s)")
        if "scan" in enabled and not self.scanned:
            gaps.append("not scanned")
        elif "scan" in enabled and self.scan_limitation_kind is not None:
            gaps.append(f"blocked: {self.scan_blocking_reason or self.scan_limitation_kind}")
        elif "scan" in enabled and self.crash_free is False and not self.scan_replayable:
            gaps.append("crash not replayed")
        for note in self.contract_violations:
            gaps.append(note)
        if "chaos" in enabled and not self.chaos_tested:
            gaps.append("no chaos testing")
        for v in self.property_violations:
            gaps.append(f"property: {v}")
        return gaps
