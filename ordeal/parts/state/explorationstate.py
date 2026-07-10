from __future__ import annotations
# ruff: noqa
@dataclass
class ExplorationState:
    """Unified exploration state for a module.

    Accumulates results from all ordeal tools. JSON-serializable
    for persistence across sessions.
    """

    module: str
    functions: dict[str, FunctionState] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    edge_coverage: float | None = None
    exploration_time: float = 0.0
    supervisor_info: dict[str, Any] = field(default_factory=dict)
    tree: Any = field(default=None, repr=False)
    scan_mode: str = "evidence"
    active_dimensions: tuple[str, ...] = _ALL_EXPLORATION_DIMENSIONS

    def function(self, name: str) -> FunctionState:
        """Get or create state for a function."""
        if name not in self.functions:
            self.functions[name] = FunctionState(name=name)
        return self.functions[name]

    def refresh(self) -> list[str]:
        """Invalidate functions whose source code changed since last exploration.

        Compares stored ``source_hash`` on each function against the
        current source.  Changed functions are :meth:`reset` — all
        prior results are discarded so the next exploration redoes them
        from scratch.  Fresh source hashes are stamped on every
        function that can be resolved.

        Returns the names of functions that were invalidated.
        """
        from ordeal.auto import _get_public_functions, _resolve_module

        invalidated: list[str] = []
        try:
            mod = _resolve_module(self.module)
            current = {name: func for name, func in _get_public_functions(mod)}
        except Exception:
            return invalidated

        for name, fs in list(self.functions.items()):
            func = current.get(name)
            if func is None:
                # Function removed — reset so frontier shows gaps.
                fs.reset()
                invalidated.append(name)
                continue
            h = _source_hash(func)
            if fs.source_hash is not None and h != fs.source_hash:
                fs.reset()
                invalidated.append(name)
            # Stamp fresh hash regardless (covers first-time and post-reset).
            fs.source_hash = h

        return invalidated

    @property
    def confidence(self) -> float:
        """Aggregate confidence across all functions."""
        if not self.functions:
            return 0.0
        return sum(
            f.confidence_for(self.active_dimensions) for f in self.functions.values()
        ) / len(self.functions)

    @property
    def frontier(self) -> dict[str, list[str]]:
        """Per-function gaps — what's unexplored."""
        return {
            name: gaps
            for name, fs in self.functions.items()
            if (gaps := fs.frontier_for(self.active_dimensions))
        }

    @property
    def findings(self) -> list[str]:
        """Promoted findings worth treating as primary scan output."""
        from ordeal.auto import _contract_violation_promoted, _scan_crash_promoted

        results: list[str] = []
        for name, fs in self.functions.items():
            if any(
                _contract_violation_promoted(detail)
                and detail.get("category") == "lifecycle_contract"
                for detail in fs.contract_violation_details
            ):
                results.append(f"{name}: violates an explicit lifecycle contract")
            if any(
                _contract_violation_promoted(detail)
                and detail.get("category") == "semantic_contract"
                for detail in fs.contract_violation_details
            ):
                results.append(f"{name}: violates an explicit semantic contract")
            if fs.crash_free is False and _scan_crash_promoted(
                category=fs.scan_crash_category,
                replayable=fs.scan_replayable,
                proof_bundle=fs.scan_proof_bundle,
                sink_categories=fs.scan_sink_categories,
            ):
                results.append(_crash_summary(name, fs.scan_crash_category, fs.scan_replayable))
        return results

    @property
    def exploratory_findings(self) -> list[str]:
        """Exploratory scan output that should not fail the run by default."""
        from ordeal.auto import _reportable_crash_category

        results: list[str] = []
        for name, fs in self.functions.items():
            category = _reportable_crash_category(
                category=fs.scan_crash_category,
                replayable=fs.scan_replayable,
                proof_bundle=fs.scan_proof_bundle,
                sink_categories=fs.scan_sink_categories,
            )
            if fs.crash_free is False and (
                category == "speculative_crash"
                or category == "coverage_gap"
                or category == "invalid_input_crash"
                or category == "beyond_declared_contract_robustness"
            ):
                results.append(_crash_summary(name, category, fs.scan_replayable))
            for v in fs.property_violations:
                results.append(f"{name}: {v}")
            for note in fs.contract_violations:
                results.append(f"{name}: {note}")
        return results

    @property
    def finding_details(self) -> list[dict[str, Any]]:
        """Structured finding details for reports and AI handoff."""
        from ordeal.auto import _reportable_crash_category

        details: list[dict[str, Any]] = []
        for name, fs in self.functions.items():
            if fs.scan_limitation_kind is not None:
                details.append(
                    {
                        "kind": "blocked",
                        "category": "tool_limitation",
                        "evidence_class": "blocked",
                        "function": name,
                        "summary": f"{name}: Ordeal could not reach a measured target execution",
                        "error": fs.scan_error,
                        "limitation_kind": fs.scan_limitation_kind,
                        "blocking_reason": fs.scan_blocking_reason,
                        "source_sha256": fs.source_hash,
                        "lifecycle_signal": 0.0,
                    }
                )
            if fs.crash_free is False:
                category = _reportable_crash_category(
                    category=fs.scan_crash_category,
                    replayable=fs.scan_replayable,
                    proof_bundle=fs.scan_proof_bundle,
                    sink_categories=fs.scan_sink_categories,
                )
                details.append(
                    {
                        "kind": "crash",
                        "category": category,
                        "evidence_class": _evidence_class(category),
                        "function": name,
                        "summary": (_crash_summary(name, category, fs.scan_replayable)),
                        "error": fs.scan_error,
                        "failing_args": fs.failing_args,
                        "replayable": fs.scan_replayable,
                        "replay_attempts": fs.scan_replay_attempts,
                        "replay_matches": fs.scan_replay_matches,
                        "minimization": fs.scan_minimization,
                        "contract_fit": fs.scan_contract_fit,
                        "reachability": fs.scan_reachability,
                        "realism": fs.scan_realism,
                        "sink_signal": fs.scan_sink_signal,
                        "sink_categories": fs.scan_sink_categories,
                        "input_sources": fs.scan_input_sources,
                        "input_source": fs.scan_input_source,
                        "proof_bundle": fs.scan_proof_bundle,
                        "source_sha256": fs.source_hash,
                        "lifecycle_signal": (
                            1.0
                            if isinstance(fs.scan_proof_bundle, dict)
                            and isinstance(fs.scan_proof_bundle.get("lifecycle"), dict)
                            else 0.0
                        ),
                    }
                )
            for item in fs.contract_violation_details:
                details.append(
                    {
                        "function": name,
                        "source_sha256": fs.source_hash,
                        "evidence_class": _evidence_class(str(item.get("category", ""))),
                        "lifecycle_signal": (
                            1.0
                            if item.get("lifecycle_phase") is not None
                            or item.get("lifecycle_probe") is not None
                            else 0.0
                        ),
                        **item,
                    }
                )
            for item in fs.property_violation_details:
                details.append(
                    {
                        "kind": "property",
                        "category": "speculative_property",
                        "evidence_class": "speculative_property",
                        "function": name,
                        "source_sha256": fs.source_hash,
                        **item,
                    }
                )
            if fs.mutation_score is not None and fs.mutation_score < 0.5:
                details.append(
                    {
                        "kind": "mutation",
                        "category": "test_strength_gap",
                        "evidence_class": "test_strength_gap",
                        "function": name,
                        "summary": f"{name}: mutation score {fs.mutation_score:.0%}",
                        "mutation_score": fs.mutation_score,
                        "survived_mutants": fs.survived_mutants,
                    }
                )
        category_rank = {
            "lifecycle_contract": 0,
            "likely_bug": 1,
            "semantic_contract": 2,
            "coverage_gap": 3,
            "speculative_crash": 4,
            "invalid_input_crash": 5,
            "speculative_property": 6,
            "test_strength_gap": 7,
            "verification_warning": 8,
            "tool_limitation": 9,
        }
        return sorted(
            details,
            key=lambda detail: (
                category_rank.get(str(detail.get("category")), 99),
                -float(detail.get("lifecycle_signal") or 0.0),
                -float(detail.get("sink_signal") or 0.0),
                -float(detail.get("contract_fit") or 0.0),
                -float(detail.get("reachability") or 0.0),
                str(detail.get("function") or detail.get("qualname") or ""),
            ),
        )

    def summary(self) -> str:
        """Human-readable exploration report."""
        lines = [f"Exploration: {self.module}"]
        lines.append(f"  confidence: {self.confidence:.0%}")
        lines.append(f"  functions:  {len(self.functions)}")
        if self.skipped:
            lines.append(f"  skipped:    {len(self.skipped)} functions")
            for name, reason in self.skipped:
                lines.append(f"    {name}: {reason}")
        if self.supervisor_info:
            seed = self.supervisor_info.get("seed", "?")
            traj = self.supervisor_info.get("trajectory_steps", 0)
            states = self.supervisor_info.get("unique_states", 0)
            lines.append(f"  seed: {seed} ({traj} transitions, {states} states)")
        if self.tree and self.tree.size > 0:
            lines.append(
                f"  state tree: {self.tree.size} checkpoints, depth {self.tree.max_depth}"
            )
        if self.findings:
            lines.append(f"  findings:   {len(self.findings)}")
            for f in self.findings:
                lines.append(f"    - {f}")
        if self.exploratory_findings:
            lines.append(f"  exploratory:{len(self.exploratory_findings):>4}")
            for finding in self.exploratory_findings:
                lines.append(f"    - {finding}")
        frontier = self.frontier
        if frontier:
            lines.append(f"  frontier:   {sum(len(v) for v in frontier.values())} gaps")
            for name, gaps in frontier.items():
                lines.append(f"    {name}: {', '.join(gaps)}")
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize to JSON for persistence."""
        return json.dumps(self.to_dict(), indent=2)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for persistence and agents.

        The state tree's snapshots are excluded (not JSON-serializable).
        The tree structure is preserved via ``tree.to_json()``.
        """
        from ordeal.suggest import suggest

        return {
            "module": self.module,
            "confidence": round(self.confidence, 4),
            "functions": {name: _json_ready(asdict(fs)) for name, fs in self.functions.items()},
            "findings": self.findings,
            "exploratory_findings": self.exploratory_findings,
            "finding_details": _json_ready(self.finding_details),
            "frontier": self.frontier,
            "suggestions": suggest(self),
            "skipped": self.skipped,
            "exploration_time": round(self.exploration_time, 2),
            "seed": self.supervisor_info.get("seed"),
            "scan_mode": self.scan_mode,
            "active_dimensions": list(self.active_dimensions),
            "supervisor_info": _json_ready(self.supervisor_info),
        }

    @classmethod
    def from_json(cls, data: str) -> ExplorationState:
        """Deserialize from JSON."""
        raw = json.loads(data)
        state = cls(module=raw["module"])
        state.edge_coverage = raw.get("edge_coverage")
        state.exploration_time = raw.get("exploration_time", 0.0)
        state.scan_mode = raw.get("scan_mode", "evidence")
        state.active_dimensions = tuple(raw.get("active_dimensions", _ALL_EXPLORATION_DIMENSIONS))
        state.supervisor_info = raw.get("supervisor_info", {})
        for name, fdata in raw.get("functions", {}).items():
            fs = FunctionState(**fdata)
            state.functions[name] = fs
        return state
# ============================================================================
# Exploration steps — each enriches the state from one dimension
# ============================================================================


def explore_mine(
    state: ExplorationState,
    *,
    max_examples: int = 50,
    include_private: bool = False,
    targets: list[str] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
) -> ExplorationState:
    """Mine all functions in the module and update state."""

    from ordeal.auto import _infer_strategies, _resolve_module, _selected_public_functions
    from ordeal.mine import _is_suspicious_property, mine

    mod = _resolve_module(state.module)
    funcs = _selected_public_functions(
        mod,
        targets=targets,
        include_private=include_private,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
    )

    # Track skipped functions with reasons
    for name, func in funcs:
        strats = _infer_strategies(func, None)
        if strats is None:
            state.skipped.append((name, "can't infer strategies (Optional/complex params)"))

    for name, func in funcs:
        try:
            result = mine(
                func,
                max_examples=max_examples,
                ignore_properties=sorted(
                    {
                        *(ignore_properties or []),
                        *(property_overrides or {}).get(name, []),
                    }
                ),
                ignore_relations=sorted(
                    {
                        *(ignore_relations or []),
                        *(relation_overrides or {}).get(name, []),
                    }
                ),
                property_overrides=property_overrides,
                relation_overrides=relation_overrides,
            )
        except Exception:
            continue
        fs = state.function(name)
        fs.source_hash = _source_hash(func)
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
        fs.edges_discovered = result.edges_discovered
        fs.saturated = result.saturated
        fs.property_violations = []
        fs.property_violation_details = []
        for prop in result.properties:
            if not _is_suspicious_property(prop):
                continue
            label = f"{prop.name} ({prop.confidence:.0%})"
            fs.property_violations.append(label)
            fs.property_violation_details.append(
                {
                    "name": prop.name,
                    "summary": label,
                    "confidence": round(prop.confidence, 4),
                    "holds": prop.holds,
                    "total": prop.total,
                    "counterexample": prop.counterexample,
                    "replayable": prop.replayable,
                    "replay_attempts": prop.replay_attempts,
                    "replay_matches": prop.replay_matches,
                    "replay_match_basis": prop.replay_match_basis,
                    "minimization": prop.minimization,
                }
            )
    return state
