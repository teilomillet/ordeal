from __future__ import annotations
# ruff: noqa
def _outcomes_equal(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    rtol: float | None,
    atol: float | None,
) -> bool:
    """Compare return/exception behavior and post-invocation arguments."""
    if baseline["kind"] != candidate["kind"]:
        return False
    baseline_exception = baseline["exception"]
    candidate_exception = candidate["exception"]
    if (baseline_exception is None) != (candidate_exception is None):
        return False
    if baseline_exception is not None and candidate_exception is not None:
        if any(
            baseline_exception.get(field) != candidate_exception.get(field)
            for field in ("type", "message")
        ):
            return False
    if rtol is not None or atol is not None:
        return _approx_equal(
            baseline["return_value"],
            candidate["return_value"],
            rtol=rtol if rtol is not None else 1e-9,
            atol=atol if atol is not None else 0.0,
        ) and _approx_equal(
            baseline["mutated_arguments"],
            candidate["mutated_arguments"],
            rtol=rtol if rtol is not None else 1e-9,
            atol=atol if atol is not None else 0.0,
        )
    return (
        baseline["canonical_return_value"] == candidate["canonical_return_value"]
        and baseline["canonical_mutated_arguments"] == candidate["canonical_mutated_arguments"]
    )
def _safe_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Return one detached canonical outcome envelope."""
    return {
        "kind": outcome["kind"],
        "return_value": copy.deepcopy(outcome["return_value"]),
        "canonical_return_value": copy.deepcopy(outcome["canonical_return_value"]),
        "exception": copy.deepcopy(outcome["exception"]),
        "mutated_arguments": copy.deepcopy(outcome["mutated_arguments"]),
        "canonical_mutated_arguments": copy.deepcopy(outcome["canonical_mutated_arguments"]),
        "canonical_observation": copy.deepcopy(outcome["canonical_observation"]),
        "observation_signature": str(outcome["observation_signature"]),
    }
def _canonical_signature(value: Any) -> str:
    """Return the shared observation layer's replay signature."""
    return observe(value, label="revision worker replay value").signature
def _canonical_mismatch(mismatches: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Shrink observed differences to one stable canonical runtime witness."""
    if not mismatches:
        return None
    original = mismatches[0]
    stable = [
        mismatch
        for mismatch in mismatches
        if int(mismatch["replay"]["attempts"]) > 0
        and int(mismatch["replay"]["attempts"]) == int(mismatch["replay"]["exact_matches"])
    ]
    pool = stable or list(mismatches)

    def rank(mismatch: Mapping[str, Any]) -> tuple[int, str, str]:
        encoded = _canonical_json(mismatch["args"])
        return len(encoded), encoded, str(mismatch["replay"]["expected_signature"])

    selected = copy.deepcopy(min(pool, key=rank))
    selected.update(
        {
            "original_args": copy.deepcopy(original["args"]),
            "original_canonical_args": copy.deepcopy(original["canonical_args"]),
            "original_base": copy.deepcopy(original["base"]),
            "original_candidate": copy.deepcopy(original["candidate"]),
            "minimization": {
                "method": "canonical observed-case shrinking",
                "candidate_count": len(mismatches),
                "boundary": (
                    "Selected the shortest canonical JSON input among the observed "
                    "generated divergent cases; inputs outside that sample were not explored."
                ),
            },
        }
    )
    return selected
def _comparison_binding(*, rtol: float | None, atol: float | None) -> dict[str, Any]:
    """Source-bind the exact revision-worker comparison pipeline."""
    comparator = _source_binding(_outcomes_equal)
    comparator.update(
        {
            "kind": "tolerance" if rtol is not None or atol is not None else "exact",
            "rtol": rtol,
            "atol": atol,
        }
    )
    normalizer = _source_binding(_identity)
    normalizer["kind"] = "identity"
    return {
        "comparator": comparator,
        "normalizer": normalizer,
        "exception_matching": "exact type and message across revisions",
        "replay_matching": (
            "exact canonical input and paired full observations, including terminal "
            "exception source locations"
        ),
    }
def _runtime() -> dict[str, Any]:
    """Return process/worktree evidence for the parent result."""
    return {"pid": os.getpid(), "worktree": str(Path.cwd().resolve())}
def _system_public_exports(system: Any) -> dict[str, str]:
    """Collect public static interface members without evaluating descriptors."""
    owner = type(system)
    exports: dict[str, str] = {}
    for name, raw in inspect.getmembers_static(owner):
        if name.startswith("_"):
            continue
        value = raw.__func__ if isinstance(raw, (classmethod, staticmethod)) else raw
        kind = "property" if isinstance(raw, property) else type(value).__name__
        try:
            signature = str(inspect.signature(value)) if callable(value) else ""
        except (TypeError, ValueError):
            signature = "<unknown>"
        exports[name] = f"{kind}{signature}"
    try:
        instance_members = vars(system)
    except TypeError:
        instance_members = {}
    for name, value in instance_members.items():
        if not name.startswith("_") and name not in exports:
            exports[name] = type(value).__name__
    return exports
def _capture_direct_call(
    call: Callable[[], Any],
    *,
    root: Path,
) -> dict[str, Any]:
    """Capture one operation or fault transition with the shared observation layer."""
    try:
        returned = _resolve_awaitable(call())
    except Exception as exc:
        exception = {
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "terminal_source_location": _terminal_source_location(exc, root=root),
            "canonical_exception": observe(
                exc,
                label="revision system exception",
            ).payload,
        }
        observation = observe(
            {"kind": "exception", "exception": exception},
            label="revision system operation outcome",
        )
        return {
            "kind": "exception",
            "return_value": None,
            "canonical_return_value": None,
            "exception": exception,
            "canonical_observation": observation.payload,
        }
    detached = isolated_deepcopy(returned, label="revision system return value")
    returned_observation = observe(detached, label="revision system return value")
    observation = observe(
        {"kind": "return", "return_value": returned_observation.payload},
        label="revision system operation outcome",
    )
    return {
        "kind": "return",
        "return_value": returned_observation.json_value,
        "canonical_return_value": returned_observation.payload,
        "exception": None,
        "canonical_observation": observation.payload,
    }
def _capture_system_sequence(
    factory: Callable[[], Any],
    sequence: Sequence[Mapping[str, Any]],
    *,
    root: Path,
) -> dict[str, Any]:
    """Run one JSON event sequence against a fresh revision-owned system."""
    inspect.signature(factory).bind()
    system = factory()
    steps: list[dict[str, Any]] = []
    for index, raw_event in enumerate(sequence):
        event = isolated_deepcopy(
            dict(raw_event),
            label="revision system event",
        )
        kind = str(event.get("kind", ""))
        if kind == "operation":
            name = str(event.get("name", ""))
            args = event.get("args", [])
            kwargs = event.get("kwargs", {})
            if not name or not isinstance(args, list) or not isinstance(kwargs, Mapping):
                raise ValueError(f"invalid operation event at index {index}")
            target = getattr(system, name)
            outcome = _capture_direct_call(
                lambda target=target, args=args, kwargs=kwargs: target(*args, **dict(kwargs)),
                root=root,
            )
        elif kind == "fault":
            name = str(event.get("name", ""))
            action = str(event.get("action", "activate"))
            parameters = event.get("parameters", {})
            if not name or not action or not isinstance(parameters, Mapping):
                raise ValueError(f"invalid fault event at index {index}")
            handler = getattr(system, "apply_fault", None)
            if not callable(handler):
                raise ValueError(
                    "revision system fault events require an apply_fault(event) method"
                )
            fault = SimpleNamespace(
                name=name,
                action=action,
                parameters=dict(parameters),
            )
            outcome = _capture_direct_call(
                lambda handler=handler, fault=fault: handler(fault), root=root
            )
        else:
            raise ValueError(f"unknown system event kind at index {index}: {kind!r}")
        try:
            public_state = {
                name: value
                for name, value in vars(system).items()
                if not name.startswith("_") and not callable(value)
            }
        except TypeError:
            public_state = {}
        state_observation = observe(public_state, label="revision system public state")
        steps.append(
            {
                "index": index,
                "event": _canonical_value(event),
                "outcome": outcome,
                "state": state_observation.json_value,
                "canonical_state": state_observation.payload,
            }
        )
    interface = _system_public_exports(system)
    sequence_observation = observe(
        {
            "interface": interface,
            "steps": [
                {
                    "event": step["event"],
                    "outcome": step["outcome"]["canonical_observation"],
                    "state": step["canonical_state"],
                }
                for step in steps
            ],
        },
        label="revision system sequence",
    )
    return {
        "kind": "system_sequence",
        "return_value": {"interface": interface, "steps": steps},
        "canonical_return_value": sequence_observation.payload,
        "exception": None,
        "mutated_arguments": {},
        "canonical_mutated_arguments": observe(
            {},
            label="revision system empty arguments",
        ).payload,
        "canonical_observation": sequence_observation.payload,
        "observation_signature": sequence_observation.signature,
    }
def _prepare_system(args: argparse.Namespace) -> None:
    """Capture one replayed system story in the baseline worktree."""
    root = Path.cwd().resolve()
    name, factory = _resolve_attribute_target(args.target, allow_missing=False)
    assert factory is not None
    sequence = json.loads(args.system_sequence)
    if not isinstance(sequence, list) or not all(isinstance(item, Mapping) for item in sequence):
        raise ValueError("system sequence must be a JSON list of event objects")
    outcome = _capture_system_sequence(factory, sequence, root=root)
    replays = [
        _capture_system_sequence(factory, sequence, root=root) for _ in range(args.replay_attempts)
    ]
    entry = {
        "signature": str(inspect.signature(factory)),
        "source": _source_binding(factory, root=root),
        "sequence": sequence,
        "outcome": outcome,
        "replays": replays,
    }
    payload = {
        "schema_version": 1,
        "target": args.target,
        "mode": "system",
        "runtime": _runtime(),
        "functions": {name: entry},
    }
    with Path(args.payload).open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    Path(args.result).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": args.target,
                "mode": "system",
                "runtime": payload["runtime"],
                "events": len(sequence),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
def _compare_system(args: argparse.Namespace) -> None:
    """Replay the baseline system story in the candidate worktree."""
    root = Path.cwd().resolve()
    with Path(args.payload).open("rb") as handle:
        baseline = pickle.load(handle)
    baseline_name, baseline_entry = next(iter(baseline["functions"].items()))
    try:
        _candidate_name, factory = _resolve_attribute_target(args.target, allow_missing=True)
        resolution_error = None
    except TargetResolutionError as exc:
        factory = None
        resolution_error = str(exc)
    function_results: list[dict[str, Any]] = []
    if factory is not None:
        sequence = baseline_entry["sequence"]
        baseline_outcome = baseline_entry["outcome"]
        candidate_outcome = _capture_system_sequence(factory, sequence, root=root)
        mismatch = (
            baseline_outcome["canonical_observation"] != candidate_outcome["canonical_observation"]
        )
        mismatches: list[dict[str, Any]] = []
        if mismatch:
            safe_base = _safe_outcome(baseline_outcome)
            safe_candidate = _safe_outcome(candidate_outcome)
            expected = observe(
                {"base": safe_base, "candidate": safe_candidate},
                label="revision system expected replay pair",
            )
            replay_matches = 0
            observed_signatures: list[str] = []
            for baseline_replay in baseline_entry["replays"]:
                candidate_replay = _capture_system_sequence(factory, sequence, root=root)
                observed = observe(
                    {
                        "base": _safe_outcome(baseline_replay),
                        "candidate": _safe_outcome(candidate_replay),
                    },
                    label="revision system observed replay pair",
                )
                observed_signatures.append(observed.signature)
                if exact_replay_match(
                    expected,
                    observed,
                    recorded_expected_signature=expected.signature,
                ):
                    replay_matches += 1
            mismatches.append(
                {
                    "args": {"sequence": _canonical_value(sequence)},
                    "base": safe_base,
                    "candidate": safe_candidate,
                    "replay": {
                        "attempts": len(baseline_entry["replays"]),
                        "exact_matches": replay_matches,
                        "expected_signature": expected.signature,
                        "observed_signatures": observed_signatures,
                    },
                    "minimization": {
                        "method": "supplied system sequence replay",
                        "boundary": "Git-revision system mode preserves the supplied event story.",
                    },
                }
            )
        function_results.append(
            {
                "name": baseline_name,
                "base_signature": baseline_entry["signature"],
                "candidate_signature": str(inspect.signature(factory)),
                "base_source": baseline_entry["source"],
                "candidate_source": _source_binding(factory, root=root),
                "total": len(sequence),
                "mismatch_count": int(mismatch),
                "mismatches": mismatches,
                "blocked_reason": None,
                "equivalent": not mismatch,
            }
        )
    comparison = _comparison_binding(rtol=None, atol=None)
    comparison["mode"] = "system_revision"
    result = {
        "schema_version": 1,
        "target": args.target,
        "execution_mode": "system",
        "system_sequence": baseline_entry["sequence"],
        "base_runtime": baseline["runtime"],
        "candidate_runtime": _runtime(),
        "base_mode": "system",
        "candidate_mode": "system" if factory is not None else "unresolved",
        "candidate_resolution_error": resolution_error,
        "comparison": comparison,
        "functions": function_results,
        "added_functions": [],
        "removed_functions": [baseline_name] if factory is None else [],
    }
    Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
