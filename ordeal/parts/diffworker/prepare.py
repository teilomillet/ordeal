from __future__ import annotations
# ruff: noqa
def _prepare(args: argparse.Namespace) -> None:
    """Generate baseline cases and outcomes in the baseline worktree."""
    root = Path.cwd().resolve()
    mode, functions = _resolve_targets(args.target, include_private=args.include_private)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "target": args.target,
        "mode": mode,
        "runtime": _runtime(),
        "functions": {},
    }
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "target": args.target,
        "mode": mode,
        "runtime": payload["runtime"],
        "functions": [],
    }
    registries = json.loads(args.fixture_registries)
    exact_cases = json.loads(args.exact_cases) if args.exact_cases is not None else None
    if exact_cases is not None:
        if not isinstance(exact_cases, dict):
            raise TypeError("exact revision cases must map function names to case lists")
        missing = set(exact_cases) - set(functions)
        if missing:
            raise TargetResolutionError(
                "exact revision case function(s) are missing: " + ", ".join(sorted(missing))
            )
    selected_functions = {
        name: function
        for name, function in functions.items()
        if exact_cases is None or name in exact_cases
    }
    for name, function in sorted(selected_functions.items()):
        signature = str(inspect.signature(function))
        try:
            if reason := _callable_block_reason(function):
                raise ValueError(reason)
            if exact_cases is None:
                cases = _generate_cases(
                    function,
                    root=root,
                    fixture_registries=registries,
                    max_examples=args.max_examples,
                    seed_value=args.seed,
                )
            else:
                encoded_cases = exact_cases[name]
                if not isinstance(encoded_cases, list) or not encoded_cases:
                    raise ValueError(f"exact cases for {name} must be a non-empty list")
                cases = [_decode_replay_value(case) for case in encoded_cases]
                if not all(
                    isinstance(case, dict) and all(isinstance(key, str) for key in case)
                    for case in cases
                ):
                    raise TypeError(f"exact cases for {name} must decode to string-keyed mappings")
            canonical_cases = [_canonical_value(case) for case in cases]
            canonical_case_payloads = [
                observe(case, label="revision worker input").payload for case in cases
            ]
            outcomes = [_capture(function, case, root=root) for case in cases]
            replays = [
                [_capture(function, case, root=root) for _ in range(args.replay_attempts)]
                for case in cases
            ]
            entry = {
                "signature": signature,
                "source": _source_binding(function, root=root),
                "cases": cases,
                "canonical_cases": canonical_cases,
                "canonical_case_payloads": canonical_case_payloads,
                "outcomes": outcomes,
                "replays": replays,
                "observations_canonicalized": True,
                "blocked_reason": None,
            }
        except Exception as exc:
            entry = {
                "signature": signature,
                "source": _source_binding(function, root=root),
                "cases": [],
                "canonical_cases": [],
                "canonical_case_payloads": [],
                "outcomes": [],
                "replays": [],
                "observations_canonicalized": True,
                "blocked_reason": str(exc),
            }
        payload["functions"][name] = entry
        metadata["functions"].append(
            {
                "name": name,
                "signature": signature,
                "total": len(entry["cases"]),
                "blocked_reason": entry["blocked_reason"],
            }
        )

    try:
        with Path(args.payload).open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        raise RuntimeError(
            "baseline cases or outputs are not serializable across revisions: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    Path(args.result).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
def _compare_cases(
    function: Callable[..., Any],
    baseline_entry: Mapping[str, Any],
    *,
    root: Path,
    rtol: float | None,
    atol: float | None,
) -> tuple[int, list[dict[str, Any]]]:
    """Compare every prepared case and retain its replay evidence privately."""
    mismatches: list[dict[str, Any]] = []
    mismatch_count = 0
    for case, canonical_case, canonical_case_payload, baseline_outcome, baseline_replays in zip(
        baseline_entry["cases"],
        baseline_entry["canonical_cases"],
        baseline_entry["canonical_case_payloads"],
        baseline_entry["outcomes"],
        baseline_entry["replays"],
        strict=True,
    ):
        if observe(case, label="candidate revision input").payload != canonical_case_payload:
            raise ObservationError(
                "candidate deserialization changed the canonical baseline input"
            )
        candidate_outcome = _capture(function, case, root=root)
        if _outcomes_equal(
            baseline_outcome,
            candidate_outcome,
            rtol=rtol,
            atol=atol,
        ):
            continue
        mismatch_count += 1
        safe_base = _safe_outcome(baseline_outcome)
        safe_candidate = _safe_outcome(candidate_outcome)
        expected_observation = observe(
            {"base": safe_base, "candidate": safe_candidate},
            label="revision worker expected replay pair",
        )
        expected_signature = expected_observation.signature
        observed_signatures: list[str] = []
        replay_matches = 0
        for baseline_replay in baseline_replays:
            candidate_replay = _capture(function, case, root=root)
            replay_pair = {
                "base": _safe_outcome(baseline_replay),
                "candidate": _safe_outcome(candidate_replay),
            }
            replay_observation = observe(
                replay_pair,
                label="revision worker observed replay pair",
            )
            observed_signatures.append(replay_observation.signature)
            if exact_replay_match(
                expected_observation,
                replay_observation,
                recorded_expected_signature=expected_signature,
            ):
                replay_matches += 1
        try:
            replay_args = _encode_replay_value(case)
        except TypeError:
            replay_args = None
        mismatches.append(
            {
                "args": canonical_case,
                "canonical_args": copy.deepcopy(canonical_case_payload),
                "replay_args": replay_args,
                "base": safe_base,
                "candidate": safe_candidate,
                "replay": {
                    "attempts": len(baseline_replays),
                    "exact_matches": replay_matches,
                    "expected_signature": expected_signature,
                    "observed_signatures": observed_signatures,
                },
            }
        )
    return mismatch_count, mismatches
def _compare(args: argparse.Namespace) -> None:
    """Replay baseline cases and compare them inside the candidate worktree."""
    root = Path.cwd().resolve()
    try:
        mode, candidate_functions = _resolve_targets(
            args.target,
            include_private=args.include_private,
            allow_missing=True,
        )
        resolution_error = None
    except TargetResolutionError as exc:
        mode = "unresolved"
        candidate_functions = {}
        resolution_error = str(exc)

    try:
        with Path(args.payload).open("rb") as handle:
            baseline = pickle.load(handle)
    except Exception as exc:
        raise RuntimeError(
            f"candidate could not load baseline cases or outputs: {type(exc).__name__}: {exc}"
        ) from exc

    baseline_functions = baseline["functions"]
    if args.exact_cases is not None:
        candidate_functions = {
            name: function
            for name, function in candidate_functions.items()
            if name in baseline_functions
        }
    baseline_names = set(baseline_functions)
    candidate_names = set(candidate_functions)
    function_results: list[dict[str, Any]] = []
    for name in sorted(baseline_names & candidate_names):
        baseline_entry = baseline_functions[name]
        function = candidate_functions[name]
        blocked_reason = baseline_entry["blocked_reason"]
        mismatches: list[dict[str, Any]] = []
        mismatch_count = 0
        if blocked_reason is None:
            if not baseline_entry.get("observations_canonicalized"):
                blocked_reason = (
                    "baseline observations were not canonicalized before candidate import"
                )
            if blocked_reason is None:
                try:
                    mismatch_count, mismatches = _compare_cases(
                        function,
                        baseline_entry,
                        root=root,
                        rtol=args.rtol,
                        atol=args.atol,
                    )
                except ObservationError as exc:
                    blocked_reason = str(exc)
                    mismatch_count = 0
                    mismatches = []
        canonical_mismatch = _canonical_mismatch(mismatches)
        function_results.append(
            {
                "name": name,
                "base_signature": baseline_entry["signature"],
                "candidate_signature": str(inspect.signature(function)),
                "base_source": baseline_entry["source"],
                "candidate_source": _source_binding(function, root=root),
                "total": len(baseline_entry["cases"]),
                "mismatch_count": mismatch_count,
                "mismatches": [canonical_mismatch] if canonical_mismatch is not None else [],
                "blocked_reason": blocked_reason,
                "equivalent": blocked_reason is None and mismatch_count == 0,
            }
        )

    result = {
        "schema_version": 1,
        "target": args.target,
        "base_runtime": baseline["runtime"],
        "candidate_runtime": _runtime(),
        "base_mode": baseline["mode"],
        "candidate_mode": mode,
        "candidate_resolution_error": resolution_error,
        "comparison": _comparison_binding(rtol=args.rtol, atol=args.atol),
        "functions": function_results,
        "added_functions": sorted(candidate_names - baseline_names),
        "removed_functions": sorted(baseline_names - candidate_names),
    }
    Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
def _parser() -> argparse.ArgumentParser:
    """Build the private worker argument parser."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", choices=("prepare", "compare"))
    parser.add_argument("--target", required=True)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--include-private", action="store_true")
    parser.add_argument("--max-examples", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fixture-registries", default="[]")
    parser.add_argument("--exact-cases", default=None)
    parser.add_argument("--rtol", type=float, default=None)
    parser.add_argument("--atol", type=float, default=None)
    parser.add_argument("--replay-attempts", type=int, default=2)
    parser.add_argument("--system-sequence", default=None)
    return parser
def main(argv: list[str] | None = None) -> int:
    """Run one worker phase."""
    args = _parser().parse_args(argv)
    _activate_worktree(Path.cwd().resolve())
    if args.system_sequence is not None and args.mode == "prepare":
        _prepare_system(args)
    elif args.system_sequence is not None:
        _compare_system(args)
    elif args.mode == "prepare":
        _prepare(args)
    else:
        _compare(args)
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
