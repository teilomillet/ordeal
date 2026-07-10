from __future__ import annotations
# ruff: noqa

def _ring_write(
    buf: memoryview,
    slot: int,
    seq: int,
    writer_id: int,
    energy: float,
    data: bytes,
    new_edges: int,
    step: int,
) -> bool:
    """Write a serialized checkpoint into a ring buffer slot.

    Writes data first, then the header, then sequence *last*.
    The sequence field is the "publish" signal — readers ignore
    slots where sequence == 0 or hasn't changed.

    Returns False if data exceeds the slot capacity.
    """
    if len(data) > _POOL_SLOT_DATA_MAX:
        return False
    base = _POOL_HEADER_SIZE + slot * _POOL_SLOT_SIZE
    # 1. Write data bytes
    d_start = base + _POOL_SLOT_HDR_SIZE
    buf[d_start : d_start + len(data)] = data
    # 2. Write header fields (except sequence)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    struct.pack_into("<HH", buf, base + 4, writer_id, 0)
    struct.pack_into("<f", buf, base + 8, energy)
    struct.pack_into("<I", buf, base + 12, len(data))
    struct.pack_into("<I", buf, base + 16, crc)
    struct.pack_into("<I", buf, base + 20, new_edges)
    struct.pack_into("<I", buf, base + 24, step)
    # 3. Sequence LAST — signals "slot is ready"
    struct.pack_into("<I", buf, base, seq)
    return True
def _pool_encode_payload(payload: dict[str, Any], auth_key: bytes | None) -> bytes:
    """Serialize one checkpoint payload with an authentication tag."""
    encoded = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    if auth_key is None:
        return encoded
    return hmac.digest(auth_key, encoded, "sha256") + encoded
def _pool_decode_payload(data: bytes, auth_key: bytes | None) -> dict[str, Any]:
    """Verify and deserialize one checkpoint payload."""
    encoded = data
    if auth_key is not None:
        if len(data) <= _POOL_AUTH_TAG_SIZE:
            raise ValueError("checkpoint payload missing authentication tag")
        tag = data[:_POOL_AUTH_TAG_SIZE]
        encoded = data[_POOL_AUTH_TAG_SIZE:]
        expected = hmac.digest(auth_key, encoded, "sha256")
        if not hmac.compare_digest(tag, expected):
            raise ValueError("checkpoint payload authentication failed")
    payload = pickle.loads(encoded)
    if not isinstance(payload, dict):
        raise TypeError("checkpoint payload must deserialize to a dict")
    return payload
def _ring_read(buf: memoryview, slot: int) -> dict[str, Any] | None:
    """Read a checkpoint from a ring buffer slot.

    Returns None for empty slots, oversized data, or checksum mismatches
    (torn reads).  Callers retry on the next poll cycle.
    """
    base = _POOL_HEADER_SIZE + slot * _POOL_SLOT_SIZE
    seq = struct.unpack_from("<I", buf, base)[0]
    if seq == 0:
        return None
    writer_id = struct.unpack_from("<H", buf, base + 4)[0]
    energy = struct.unpack_from("<f", buf, base + 8)[0]
    data_len = struct.unpack_from("<I", buf, base + 12)[0]
    checksum = struct.unpack_from("<I", buf, base + 16)[0]
    new_edges = struct.unpack_from("<I", buf, base + 20)[0]
    step_val = struct.unpack_from("<I", buf, base + 24)[0]
    if data_len == 0 or data_len > _POOL_SLOT_DATA_MAX:
        return None
    d_start = base + _POOL_SLOT_HDR_SIZE
    data = bytes(buf[d_start : d_start + data_len])
    if (zlib.crc32(data) & 0xFFFFFFFF) != checksum:
        return None  # torn read — skip until next poll
    return {
        "sequence": seq,
        "writer_id": writer_id,
        "energy": energy,
        "data": data,
        "new_edge_count": new_edges,
        "step": step_val,
        "slot": slot,
    }
def _ring_update_energy(buf: memoryview, slot: int, energy: float) -> None:
    """Propagate an energy update to a ring buffer slot.

    Any worker can call this.  Relaxed consistency: other workers
    see the update on their next poll, no barriers needed.
    """
    base = _POOL_HEADER_SIZE + slot * _POOL_SLOT_SIZE
    struct.pack_into("<f", buf, base + 8, energy)
@dataclass
class _MachineSnapshot:
    """Lightweight snapshot: user state dict + fault active flags.

    Avoids deep-copying Fault objects (which carry locks, compiled
    patterns, and monkeypatched references).  Restore by creating a
    fresh machine and overlaying the saved state.
    """

    state_dict: dict[str, Any]
    fault_active: dict[str, bool]
@dataclass
class Checkpoint:
    """A saved machine state with energy-based scheduling weight and seed corpus.

    Each checkpoint stores the machine state *and* the rule parameters that
    led to new coverage from that state.  When the explorer branches from
    this checkpoint, it can either generate fresh parameters (Hypothesis
    strategies) or **mutate** a productive seed — the AFL closed-loop
    pattern adapted for stateful testing.

    The ``seed_params`` list is bounded by ``_MAX_SEEDS_PER_CHECKPOINT``
    to prevent memory growth.  When full, new seeds replace the lowest-energy
    entry (the one that was mutated most without finding new coverage).

    Attributes:
        snapshot: The machine state at checkpoint time.
        new_edge_count: Number of new edges found when this checkpoint was created.
        step: The step index within the run where this checkpoint was taken.
        run_id: The run that produced this checkpoint.
        energy: Energy-based scheduling weight (AFL++ power schedule analog).
            Checkpoints that lead to new edges get rewarded; others decay.
        times_selected: How many times this checkpoint has been branched from.
            Used in energy selection to penalize over-exploitation.
        seed_params: Productive ``(rule_name, params_dict)`` pairs that led
            to new coverage from this checkpoint's state.  Used as mutation
            seeds when branching from this checkpoint.
    """

    snapshot: _MachineSnapshot
    new_edge_count: int
    step: int
    run_id: int
    energy: float = 1.0
    times_selected: int = 0
    seed_params: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _pool_slot: int = -1  # ring buffer slot (-1 = local checkpoint)
# ============================================================================
# Progress reporting
# ============================================================================


@dataclass
class ProgressSnapshot:
    """Live stats emitted during exploration."""

    elapsed: float
    total_runs: int
    total_steps: int
    unique_edges: int
    checkpoints: int
    failures: int
    runs_per_second: float
# ============================================================================
# Results
# ============================================================================


def _error_display_name(error: Exception) -> str:
    """Return the most useful display name for *error*."""
    return getattr(error, "error_type", type(error).__name__)
@dataclass
class Failure:
    """A failure found during exploration, with optional trace for replay."""

    error: Exception
    step: int
    run_id: int
    active_faults: list[str]
    rule_log: list[str]
    trace: Trace | None = None
    necessary_faults: dict[str, bool] | None = None
    error_traceback: str | None = None
    native_boundary: dict[str, Any] | None = None

    def __str__(self) -> str:
        faults = ", ".join(self.active_faults) or "none"
        last_rules = " -> ".join(self.rule_log[-10:])
        shrunk = ""
        if self.trace:
            shrunk = f" (shrunk to {len(self.trace.steps)} steps)"
        ablation = ""
        if self.necessary_faults:
            needed = [f for f, necessary in self.necessary_faults.items() if necessary]
            if needed:
                ablation = f"\n  Necessary faults: {', '.join(needed)}"
            else:
                ablation = "\n  Necessary faults: none (fails without any faults)"
        boundary = ""
        if self.native_boundary:
            boundary = f"\n  Native boundary: {_format_native_boundary(self.native_boundary)}"
        return (
            f"Run {self.run_id}, step {self.step}: "
            f"{_error_display_name(self.error)}: {self.error}{shrunk}\n"
            f"  Active faults: {faults}{ablation}{boundary}\n"
            f"  Sequence: {last_rules}"
        )
@dataclass
class ExplorationResult:
    """Aggregated results from an exploration run."""
    total_runs: int = 0
    total_steps: int = 0
    skipped_steps: int = 0
    unique_edges: int = 0
    checkpoints_saved: int = 0
    failures: list[Failure] = field(default_factory=list)
    duration_seconds: float = 0.0
    edge_log: list[tuple[int, int]] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)
    last_new_edge_run: int = 0
    runs_since_new_edge: int = 0
    saturated: bool = False
    stopped_reason: str = ""
    adaptation_phase: int = 0
    unique_states: int = 0
    properties_satisfied: int = 0
    mutations_total: int = 0
    mutations_killed: int = 0
    seed_mutations_used: int = 0
    seed_mutations_productive: int = 0
    strategy_failures: dict[str, int] = field(default_factory=dict)
    ngram: int = 1
    seed_replays: list[dict[str, Any]] = field(default_factory=list)
    rule_swarm_runs: int = 0
    swarm_stats: list[dict[str, Any]] = field(default_factory=list)
    fault_pair_coverage: list[dict[str, Any]] = field(default_factory=list)
    uncovered_fault_pairs: list[list[str]] = field(default_factory=list)
    rule_fault_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    behavior_coverage: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    property_stress: dict[str, dict[str, int]] = field(default_factory=dict)
    native_boundary_findings: list[dict[str, Any]] = field(default_factory=list)
    coverage_gaps: list[dict[str, Any]] = field(default_factory=list)
    lines_covered: int = 0
    lines_total: int = 0
    coordination_mode: str = "sequential"
    coordination_degraded_reason: str = ""
    parallel_result_warning: str = ""
    parallel_fallback_reason: str = ""

    def summary(self) -> str:
        """Human-readable exploration summary."""
        steps_info = f"{self.total_steps} steps"
        if self.skipped_steps > 0:
            steps_info += f" ({self.skipped_steps} skipped — strategy generation failed)"
        ngram_label = (
            f" (ngram={self.ngram}, path-context)" if self.ngram > 1 else " (single-edge)"
        )
        lines = [
            f"Exploration: {self.total_runs} runs, {steps_info}, {self.duration_seconds:.1f}s",
            f"Coverage: {self.unique_edges} edges{ngram_label}, "
            f"{self.checkpoints_saved} checkpoints",
        ]
        if self.unique_states > 0:
            lines.append(f"States: {self.unique_states} unique state hashes")
        if self.properties_satisfied > 0:
            lines.append(f"Properties: {self.properties_satisfied} sometimes-properties satisfied")
        if self.mutations_total > 0:
            survived = self.mutations_total - self.mutations_killed
            lines.append(
                f"Mutations: {self.mutations_killed}/{self.mutations_total} killed"
                f" ({survived} survived)"
            )
        if self.rule_swarm_runs > 0:
            lines.append(
                f"Swarm: {self.rule_swarm_runs}/{self.total_runs} runs"
                f" used joint rule+fault configs"
            )
            if self.swarm_stats:
                lines.append(
                    "  Swarm leaders: "
                    + "; ".join(_format_swarm_config_summary(row) for row in self.swarm_stats[:3])
                )
            if self.uncovered_fault_pairs:
                missing = ["/".join(pair) for pair in self.uncovered_fault_pairs[:4]]
                suffix = (
                    f" (+{len(self.uncovered_fault_pairs) - 4} more)"
                    if len(self.uncovered_fault_pairs) > 4
                    else ""
                )
                lines.append(f"  Missing fault pairs: {', '.join(missing)}{suffix}")
        rule_fault_combos = sum(len(faults) for faults in self.rule_fault_coverage.values())
        property_combos = sum(
            len(props)
            for fault_map in self.behavior_coverage.values()
            for props in fault_map.values()
        )
        if rule_fault_combos > 0:
            lines.append(
                f"Behavior: {rule_fault_combos} rule/fault combos, "
                f"{property_combos} rule/fault/property observations"
            )
            hotspots = _top_property_stress(self.property_stress)
            if hotspots:
                lines.append(
                    "  Property stress: "
                    + "; ".join(
                        f"{item['property']} under {item['faults']} x{item['hits']}"
                        for item in hotspots[:3]
                    )
                )
        if self.native_boundary_findings:
            labels = Counter(
                "timeout"
                if item.get("mode") == "timeout"
                else (
                    "signal death"
                    if item.get("mode") == "signal"
                    else "nonzero exit"
                    if item.get("mode") == "exit_code"
                    else str(item.get("mode", "subprocess failure"))
                )
                for item in self.native_boundary_findings
            )
            lines.append(
                "Native boundary: "
                f"{len(self.native_boundary_findings)} findings "
                f"({_format_counter_summary(labels)})"
            )
        if self.seed_mutations_used > 0:
            lines.append(
                f"Seed mutations: {self.seed_mutations_used} used, "
                f"{self.seed_mutations_productive} productive"
            )
        if self.strategy_failures:
            parts = [
                f"{name} ({count} times)"
                for name, count in sorted(self.strategy_failures.items(), key=lambda x: -x[1])
            ]
            lines.append(
                f"Strategy failures: {', '.join(parts)} — check type hints or provide fixtures"
            )
        if self.adaptation_phase > 0:
            lines.append(f"Adapted: {self.adaptation_phase} phase(s) of escalation")
        mode = self.coordination_mode.replace("_and_", " + ").replace("_", " ")
        if mode == "independent workers":
            mode = "independent multiprocess workers"
        lines.append(f"Coordination: {mode}")
        if self.coordination_degraded_reason:
            lines.append(f"Coordination degraded: {self.coordination_degraded_reason}")
        if self.parallel_result_warning:
            lines.append(f"Parallel result warning: {self.parallel_result_warning}")
        if self.parallel_fallback_reason:
            lines.append(
                f"Parallel fallback: reran with workers=1 after {self.parallel_fallback_reason}"
            )
        if self.unique_edges > 0 and self.total_runs > 0:
            if self.saturated:
                lines.append(
                    f"Saturated: no new edges for {self.runs_since_new_edge} runs "
                    f"(last discovery at run {self.last_new_edge_run})"
                )
            elif self.runs_since_new_edge > self.total_runs * 0.5:
                lines.append(
                    f"Coverage stale: {self.runs_since_new_edge} runs since last new edge"
                )
        if self.failures:
            lines.append(f"Failures found: {len(self.failures)}")
            for f in self.failures[:5]:
                lines.append(f"  {f}")
        elif self.saturated:
            lines.append("No failures found \u2014 all reachable paths explored.")
        else:
            lines.append("No failures found.")
        if self.lines_total > 0:
            pct = self.lines_covered / self.lines_total * 100
            lines.append(f"Line coverage: {self.lines_covered}/{self.lines_total} ({pct:.0f}%)")
        if self.coverage_gaps:
            n = len(self.coverage_gaps)
            run_ctx = f" in {self.total_runs} runs" if self.total_runs else ""
            lines.append(f"Not reached{run_ctx}: {n} branch(es) in target modules")
            suggestions = self.reachability_suggestions()
            for s in suggestions[:5]:
                lines.append(f"  {s['file']}:{s['line']} {s['code']}")
                lines.append(f"    add: {s['suggestion']}")
            if n > 5:
                lines.append(f"  ... and {n - 5} more")
        if self.seed_replays:
            reproduced = sum(1 for s in self.seed_replays if s["reproduced"])
            fixed = len(self.seed_replays) - reproduced
            parts = []
            if reproduced:
                parts.append(f"{reproduced} reproduced")
            if fixed:
                parts.append(f"{fixed} fixed")
            lines.append(
                f"Seed corpus: {len(self.seed_replays)} seeds replayed ({', '.join(parts)})"
            )
        if self.stopped_reason:
            lines.append(f"Stopped: {self.stopped_reason}")

        # Structured capabilities — what was active vs not.
        caps = self.capabilities_used
        unused = [k for k, v in caps.items() if not v]
        if unused:
            lines.append(f"Unused capabilities: {', '.join(unused)}")

        return "\n".join(lines)

    @property
    def capabilities_used(self) -> dict[str, bool]:
        """Which exploration capabilities were active for this run.

        Exposes structured metadata so tooling (or an AI assistant) can
        identify what's available but wasn't exercised, and decide
        whether to suggest it based on context.
        """
        return {
            "state_hash": self.unique_states > 0,
            "mutations": self.mutations_total > 0,
            "checkpoints": self.checkpoints_saved > 0,
            "sometimes_properties": self.properties_satisfied > 0,
            "behavior_coverage": bool(self.rule_fault_coverage),
            "rule_swarm": self.rule_swarm_runs > 0,
            "native_boundary": bool(self.native_boundary_findings),
        }

    def reachability_suggestions(self) -> list[dict[str, Any]]:
        """Generate ``reachable()`` suggestions for branches not reached.

        Each suggestion is a structured dict an AI assistant can act on:

        - ``file``: source file path
        - ``line``: line number of the branch not reached
        - ``code``: the branch statement (``if``, ``for``, etc.)
        - ``suggestion``: a ``reachable()`` call to insert near that line
        - ``confidence``: ``"not_reached"`` — the explorer did not hit
          this line; it may be reachable with more runs or different faults
        - ``runs``: number of exploration runs in this session

        **Epistemic note**: these are branches the explorer did not reach.
        Adding ``reachable()`` lets future runs prove whether the branch
        is reachable or genuinely dead code.

        Returns an empty list if there are no coverage gaps.
        """
        suggestions = []
        for gap in self.coverage_gaps:
            label = f"{gap['file']}:{gap['line']}"
            suggestion = f'reachable("{label}: {gap["code"]}")'
            suggestions.append(
                {
                    "file": gap["file"],
                    "line": gap["line"],
                    "code": gap["code"],
                    "suggestion": suggestion,
                    "confidence": "not_reached",
                    "runs": self.total_runs,
                }
            )
        return suggestions
def _fault_signature(active_faults: list[str]) -> str:
    """Stable label for one active-fault set."""
    if not active_faults:
        return "none"
    return ",".join(sorted(dict.fromkeys(active_faults)))
def _property_counter_snapshot(tracker: Any) -> dict[str, tuple[str, int, int, int]]:
    """Capture the current property counters keyed by property name."""
    return {
        p.name: (p.type, int(p.hits), int(p.passes), int(p.failures))
        for p in tracker.results
        if isinstance(getattr(p, "name", None), str)
    }
