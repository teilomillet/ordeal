from __future__ import annotations
# ruff: noqa
def _run_mutation_benchmark_trial(
    target: str,
    *,
    preset: str | None,
    workers: int,
    filter_equivalent: bool,
    test_filter: str | None,
    python_executable: str,
    cwd: str,
) -> MutationBenchmarkTrial:
    """Run one mutation benchmark trial in a fresh Python subprocess."""
    script = textwrap.dedent(
        f"""
        from __future__ import annotations

        import json
        import time
        import warnings

        from ordeal import mutate

        warnings.simplefilter("ignore")
        started = time.perf_counter()
        result = mutate(
            {target!r},
            preset={preset!r},
            workers={workers!r},
            filter_equivalent={filter_equivalent!r},
            test_filter={test_filter!r},
            resume=False,
        )
        elapsed = time.perf_counter() - started
        payload = {{
            "seconds": elapsed,
            "total": result.total,
            "killed": result.killed,
            "score": result.score,
            "timings": result.timings,
            "diagnostics": result.diagnostics,
        }}
        print({_MUTATION_BENCHMARK_MARKER!r} + json.dumps(payload, sort_keys=True))
        """
    )
    proc = subprocess.run(
        [python_executable, "-c", script],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_MUTATION_BENCHMARK_MARKER):
            payload = json.loads(line[len(_MUTATION_BENCHMARK_MARKER) :])
            return MutationBenchmarkTrial(
                seconds=float(payload["seconds"]),
                total=int(payload["total"]),
                killed=int(payload["killed"]),
                score=float(payload["score"]),
                timings={k: float(v) for k, v in payload.get("timings", {}).items()},
                diagnostics=dict(payload.get("diagnostics", {})),
            )
    raise RuntimeError(
        "Mutation benchmark trial produced no parseable payload.\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
def _benchmark_mutations(
    targets: list[str],
    *,
    repeats: int,
    workers: int,
    preset: str | None,
    filter_equivalent: bool,
    test_filter: str | None,
    python_executable: str | None,
    cwd: str | None,
) -> MutationBenchmarkSuite:
    """Benchmark mutation latency on one or more targets."""
    executable = python_executable or sys.executable
    workdir = cwd or os.getcwd()
    cases: list[MutationBenchmarkCase] = []
    for target in targets:
        trials = [
            _run_mutation_benchmark_trial(
                target,
                preset=preset,
                workers=workers,
                filter_equivalent=filter_equivalent,
                test_filter=test_filter,
                python_executable=executable,
                cwd=workdir,
            )
            for _ in range(repeats)
        ]
        cases.append(MutationBenchmarkCase(target=target, trials=trials))
    return MutationBenchmarkSuite(
        cases=cases,
        repeats=repeats,
        workers=workers,
        preset=preset,
    )
_IMPORT_BENCHMARK_MARKER = "__ORDEAL_IMPORT_BENCH__ "
_AUDIT_DIFF_MARKER = "__ORDEAL_AUDIT_DIFF__ "
def _parse_benchmark_payload(stdout: str, marker: str) -> dict[str, Any]:
    """Extract the trailing JSON payload emitted by a benchmark subprocess."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise RuntimeError(f"Benchmark subprocess produced no parseable payload for marker {marker!r}")
def _run_import_benchmark_trial(
    module: str,
    *,
    python_executable: str,
    cwd: str,
) -> float:
    """Measure pure import latency inside a fresh Python subprocess."""
    script = textwrap.dedent(
        f"""
        import json
        import time

        started = time.perf_counter()
        import {module}  # noqa: F401
        elapsed = time.perf_counter() - started
        print({_IMPORT_BENCHMARK_MARKER!r} + json.dumps({{"seconds": elapsed}}, sort_keys=True))
        """
    )
    proc = subprocess.run(
        [python_executable, "-c", script],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = _parse_benchmark_payload(proc.stdout, _IMPORT_BENCHMARK_MARKER)
    return float(payload["seconds"])
def _audit_cache_file(module: str, cwd: str) -> Path:
    """Return the audit cache file path for *module* inside *cwd*."""
    return Path(cwd) / ".ordeal" / "audit" / f"{module.replace('.', '_')}.json"
def _generated_audit_test_file(module: str, cwd: str) -> Path:
    """Return the generated migrated-test path for *module* inside *cwd*."""
    short = module.rsplit(".", 1)[-1]
    return Path(cwd) / ".ordeal" / f"test_{short}_migrated.py"
def _clear_audit_artifacts(module: str, cwd: str) -> None:
    """Remove cached/generated audit artifacts for a clean benchmark trial."""
    _audit_cache_file(module, cwd).unlink(missing_ok=True)
    _generated_audit_test_file(module, cwd).unlink(missing_ok=True)
def _run_cli_audit_once(
    module: str,
    *,
    test_dir: str,
    max_examples: int,
    workers: int,
    validation_mode: str,
    python_executable: str,
    cwd: str,
) -> float:
    """Run one `ordeal audit` CLI invocation and return wall time."""
    args = [
        python_executable,
        "-m",
        "ordeal.cli",
        "audit",
        module,
        "--test-dir",
        test_dir,
        "--max-examples",
        str(max_examples),
        "--workers",
        str(workers),
        "--validation-mode",
        validation_mode,
    ]
    started = time.perf_counter()
    subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return time.perf_counter() - started
def _run_audit_benchmark_trial(
    module: str,
    *,
    mode: str,
    test_dir: str,
    max_examples: int,
    workers: int,
    validation_mode: str,
    python_executable: str,
    cwd: str,
) -> float:
    """Benchmark cold or warm `ordeal audit` CLI latency."""
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"cold", "warm"}:
        raise ValueError(f"Unknown audit benchmark mode: {mode!r}")

    _clear_audit_artifacts(module, cwd)
    if normalized_mode == "warm":
        _run_cli_audit_once(
            module,
            test_dir=test_dir,
            max_examples=max_examples,
            workers=workers,
            validation_mode=validation_mode,
            python_executable=python_executable,
            cwd=cwd,
        )
    return _run_cli_audit_once(
        module,
        test_dir=test_dir,
        max_examples=max_examples,
        workers=workers,
        validation_mode=validation_mode,
        python_executable=python_executable,
        cwd=cwd,
    )
def _run_audit_differential_trial(
    module: str,
    *,
    test_dir: str,
    max_examples: int,
    workers: int,
    validation_mode: str,
    compare_validation_mode: str,
    python_executable: str,
    cwd: str,
) -> dict[str, Any]:
    """Run an audit comparison with one fresh process per validation mode."""

    def run_mode(mode: str) -> dict[str, Any]:
        _clear_audit_artifacts(module, cwd)
        script = textwrap.dedent(
            f"""
        import json
        import time

        from ordeal.audit import audit

        started = time.perf_counter()
        result = audit(
            {module!r},
            test_dir={test_dir!r},
            max_examples={max_examples},
            workers={workers},
            validation_mode={mode!r},
        )
        ended = time.perf_counter()
        payload = {{
            "seconds": ended - started,
            "score": result.mutation_score_fraction,
            "mutation_score": result.mutation_score,
        }}
        print({_AUDIT_DIFF_MARKER!r} + json.dumps(payload, sort_keys=True))
        """
        )
        proc = subprocess.run(
            [python_executable, "-c", script],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        return _parse_benchmark_payload(proc.stdout, _AUDIT_DIFF_MARKER)

    primary = run_mode(validation_mode)
    reference = run_mode(compare_validation_mode)
    primary_seconds = float(primary["seconds"])
    reference_seconds = float(reference["seconds"])
    return {
        "seconds": primary_seconds + reference_seconds,
        "primary_seconds": primary_seconds,
        "reference_seconds": reference_seconds,
        "primary_score": primary.get("score"),
        "reference_score": reference.get("score"),
        "primary_mutation_score": primary.get("mutation_score", ""),
        "reference_mutation_score": reference.get("mutation_score", ""),
    }
def _parse_perf_contract(path: str) -> list[PerfContractSpec]:
    """Parse a TOML perf/quality contract file."""
    contract_path = Path(path)
    with contract_path.open("rb") as fh:
        data = tomllib.load(fh)

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Performance contract must define at least one [[cases]] entry")

    specs: list[PerfContractSpec] = []
    for idx, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise ValueError(f"Case #{idx} must be a table")
        name = str(raw_case.get("name", "")).strip()
        kind = str(raw_case.get("kind", "")).strip()
        if not name:
            raise ValueError(f"Case #{idx} is missing 'name'")
        if kind not in {"import", "audit", "audit_compare", "mutate"}:
            raise ValueError(f"Case {name!r} has unsupported kind {kind!r}")

        tier = str(raw_case.get("tier", "pr")).strip()
        if tier not in {"pr", "nightly"}:
            raise ValueError(
                f"Case {name!r} has unsupported tier {tier!r} (must be 'pr' or 'nightly')"
            )

        spec = PerfContractSpec(
            name=name,
            kind=kind,
            tier=tier,
            repeats=int(raw_case.get("repeats", 3)),
            max_seconds=(
                float(raw_case["max_seconds"]) if raw_case.get("max_seconds") is not None else None
            ),
            module=(str(raw_case["module"]) if "module" in raw_case else None),
            target=(str(raw_case["target"]) if "target" in raw_case else None),
            mode=str(raw_case.get("mode", "cold")),
            validation_mode=str(raw_case.get("validation_mode", "fast")),
            compare_validation_mode=(
                str(raw_case["compare_validation_mode"])
                if "compare_validation_mode" in raw_case
                else ("deep" if kind == "audit_compare" else None)
            ),
            workers=int(raw_case.get("workers", 1)),
            preset=(str(raw_case["preset"]) if "preset" in raw_case else "standard"),
            test_filter=(str(raw_case["test_filter"]) if "test_filter" in raw_case else None),
            filter_equivalent=bool(raw_case.get("filter_equivalent", True)),
            test_dir=str(raw_case.get("test_dir", "tests")),
            max_examples=int(raw_case.get("max_examples", 20)),
            max_score_gap=(
                float(raw_case["max_score_gap"])
                if raw_case.get("max_score_gap") is not None
                else None
            ),
            min_score=(
                float(raw_case["min_score"]) if raw_case.get("min_score") is not None else None
            ),
        )

        if spec.repeats <= 0:
            raise ValueError(f"Case {name!r} must have repeats >= 1")
        if spec.max_seconds is not None and spec.max_seconds <= 0:
            raise ValueError(f"Case {name!r} must have max_seconds > 0")
        if spec.max_score_gap is not None and not (0 <= spec.max_score_gap <= 1):
            raise ValueError(f"Case {name!r} must have max_score_gap between 0 and 1")
        if spec.min_score is not None and not (0 <= spec.min_score <= 1):
            raise ValueError(f"Case {name!r} must have min_score between 0 and 1")
        if spec.min_score is not None and spec.kind not in {"audit_compare", "mutate"}:
            raise ValueError(f"Case {name!r} can only set min_score for audit_compare or mutate")
        if spec.validation_mode not in {"fast", "deep"}:
            raise ValueError(
                f"Case {name!r} has unsupported validation_mode {spec.validation_mode!r}"
            )
        if spec.compare_validation_mode is not None and spec.compare_validation_mode not in {
            "fast",
            "deep",
        }:
            raise ValueError(
                f"Case {name!r} has unsupported compare_validation_mode "
                f"{spec.compare_validation_mode!r}"
            )
        if spec.kind in {"import", "audit", "audit_compare"} and not spec.module:
            raise ValueError(f"Case {name!r} requires 'module'")
        if spec.kind == "mutate" and not spec.target:
            raise ValueError(f"Case {name!r} requires 'target'")
        if spec.kind == "audit_compare":
            if spec.compare_validation_mode is None:
                raise ValueError(f"Case {name!r} requires compare_validation_mode")
            if spec.compare_validation_mode == spec.validation_mode:
                raise ValueError(f"Case {name!r} must compare two different validation modes")
            if spec.max_score_gap is None:
                raise ValueError(f"Case {name!r} requires max_score_gap")
        specs.append(spec)

    return specs
