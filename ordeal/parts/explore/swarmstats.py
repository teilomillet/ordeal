from __future__ import annotations
# ruff: noqa
def _swarm_stats(self) -> list[dict[str, Any]]:
    """Structured summary rows for the tracked swarm configurations."""
    rows = [
        {
            "active_rules": list(cfg.active_rules),
            "active_faults": list(cfg.active_faults),
            "energy": float(cfg.energy),
            "times_used": int(cfg.times_used),
            "edges_found": int(cfg.edges_found),
            "runs_with_new_edges": int(cfg.runs_with_new_edges),
            "failure_count": int(cfg.failure_count),
            "property_hits": int(cfg.property_hits),
        }
        for cfg in self._swarm_configs.values()
        if cfg.times_used > 0
    ]
    rows.sort(
        key=lambda row: (
            -row["failure_count"],
            -row["edges_found"],
            -row["property_hits"],
            -row["energy"],
            -row["times_used"],
            tuple(row["active_rules"]),
            tuple(row["active_faults"]),
        )
    )
    return rows
_swarm_stats.__qualname__ = "Explorer._swarm_stats"
Explorer._swarm_stats = _swarm_stats
del _swarm_stats
def _fault_pair_rows(self, all_fault_names: list[str]) -> list[dict[str, Any]]:
    """Structured coverage rows for every possible fault pair."""
    rows = []
    for pair in combinations(sorted(dict.fromkeys(all_fault_names)), 2):
        rows.append({"pair": list(pair), "hits": int(self._fault_pair_hits.get(pair, 0))})
    rows.sort(key=lambda row: (row["hits"], row["pair"]))
    return rows
_fault_pair_rows.__qualname__ = "Explorer._fault_pair_rows"
Explorer._fault_pair_rows = _fault_pair_rows
del _fault_pair_rows
def _select_swarm_config(self, machine: ChaosTest, total_runs: int) -> SwarmConfig | None:
    """Select a joint rule+fault configuration for this run.

    **Phase 1 (warmup)**: pure coin-flip per feature (Groce et al.).
    **Phase 2 (adaptive)**: energy-weighted selection from previously
    seen configs, with coin-flip fallback for exploration.

    Returns ``None`` if swarm shouldn't apply (e.g. single rule, no faults).
    """
    n_rules = len(self._rules)
    all_fault_names = [f.name for f in machine._faults]
    n_faults = len(all_fault_names)
    n_features = n_rules + n_faults

    if n_features <= 1:
        return None

    # Phase 1: pure coin-flip (warmup or no history)
    if total_runs < _SWARM_WARMUP_RUNS or not self._swarm_configs:
        pairwise = self._pairwise_fault_config(n_rules, all_fault_names)
        if pairwise is not None and self.rng.random() < 0.5:
            return pairwise
        return self._coin_flip_config(n_rules, all_fault_names)

    # Phase 2: mixed strategy (paper §2.2: "include C_D in every swarm set").
    #   10% full config C_D (all rules + all faults — catches sequence bugs)
    #   15% pure coin-flip (explore — catches accumulation bugs)
    #   10% coverage-directed (steer — reaches uncovered branches)
    #   15% pairwise-directed (close fault-pair gaps early)
    #   50% energy-weighted from history (exploit — repeats what works)
    roll = self.rng.random()
    if roll < 0.1:
        # C_D: the default all-inclusive config. Guarantees sequence
        # bugs are always findable (paper §2.2 mitigation strategy).
        all_rules = [r.name for r in self._rules]
        cfg = SwarmConfig(active_rules=all_rules, active_faults=list(all_fault_names))
        key = cfg.key
        if key not in self._swarm_configs:
            self._swarm_configs[key] = cfg
        self._swarm_configs[key].times_used += 1
        return self._swarm_configs[key]
    if roll < 0.25:
        return self._coin_flip_config(n_rules, all_fault_names)
    if roll < 0.35:
        # Coverage-directed: bias toward rules that exercise files
        # with uncovered branches (if we have gap data)
        directed = self._coverage_directed_config(n_rules, all_fault_names)
        if directed is not None:
            return directed
        return self._coin_flip_config(n_rules, all_fault_names)
    if roll < 0.5:
        pairwise = self._pairwise_fault_config(n_rules, all_fault_names)
        if pairwise is not None:
            return pairwise
        return self._coin_flip_config(n_rules, all_fault_names)

    # Select from existing configs weighted by energy
    configs = list(self._swarm_configs.values())
    energies = [c.energy for c in configs]
    total_energy = sum(energies)
    if total_energy <= 0:
        return self._coin_flip_config(n_rules, all_fault_names)

    r = self.rng.random() * total_energy
    cumulative = 0.0
    for cfg in configs:
        cumulative += cfg.energy
        if cumulative >= r:
            cfg.times_used += 1
            return cfg

    configs[-1].times_used += 1
    return configs[-1]  # fallback
_select_swarm_config.__qualname__ = "Explorer._select_swarm_config"
Explorer._select_swarm_config = _select_swarm_config
del _select_swarm_config
def _coin_flip_config(self, n_rules: int, all_fault_names: list[str]) -> SwarmConfig:
    """Generate a random config via independent Bernoulli(0.5) per feature.

    Joint bitmask over rules + faults.  At least one rule is always kept.
    """
    # Rules: coin flip, at least one
    if n_rules > 1:
        rule_mask = self.rng.randint(1, (1 << n_rules) - 1)
        active_rules = [self._rules[i].name for i in range(n_rules) if rule_mask & (1 << i)]
    else:
        active_rules = [self._rules[0].name]

    # Faults: coin flip, can be empty (no faults toggled this run is fine)
    active_faults: list[str] = []
    for fname in all_fault_names:
        if self.rng.random() < 0.5:
            active_faults.append(fname)

    cfg = SwarmConfig(active_rules=active_rules, active_faults=active_faults)

    # Register in history for energy tracking (dedup by key)
    key = cfg.key
    if key not in self._swarm_configs:
        self._swarm_configs[key] = cfg
    self._swarm_configs[key].times_used += 1
    return self._swarm_configs[key]
_coin_flip_config.__qualname__ = "Explorer._coin_flip_config"
Explorer._coin_flip_config = _coin_flip_config
del _coin_flip_config
def _coverage_directed_config(
    self, n_rules: int, all_fault_names: list[str]
) -> SwarmConfig | None:
    """Generate a config biased toward rules that exercise uncovered files.

    Uses ``_rule_file_coverage`` (which rules led to edges in which
    files) and ``_gap_files`` (files with uncovered branches) to
    boost inclusion probability for rules that exercise gap files.

    Returns ``None`` if no gap data is available.
    """
    if not self._gap_files or not self._rule_file_coverage:
        return None

    # Identify rules that exercise files with coverage gaps
    gap_rules: set[str] = set()
    for rule_name, files in self._rule_file_coverage.items():
        if files & self._gap_files:
            gap_rules.add(rule_name)

    if not gap_rules:
        return None

    # Include gap-relevant rules with probability 0.8 (boosted),
    # other rules with probability 0.3 (suppressed).
    active_rules: list[str] = []
    for r in self._rules:
        prob = 0.8 if r.name in gap_rules else 0.3
        if self.rng.random() < prob:
            active_rules.append(r.name)
    if not active_rules:
        # Ensure at least one gap-relevant rule
        active_rules = [self.rng.choice(list(gap_rules))]

    # Faults: standard coin flip
    active_faults = [f for f in all_fault_names if self.rng.random() < 0.5]

    cfg = SwarmConfig(active_rules=active_rules, active_faults=active_faults)
    key = cfg.key
    if key not in self._swarm_configs:
        self._swarm_configs[key] = cfg
    self._swarm_configs[key].times_used += 1
    return self._swarm_configs[key]
_coverage_directed_config.__qualname__ = "Explorer._coverage_directed_config"
Explorer._coverage_directed_config = _coverage_directed_config
del _coverage_directed_config
def _update_swarm_energy(self, new_edges: int) -> None:
    """Update energy for the current run's swarm config.

    Rewards the config that found new edges.  Previously-productive
    configs (``edges_found > 0``) decay slower than never-productive
    ones — a config that found 15 edges on run 5 is still likely
    to find more on run 50, even if it hasn't found any recently.
    """
    cfg = self._current_swarm_config
    if cfg is None:
        return
    if new_edges > 0:
        cfg.energy = min(cfg.energy * _SWARM_ENERGY_REWARD, 10.0)
        cfg.edges_found += new_edges
        cfg.runs_with_new_edges += 1
    elif cfg.edges_found > 0:
        # Previously productive — slow decay (keep exploring this config)
        cfg.energy = max(cfg.energy * 0.98, _SWARM_ENERGY_MIN)
    else:
        # Never productive — fast decay
        cfg.energy = max(cfg.energy * _SWARM_ENERGY_DECAY, _SWARM_ENERGY_MIN)
_update_swarm_energy.__qualname__ = "Explorer._update_swarm_energy"
Explorer._update_swarm_energy = _update_swarm_energy
del _update_swarm_energy
def _record_behavior_observations(
    self,
    result: ExplorationResult,
    machine: ChaosTest,
    property_events: list[dict[str, Any]],
    trace_steps: list[TraceStep],
) -> None:
    """Record rule/fault/property coverage for the most recent executed step."""
    property_names = sorted({event["name"] for event in property_events})
    if trace_steps:
        trace_steps[-1].properties_observed = property_names

    if self._last_step_rule is None:
        return

    rule_name = self._last_step_rule[0]
    fault_sig = _fault_signature([f.name for f in machine.active_faults])

    rule_fault = result.rule_fault_coverage.setdefault(rule_name, {})
    rule_fault[fault_sig] = rule_fault.get(fault_sig, 0) + 1

    if not property_names:
        return

    if self._current_swarm_config is not None:
        self._current_swarm_config.property_hits += len(property_names)
    self._current_run_properties.update(property_names)

    behavior = result.behavior_coverage.setdefault(rule_name, {})
    existing = set(behavior.get(fault_sig, []))
    existing.update(property_names)
    behavior[fault_sig] = sorted(existing)

    for prop in property_names:
        fault_hits = result.property_stress.setdefault(prop, {})
        fault_hits[fault_sig] = fault_hits.get(fault_sig, 0) + 1
_record_behavior_observations.__qualname__ = "Explorer._record_behavior_observations"
Explorer._record_behavior_observations = _record_behavior_observations
del _record_behavior_observations
def _execute_step(
    self,
    machine: ChaosTest,
    rule_log: list[str],
    trace_steps: list[TraceStep],
    ts_offset: float,
    new_edges_this_run: int,
    source_cp: Checkpoint | None = None,
) -> bool:
    """Execute one exploration step: either a fault toggle or a rule.

    When ``source_cp`` is provided, rule executions may use seed
    mutation (see ``_execute_rule``).  The executed rule name and
    params are stored in ``self._last_step_rule`` so that
    ``_process_coverage`` can record productive params on the
    checkpoint when new edges are found.

    Returns ``True`` if the step executed, ``False`` if it was
    skipped (strategy generation failed for required parameters).
    """
    self._last_step_rule = None
    self._last_step_used_mutation = False

    if machine._faults and self.rng.random() < self.fault_toggle_prob:
        toggle_name = self._toggle_fault(machine)
        rule_log.append(toggle_name)
        trace_steps.append(
            TraceStep(
                kind="fault_toggle",
                name=toggle_name,
                active_faults=[f.name for f in machine.active_faults],
                edge_count=len(self._total_edges) + new_edges_this_run,
                timestamp_offset=ts_offset,
            )
        )
        return True
    else:
        rule_info = self.rng.choice(self._active_rules)
        try:
            params = self._execute_rule(machine, rule_info, source_cp=source_cp)
        except Exception:
            # Record the failing rule with actual generated params
            # so replay reproduces the exact same call.
            rule_log.append(rule_info.name)
            failing_params = getattr(self, "_last_generated_params", {})
            trace_steps.append(
                TraceStep(
                    kind="rule",
                    name=rule_info.name,
                    params=failing_params,
                    edge_count=len(self._total_edges) + new_edges_this_run,
                    timestamp_offset=ts_offset,
                )
            )
            raise
        # Detect skipped rules: required params missing means strategy
        # generation failed. Don't log as a real step — prevents the
        # "spinning" problem where run counts inflate with no-op calls.
        required = sum(
            1 for p in rule_info.strategies if not isinstance(params.get(p), _DataProxy)
        )
        generated = sum(1 for k, v in params.items() if not isinstance(v, _DataProxy))
        if required > 0 and generated < required:
            return False  # skip — strategy generation failed
        rule_log.append(rule_info.name)

        # Store for seed feedback — _process_coverage may promote these
        # params onto the source checkpoint if they lead to new edges.
        serializable_params = {k: v for k, v in params.items() if not isinstance(v, _DataProxy)}
        self._last_step_rule = (rule_info.name, serializable_params)

        # active_faults omitted on rule steps (derivable from
        # fault_toggle sequence, saves ~70% of list comprehensions)
        trace_steps.append(
            TraceStep(
                kind="rule",
                name=rule_info.name,
                params=serializable_params,
                edge_count=len(self._total_edges) + new_edges_this_run,
                timestamp_offset=ts_offset,
            )
        )
    return True
_execute_step.__qualname__ = "Explorer._execute_step"
Explorer._execute_step = _execute_step
del _execute_step
def _process_coverage(
    self,
    machine: ChaosTest,
    collector: CoverageCollector | None,
    step: int,
    run_id: int,
    new_edges_this_run: int,
    result: ExplorationResult,
    use_coverage: bool,
    property_events: list[dict[str, Any]],
    source_cp: Checkpoint | None = None,
) -> int:
    """Check for new edges, states, and properties after a step.

    When new edges are found and the last step was a rule execution,
    the rule's parameters are recorded as a productive seed on the
    source checkpoint (if any).  This feeds the mutation loop: next
    time the explorer branches from this checkpoint, it may mutate
    these parameters instead of generating fresh ones.

    Returns the updated ``new_edges_this_run`` count.
    """
    # Edge coverage
    if collector:
        edges = collector.snapshot()
        new = edges - self._total_edges
        if new and self._shared_bitmap is not None:
            new = {e for e in new if not self._shared_bitmap[e]}
        if new:
            new_edges_this_run += len(new)
            self._total_edges |= new
            if self._shared_bitmap is not None:
                for e in new:
                    self._shared_bitmap[e] = 1
            self._save_checkpoint(machine, len(new), step, run_id)
            self._pool_publish(machine, len(new), step, run_id)
            result.checkpoints_saved += 1

            # Record productive params as seeds on the source checkpoint.
            # These become mutation targets when branching from this
            # checkpoint again — the AFL closed-loop for stateful testing.
            self._record_productive_seed(source_cp, result)

            # Track which files this rule exercises (for coverage-directed swarm)
            if self._last_step_rule is not None:
                rule_name = self._last_step_rule[0]
                hit_files = self._rule_file_coverage.get(rule_name)
                if hit_files is None:
                    hit_files = set()
                    self._rule_file_coverage[rule_name] = hit_files
                hit_files.update(collector.lines_hit.keys())

    # State-aware coverage (with global dedup via shared state bitmap)
    if hasattr(machine, "state_hash"):
        sh = machine.state_hash()
        if sh and sh not in self._total_states:
            # Global dedup: skip states another worker already explored
            sh16 = sh & 0xFFFF
            if self._shared_state_bitmap is not None and self._shared_state_bitmap[sh16]:
                pass  # another worker already found this state
            else:
                self._total_states.add(sh)
                if self._shared_state_bitmap is not None:
                    self._shared_state_bitmap[sh16] = 1
                new_edges_this_run += 1
                if use_coverage:
                    self._save_checkpoint(machine, 1, step, run_id)
                    self._pool_publish(machine, 1, step, run_id)
                    result.checkpoints_saved += 1

    # Property-guided search: only inspect properties changed by this step.
    for event in property_events:
        name = str(event["name"])
        if (
            event["type"] == "sometimes"
            and int(event["delta_passes"]) > 0
            and name not in self._satisfied_properties
        ):
            self._satisfied_properties.add(name)
            result.properties_satisfied += 1
            new_edges_this_run += 1
            if use_coverage:
                self._save_checkpoint(machine, 1, step, run_id)
                result.checkpoints_saved += 1

    return new_edges_this_run
_process_coverage.__qualname__ = "Explorer._process_coverage"
Explorer._process_coverage = _process_coverage
del _process_coverage
def _record_failure(
    self,
    e: Exception,
    run_id: int,
    step: int,
    trace_steps: list[TraceStep],
    rule_log: list[str],
    machine: ChaosTest,
    source_cp: Checkpoint | None,
    new_edges_this_run: int,
    run_start: float,
    class_name: str,
    result: ExplorationResult,
    _mutation_pairs: list[tuple],
) -> Trace:
    """Record a failure into the result and return the trace."""
    native_boundary = _extract_native_boundary(e)
    if native_boundary is not None and trace_steps:
        trace_steps[-1].native_boundary = native_boundary
    trace = Trace(
        run_id=run_id,
        seed=self.seed,
        test_class=class_name,
        from_checkpoint=source_cp.run_id if source_cp else None,
        steps=trace_steps,
        failure=TraceFailure(
            error_type=type(e).__name__,
            error_message=str(e)[:500],
            step=step,
            native_boundary=native_boundary,
        ),
        edges_discovered=new_edges_this_run,
        duration=_time.monotonic() - run_start,
    )
    # Check if any mutation faults are active (killed mutants)
    _active_names = {f.name for f in machine.active_faults}
    for _mutant, _mfault in _mutation_pairs:
        if _mfault.name in _active_names and not _mutant.killed:
            _mutant.killed = True
            _mutant.error = str(e)[:200]
            result.mutations_killed += 1

    result.failures.append(
        Failure(
            error=e,
            step=step,
            run_id=run_id,
            active_faults=[f.name for f in machine.active_faults],
            rule_log=rule_log,
            trace=trace,
            error_traceback=_format_exception_traceback(e),
            native_boundary=native_boundary,
        )
    )
    return trace
_record_failure.__qualname__ = "Explorer._record_failure"
Explorer._record_failure = _record_failure
del _record_failure
def _find_min_energy_idx(self) -> int:
    """Find the index of the lowest-energy checkpoint."""
    min_e = self._checkpoints[0].energy
    min_i = 0
    for i in range(1, len(self._checkpoints)):
        if self._checkpoints[i].energy < min_e:
            min_e = self._checkpoints[i].energy
            min_i = i
    return min_i
_find_min_energy_idx.__qualname__ = "Explorer._find_min_energy_idx"
