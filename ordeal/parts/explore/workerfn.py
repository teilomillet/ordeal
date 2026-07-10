from __future__ import annotations
# ruff: noqa
def _worker_fn(args: dict[str, Any]) -> dict[str, Any]:
    """Worker process: import test class, run single-worker Explorer, return results.

    Defined at module level so it can be pickled by multiprocessing.
    If ``shared_edges_name`` is set, attaches to the shared-memory edge
    bitmap for cross-worker deduplication.
    """
    from multiprocessing.shared_memory import SharedMemory

    shm: SharedMemory | None = None
    state_shm: SharedMemory | None = None
    ring_shm: SharedMemory | None = None
    worker_id = int(args.get("worker_id", 0))
    explorer: Explorer | None = None

    try:
        class_path = args["class_path"]
        module_path, _, class_name = class_path.rpartition(".")
        mod = importlib.import_module(module_path)
        test_class = getattr(mod, class_name)

        explorer = Explorer(
            test_class,
            target_modules=args.get("target_modules"),
            seed=args["seed"],
            max_checkpoints=args["max_checkpoints"],
            checkpoint_prob=args["checkpoint_prob"],
            checkpoint_strategy=args["checkpoint_strategy"],
            fault_toggle_prob=args["fault_toggle_prob"],
            record_traces=args.get("record_traces", False),
            workers=1,  # each worker runs sequentially
            mutation_targets=args.get("mutation_targets"),
            seed_mutation_prob=args.get("seed_mutation_prob"),
            seed_mutation_respect_strategies=args.get(
                "seed_mutation_respect_strategies",
                False,
            ),
            ngram=args.get("ngram", 2),
            corpus_dir=args.get("corpus_dir"),
            rule_swarm=args.get("rule_swarm", False),
        )

        # Attach to shared edge bitmap
        shm_name = args.get("shared_edges_name")
        if shm_name:
            shm = SharedMemory(name=shm_name, create=False)
            explorer._shared_bitmap = shm.buf

        # Attach to shared state bitmap
        state_name = args.get("shared_state_name")
        if state_name:
            state_shm = SharedMemory(name=state_name, create=False)
            explorer._shared_state_bitmap = state_shm.buf

        # Attach to shared ring buffer for checkpoint exchange
        ring_name = args.get("ring_shm_name")
        if ring_name:
            ring_shm = SharedMemory(name=ring_name, create=False)
            explorer._pool_ring = ring_shm.buf
            auth_key_hex = args.get("pool_auth_key")
            explorer._pool_auth_key = (
                bytes.fromhex(str(auth_key_hex)) if auth_key_hex is not None else None
            )
            explorer._worker_id = worker_id
            explorer._pool_num_workers = args.get("num_workers", 1)
            explorer._pool_slots_per_worker = args.get("slots_per_worker", _POOL_NUM_SLOTS)

        result = explorer.run(
            max_time=args["max_time"],
            max_runs=args.get("max_runs"),
            steps_per_run=args["steps_per_run"],
            shrink=args.get("shrink", True),
            max_shrink_time=args.get("max_shrink_time", 30.0),
            patience=args.get("patience", 0),
        )

        serialized_failures = []
        for f in result.failures:
            serialized_failures.append(
                _serialize_failure_payload(
                    f.error,
                    worker_id=worker_id,
                    run_id=f.run_id,
                    step=f.step,
                    active_faults=f.active_faults,
                    rule_log=f.rule_log,
                    trace=f.trace,
                    error_traceback=f.error_traceback,
                )
            )

        return {
            "worker_id": worker_id,
            "total_runs": result.total_runs,
            "total_steps": result.total_steps,
            "skipped_steps": result.skipped_steps,
            "unique_edges": result.unique_edges,
            "unique_states": result.unique_states,
            "checkpoints_saved": result.checkpoints_saved,
            "duration_seconds": result.duration_seconds,
            "properties_satisfied": result.properties_satisfied,
            "seed_mutations_used": result.seed_mutations_used,
            "seed_mutations_productive": result.seed_mutations_productive,
            "rule_swarm_runs": result.rule_swarm_runs,
            "strategy_failures": dict(result.strategy_failures),
            "seed_replays": list(result.seed_replays),
            "swarm_stats": list(result.swarm_stats),
            "fault_pair_coverage": list(result.fault_pair_coverage),
            "rule_fault_coverage": result.rule_fault_coverage,
            "behavior_coverage": result.behavior_coverage,
            "property_stress": result.property_stress,
            "coverage_gaps": list(result.coverage_gaps),
            "lines_covered": result.lines_covered,
            "lines_total": result.lines_total,
            "failures": serialized_failures,
            "worker_error": None,
            "edge_log": result.edge_log,
            "edges": list(explorer._total_edges),
            "traces": (
                [trace.to_dict() for trace in result.traces]
                if args.get("record_traces", False)
                else []
            ),
        }
    except Exception as exc:
        return {
            "worker_id": worker_id,
            "total_runs": 0,
            "total_steps": 0,
            "skipped_steps": 0,
            "unique_edges": 0,
            "unique_states": 0,
            "checkpoints_saved": 0,
            "duration_seconds": 0.0,
            "properties_satisfied": 0,
            "seed_mutations_used": 0,
            "seed_mutations_productive": 0,
            "rule_swarm_runs": 0,
            "strategy_failures": {},
            "seed_replays": [],
            "swarm_stats": [],
            "fault_pair_coverage": [],
            "rule_fault_coverage": {},
            "behavior_coverage": {},
            "property_stress": {},
            "coverage_gaps": [],
            "lines_covered": 0,
            "lines_total": 0,
            "failures": [],
            "worker_error": _serialize_failure_payload(
                exc,
                worker_id=worker_id,
                run_id=-1,
                step=0,
                active_faults=[],
                rule_log=[f"[worker {worker_id}]"],
                trace=None,
            ),
            "edge_log": [],
            "edges": list(explorer._total_edges) if explorer is not None else [],
            "traces": [],
        }
    finally:
        if shm is not None:
            shm.close()
        if state_shm is not None:
            state_shm.close()
        if ring_shm is not None:
            ring_shm.close()
