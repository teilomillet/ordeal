from __future__ import annotations
# ruff: noqa
def _save_checkpoint(self, machine: ChaosTest, new_count: int, step: int, run_id: int) -> None:
    """Save a checkpoint with the productive seed that led here.

    When a rule execution triggers new edge coverage, the checkpoint
    is created with that rule's params as its initial seed.  This
    means the very first branch from this checkpoint can already
    mutate — no warm-up period needed.

    Evicts lowest-energy checkpoint if at capacity.
    """
    if self.max_checkpoints <= 0:
        return
    if len(self._checkpoints) >= self.max_checkpoints:
        if self.checkpoint_strategy == "energy":
            self._checkpoints.pop(self._find_min_energy_idx())
        else:
            idx = self.rng.randint(0, max(0, len(self._checkpoints) - 2))
            self._checkpoints.pop(idx)

    # Seed the new checkpoint with the rule params that led to its creation.
    initial_seeds: list[tuple[str, dict[str, Any]]] = []
    if self._last_step_rule is not None:
        rule_name, params = self._last_step_rule
        if params:
            initial_seeds.append((rule_name, params))

    self._checkpoints.append(
        Checkpoint(
            snapshot=self._snapshot_machine(machine),
            new_edge_count=new_count,
            step=step,
            run_id=run_id,
            seed_params=initial_seeds,
        )
    )
_save_checkpoint.__qualname__ = "Explorer._save_checkpoint"
Explorer._save_checkpoint = _save_checkpoint
del _save_checkpoint
def _corpus_class_dir(self) -> Path | None:
    """Return the seed directory for this test class, or None if disabled."""
    if self.corpus_dir is None:
        return None
    safe_name = _qualified_name(self.test_class).replace(":", "_").replace(".", "_")
    return self.corpus_dir / safe_name
_corpus_class_dir.__qualname__ = "Explorer._corpus_class_dir"
Explorer._corpus_class_dir = _corpus_class_dir
del _corpus_class_dir
def _save_seed(self, trace: Trace) -> Path | None:
    """Save a failing trace to the seed corpus.  Returns path or None if dedup."""
    d = self._corpus_class_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    name = f"seed-{trace.content_hash()}.json"
    p = d / name
    if p.exists():
        return None  # already saved (dedup)
    trace.save(p)
    return p
_save_seed.__qualname__ = "Explorer._save_seed"
Explorer._save_seed = _save_seed
del _save_seed
def _replay_seeds(self) -> list[dict[str, Any]]:
    """Load and replay all seeds for this test class.  Returns replay results."""
    from ordeal.trace import replay as _replay

    d = self._corpus_class_dir()
    if d is None or not d.exists():
        return []
    results: list[dict[str, Any]] = []
    for p in sorted(d.glob("seed-*.json")):
        try:
            trace = Trace.load(p)
        except Exception:
            continue  # skip corrupt / incompatible seeds
        error = _replay(trace, self.test_class)
        results.append(
            {
                "path": str(p),
                "seed_name": p.stem,
                "reproduced": error is not None,
                "error": f"{type(error).__name__}: {error}" if error else None,
                "test_class": trace.test_class,
                "run_id": trace.run_id,
                "steps": len(trace.steps),
            }
        )
    return results
_replay_seeds.__qualname__ = "Explorer._replay_seeds"
Explorer._replay_seeds = _replay_seeds
del _replay_seeds
def run(
    self,
    *,
    max_time: float = 60.0,
    max_runs: int | None = None,
    steps_per_run: int = 50,
    shrink: bool = True,
    max_shrink_time: float = 30.0,
    patience: int = 0,
    progress: Callable[[ProgressSnapshot], None] | None = None,
    resume_from: str | Path | None = None,
    save_state_to: str | Path | None = None,
    allow_unsafe_resume: bool = False,
) -> ExplorationResult:
    """Run the coverage-guided exploration loop.

    Args:
        max_time: Wall-clock time limit in seconds.
        max_runs: Maximum number of runs (or ``None`` for time-only).
        steps_per_run: Max rule steps per run.
        shrink: If True, shrink failing traces after exploration.
        max_shrink_time: Time limit for shrinking each failure.
        patience: Stop after N consecutive runs without new edges. 0=disabled.
        progress: Optional callback for live progress updates.
        resume_from: Path to a saved state file from a previous run.
            Restores checkpoints, edges, and RNG state so exploration
            continues where it left off.
        save_state_to: Path to save exploration state on completion
            (and on interrupt).  Use with ``resume_from`` on the next
            run to continue exploration across sessions.
        allow_unsafe_resume: Permit loading trusted pickle state files
            passed via ``resume_from``. Disabled by default because
            pickle deserialization can execute arbitrary code.
    """
    if self.workers > 1:
        return self._run_parallel(
            max_time=max_time,
            max_runs=max_runs,
            steps_per_run=steps_per_run,
            shrink=shrink,
            max_shrink_time=max_shrink_time,
            patience=patience,
            progress=progress,
        )

    # Reset Hypothesis internal state to prevent leakage from previous Explorer runs.
    # When the CLI runs multiple ChaosTest classes sequentially, Hypothesis's
    # strategy caches and ConjectureData machinery can leak between instances,
    # causing strategy.example() to fail silently for subsequent classes.
    try:
        from hypothesis import settings
        from hypothesis.database import InMemoryExampleDatabase

        settings.default.database = InMemoryExampleDatabase()
    except Exception:
        pass

    self._strategy_failures.clear()
    self._discover()

    # Activate property tracker for property-guided search
    from ordeal import assertions as _assertions
    from ordeal.chaos import rule_timeout_context as _rule_timeout_context

    _tracker_snapshot = _assertions.tracker.snapshot()
    _assertions.tracker.reset()
    _assertions.tracker.active = True
    _original_test_class = self.test_class
    _orig_fault_prob = self.fault_toggle_prob
    _orig_cp_strategy = self.checkpoint_strategy
    _timeout_context = _rule_timeout_context()
    _timeout_context.__enter__()
    try:
        if not self._rules:
            raise ValueError(f"No callable rules found on {self.test_class.__name__}")

        result = ExplorationResult()
        result.ngram = self.ngram

        # Replay seed corpus before exploration
        result.seed_replays = self._replay_seeds()
        _assertions.tracker.reset()

        # Resume from saved state if provided
        if resume_from is not None:
            restored = self.load_state(
                resume_from,
                allow_unsafe=allow_unsafe_resume,
            )
            result.unique_edges = restored["total_edges"]
            result.checkpoints_saved = restored["checkpoints"]

        use_coverage = bool(self.target_paths)
        _lines_hit_all: dict[str, set[int]] = {}
        start = _time.monotonic()
        class_name = _qualified_name(self.test_class)

        # Generate mutation faults from target functions
        _mutation_pairs: list[tuple] = []
        if self.mutation_targets:
            from ordeal.mutations import mutation_faults as _gen_mutation_faults

            for mt in self.mutation_targets:
                try:
                    _mutation_pairs.extend(_gen_mutation_faults(mt))
                except Exception:
                    pass
            if _mutation_pairs:
                _mfaults = [f for _, f in _mutation_pairs]

                class _MutatedTest(self.test_class):
                    faults = list(self.test_class.faults) + _mfaults

                self.test_class = _MutatedTest
                result.mutations_total = len(_mutation_pairs)

        _runs_since_new: int = 0
        _adapt_phase: int = 0

        while True:
            elapsed = _time.monotonic() - start
            if elapsed >= max_time:
                result.stopped_reason = "time"
                break
            if max_runs is not None and result.total_runs >= max_runs:
                result.stopped_reason = "max_runs"
                break
            if patience > 0 and _runs_since_new >= patience and use_coverage:
                if _adapt_phase < 3:
                    # Escalate: go deeper before giving up
                    _adapt_phase += 1
                    _runs_since_new = 0
                    steps_per_run = min(steps_per_run * 2, 500)
                    self.fault_toggle_prob = min(0.5, self.fault_toggle_prob + 0.1)
                    if _adapt_phase == 2:
                        self.checkpoint_strategy = "uniform"
                    result.adaptation_phase = _adapt_phase
                else:
                    result.saturated = True
                    result.stopped_reason = "saturated"
                    break

            # Pull checkpoints from other workers
            self._pool_subscribe()

            result.total_runs += 1
            run_id = result.total_runs
            rule_log: list[str] = []
            trace_steps: list[TraceStep] = []
            run_start = _time.monotonic()
            source_cp: Checkpoint | None = None

            # -- Start: fresh or from checkpoint --
            from_cp = self._checkpoints and self.rng.random() < self._checkpoint_start_probability()
            restore_seconds = 0.0
            if from_cp:
                source_cp = self._select_checkpoint()
                restore_started = _time.monotonic()
                machine = self._restore_machine(source_cp.snapshot)
                restore_seconds = _time.monotonic() - restore_started
                rule_log.append(f"[checkpoint r{source_cp.run_id}s{source_cp.step}]")
            else:
                machine = self.test_class()

            # Unified swarm: joint rule+fault configuration per run.
            self._active_fault_names = None  # reset — means "all faults"
            self._current_swarm_config = None
            self._current_run_properties = set()
            if self.rule_swarm:
                swarm_cfg = self._select_swarm_config(machine, result.total_runs)
                if swarm_cfg is not None:
                    self._current_swarm_config = swarm_cfg
                    self._active_rules = [
                        r for r in self._rules if r.name in swarm_cfg.active_rules
                    ]
                    self._active_fault_names = swarm_cfg.active_faults
                    result.rule_swarm_runs += 1
                    trace_steps.append(
                        TraceStep(
                            kind="rule_swarm",
                            name=(
                                f"[swarm rules={len(swarm_cfg.active_rules)}"
                                f" faults={len(swarm_cfg.active_faults)}]"
                            ),
                            params={
                                "active_rules": swarm_cfg.active_rules,
                                "active_faults": swarm_cfg.active_faults,
                            },
                        )
                    )
                    self._register_swarm_run(swarm_cfg.active_faults)
                else:
                    self._active_rules = self._rules
            else:
                self._active_rules = self._rules

            n_steps = self.rng.randint(1, steps_per_run)
            collector = (
                CoverageCollector(self.target_paths, ngram=self.ngram) if use_coverage else None
            )
            if collector:
                collector.start()

            step = 0
            new_edges_this_run = 0
            try:
                for step in range(n_steps):
                    result.total_steps += 1
                    ts_offset = _time.monotonic() - run_start
                    tracker_before = _assertions.tracker.counter_snapshot()

                    executed = self._execute_step(
                        machine,
                        rule_log,
                        trace_steps,
                        ts_offset,
                        new_edges_this_run,
                        source_cp=source_cp,
                    )
                    if not executed:
                        result.skipped_steps += 1
                        continue
                    if self._last_step_used_mutation:
                        result.seed_mutations_used += 1
                    self._check_invariants(machine)
                    property_events = _assertions.tracker.counter_delta(tracker_before)
                    self._record_behavior_observations(
                        result,
                        machine,
                        property_events,
                        trace_steps,
                    )
                    new_edges_this_run = self._process_coverage(
                        machine,
                        collector,
                        step,
                        run_id,
                        new_edges_this_run,
                        result,
                        use_coverage,
                        property_events,
                        source_cp=source_cp,
                    )

            except Exception as e:
                trace = self._record_failure(
                    e,
                    run_id,
                    step,
                    trace_steps,
                    rule_log,
                    machine,
                    source_cp,
                    new_edges_this_run,
                    run_start,
                    class_name,
                    result,
                    _mutation_pairs,
                )
                if self._current_swarm_config is not None:
                    self._current_swarm_config.failure_count += 1
                if self.record_traces:
                    result.traces.append(trace)

            else:
                if self.record_traces:
                    result.traces.append(
                        Trace(
                            run_id=run_id,
                            seed=self.seed,
                            test_class=class_name,
                            from_checkpoint=source_cp.run_id if source_cp else None,
                            steps=trace_steps,
                            edges_discovered=new_edges_this_run,
                            duration=_time.monotonic() - run_start,
                        )
                    )
            finally:
                if collector:
                    collector.stop()
                    # Accumulate line-level coverage across runs
                    for fn, lines in collector.lines_hit.items():
                        existing = _lines_hit_all.get(fn)
                        if existing is None:
                            _lines_hit_all[fn] = set(lines)
                        else:
                            existing.update(lines)
                machine.teardown()

            # Update checkpoint energy
            if source_cp is not None:
                self._update_checkpoint_energy(source_cp, new_edges_this_run)
                self._record_checkpoint_feedback(
                    restore_seconds,
                    _time.monotonic() - run_start - restore_seconds,
                    new_edges_this_run,
                )

            result.edge_log.append((run_id, len(self._total_edges)))

            # Saturation tracking
            if new_edges_this_run > 0:
                _runs_since_new = 0
                result.last_new_edge_run = run_id
            else:
                _runs_since_new += 1
            result.runs_since_new_edge = _runs_since_new

            # Update swarm config energy + coverage-directed gap files
            if self.rule_swarm:
                self._update_swarm_energy(new_edges_this_run)
                if (
                    use_coverage
                    and self.target_modules
                    and result.total_runs % 50 == 0
                    and _lines_hit_all
                ):
                    gaps, _, _ = _compute_coverage_gaps(
                        _lines_hit_all, self.target_modules, result.total_runs
                    )
                    self._gap_files = {g["file"] for g in gaps}

            # Progress callback
            if progress:
                elapsed_now = _time.monotonic() - start
                progress(
                    ProgressSnapshot(
                        elapsed=elapsed_now,
                        total_runs=result.total_runs,
                        total_steps=result.total_steps,
                        unique_edges=len(self._total_edges),
                        checkpoints=len(self._checkpoints),
                        failures=len(result.failures),
                        runs_per_second=result.total_runs / max(elapsed_now, 0.001),
                    )
                )

        result.unique_states = len(self._total_states)
        result.strategy_failures = dict(self._strategy_failures)

        # -- Post-exploration: shrink failures --
        if shrink:
            _assertions.tracker.reset()
            for failure in result.failures:
                if failure.trace and failure.trace.steps:
                    failure.trace = _shrink_trace(
                        failure.trace,
                        self.test_class,
                        max_time=max_shrink_time,
                    )

        # -- Post-exploration: fault ablation --
        if shrink:
            from ordeal.trace import ablate_faults as _ablate

            _assertions.tracker.reset()
            for failure in result.failures:
                if failure.trace and failure.trace.steps:
                    failure.necessary_faults = _ablate(failure.trace, self.test_class)

        # -- Post-exploration: save failing traces to seed corpus --
        for failure in result.failures:
            if failure.trace:
                self._save_seed(failure.trace)
        result.native_boundary_findings = [
            dict(failure.native_boundary)
            for failure in result.failures
            if failure.native_boundary is not None
        ]

        result.unique_edges = len(self._total_edges)
        result.duration_seconds = _time.monotonic() - start
        if self.rule_swarm:
            all_fault_names = [f.name for f in getattr(_original_test_class, "faults", [])]
            result.swarm_stats = self._swarm_stats()
            result.fault_pair_coverage = self._fault_pair_rows(all_fault_names)
            result.uncovered_fault_pairs = [
                list(pair) for pair in self._uncovered_fault_pairs(all_fault_names)
            ]

        # -- Post-exploration: coverage gap analysis --
        if use_coverage and _lines_hit_all and self.target_modules:
            gaps, covered, total = _compute_coverage_gaps(
                _lines_hit_all, self.target_modules, result.total_runs
            )
            result.coverage_gaps = gaps
            result.lines_covered = covered
            result.lines_total = total

        # Save state for future resumption
        if save_state_to is not None:
            self.save_state(save_state_to)

        return result
    finally:
        _timeout_context.__exit__(None, None, None)
        self.test_class = _original_test_class
        _assertions.tracker.restore(_tracker_snapshot)
        self.fault_toggle_prob = _orig_fault_prob
        self.checkpoint_strategy = _orig_cp_strategy
        try:
            from hypothesis import settings
            from hypothesis.database import InMemoryExampleDatabase

            settings.default.database = InMemoryExampleDatabase()
        except Exception:
            pass
