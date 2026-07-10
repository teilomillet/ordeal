from __future__ import annotations
# ruff: noqa
def explore_scan(
    state: ExplorationState,
    *,
    max_examples: int = 30,
    targets: list[str] | None = None,
    fixtures: dict[str, Any] | None = None,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
    expected_failures: list[str] | None = None,
    expected_preconditions: dict[str, list[str]] | None = None,
    ignore_contracts: list[str] | None = None,
    ignore_properties: list[str] | None = None,
    ignore_relations: list[str] | None = None,
    contract_overrides: dict[str, list[str]] | None = None,
    expected_properties: dict[str, list[str]] | None = None,
    expected_relations: dict[str, list[str]] | None = None,
    property_overrides: dict[str, list[str]] | None = None,
    relation_overrides: dict[str, list[str]] | None = None,
    contract_checks: dict[str, list[Any]] | None = None,
    mode: str = "evidence",
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    proof_bundles: bool = True,
    auto_contracts: list[str] | None = None,
    require_replayable: bool = True,
    min_contract_fit: float = 0.6,
    min_reachability: float = 0.5,
    min_realism: float = 0.55,
    security_focus: bool = False,
    minimize_findings: bool = False,
) -> ExplorationState:
    """Scan module for crashes and update state."""
    from ordeal.auto import scan_module

    result = scan_module(
        state.module,
        max_examples=max_examples,
        targets=targets,
        fixtures=fixtures,
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
        expected_failures=expected_failures,
        expected_preconditions=expected_preconditions,
        ignore_contracts=ignore_contracts,
        ignore_properties=ignore_properties,
        ignore_relations=ignore_relations,
        contract_overrides=contract_overrides,
        expected_properties=expected_properties,
        expected_relations=expected_relations,
        property_overrides=property_overrides,
        relation_overrides=relation_overrides,
        contract_checks=contract_checks,
        mode=mode,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
        treat_any_as_weak=treat_any_as_weak,
        proof_bundles=proof_bundles,
        auto_contracts=auto_contracts,
        require_replayable=require_replayable,
        min_contract_fit=min_contract_fit,
        min_reachability=min_reachability,
        min_realism=min_realism,
        security_focus=security_focus,
        minimize_findings=minimize_findings,
    )
    state.scan_mode = mode
    if "scan" not in state.active_dimensions:
        state.active_dimensions = tuple(dict.fromkeys((*state.active_dimensions, "scan")))
    existing_skips = {(name, reason) for name, reason in state.skipped}
    for name, reason in result.skipped:
        if (name, reason) not in existing_skips:
            state.skipped.append((name, reason))
            existing_skips.add((name, reason))
    for fr in result.functions:
        fs = state.function(fr.name)
        fs.source_hash = fr.source_sha256 or fs.source_hash
        fs.scanned = True
        fs.crash_free = None if fr.limitation_kind is not None else fr.execution_ok
        fs.scan_error = fr.error
        fs.failing_args = fr.failing_args
        fs.scan_crash_category = fr.crash_category
        fs.scan_replayable = fr.replayable
        fs.scan_replay_attempts = fr.replay_attempts
        fs.scan_replay_matches = fr.replay_matches
        fs.scan_minimization = fr.minimization
        fs.scan_contract_fit = fr.contract_fit
        fs.scan_reachability = fr.reachability
        fs.scan_realism = fr.realism
        fs.scan_sink_signal = fr.sink_signal
        fs.scan_sink_categories = list(fr.sink_categories)
        fs.scan_input_sources = list(fr.input_sources)
        fs.scan_input_source = fr.input_source
        fs.scan_proof_bundle = fr.proof_bundle
        fs.scan_limitation_kind = fr.limitation_kind
        fs.scan_blocking_reason = fr.blocking_reason
        if fr.contract_violations:
            fs.contract_violations = list(fr.contract_violations)
            fs.contract_violation_details = list(fr.contract_violation_details)
            fs.crash_free = True
            fs.scan_error = None
        if fr.property_violations:
            # Merge, don't duplicate
            existing = set(fs.property_violations)
            for v in fr.property_violations:
                if v not in existing:
                    fs.property_violations.append(v)
            existing_details = {detail.get("summary") for detail in fs.property_violation_details}
            for detail in fr.property_violation_details:
                if detail.get("summary") not in existing_details:
                    fs.property_violation_details.append(detail)
                    existing_details.add(detail.get("summary"))
    return state
def explore_mutate(
    state: ExplorationState,
    *,
    workers: int = 1,
    extra_mutants: dict[str, list[str | tuple[str, str]]] | None = None,
    concern: str | None = None,
    llm: Any | None = None,
) -> ExplorationState:
    """Mutation-test all mined functions and update state.

    Scales with *workers*: more CPUs = more mutants tested in parallel.

    Args:
        state: Exploration state to enrich.
        workers: Parallel workers for mutation testing.
        extra_mutants: Per-function extra mutant source strings, keyed by
            function name.  Written by the AI assistant or human.
        concern: Free-text concern for targeted mutation generation.
        llm: Optional LLM callable for automated mutant generation.
    """
    from ordeal.mutations import mutate

    for name, fs in list(state.functions.items()):
        if fs.mutated:
            continue
        target = f"{state.module}.{name}"
        fn_extras = (extra_mutants or {}).get(name)
        try:
            result = mutate(
                target,
                preset="essential",
                workers=workers,
                extra_mutants=fn_extras,
                concern=concern,
                llm=llm,
            )
        except Exception:
            continue
        fs.mutated = True
        fs.mutation_score = result.score
        fs.killed_mutants = sum(1 for m in result.mutants if m.killed)
        fs.survived_mutants = sum(1 for m in result.mutants if not m.killed)
    return state
def explore_harden(
    state: ExplorationState,
    extra_tests: dict[str, list[str]],
) -> ExplorationState:
    """Verify tests against surviving mutants and update state (Meta ACH pattern).

    For each function in *extra_tests*, re-runs mutation testing to get
    surviving mutants, then verifies each test with the three-assurance
    loop: buildable, valid regression, kills mutant.

    This is the step where an AI assistant closes the loop: it reads
    ``state.frontier`` to find unhardened survivors, writes tests, and
    calls ``explore_harden`` to verify them.

    Args:
        state: Exploration state with prior mutation results.
        extra_tests: Per-function test source strings, keyed by function
            name.  Each test should import the target and assert behavior.

    Returns:
        Updated state with hardening results.
    """
    from ordeal.mutations import mutate

    for name, tests in extra_tests.items():
        fs = state.function(name)
        if not fs.mutated or fs.survived_mutants == 0:
            continue
        target = f"{state.module}.{name}"
        try:
            result = mutate(target, preset="essential")
        except Exception:
            continue
        if not result.survived:
            continue
        hardened = result.harden(tests)
        if hardened.verified:
            fs.hardened = True
            fs.hardened_kills += hardened.total_kills
    return state
def explore_chaos(
    state: ExplorationState,
    *,
    max_examples: int = 10,
    object_factories: dict[str, Any] | None = None,
    object_setups: dict[str, Any] | None = None,
    object_scenarios: dict[str, Any] | None = None,
    object_state_factories: dict[str, Any] | None = None,
    object_teardowns: dict[str, Any] | None = None,
    object_harnesses: dict[str, str] | None = None,
) -> ExplorationState:
    """Auto-generate and run chaos tests, update state."""
    from ordeal.auto import chaos_for

    completed = False
    try:
        TestCase = chaos_for(
            state.module,
            max_examples=max_examples,
            stateful_step_count=10,
            object_factories=object_factories,
            object_setups=object_setups,
            object_scenarios=object_scenarios,
            object_state_factories=object_state_factories,
            object_teardowns=object_teardowns,
            object_harnesses=object_harnesses,
        )
        test = TestCase("runTest")
        test.runTest()
        completed = True
    except Exception:
        pass

    if completed:
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
    seed: int = 42,
    patch_io: bool = False,
    include_private: bool = False,
    scan_targets: list[str] | None = None,
    scan_fixtures: dict[str, Any] | None = None,
    scan_object_factories: dict[str, Any] | None = None,
    scan_object_setups: dict[str, Any] | None = None,
    scan_object_scenarios: dict[str, Any] | None = None,
    scan_object_state_factories: dict[str, Any] | None = None,
    scan_object_teardowns: dict[str, Any] | None = None,
    scan_object_harnesses: dict[str, str] | None = None,
    scan_expected_failures: list[str] | None = None,
    scan_expected_preconditions: dict[str, list[str]] | None = None,
    scan_ignore_contracts: list[str] | None = None,
    scan_ignore_properties: list[str] | None = None,
    scan_ignore_relations: list[str] | None = None,
    scan_contract_overrides: dict[str, list[str]] | None = None,
    scan_expected_properties: dict[str, list[str]] | None = None,
    scan_expected_relations: dict[str, list[str]] | None = None,
    scan_property_overrides: dict[str, list[str]] | None = None,
    scan_relation_overrides: dict[str, list[str]] | None = None,
    scan_contract_checks: dict[str, list[Any]] | None = None,
    scan_mode: str = "evidence",
    scan_seed_from_tests: bool = True,
    scan_seed_from_fixtures: bool = True,
    scan_seed_from_docstrings: bool = True,
    scan_seed_from_code: bool = True,
    scan_seed_from_call_sites: bool = True,
    scan_treat_any_as_weak: bool = True,
    scan_proof_bundles: bool = True,
    scan_auto_contracts: list[str] | None = None,
    scan_require_replayable: bool = True,
    scan_min_contract_fit: float = 0.6,
    scan_min_reachability: float = 0.5,
    scan_min_realism: float = 0.55,
    scan_security_focus: bool = False,
    scan_minimize_findings: bool = False,
    run_mine: bool = True,
    run_scan: bool = True,
    run_mutate: bool = True,
    run_chaos: bool = True,
) -> ExplorationState:
    """Run all exploration strategies on a module.

    Assembles mine → scan → mutate → chaos into one pass.
    Each step enriches the shared ``ExplorationState``.  The entire
    exploration runs inside a ``DeterministicSupervisor`` for
    reproducibility, and checkpoints into a ``StateTree`` so the
    AI can navigate the exploration trajectory.

    Scales with compute: more *workers* → more mutations tested
    in parallel, more *max_examples* → more input space sampled.
    Confidence grows with both.

    Deterministic: same *seed* + same code = same exploration.
    Different seeds explore different regions of the state space.
    The trajectory is logged in ``state.supervisor`` and the
    state tree is in ``state.tree``.

    The AI assistant can also run steps individually via
    ``explore_mine``, ``explore_scan``, ``explore_mutate``,
    ``explore_harden``, ``explore_chaos`` for finer control.

    Args:
        module: Dotted module path.
        state: Resume from a previous exploration. ``None`` starts fresh.
        time_limit: Optional time budget in seconds (soft limit).
        workers: Parallel workers for mutation testing. More CPUs = more
            state space explored per unit time.
        max_examples: Hypothesis examples for mining and scanning. More
            examples = more input space sampled = higher confidence.
        seed: RNG seed for deterministic exploration. Same seed = same
            trajectory. Default 42.
        patch_io: If ``True``, enable deterministic file/network/subprocess
            I/O inside the supervisor while exploring.
    """
    import time as _time

    from ordeal.supervisor import DeterministicSupervisor, StateTree

    if state is None:
        state = ExplorationState(module=module)
    else:
        # Resuming — invalidate any functions whose source changed so
        # the pipeline redoes them from scratch instead of skipping.
        state.refreshed = state.refresh()

    active_dimensions = tuple(
        dimension
        for dimension, enabled in (
            ("mine", run_mine),
            ("scan", run_scan),
            ("mutate", run_mutate),
            ("chaos", run_chaos),
        )
        if enabled
    )
    state.active_dimensions = active_dimensions or ("scan",)

    # Initialize supervisor and state tree if not already present
    if not hasattr(state, "supervisor") or state.supervisor is None:
        state.supervisor = None  # set below inside context
    if not hasattr(state, "tree") or state.tree is None:
        state.tree = StateTree()

    sup = DeterministicSupervisor(seed=seed, patch_io=patch_io)
    sup.__enter__()

    try:
        start = _time.monotonic()

        # Checkpoint: initial state
        state_hash = hash(("init", module, seed))
        state.tree.checkpoint(state_hash, snapshot=state, action="start", seed=seed)
        sup.log_transition("explore_start", state_hash=state_hash)

        # Step 1: Mine properties
        prev_hash = state_hash
        if run_mine:
            state = explore_mine(
                state,
                max_examples=max_examples,
                include_private=include_private,
                targets=scan_targets,
                object_factories=scan_object_factories,
                object_setups=scan_object_setups,
                object_scenarios=scan_object_scenarios,
                object_state_factories=scan_object_state_factories,
                object_teardowns=scan_object_teardowns,
                object_harnesses=scan_object_harnesses,
                ignore_properties=scan_ignore_properties,
                ignore_relations=scan_ignore_relations,
                property_overrides=scan_property_overrides,
                relation_overrides=scan_relation_overrides,
            )
            mine_hash = hash(("mined", len(state.functions), state.confidence))
            state.tree.checkpoint(
                mine_hash,
                parent=state_hash,
                action="mine",
                snapshot=None,
                edges=sum(f.edges_discovered for f in state.functions.values()),
                seed=seed,
            )
            sup.log_transition("explore_mine", state_hash=mine_hash)
            prev_hash = mine_hash

        # Step 2: Crash safety
        if run_scan and (time_limit is None or (_time.monotonic() - start) < time_limit):
            state = explore_scan(
                state,
                max_examples=max_examples,
                targets=scan_targets,
                fixtures=scan_fixtures,
                object_factories=scan_object_factories,
                object_setups=scan_object_setups,
                object_scenarios=scan_object_scenarios,
                object_state_factories=scan_object_state_factories,
                object_teardowns=scan_object_teardowns,
                object_harnesses=scan_object_harnesses,
                expected_failures=scan_expected_failures,
                expected_preconditions=scan_expected_preconditions,
                ignore_contracts=scan_ignore_contracts,
                ignore_properties=scan_ignore_properties,
                ignore_relations=scan_ignore_relations,
                contract_overrides=scan_contract_overrides,
                expected_properties=scan_expected_properties,
                expected_relations=scan_expected_relations,
                property_overrides=scan_property_overrides,
                relation_overrides=scan_relation_overrides,
                contract_checks=scan_contract_checks,
                mode=scan_mode,
                seed_from_tests=scan_seed_from_tests,
                seed_from_fixtures=scan_seed_from_fixtures,
                seed_from_docstrings=scan_seed_from_docstrings,
                seed_from_code=scan_seed_from_code,
                seed_from_call_sites=scan_seed_from_call_sites,
                treat_any_as_weak=scan_treat_any_as_weak,
                proof_bundles=scan_proof_bundles,
                auto_contracts=scan_auto_contracts,
                require_replayable=scan_require_replayable,
                min_contract_fit=scan_min_contract_fit,
                min_reachability=scan_min_reachability,
                min_realism=scan_min_realism,
                security_focus=scan_security_focus,
                minimize_findings=scan_minimize_findings,
            )
            scan_hash = hash(("scanned", state.confidence))
            state.tree.checkpoint(
                scan_hash,
                parent=prev_hash,
                action="scan",
                seed=seed,
            )
            sup.log_transition("explore_scan", state_hash=scan_hash)
            prev_hash = scan_hash

        # Step 3: Mutation testing
        if run_mutate and (time_limit is None or (_time.monotonic() - start) < time_limit):
            state = explore_mutate(state, workers=workers)
            mutate_hash = hash(("mutated", state.confidence))
            state.tree.checkpoint(
                mutate_hash,
                parent=prev_hash,
                action="mutate",
                seed=seed,
            )
            sup.log_transition("explore_mutate", state_hash=mutate_hash)
            prev_hash = mutate_hash

        # Step 4: Chaos testing
        if run_chaos and (time_limit is None or (_time.monotonic() - start) < time_limit):
            state = explore_chaos(
                state,
                max_examples=max_examples,
                object_factories=scan_object_factories,
                object_setups=scan_object_setups,
                object_scenarios=scan_object_scenarios,
                object_state_factories=scan_object_state_factories,
                object_teardowns=scan_object_teardowns,
                object_harnesses=scan_object_harnesses,
            )
            chaos_hash = hash(("chaos", state.confidence))
            state.tree.checkpoint(
                chaos_hash,
                parent=prev_hash,
                action="chaos",
                seed=seed,
            )
            sup.log_transition("explore_chaos", state_hash=chaos_hash)

        state.exploration_time += _time.monotonic() - start

    finally:
        # Store supervisor info on the state for inspection
        state.supervisor_info = sup.reproduction_info()
        state.supervisor_info["trajectory_steps"] = len(sup.trajectory)
        state.supervisor_info["unique_states"] = len(sup.visited_states)
        sup.__exit__(None, None, None)

    return state
