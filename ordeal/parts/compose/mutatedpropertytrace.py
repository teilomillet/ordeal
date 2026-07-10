from __future__ import annotations
# ruff: noqa
def _mutated_property_trace(
    trace: ComposeTrace,
    *,
    action_index: int,
    property_name: str,
) -> ComposeTrace | None:
    """Return a prefix trace whose selected response oracle is deliberately wrong."""
    actions = copy.deepcopy(trace.actions[: action_index + 1])
    if not actions or actions[-1].index != action_index:
        return None
    action = actions[-1]
    if property_name.startswith("status:"):
        observed_status = int(action.result.get("status", 200))
        mutant_status = 599 if observed_status != 599 else 598
        action.params["expect_status"] = [mutant_status]
    elif property_name.startswith("json:"):
        path = property_name.removeprefix("json:")
        expectations = action.params.get("expect_json")
        if not isinstance(expectations, Mapping) or path not in expectations:
            return None
        action.params["expect_json"] = {
            **dict(expectations),
            path: {"__ordeal_mutated_expectation__": True},
        }
    elif property_name.startswith("capture:"):
        state_name = property_name.removeprefix("capture:")
        captures = action.params.get("capture")
        if not isinstance(captures, Mapping) or state_name not in captures:
            return None
        action.params["capture"] = {
            **dict(captures),
            state_name: "json.__ordeal_missing_capture__",
        }
    else:
        return None
    return replace(
        trace,
        actions=actions,
        failure=None,
        final_state={},
        duration=0.0,
        replay=None,
    )
def _control_prefix(trace: ComposeTrace, action_index: int) -> ComposeTrace:
    """Return the exact unmutated prefix ending at one request action."""
    return replace(
        trace,
        actions=copy.deepcopy(trace.actions[: action_index + 1]),
        failure=None,
        final_state={},
        duration=0.0,
        replay=None,
    )
def measure_compose_workload_strength(
    trace: ComposeTrace,
    *,
    budget: int,
    runner_factory: Callable[[ComposeConfig], ComposeRunner] | None = None,
) -> dict[str, Any]:
    """Measure whether observed Compose properties detect oracle mutations.

    Each trial first replays the unmodified trace prefix. Only a clean control
    allows the corresponding status, JSON, or capture expectation to be
    mutated. A mutant is killed only when the altered expectation fails at the
    selected operation. This measures the recorded workload and harness wiring;
    it does not claim that production service source code was mutated.
    """
    if budget < 0:
        raise ValueError("workload mutation budget must be >= 0")
    coverage = compose_reliability_coverage(trace)
    failed_cells = [
        {
            "operation": row["operation"],
            "fault": row["fault"],
            "property": row["property"],
        }
        for row in coverage["rows"]
        if row["status"] == "FAIL"
    ]
    unexercised = [
        {
            "operation": row["operation"],
            "fault": row["fault"],
            "property": row["property"],
        }
        for row in coverage["rows"]
        if row["status"] == "NOT EXERCISED"
    ]
    candidates: list[tuple[ComposeTraceAction, str]] = []
    for action in trace.actions:
        if action.kind != "request" or not bool(action.params.get("validate", True)):
            continue
        observations = action.result.get("property_results", [])
        if not isinstance(observations, list):
            continue
        for observation in observations:
            if not isinstance(observation, Mapping) or not bool(observation.get("passed")):
                continue
            property_name = str(observation.get("property") or "")
            if property_name == "valid_json":
                continue
            if property_name.startswith(("status:", "json:", "capture:")):
                candidates.append((action, property_name))

    mutations: list[dict[str, Any]] = []
    if budget > 0:
        factory = runner_factory or ComposeRunner
        config = _config_from_payload(trace.compose)
        for action, property_name in candidates[:budget]:
            control_failure = factory(config).replay(_control_prefix(trace, action.index))
            row = {
                "operation": action.name,
                "fault": str(action.params.get("fault") or "none"),
                "property": property_name,
                "action_index": action.index,
            }
            if control_failure is not None:
                mutations.append(
                    {
                        **row,
                        "status": "inconclusive",
                        "reason": "unmodified control prefix did not replay cleanly",
                    }
                )
                continue
            mutant = _mutated_property_trace(
                trace,
                action_index=action.index,
                property_name=property_name,
            )
            if mutant is None:
                continue
            observed = factory(config).replay(mutant)
            killed = observed is not None and observed.action_index == action.index
            mutations.append(
                {
                    **row,
                    "status": "killed" if killed else "survived",
                    "observed_failure_kind": observed.kind if observed is not None else None,
                }
            )

    killed = sum(row["status"] == "killed" for row in mutations)
    survived = sum(row["status"] == "survived" for row in mutations)
    inconclusive = sum(row["status"] == "inconclusive" for row in mutations)
    tested = killed + survived
    if failed_cells:
        status = "weak"
        protects = False
        summary = f"{len(failed_cells)} reliability cell(s) failed"
    elif survived:
        status = "weak"
        protects: bool | None = False
        summary = f"{survived}/{tested} workload oracle mutation(s) survived"
    elif unexercised:
        status = "weak"
        protects = False
        summary = f"{len(unexercised)} reliability cell(s) were not exercised"
    elif inconclusive:
        status = "inconclusive"
        protects = None
        summary = f"{inconclusive} workload mutation control(s) were unstable"
    elif tested <= 0:
        status = "inconclusive"
        protects = None
        summary = (
            "workload mutation strength was not requested"
            if budget == 0
            else "no observed mutable response properties were available"
        )
    else:
        status = "protective_within_measured_scope"
        protects = True
        summary = f"all {tested} tested workload oracle mutation(s) were killed"
    mutation_score = f"{killed}/{tested} ({killed / tested:.0%})" if tested else None
    return {
        "label": "compose workload protection",
        "status": status,
        "protects": protects,
        "summary": summary,
        "mutation_scope": "recorded Compose response oracles, not service source code",
        "mutation_score": mutation_score,
        "killed_mutants": killed,
        "tested_mutants": tested,
        "surviving_mutants": survived,
        "inconclusive_mutants": inconclusive,
        "failed_properties": failed_cells,
        "unexercised_properties": unexercised,
        "mutations": mutations,
    }
def build_compose_finding_evidence(
    trace: ComposeTrace,
    *,
    replay: ComposeReplayReport | None,
    coverage: Mapping[str, Any],
    protection: Mapping[str, Any],
    trace_path: Path | None = None,
) -> dict[str, Any]:
    """Build the shared bounded evidence-card schema for a Compose failure."""
    from ordeal.finding_evidence import _build_compose_finding_evidence

    return _build_compose_finding_evidence(
        trace.to_dict(),
        replay=replay.to_dict() if replay is not None else None,
        coverage=coverage,
        protection=protection,
        trace_path=trace_path.as_posix() if trace_path is not None else None,
    )
def run_compose_exploration(
    config: ComposeConfig,
    *,
    seed: int | None = None,
    max_time: float | None = None,
    replay_attempts: int | None = None,
) -> ComposeExplorationResult:
    """Run the Compose harness, save its exact trace, and replay failures."""
    effective = replace(
        config,
        seed=config.seed if seed is None else seed,
        max_time=config.max_time if max_time is None else max_time,
        replay_attempts=(config.replay_attempts if replay_attempts is None else replay_attempts),
    )
    if effective.max_time <= 0:
        raise ValueError("max_time must be > 0")
    if effective.replay_attempts < 1:
        raise ValueError("replay_attempts must be >= 1")
    trace = ComposeRunner(effective).run()
    trace_dir = Path(effective.trace_dir)
    trace_path = trace_dir / f"compose-{effective.seed}-{trace.content_hash()}.json"
    report = None
    if trace.failure is not None:
        report = replay_compose_trace(trace, attempts=effective.replay_attempts)
        trace.replay = report
    coverage = compose_reliability_coverage(trace)
    protection = measure_compose_workload_strength(
        trace,
        budget=effective.workload_mutations,
    )
    evidence = (
        build_compose_finding_evidence(
            trace,
            replay=report,
            coverage=coverage,
            protection=protection,
            trace_path=trace_path,
        )
        if trace.failure is not None
        else None
    )
    trace.save(trace_path)
    return ComposeExplorationResult(
        trace=trace,
        trace_path=trace_path,
        replay=report,
        requests=sum(action.kind == "request" for action in trace.actions),
        faults=sum(action.kind == "fault" for action in trace.actions),
        duration=trace.duration,
        coverage=coverage,
        protection=protection,
        evidence=evidence,
    )
def _portable_workspace_path(value: object, workspace: Path) -> object:
    """Render an in-workspace path portably while leaving external paths explicit."""
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
def _portable_trace_payload(trace: ComposeTrace, workspace: Path) -> dict[str, Any]:
    """Return a trace payload with repository-local config paths made relative."""
    payload = trace.to_dict()
    compose = payload.get("compose")
    if isinstance(compose, dict):
        for key in ("file", "trace_dir"):
            if key in compose:
                compose[key] = _portable_workspace_path(compose[key], workspace)
    return payload
def _evidence_with_compose_regression(
    evidence: Mapping[str, Any],
    *,
    trace_path: str,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one committed Compose trace binding to a copied evidence card."""
    copied = copy.deepcopy(dict(evidence))
    witness = copied.get("witness")
    if isinstance(witness, Mapping):
        witness_input = witness.get("input")
        if isinstance(witness_input, Mapping):
            portable_input = {**dict(witness_input), "trace_path": trace_path}
            from ordeal.finding_evidence import _sha256_json

            copied["witness"] = {
                **dict(witness),
                "input": portable_input,
                "sha256": _sha256_json(portable_input),
            }
    replay = copied.get("replay")
    if isinstance(replay, Mapping):
        copied["replay"] = {
            **dict(replay),
            "command": f"ordeal replay {trace_path}",
        }
    copied["regression"] = {
        "status": "saved",
        "path": trace_path,
        "test_name": None,
        "binding": dict(binding),
    }
    copied["ci_guard"] = {
        "status": "ready",
        "command": "uv run ordeal verify --ci",
        "acceptance": "Every replay attempt must complete without any failure after the fix.",
    }
    subject = copied.get("subject")
    if isinstance(subject, Mapping):
        copied["subject"] = {
            **dict(subject),
            "trace_sha256": binding.get("trace_sha256"),
        }
    workflow = copied.get("workflow")
    if isinstance(workflow, Mapping):
        copied["workflow"] = {
            **dict(workflow),
            "save_regression": "saved",
            "guard_ci": "ready",
        }
    return copied
def save_compose_regression(
    result: ComposeExplorationResult,
    *,
    workspace: str | Path = ".",
    regression_dir: str | Path = "tests/ordeal-compose-regressions",
    manifest_path: str | Path = "tests/ordeal-regressions.json",
) -> ComposeRegressionArtifacts | None:
    """Save a replay-backed Compose failure as a portable durable regression.

    Findings that never reproduced are deliberately not promoted. The manifest
    keeps the existing v1 envelope and distinguishes Compose records with
    ``runner=compose`` plus a canonical trace binding.
    """
    if (
        result.trace.failure is None
        or result.trace.failure.kind not in _DURABLE_FAILURE_KINDS
        or result.replay is None
        or result.replay.reproduced < 1
        or result.evidence is None
    ):
        return None
    root = Path(workspace).resolve()
    compose_file_value = str(result.trace.compose.get("file") or "").strip()
    compose_file = Path(compose_file_value)
    if not compose_file.is_absolute():
        compose_file = root / compose_file
    try:
        compose_file.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "durable Compose regressions require a Compose file in the workspace"
        ) from exc
    if not compose_file_value:
        raise ValueError("durable Compose regressions require a recorded Compose file")
    payload = _portable_trace_payload(result.trace, root)
    identity = {
        "failure_signature": result.trace.failure_signature,
        "actions": [
            {
                "kind": action.kind,
                "name": action.name,
                "params": action.params,
            }
            for action in result.trace.actions
        ],
    }
    canonical_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    finding_id = "fnd_compose_" + hashlib.sha256(canonical_identity.encode()).hexdigest()[:16]
    trace_root = Path(regression_dir)
    if not trace_root.is_absolute():
        trace_root = root / trace_root
    trace_path = trace_root / f"{finding_id}.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    from ordeal.regression_evidence import _compose_regression_binding

    binding = _compose_regression_binding(payload)
    relative_trace = trace_path.resolve().relative_to(root).as_posix()
    evidence = _evidence_with_compose_regression(
        result.evidence,
        trace_path=relative_trace,
        binding=binding,
    )
    result.evidence = evidence
    target_manifest = Path(manifest_path)
    if not target_manifest.is_absolute():
        target_manifest = root / target_manifest
    manifest: dict[str, Any] = {
        "schema": "ordeal.regression-manifest/v1",
        "regressions": [],
    }
    if target_manifest.is_file():
        try:
            loaded = json.loads(target_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("existing regression manifest is not valid JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("schema") != manifest["schema"]
            or not isinstance(loaded.get("regressions"), list)
        ):
            raise ValueError("existing regression manifest does not use the supported v1 schema")
        manifest["regressions"] = list(loaded["regressions"])
    records = {
        str(record.get("finding_id")): record
        for record in manifest["regressions"]
        if isinstance(record, Mapping) and record.get("finding_id")
    }
    records[finding_id] = {
        "finding_id": finding_id,
        "runner": "compose",
        "trace_file": relative_trace,
        "binding": binding,
        "failure_signature": result.trace.failure_signature,
        "replay_policy": {
            "attempts": result.replay.attempted,
            "expected": "clean",
            "maximum_failures": 0,
        },
        "evidence": evidence,
    }
    manifest["regressions"] = [records[key] for key in sorted(records)]
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    target_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return ComposeRegressionArtifacts(
        finding_id=finding_id,
        trace_path=trace_path,
        manifest_path=target_manifest,
        binding=binding,
    )
