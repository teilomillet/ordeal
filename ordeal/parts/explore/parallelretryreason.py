from __future__ import annotations
# ruff: noqa
# ruff: noqa: F401, F821

def _parallel_retry_reason(
    self,
    worker_results: list[dict[str, Any]],
    result: ExplorationResult,
) -> str | None:
    """Detect suspicious parallel outcomes that should be reported explicitly."""
    issues: list[str] = []
    worker_count = max(1, len(worker_results))
    worker_errors = [wr["worker_error"] for wr in worker_results if wr.get("worker_error")]
    all_failures = list(worker_errors)
    for wr in worker_results:
        all_failures.extend(wr.get("failures", []))

    if worker_errors:
        issues.append(f"{len(worker_errors)} worker bootstrap failure(s)")

    if self.target_paths and result.total_runs > 0 and result.unique_edges == 0:
        issues.append("0 edges discovered")

    elif all_failures:
        step_zero_failures = sum(1 for f in all_failures if int(f.get("step", 0)) <= 0)
        if step_zero_failures >= max(3, worker_count):
            issues.append(f"{step_zero_failures} step-0 failure(s)")

        spam_count = Counter(_parallel_failure_signature(f) for f in all_failures).most_common(1)[
            0
        ][1]
        if spam_count >= max(3, worker_count):
            issues.append(f"{spam_count} identical crash(es)")

    if not issues:
        return None
    return ", ".join(issues)
_parallel_retry_reason.__qualname__ = "Explorer._parallel_retry_reason"
Explorer._parallel_retry_reason = _parallel_retry_reason
del _parallel_retry_reason
def _rerun_sequential_after_parallel(
    self,
    *,
    reason: str,
    max_time: float,
    max_runs: int | None,
    steps_per_run: int,
    shrink: bool,
    max_shrink_time: float,
    patience: int,
    progress: Callable[[ProgressSnapshot], None] | None,
) -> ExplorationResult:
    """Run the same exploration config with workers=1 after a suspicious parallel result."""
    explorer = Explorer(
        self.test_class,
        target_modules=self.target_modules,
        seed=self.seed,
        max_checkpoints=self.max_checkpoints,
        checkpoint_prob=self.checkpoint_prob,
        checkpoint_strategy=self.checkpoint_strategy,
        fault_toggle_prob=self.fault_toggle_prob,
        record_traces=self.record_traces,
        workers=1,
        share_edges=False,
        share_checkpoints=False,
        mutation_targets=list(self.mutation_targets),
        seed_mutation_prob=self.seed_mutation_prob,
        seed_mutation_respect_strategies=self.seed_mutation_respect_strategies,
        ngram=self.ngram,
        corpus_dir=self.corpus_dir,
        rule_swarm=self.rule_swarm,
    )
    result = explorer.run(
        max_time=max_time,
        max_runs=max_runs,
        steps_per_run=steps_per_run,
        shrink=shrink,
        max_shrink_time=max_shrink_time,
        patience=patience,
        progress=None,
    )
    result.coordination_mode = "sequential"
    result.parallel_fallback_reason = reason
    return result
_rerun_sequential_after_parallel.__qualname__ = "Explorer._rerun_sequential_after_parallel"
Explorer._rerun_sequential_after_parallel = _rerun_sequential_after_parallel
del _rerun_sequential_after_parallel
def _run_parallel(
    self,
    *,
    max_time: float,
    max_runs: int | None,
    steps_per_run: int,
    shrink: bool,
    max_shrink_time: float,
    patience: int,
    progress: Callable[[ProgressSnapshot], None] | None,
) -> ExplorationResult:
    """Run exploration across multiple worker processes.

    Each worker gets a unique seed (base + i*7919) for independent
    state-space exploration.  When ``share_edges`` is True, workers
    communicate via a shared-memory edge bitmap (AFL-style): one
    byte per 16-bit edge hash, single-byte atomic writes, zero locks.

    Results are aggregated: runs/steps summed, edges unioned.
    """
    from multiprocessing.shared_memory import SharedMemory

    start = _time.monotonic()
    class_path = f"{self.test_class.__module__}.{self.test_class.__qualname__}"
    worker_count = self.workers
    if max_runs is not None:
        worker_count = max(1, min(worker_count, max_runs))
    if worker_count <= 1:
        result = self._rerun_sequential_after_parallel(
            reason="",
            max_time=max_time,
            max_runs=max_runs,
            steps_per_run=steps_per_run,
            shrink=shrink,
            max_shrink_time=max_shrink_time,
            patience=patience,
            progress=progress,
        )
        result.parallel_fallback_reason = ""
        return result

    # Create shared coordination regions progressively. Edge/state sharing is
    # one tier because both regions participate in cross-worker deduplication;
    # checkpoint sharing is a deeper tier that can be dropped independently.
    shm: SharedMemory | None = None
    shm_name: str | None = None
    state_shm: SharedMemory | None = None
    state_shm_name: str | None = None
    ring_shm: SharedMemory | None = None
    ring_shm_name: str | None = None
    pool_auth_key: bytes | None = None
    coordination_degraded_reasons: list[str] = []

    def release_shared_memory(*regions: SharedMemory | None) -> None:
        """Best-effort parent cleanup for fully or partially allocated regions."""
        for region in regions:
            if region is None:
                continue
            try:
                region.close()
            except (BufferError, OSError):
                pass
            try:
                region.unlink()
            except (FileNotFoundError, OSError):
                pass

    if self.share_edges:
        try:
            shm = SharedMemory(create=True, size=_EDGE_BITMAP_SIZE)
            shm.buf[:] = b"\x00" * _EDGE_BITMAP_SIZE
            shm_name = shm.name
            state_shm = SharedMemory(create=True, size=_STATE_BITMAP_SIZE)
            state_shm.buf[:] = b"\x00" * _STATE_BITMAP_SIZE
            state_shm_name = state_shm.name
        except (BufferError, OSError) as exc:
            release_shared_memory(state_shm, shm)
            shm = None
            shm_name = None
            state_shm = None
            state_shm_name = None
            coordination_degraded_reasons.append(
                f"shared edge/state memory unavailable: {type(exc).__name__}: {exc}"
            )

    # The normal fallback order is full sharing -> shared edges only ->
    # independent workers. A checkpoint-only mode remains available when the
    # caller explicitly disables edge sharing.
    checkpoint_tier_available = not self.share_edges or shm_name is not None
    if self.share_checkpoints and checkpoint_tier_available:
        try:
            ring_shm = SharedMemory(create=True, size=_POOL_RING_SIZE)
            ring_shm_name = ring_shm.name
            pool_auth_key = secrets.token_bytes(_POOL_AUTH_TAG_SIZE)
        except (BufferError, OSError) as exc:
            release_shared_memory(ring_shm)
            ring_shm = None
            ring_shm_name = None
            pool_auth_key = None
            coordination_degraded_reasons.append(
                f"shared checkpoint memory unavailable: {type(exc).__name__}: {exc}"
            )

    if shm_name is not None and ring_shm_name is not None:
        coordination_mode = "shared_edges_and_checkpoints"
    elif shm_name is not None:
        coordination_mode = "shared_edges"
    elif ring_shm_name is not None:
        coordination_mode = "shared_checkpoints"
    else:
        coordination_mode = "independent_workers"

    slots_per_worker = max(1, _POOL_NUM_SLOTS // max(worker_count, 1))

    try:
        worker_args = []
        base_runs = (max_runs // worker_count) if max_runs is not None else None
        extra_runs = (max_runs % worker_count) if max_runs is not None else 0
        for i in range(worker_count):
            worker_max_runs = None
            if max_runs is not None:
                worker_max_runs = base_runs + (1 if i < extra_runs else 0)
            worker_args.append(
                {
                    "class_path": class_path,
                    "target_modules": self.target_modules,
                    "seed": self.seed + i * 7919,
                    "max_time": max_time,
                    "max_runs": worker_max_runs,
                    "steps_per_run": steps_per_run,
                    "max_checkpoints": self.max_checkpoints,
                    "checkpoint_prob": self.checkpoint_prob,
                    "checkpoint_strategy": self.checkpoint_strategy,
                    "fault_toggle_prob": self.fault_toggle_prob,
                    "record_traces": self.record_traces,
                    "mutation_targets": list(self.mutation_targets),
                    "seed_mutation_prob": self.seed_mutation_prob,
                    "seed_mutation_respect_strategies": (self.seed_mutation_respect_strategies),
                    "shrink": shrink,
                    "max_shrink_time": max_shrink_time,
                    "patience": patience,
                    "corpus_dir": (str(self.corpus_dir) if self.corpus_dir is not None else None),
                    "rule_swarm": self.rule_swarm,
                    "shared_edges_name": shm_name,
                    "shared_state_name": state_shm_name,
                    "ring_shm_name": ring_shm_name,
                    "pool_auth_key": (pool_auth_key.hex() if pool_auth_key is not None else None),
                    "worker_id": i,
                    "num_workers": worker_count,
                    "slots_per_worker": slots_per_worker,
                    "ngram": self.ngram,
                }
            )

        ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
        pool = None
        parent_timeout = max_time + (max_shrink_time if shrink else 0.0) + max(
            30.0, steps_per_run * 0.1
        )
        try:
            pool = ctx.Pool(worker_count)
            if hasattr(pool, "map_async"):
                pending = pool.map_async(_worker_fn, worker_args)
                worker_results = pending.get(timeout=parent_timeout)
            else:  # pragma: no cover - compatibility for simple pool test doubles
                worker_results = pool.map(_worker_fn, worker_args)
            if hasattr(pool, "close"):
                pool.close()
            if hasattr(pool, "join"):
                pool.join()
        except mp.TimeoutError:
            if pool is not None:
                pool.terminate()
                pool.join()
            return self._rerun_sequential_after_parallel(
                reason=f"worker pool exceeded parent timeout ({parent_timeout:.1f}s)",
                max_time=max_time,
                max_runs=max_runs,
                steps_per_run=steps_per_run,
                shrink=shrink,
                max_shrink_time=max_shrink_time,
                patience=patience,
                progress=progress,
            )
        except Exception as exc:
            if pool is not None:
                try:
                    pool.terminate()
                    pool.join()
                except (OSError, RuntimeError):
                    pass
            return self._rerun_sequential_after_parallel(
                reason=f"worker pool unavailable: {type(exc).__name__}: {exc}",
                max_time=max_time,
                max_runs=max_runs,
                steps_per_run=steps_per_run,
                shrink=shrink,
                max_shrink_time=max_shrink_time,
                patience=patience,
                progress=progress,
            )

        # Aggregate results
        result = ExplorationResult(
            coordination_mode=coordination_mode,
            coordination_degraded_reason="; ".join(coordination_degraded_reasons),
        )
        result.ngram = self.ngram
        all_edges: set[int] = set()
        seen_failures: set[tuple[Any, ...]] = set()
        merged_swarm: dict[tuple[tuple[str, ...], tuple[str, ...]], dict[str, Any]] = {}
        merged_rule_fault: dict[str, dict[str, int]] = {}
        merged_behavior: dict[str, dict[str, set[str]]] = {}
        merged_property_stress: dict[str, dict[str, int]] = {}
        merged_pair_hits: Counter[tuple[str, str]] = Counter()

        for wr in worker_results:
            result.total_runs += wr["total_runs"]
            result.total_steps += wr["total_steps"]
            result.skipped_steps += wr.get("skipped_steps", 0)
            result.checkpoints_saved += wr["checkpoints_saved"]
            result.edge_log.extend(wr["edge_log"])
            all_edges.update(wr["edges"])
            result.unique_states += wr.get("unique_states", 0)
            result.properties_satisfied += wr.get("properties_satisfied", 0)
            result.seed_mutations_used += wr.get("seed_mutations_used", 0)
            result.seed_mutations_productive += wr.get("seed_mutations_productive", 0)
            result.rule_swarm_runs += wr.get("rule_swarm_runs", 0)
            result.lines_covered += wr.get("lines_covered", 0)
            result.lines_total += wr.get("lines_total", 0)
            result.coverage_gaps.extend(wr.get("coverage_gaps", []))
            if not result.seed_replays:
                result.seed_replays = list(wr.get("seed_replays", []))
            for name, count in wr.get("strategy_failures", {}).items():
                result.strategy_failures[name] = result.strategy_failures.get(name, 0) + int(count)
            if self.record_traces:
                result.traces.extend(
                    Trace.from_dict(trace_payload) for trace_payload in wr.get("traces", [])
                )
            for row in wr.get("swarm_stats", []):
                key = (
                    tuple(row.get("active_rules", [])),
                    tuple(row.get("active_faults", [])),
                )
                existing = merged_swarm.setdefault(
                    key,
                    {
                        "active_rules": list(row.get("active_rules", [])),
                        "active_faults": list(row.get("active_faults", [])),
                        "energy": 0.0,
                        "times_used": 0,
                        "edges_found": 0,
                        "runs_with_new_edges": 0,
                        "failure_count": 0,
                        "property_hits": 0,
                    },
                )
                existing["energy"] = max(existing["energy"], float(row.get("energy", 0.0)))
                existing["times_used"] += int(row.get("times_used", 0))
                existing["edges_found"] += int(row.get("edges_found", 0))
                existing["runs_with_new_edges"] += int(row.get("runs_with_new_edges", 0))
                existing["failure_count"] += int(row.get("failure_count", 0))
                existing["property_hits"] += int(row.get("property_hits", 0))
            for row in wr.get("fault_pair_coverage", []):
                pair = tuple(row.get("pair", []))
                if len(pair) == 2:
                    merged_pair_hits[(str(pair[0]), str(pair[1]))] += int(row.get("hits", 0))
            for rule_name, fault_map in wr.get("rule_fault_coverage", {}).items():
                merged_faults = merged_rule_fault.setdefault(rule_name, {})
                for fault_sig, hits in fault_map.items():
                    merged_faults[fault_sig] = merged_faults.get(fault_sig, 0) + int(hits)
            for rule_name, fault_map in wr.get("behavior_coverage", {}).items():
                merged_faults = merged_behavior.setdefault(rule_name, {})
                for fault_sig, props in fault_map.items():
                    merged_props = merged_faults.setdefault(fault_sig, set())
                    merged_props.update(str(prop) for prop in props)
            for prop, fault_hits in wr.get("property_stress", {}).items():
                merged_hits = merged_property_stress.setdefault(prop, {})
                for fault_sig, hits in fault_hits.items():
                    merged_hits[fault_sig] = merged_hits.get(fault_sig, 0) + int(hits)
            payloads: list[dict[str, Any]] = []
            if wr.get("worker_error") is not None:
                payloads.append(wr["worker_error"])
            payloads.extend(wr["failures"])
            for finfo in payloads:
                signature = _parallel_failure_signature(finfo)
                if signature in seen_failures:
                    continue
                seen_failures.add(signature)
                result.failures.append(_deserialize_failure_payload(finfo))

        result.unique_edges = len(all_edges)
        self._total_edges = all_edges
        result.swarm_stats = sorted(
            merged_swarm.values(),
            key=lambda row: (
                -row["failure_count"],
                -row["edges_found"],
                -row["property_hits"],
                -row["energy"],
                -row["times_used"],
                tuple(row["active_rules"]),
                tuple(row["active_faults"]),
            ),
        )
        result.rule_fault_coverage = merged_rule_fault
        result.behavior_coverage = {
            rule_name: {fault_sig: sorted(props) for fault_sig, props in fault_map.items()}
            for rule_name, fault_map in merged_behavior.items()
        }
        result.property_stress = merged_property_stress
        result.native_boundary_findings = [
            dict(f.native_boundary) for f in result.failures if f.native_boundary is not None
        ]
        all_fault_names = [f.name for f in getattr(self.test_class, "faults", [])]
        result.fault_pair_coverage = [
            {"pair": list(pair), "hits": hits}
            for pair, hits in sorted(
                merged_pair_hits.items(),
                key=lambda item: (item[1], item[0]),
            )
        ]
        result.uncovered_fault_pairs = [
            list(pair)
            for pair in combinations(sorted(dict.fromkeys(all_fault_names)), 2)
            if merged_pair_hits.get(pair, 0) <= 0
        ]
        result.duration_seconds = _time.monotonic() - start
        result.parallel_result_warning = self._parallel_retry_reason(worker_results, result) or ""
        return result
    finally:
        release_shared_memory(ring_shm, state_shm, shm)
_run_parallel.__qualname__ = "Explorer._run_parallel"
Explorer._run_parallel = _run_parallel
del _run_parallel
