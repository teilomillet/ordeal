from __future__ import annotations
# ruff: noqa
def load_state(
    self,
    path: str | Path,
    *,
    allow_unsafe: bool = False,
) -> dict[str, Any]:
    """Load saved exploration state, restoring checkpoints and edges.

    Returns a dict of counters (``total_runs``, ``total_steps``, etc.)
    that the caller should seed into the ``ExplorationResult``.
    """
    if not allow_unsafe:
        raise ValueError(
            "Explorer state files use pickle and may execute arbitrary code. "
            "Pass allow_unsafe=True only for trusted files."
        )
    with open(path, "rb") as f:
        payload = pickle.load(f)

    self._total_edges = set(payload.get("total_edges", set()))
    self._total_states = set(payload.get("total_states", set()))
    self._satisfied_properties = set(payload.get("satisfied_properties", set()))

    rng_state = payload.get("rng_state")
    if rng_state is not None:
        self.rng.setstate(rng_state)

    # Reconstruct checkpoints
    self._checkpoints.clear()
    for cpd in payload.get("checkpoints", []):
        snap = _MachineSnapshot(
            state_dict=cpd["state_dict"],
            fault_active=cpd.get("fault_active", {}),
        )
        self._checkpoints.append(
            Checkpoint(
                snapshot=snap,
                new_edge_count=cpd.get("new_edge_count", 0),
                step=cpd.get("step", 0),
                run_id=cpd.get("run_id", 0),
                energy=cpd.get("energy", 1.0),
                times_selected=cpd.get("times_selected", 0),
            )
        )

    return {
        "total_edges": len(self._total_edges),
        "checkpoints": len(self._checkpoints),
    }
load_state.__qualname__ = "Explorer.load_state"
Explorer.load_state = load_state
del load_state
def _discover(self) -> None:
    """Introspect the test class for rules (including parameterized) and invariants."""
    self._rules.clear()
    self._invariant_names.clear()
    skip = {"_nemesis", "_swarm_init"}

    for name in dir(self.test_class):
        attr = getattr(self.test_class, name, None)
        if attr is None:
            continue

        # Rules — read strategy info from Hypothesis metadata
        rule_meta = getattr(attr, "hypothesis_stateful_rule", None)
        if rule_meta is not None and name not in skip:
            strategies: dict[str, Any] = {}
            has_data = False

            if hasattr(rule_meta, "arguments_strategies"):
                strategies = dict(rule_meta.arguments_strategies)
            elif hasattr(rule_meta, "arguments"):
                strategies = dict(rule_meta.arguments)

            # Detect Hypothesis's st.data() strategy (NOT user params named "data").
            # Previously: param_name == "data" matched user params like
            # @rule(data=st.binary()), silently replacing them with _DataProxy
            # and causing 96%+ false failure rate.
            for param_name, strat in strategies.items():
                strat_repr = repr(strat).lower()
                is_data = "dataobject" in strat_repr or "data()" in strat_repr
                if is_data:
                    has_data = True

            # Skip Bundle-consuming rules (can't execute outside Hypothesis)
            if hasattr(rule_meta, "bundles") and rule_meta.bundles:
                continue

            self._rules.append(
                _RuleInfo(
                    name=name,
                    strategies=strategies,
                    has_data=has_data,
                )
            )

        # Invariants
        if hasattr(attr, "hypothesis_stateful_invariant"):
            self._invariant_names.append(name)
_discover.__qualname__ = "Explorer._discover"
Explorer._discover = _discover
del _discover
def _execute_rule(
    self,
    machine: ChaosTest,
    rule: _RuleInfo,
    source_cp: Checkpoint | None = None,
) -> dict[str, Any]:
    """Execute a rule, drawing parameters from strategies or seed mutation.

    When ``source_cp`` is provided and has productive seeds for this
    rule, there is a ``seed_mutation_prob`` chance of mutating one of
    those seeds instead of generating fresh parameters.  This is the
    AFL closed-loop pattern: productive inputs are perturbed to find
    nearby coverage, while fresh generation maintains exploration
    diversity.

    The decision between mutation and fresh generation happens per
    rule execution, not per run — so a single run from a checkpoint
    may mix mutated and fresh parameters across different steps.

    Args:
        machine: The ChaosTest instance to execute the rule on.
        rule: The rule to execute (name, strategies, has_data).
        source_cp: The checkpoint this run branched from, if any.
            Used to look up productive seeds for mutation.

    Returns:
        The drawn or mutated parameters.  If a required strategy
        fails to generate, returns incomplete params (caller skips
        the rule).
    """
    params: dict[str, Any] = {}
    required_count = 0
    used_mutation = False

    # Seed mutation path: if we branched from a checkpoint with seeds
    # for this rule, sometimes mutate instead of generating fresh.
    if (
        source_cp is not None
        and source_cp.seed_params
        and self.seed_mutation_prob > 0
        and self.rng.random() < self.seed_mutation_prob
    ):
        # Filter seeds to those matching this rule
        matching = [(n, p) for n, p in source_cp.seed_params if n == rule.name]
        if matching:
            from ordeal.mutagen import mutate_inputs

            _, seed = self.rng.choice(matching)
            params = mutate_inputs(
                seed,
                self.rng,
                strategies=rule.strategies,
                respect_strategies=self.seed_mutation_respect_strategies,
            )
            used_mutation = True

    # Fresh generation path (default, or fallback if no seeds matched)
    if not used_mutation:
        for param_name, strategy in rule.strategies.items():
            # Only substitute _DataProxy for Hypothesis's st.data() strategy,
            # NOT for user parameters that happen to be named "data".
            strat_repr = repr(strategy).lower()
            is_hyp_data = "dataobject" in strat_repr or "data()" in strat_repr
            if rule.has_data and is_hyp_data:
                params[param_name] = _DataProxy()
            else:
                required_count += 1
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    try:
                        params[param_name] = strategy.example()
                    except Exception:
                        # Log strategy failure — helps diagnose state leakage
                        # between sequential Explorer runs
                        self._strategy_failures[param_name] = (
                            self._strategy_failures.get(param_name, 0) + 1
                        )

        # If any required strategy failed, skip the rule entirely.
        # This prevents spinning: calling rules with missing arguments
        # that return immediately and inflate run counts.
        generated = len(params) - (1 if "data" in params else 0)
        if required_count > 0 and generated < required_count:
            return params  # caller sees incomplete params

    self._last_step_used_mutation = used_mutation
    # Store params before calling the method so they're available
    # if the method raises — the except block in _execute_step
    # uses these to record the failing step with actual params.
    self._last_generated_params = {
        k: v for k, v in params.items() if not isinstance(v, _DataProxy)
    }

    try:
        getattr(machine, rule.name)(**params)
    except TypeError:
        # Fallback: call with no args (rule may have defaults)
        try:
            getattr(machine, rule.name)()
        except TypeError:
            pass  # rule genuinely can't execute — skip

    return params
_execute_rule.__qualname__ = "Explorer._execute_rule"
Explorer._execute_rule = _execute_rule
del _execute_rule
def _toggle_fault(self, machine: ChaosTest) -> str:
    """Toggle a random fault. Returns signed name like ``+name`` or ``-name``.

    When unified swarm is active, only faults in ``_active_fault_names``
    are eligible for toggling.
    """
    if self._active_fault_names is not None:
        eligible = [f for f in machine._faults if f.name in self._active_fault_names]
        if not eligible:
            eligible = machine._faults  # fallback
        fault = self.rng.choice(eligible)
    else:
        fault = self.rng.choice(machine._faults)
    if fault.active:
        fault.deactivate()
        return f"-{fault.name}"
    fault.activate()
    return f"+{fault.name}"
_toggle_fault.__qualname__ = "Explorer._toggle_fault"
Explorer._toggle_fault = _toggle_fault
del _toggle_fault
def _check_invariants(self, machine: ChaosTest) -> None:
    """Run all @invariant methods."""
    for name in self._invariant_names:
        getattr(machine, name)()
_check_invariants.__qualname__ = "Explorer._check_invariants"
Explorer._check_invariants = _check_invariants
del _check_invariants
def _select_checkpoint(self) -> Checkpoint:
    """Select a checkpoint using the configured strategy."""
    if self.checkpoint_strategy == "energy":
        return self._select_energy()
    elif self.checkpoint_strategy == "recent":
        return self._select_recent()
    return self.rng.choice(self._checkpoints)  # uniform
_select_checkpoint.__qualname__ = "Explorer._select_checkpoint"
Explorer._select_checkpoint = _select_checkpoint
del _select_checkpoint
def _select_energy(self) -> Checkpoint:
    """Energy-weighted selection with recency and exploration bonuses.

    Combines three signals to balance exploitation and exploration:
    - **Energy**: checkpoints that found new edges get higher weight
    - **Recency**: newer checkpoints (frontier) get a sqrt bonus
    - **Exploration**: over-selected checkpoints are penalized
    """
    weights = [
        cp.energy * (1 + i) ** 0.5 / (1 + cp.times_selected) ** 0.5
        for i, cp in enumerate(self._checkpoints)
    ]
    (cp,) = self.rng.choices(self._checkpoints, weights=weights, k=1)
    cp.times_selected += 1
    return cp
_select_energy.__qualname__ = "Explorer._select_energy"
Explorer._select_energy = _select_energy
del _select_energy
def _select_recent(self) -> Checkpoint:
    """Favor recently-created checkpoints."""
    n = len(self._checkpoints)
    weights = list(range(1, n + 1))
    return self.rng.choices(self._checkpoints, weights=weights, k=1)[0]
_select_recent.__qualname__ = "Explorer._select_recent"
Explorer._select_recent = _select_recent
del _select_recent
def _update_checkpoint_energy(self, cp: Checkpoint, new_edges: int) -> None:
    """Reward checkpoints that led to new discoveries, decay others.

    When the checkpoint came from the shared ring buffer, propagate
    the energy update back so all workers see it on their next poll.
    """
    if new_edges > 0:
        cp.energy += new_edges * _ENERGY_REWARD
    else:
        cp.energy = max(_ENERGY_MIN, cp.energy * _ENERGY_DECAY)
    # Propagate to ring buffer — other workers see the updated energy
    if cp._pool_slot >= 0 and self._pool_ring is not None:
        _ring_update_energy(self._pool_ring, cp._pool_slot, cp.energy)
_update_checkpoint_energy.__qualname__ = "Explorer._update_checkpoint_energy"
Explorer._update_checkpoint_energy = _update_checkpoint_energy
del _update_checkpoint_energy
def _checkpoint_start_probability(self) -> float:
    """Return the measured-cost-adjusted probability of restoring a checkpoint."""
    return min(max(0.0, self.checkpoint_prob), self._effective_checkpoint_prob)
_checkpoint_start_probability.__qualname__ = "Explorer._checkpoint_start_probability"
Explorer._checkpoint_start_probability = _checkpoint_start_probability
del _checkpoint_start_probability
def _record_checkpoint_feedback(
    self,
    restore_seconds: float,
    other_seconds: float,
    new_edges: int,
) -> None:
    """Downshift expensive checkpoint restores after a no-discovery plateau.

    A rolling window prevents one slow restore or one unlucky branch from
    changing the schedule. Productive checkpoints recover gradually toward
    the configured probability.
    """
    self._checkpoint_feedback.append(
        (max(0.0, restore_seconds), max(0.0, other_seconds), new_edges > 0)
    )
    if len(self._checkpoint_feedback) < _CHECKPOINT_FEEDBACK_MIN_SAMPLES:
        return

    restore_total = sum(item[0] for item in self._checkpoint_feedback)
    other_total = sum(item[1] for item in self._checkpoint_feedback)
    total = restore_total + other_total
    self._checkpoint_restore_share = restore_total / total if total else 0.0
    productive = any(item[2] for item in self._checkpoint_feedback)
    base_probability = max(0.0, self.checkpoint_prob)

    if not productive and self._checkpoint_restore_share >= _HIGH_RESTORE_SHARE:
        floor = base_probability * _MIN_ADAPTIVE_CHECKPOINT_PROB
        self._effective_checkpoint_prob = max(
            floor,
            self._effective_checkpoint_prob * 0.5,
        )
    elif productive and self._effective_checkpoint_prob < base_probability:
        self._effective_checkpoint_prob = min(
            base_probability,
            self._effective_checkpoint_prob + (base_probability - self._effective_checkpoint_prob) * 0.25,
        )
_record_checkpoint_feedback.__qualname__ = "Explorer._record_checkpoint_feedback"
Explorer._record_checkpoint_feedback = _record_checkpoint_feedback
del _record_checkpoint_feedback
def _record_productive_seed(self, source_cp: Checkpoint | None, result: ExplorationResult) -> None:
    """Record the last step's rule params as a productive seed on the checkpoint.

    Called when new edge coverage is found.  The params that produced
    that coverage become seeds for future mutation — closing the
    AFL-style feedback loop at the rule-parameter level.

    If the productive step used seed mutation (rather than fresh
    generation), increments ``result.seed_mutations_productive`` —
    this tracks the mutation hit rate for diagnostics.

    Seeds are bounded by ``_MAX_SEEDS_PER_CHECKPOINT``.  When full,
    the oldest seed is evicted (FIFO), favoring recent discoveries
    over stale ones.

    Only records if:
    - The last step was a rule execution (not a fault toggle)
    - A source checkpoint exists to store the seed on
    - The params are non-empty (no-arg rules produce nothing useful)
    """
    if source_cp is None or self._last_step_rule is None:
        return
    rule_name, params = self._last_step_rule
    if not params:
        return
    # Track productive mutations for diagnostics
    if self._last_step_used_mutation:
        result.seed_mutations_productive += 1
    # Bound the corpus — evict oldest when full (FIFO)
    if len(source_cp.seed_params) >= _MAX_SEEDS_PER_CHECKPOINT:
        source_cp.seed_params.pop(0)
    source_cp.seed_params.append((rule_name, params))
_record_productive_seed.__qualname__ = "Explorer._record_productive_seed"
Explorer._record_productive_seed = _record_productive_seed
del _record_productive_seed
def _pool_publish(self, machine: ChaosTest, new_count: int, step: int, run_id: int) -> None:
    """Publish a checkpoint to the shared-memory ring buffer.

    Each worker owns a contiguous slice of slots and writes to them
    in round-robin order.  The ring buffer uses per-worker monotonic
    sequence numbers so readers can detect new writes without locks.
    """
    if self._pool_ring is None:
        return
    try:
        snap = self._snapshot_machine(machine)
        picklable_state: dict[str, Any] = {}
        for k, v in snap.state_dict.items():
            try:
                pickle.dumps(v)
                picklable_state[k] = v
            except Exception:
                pass
        payload = {"state_dict": picklable_state, "fault_active": snap.fault_active}
        data = _pool_encode_payload(payload, self._pool_auth_key)
        if len(data) > _POOL_SLOT_DATA_MAX:
            return  # checkpoint too large for a slot — skip

        # Claim our next slot (round-robin within our owned range)
        base_slot = self._worker_id * self._pool_slots_per_worker
        slot = base_slot + (self._pool_next_slot % self._pool_slots_per_worker)
        self._pool_next_slot += 1
        self._pool_write_seq += 1

        energy = 1.0 + new_count * _ENERGY_REWARD
        _ring_write(
            self._pool_ring,
            slot,
            self._pool_write_seq,
            self._worker_id,
            energy,
            data,
            new_count,
            step,
        )
    except Exception:
        pass
_pool_publish.__qualname__ = "Explorer._pool_publish"
Explorer._pool_publish = _pool_publish
del _pool_publish
def _pool_subscribe(self) -> None:
    """Import new checkpoints from other workers via the ring buffer.

    Scans all slots outside our owned range.  Skips slots already
    seen (via per-slot sequence tracking) and torn reads (CRC32
    mismatch).  Imported checkpoints carry the energy from the
    ring buffer, reflecting global energy propagation.
    """
    if self._pool_ring is None:
        return
    now = _time.monotonic()
    if now - self._pool_last_sync < self._pool_sync_interval:
        return
    self._pool_last_sync = now
    try:
        my_base = self._worker_id * self._pool_slots_per_worker
        my_end = my_base + self._pool_slots_per_worker
        for slot in range(_POOL_NUM_SLOTS):
            if my_base <= slot < my_end:
                continue  # skip our own slots
            entry = _ring_read(self._pool_ring, slot)
            if entry is None:
                continue
            seq = entry["sequence"]
            if seq <= self._pool_seen_seq.get(slot, 0):
                continue  # already imported
            self._pool_seen_seq[slot] = seq
            try:
                payload = _pool_decode_payload(entry["data"], self._pool_auth_key)
                snap = _MachineSnapshot(
                    state_dict=payload.get("state_dict", payload),
                    fault_active=payload.get("fault_active", {}),
                )
                self._checkpoints.append(
                    Checkpoint(
                        snapshot=snap,
                        new_edge_count=entry["new_edge_count"],
                        step=entry["step"],
                        run_id=-1,
                        energy=entry["energy"],
                        _pool_slot=slot,
                    )
                )
            except Exception:
                continue
    except Exception:
        pass
_pool_subscribe.__qualname__ = "Explorer._pool_subscribe"
Explorer._pool_subscribe = _pool_subscribe
del _pool_subscribe
def _register_swarm_run(self, active_faults: list[str]) -> None:
    """Track which fault pairs were exercised by one swarm configuration."""
    uniq = sorted(dict.fromkeys(active_faults))
    for pair in combinations(uniq, 2):
        self._fault_pair_hits[pair] += 1
_register_swarm_run.__qualname__ = "Explorer._register_swarm_run"
Explorer._register_swarm_run = _register_swarm_run
del _register_swarm_run
def _uncovered_fault_pairs(self, all_fault_names: list[str]) -> list[tuple[str, str]]:
    """Return fault pairs never exercised by the current run history."""
    uniq = sorted(dict.fromkeys(all_fault_names))
    missing = [pair for pair in combinations(uniq, 2) if self._fault_pair_hits.get(pair, 0) <= 0]
    return missing
_uncovered_fault_pairs.__qualname__ = "Explorer._uncovered_fault_pairs"
Explorer._uncovered_fault_pairs = _uncovered_fault_pairs
del _uncovered_fault_pairs
