from __future__ import annotations
# ruff: noqa
def diff(
    fn_a: Callable[..., Any],
    fn_b: Callable[..., Any],
    *,
    max_examples: int = 100,
    rtol: float | None = None,
    atol: float | None = None,
    compare: Callable[[Any, Any], bool] | None = None,
    normalize: Callable[[Any], Any] | None = None,
    replay_attempts: int = 2,
    artifact_dir: str | Path | None = None,
    regression_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    side_effects: Mapping[str, SideEffect] | Callable[[Any], Any] | None = None,
    equivalence_proof: Callable[[Callable[..., Any], Callable[..., Any]], bool] | None = None,
    sequence: Sequence[SystemEvent] | None = None,
    state: Callable[[Any], Any] | None = None,
    apply_fault: Callable[[Any, FaultEvent], None] | None = None,
    performance: PerformanceBudget | None = None,
    minimize: bool = True,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> DiffResult | SystemDiffResult:
    """Search two revisions for a full-envelope behavioral divergence.

    Every observed divergence is minimized, replayed on the same input, and
    represented by a source-bound ``ordeal.divergence-evidence/v1`` artifact.
    The artifact is always available in ``result.artifacts``; pass
    ``artifact_dir`` to persist it as JSON. ``normalize`` transforms returned
    values before the default or custom comparator runs. Exceptions, mutated
    arguments, bound receiver state, and selected ``side_effects`` remain part
    of the comparison independently of return-value normalization.

    ``no_divergence_found`` is bounded by the sampled inputs and selected
    observable channels. Only an explicit successful ``equivalence_proof``
    produces ``status == "proven_equivalent"``.

    Providing ``sequence`` switches to system mode. The same ordered
    operations and fault transitions run against fresh instances from both
    zero-argument factories. Public exports, signatures, outcomes, public
    state, selected side effects, and recovery are reported together;
    ``performance`` remains a separate measured contract.

    System example::

        result = diff(
            OldStore,
            NewStore,
            sequence=[
                FaultEvent("timeout", "activate"),
                Operation("read"),
                FaultEvent("timeout", "deactivate"),
                Operation("read"),
            ],
            state=lambda store: store.snapshot(),
        )

    See ``docs/concepts/system-differential.md`` for the layman model and
    ``docs/guides/system-differential.md`` for a complete copyable tutorial.
    """
    if replay_attempts < 1:
        raise ValueError("replay_attempts must be >= 1")
    if max_examples < 1:
        raise ValueError("max_examples must be >= 1")
    if (regression_path is None) != (manifest_path is None):
        raise ValueError("regression_path and manifest_path must be provided together")
    if sequence is not None:
        if fixtures:
            raise TypeError("system diff does not accept function fixtures")
        if equivalence_proof is not None:
            raise TypeError("system diff does not accept equivalence_proof")
        if side_effects is not None and not callable(side_effects):
            raise TypeError("system diff side_effects must be a system probe")
        normalizer = normalize or _identity

        def return_compare(a: Any, b: Any) -> bool:
            try:
                normalized_a = normalizer(a)
                normalized_b = normalizer(b)
                _observe_losslessly(normalized_a, label="normalized system a return value")
                _observe_losslessly(normalized_b, label="normalized system b return value")
                if compare is not None:
                    return bool(compare(normalized_a, normalized_b))
                return _default_compare(normalized_a, normalized_b, rtol, atol)
            except _ReconstructionInconclusive as exc:
                raise ObservationError(str(exc)) from exc

        system_result = _diff_system(
            fn_a,
            fn_b,
            sequence,
            return_compare=return_compare,
            state_compare=_state_equal,
            state=state,
            side_effects=side_effects,
            apply_fault=apply_fault,
            performance=performance,
            minimize=minimize,
            replay_attempts=replay_attempts,
        )
        system_result = _attach_system_artifact(
            system_result,
            factory_a=fn_a,
            factory_b=fn_b,
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
            state=state,
            side_effects=side_effects,
            apply_fault=apply_fault,
            artifact_dir=artifact_dir,
        )
        if system_result.divergent and regression_path is not None and manifest_path is not None:
            (
                system_result.regression_path,
                system_result.manifest_path,
                system_result.finding_id,
                system_result.regression_error,
            ) = _persist_diff_regression(
                mode="system",
                fn_a=fn_a,
                fn_b=fn_b,
                artifact=system_result.artifact or {},
                artifact_path=system_result.artifact_path,
                regression_path=regression_path,
                manifest_path=manifest_path,
                compare=compare,
                normalize=normalize,
                rtol=rtol,
                atol=atol,
                sequence=system_result.sequence,
                state=state,
                side_effects=side_effects,
                apply_fault=apply_fault,
            )
        return system_result
    if any(value is not None for value in (state, apply_fault, performance)):
        raise TypeError("system diff options require sequence=")
    if side_effects is not None and not isinstance(side_effects, Mapping):
        raise TypeError("function diff side_effects must map names to SideEffect objects")
    selected_side_effects = dict(side_effects or {})
    invalid_effects = [
        name
        for name, effect in selected_side_effects.items()
        if not isinstance(effect, SideEffect)
    ]
    if invalid_effects:
        raise TypeError(
            "function diff side_effects values must be SideEffect objects: "
            + ", ".join(repr(name) for name in invalid_effects)
        )
    concrete_example: dict[str, Any] | None = None
    if fixtures and all(not isinstance(value, st.SearchStrategy) for value in fixtures.values()):
        try:
            _bind_named_arguments(fn_a, dict(fixtures))
        except TypeError:
            pass
        else:
            concrete_example = dict(fixtures)
    normalized_strategies: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized_strategies = {
            name: value if isinstance(value, st.SearchStrategy) else st.just(value)
            for name, value in fixtures.items()
        }
    strategies = (
        {} if concrete_example is not None else _infer_strategies(fn_a, normalized_strategies)
    )
    if strategies is None:
        raise ValueError(
            f"Cannot infer strategies for {getattr(fn_a, '__name__', fn_a)}. "
            "Provide fixtures for untyped parameters."
        )

    minimized: _Candidate | None = None
    example_count = [0]

    def evaluate(kwargs: dict[str, Any]) -> None:
        """Evaluate one generated or zero-argument example."""
        example_count[0] += 1
        candidate = _run_pair(
            fn_a,
            fn_b,
            kwargs,
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
            side_effects=selected_side_effects,
        )
        if candidate.differences:
            raise _DivergenceFound(candidate)

    try:
        if concrete_example is not None:
            evaluate(concrete_example)
        elif strategies:

            @given(**strategies)
            @settings(max_examples=max_examples, database=None)
            def test(**kwargs: Any) -> None:
                evaluate(kwargs)

            test()
        else:
            evaluate({})
    except _DivergenceFound as mismatch:
        minimized = mismatch.candidate
    except _ReconstructionInconclusive as exc:
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status="inconclusive",
            reason=str(exc),
        )
    except FlakyFailure:
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status="inconclusive",
            reason="Hypothesis could not replay one stable outcome mismatch",
        )

    if minimized is None:
        status = "no_divergence_observed"
        proof_method = None
        if equivalence_proof is not None and equivalence_proof(fn_a, fn_b):
            status = "proven_equivalent"
            proof_method = _callable_name(equivalence_proof)
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status=status,
            proof_method=proof_method,
        )

    expected_observation = _candidate_observation(minimized)
    expected_signature = expected_observation.signature
    observed_signatures: list[str | None] = []
    replay_matches = 0
    for _ in range(replay_attempts):
        try:
            replayed = _run_pair(
                fn_a,
                fn_b,
                minimized.args,
                compare=compare,
                normalize=normalize,
                rtol=rtol,
                atol=atol,
                side_effects=selected_side_effects,
            )
        except _ReconstructionInconclusive:
            observed_signatures.append(None)
            continue
        replayed_observation = _candidate_observation(replayed)
        observed_signatures.append(replayed_observation.signature)
        if _candidate_replays(
            expected_observation,
            replayed_observation,
            expected_signature=expected_signature,
        ):
            replay_matches += 1

    if replay_matches != replay_attempts:
        return DiffResult(
            function_a=_callable_name(fn_a),
            function_b=_callable_name(fn_b),
            total=example_count[0],
            status="inconclusive",
            reason=(
                "the minimized outcome mismatch was not stable under exact replay: "
                f"{replay_matches}/{replay_attempts} matched"
            ),
        )

    input_observation = _observe_losslessly(
        minimized.args,
        label="differential witness input",
    )
    artifact = _build_divergence_evidence(
        revisions={
            "a": _callable_binding(fn_a),
            "b": _callable_binding(fn_b),
        },
        comparison=_comparison_binding(
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
        ),
        original_input=input_observation.json_value,
        minimized_input=input_observation.json_value,
        original_input_canonical=input_observation.payload,
        minimized_input_canonical=input_observation.payload,
        original_observations=_candidate_observations(minimized),
        observations=_candidate_observations(minimized),
        differences=minimized.differences,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        expected_signature=expected_signature,
        observed_signatures=observed_signatures,
    )
    artifact_path = _write_artifact(artifact, artifact_dir) if artifact_dir else None
    public_a = _public_outcome(minimized.outcome_a, minimized.normalized_a)
    public_b = _public_outcome(minimized.outcome_b, minimized.normalized_b)
    mismatch = Mismatch(
        args=_observe_losslessly(minimized.args, label="public witness input").json_value,
        output_a=(
            public_a.return_value
            if public_a.returned
            else {
                "exception_type": public_a.exception_type,
                "message": public_a.exception_message,
                "terminal_source_location": public_a.terminal_source_location,
            }
        ),
        output_b=(
            public_b.return_value
            if public_b.returned
            else {
                "exception_type": public_b.exception_type,
                "message": public_b.exception_message,
                "terminal_source_location": public_b.terminal_source_location,
            }
        ),
        artifact=artifact,
        artifact_path=artifact_path,
    )
    try:
        replay_args_json = json.dumps(
            _encode_replay_value(minimized.args),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except TypeError:
        replay_args_json = None
    witness = DiffWitness(
        args=_freeze(minimized.args),
        outcome_a=public_a,
        outcome_b=public_b,
        differences=minimized.differences,
        replay_attempts=replay_attempts,
        replay_matches=replay_matches,
        replay_verified=True,
        replay_args_json=replay_args_json,
        artifact=_freeze(artifact),
        artifact_path=artifact_path,
    )
    result = DiffResult(
        function_a=_callable_name(fn_a),
        function_b=_callable_name(fn_b),
        total=example_count[0],
        mismatches=[mismatch],
        status="divergent",
        witness=witness,
    )
    if regression_path is not None and manifest_path is not None:
        (
            result.regression_path,
            result.manifest_path,
            result.finding_id,
            result.regression_error,
        ) = _persist_diff_regression(
            mode="function",
            fn_a=fn_a,
            fn_b=fn_b,
            artifact=artifact,
            artifact_path=artifact_path,
            regression_path=regression_path,
            manifest_path=manifest_path,
            compare=compare,
            normalize=normalize,
            rtol=rtol,
            atol=atol,
            kwargs=minimized.args,
            side_effects=selected_side_effects,
        )
    return result
