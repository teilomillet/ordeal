"""Unified exploration state — what ordeal knows about your code.

Every ordeal tool (mine, mutate, scan, chaos_for, fuzz, explore)
explores one dimension of the state space.  ``ExplorationState``
accumulates their results into a single, persistent, queryable
picture.  AI assistants read this to decide what to explore next.

Quick start — explore everything in one pass::

    from ordeal.state import explore

    state = explore("myapp.scoring")
    print(state.confidence)       # 0.72
    print(state.frontier)         # what's unexplored
    print(state.findings)         # bugs and anomalies

Resume exploration (state persists)::

    state = explore("myapp.scoring", state=state)
    print(state.confidence)       # 0.89 — growing

Use tools individually — they enrich the same state::

    from ordeal import mine, mutate
    state = ExplorationState("myapp.scoring")
    state = explore_mine(state)
    state = explore_mutate(state)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FunctionState:
    """Exploration state for a single function."""

    name: str

    # mine() results
    mined: bool = False
    properties: list[dict[str, Any]] = field(default_factory=list)
    property_violations: list[str] = field(default_factory=list)

    # mutate() results
    mutated: bool = False
    mutation_score: float | None = None
    survived_mutants: int = 0
    killed_mutants: int = 0

    # scan/fuzz results
    scanned: bool = False
    crash_free: bool | None = None
    fuzz_examples: int = 0

    # chaos testing
    chaos_tested: bool = False
    faults_tested: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Per-function exploration confidence [0, 1].

        Combines coverage across dimensions. Each dimension contributes
        independently — more exploration = higher confidence.
        """
        scores: list[float] = []
        if self.mined:
            # High confidence if many properties hold universally
            total = len(self.properties)
            universal = sum(1 for p in self.properties if p.get("universal", False))
            scores.append(universal / total if total > 0 else 0.5)
        if self.mutated and self.mutation_score is not None:
            scores.append(self.mutation_score)
        if self.scanned:
            scores.append(1.0 if self.crash_free else 0.0)
        if self.chaos_tested:
            scores.append(min(1.0, len(self.faults_tested) / 3))
        return sum(scores) / max(len(scores), 1)

    @property
    def frontier(self) -> list[str]:
        """What's unexplored for this function."""
        gaps: list[str] = []
        if not self.mined:
            gaps.append("not mined")
        if not self.mutated:
            gaps.append("not mutation-tested")
        elif self.mutation_score is not None and self.mutation_score < 0.8:
            gaps.append(f"mutation score {self.mutation_score:.0%}")
        if not self.scanned:
            gaps.append("not scanned")
        if not self.chaos_tested:
            gaps.append("no chaos testing")
        for v in self.property_violations:
            gaps.append(f"property: {v}")
        return gaps


@dataclass
class ExplorationState:
    """Unified exploration state for a module.

    Accumulates results from all ordeal tools. JSON-serializable
    for persistence across sessions.
    """

    module: str
    functions: dict[str, FunctionState] = field(default_factory=dict)
    edge_coverage: float | None = None
    exploration_time: float = 0.0

    def function(self, name: str) -> FunctionState:
        """Get or create state for a function."""
        if name not in self.functions:
            self.functions[name] = FunctionState(name=name)
        return self.functions[name]

    @property
    def confidence(self) -> float:
        """Aggregate confidence across all functions."""
        if not self.functions:
            return 0.0
        return sum(f.confidence for f in self.functions.values()) / len(self.functions)

    @property
    def frontier(self) -> dict[str, list[str]]:
        """Per-function gaps — what's unexplored."""
        return {name: fs.frontier for name, fs in self.functions.items() if fs.frontier}

    @property
    def findings(self) -> list[str]:
        """All bugs and anomalies found."""
        results: list[str] = []
        for name, fs in self.functions.items():
            if fs.crash_free is False:
                results.append(f"{name}: crashes on random inputs")
            for v in fs.property_violations:
                results.append(f"{name}: {v}")
            if fs.mutation_score is not None and fs.mutation_score < 0.5:
                results.append(f"{name}: mutation score {fs.mutation_score:.0%}")
        return results

    def summary(self) -> str:
        """Human-readable exploration report."""
        lines = [f"Exploration: {self.module}"]
        lines.append(f"  confidence: {self.confidence:.0%}")
        lines.append(f"  functions:  {len(self.functions)}")
        if self.findings:
            lines.append(f"  findings:   {len(self.findings)}")
            for f in self.findings:
                lines.append(f"    - {f}")
        frontier = self.frontier
        if frontier:
            lines.append(f"  frontier:   {sum(len(v) for v in frontier.values())} gaps")
            for name, gaps in frontier.items():
                lines.append(f"    {name}: {', '.join(gaps)}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize to JSON for persistence."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> ExplorationState:
        """Deserialize from JSON."""
        raw = json.loads(data)
        state = cls(module=raw["module"])
        state.edge_coverage = raw.get("edge_coverage")
        state.exploration_time = raw.get("exploration_time", 0.0)
        for name, fdata in raw.get("functions", {}).items():
            fs = FunctionState(**fdata)
            state.functions[name] = fs
        return state


# ============================================================================
# Exploration steps — each enriches the state from one dimension
# ============================================================================


def explore_mine(state: ExplorationState, *, max_examples: int = 50) -> ExplorationState:
    """Mine all functions in the module and update state."""

    from ordeal.auto import _get_public_functions, _resolve_module
    from ordeal.mine import mine

    mod = _resolve_module(state.module)
    for name, func in _get_public_functions(mod):
        try:
            result = mine(func, max_examples=max_examples)
        except Exception:
            continue
        fs = state.function(name)
        fs.mined = True
        fs.properties = [
            {
                "name": p.name,
                "confidence": p.confidence,
                "universal": p.universal,
                "holds": p.holds,
                "total": p.total,
            }
            for p in result.properties
            if p.total > 0
        ]
        # Flag suspicious properties
        fs.property_violations = [
            f"{p.name} ({p.confidence:.0%})"
            for p in result.properties
            if p.total >= 10 and 0.90 <= p.confidence < 1.0
        ]
    return state


def explore_scan(state: ExplorationState, *, max_examples: int = 30) -> ExplorationState:
    """Scan module for crashes and update state."""
    from ordeal.auto import scan_module

    result = scan_module(state.module, max_examples=max_examples)
    for fr in result.functions:
        fs = state.function(fr.name)
        fs.scanned = True
        fs.crash_free = fr.passed
        if fr.property_violations:
            # Merge, don't duplicate
            existing = set(fs.property_violations)
            for v in fr.property_violations:
                if v not in existing:
                    fs.property_violations.append(v)
    return state


def explore_mutate(state: ExplorationState, *, workers: int = 1) -> ExplorationState:
    """Mutation-test all mined functions and update state.

    Scales with *workers*: more CPUs = more mutants tested in parallel.
    """
    from ordeal.mutations import mutate

    for name, fs in list(state.functions.items()):
        if fs.mutated:
            continue
        target = f"{state.module}.{name}"
        try:
            result = mutate(target, preset="essential", workers=workers)
        except Exception:
            continue
        fs.mutated = True
        fs.mutation_score = result.score
        fs.killed_mutants = sum(1 for m in result.mutants if m.killed)
        fs.survived_mutants = sum(1 for m in result.mutants if not m.killed)
    return state


def explore_chaos(state: ExplorationState, *, max_examples: int = 10) -> ExplorationState:
    """Auto-generate and run chaos tests, update state."""
    from ordeal.auto import chaos_for

    try:
        TestCase = chaos_for(state.module, max_examples=max_examples, stateful_step_count=10)
        test = TestCase("runTest")
        test.runTest()
    except Exception:
        pass

    # Mark all functions as chaos-tested
    for fs in state.functions.values():
        fs.chaos_tested = True
    return state


def explore(
    module: str,
    *,
    state: ExplorationState | None = None,
    time_limit: float | None = None,
    workers: int = 1,
    max_examples: int = 50,
) -> ExplorationState:
    """Run all exploration strategies on a module.

    Assembles mine → scan → mutate → chaos into one pass.
    Each step enriches the shared ``ExplorationState``.

    Scales with compute: more *workers* → more mutations tested
    in parallel, more *max_examples* → more input space sampled.
    Confidence grows with both.

    The AI assistant can also run steps individually via
    ``explore_mine``, ``explore_scan``, ``explore_mutate``,
    ``explore_chaos`` for finer control.

    Args:
        module: Dotted module path.
        state: Resume from a previous exploration. ``None`` starts fresh.
        time_limit: Optional time budget in seconds (soft limit).
        workers: Parallel workers for mutation testing. More CPUs = more
            state space explored per unit time.
        max_examples: Hypothesis examples for mining and scanning. More
            examples = more input space sampled = higher confidence.
    """
    import time as _time

    if state is None:
        state = ExplorationState(module=module)

    start = _time.monotonic()

    # Step 1: Mine properties — scales with max_examples
    state = explore_mine(state, max_examples=max_examples)

    # Step 2: Crash safety — scales with max_examples
    if time_limit is None or (_time.monotonic() - start) < time_limit:
        state = explore_scan(state, max_examples=max_examples)

    # Step 3: Mutation testing — scales with workers
    if time_limit is None or (_time.monotonic() - start) < time_limit:
        state = explore_mutate(state, workers=workers)

    # Step 4: Chaos testing
    if time_limit is None or (_time.monotonic() - start) < time_limit:
        state = explore_chaos(state, max_examples=max_examples)

    state.exploration_time += _time.monotonic() - start
    return state
