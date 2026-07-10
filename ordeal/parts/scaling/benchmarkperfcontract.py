from __future__ import annotations
# ruff: noqa
def benchmark_perf_contract(
    contract_path: str,
    *,
    python_executable: str | None = None,
    cwd: str | None = None,
    tier: str | None = None,
) -> PerfContractSuite:
    """Run a checked-in perf/quality contract and return the observed medians.

    Parameters
    ----------
    tier:
        When set, only run cases whose ``tier`` matches.  ``"pr"`` for the
        lean PR gate, ``"nightly"`` for the full matrix.  ``None`` runs all.
    """
    executable = python_executable or sys.executable
    workdir = cwd or os.getcwd()
    cases: list[PerfContractCase] = []

    all_specs = _parse_perf_contract(contract_path)
    if tier is not None:
        all_specs = [s for s in all_specs if s.tier == tier]

    for spec in all_specs:
        seconds: list[float] = []
        details: dict[str, Any] = {}
        if spec.kind == "import":
            assert spec.module is not None
            for _ in range(spec.repeats):
                seconds.append(
                    _run_import_benchmark_trial(
                        spec.module,
                        python_executable=executable,
                        cwd=workdir,
                    )
                )
        elif spec.kind == "audit":
            assert spec.module is not None
            for _ in range(spec.repeats):
                seconds.append(
                    _run_audit_benchmark_trial(
                        spec.module,
                        mode=spec.mode,
                        test_dir=spec.test_dir,
                        max_examples=spec.max_examples,
                        workers=spec.workers,
                        validation_mode=spec.validation_mode,
                        python_executable=executable,
                        cwd=workdir,
                    )
                )
        elif spec.kind == "audit_compare":
            assert spec.module is not None
            assert spec.compare_validation_mode is not None
            primary_scores: list[float] = []
            reference_scores: list[float] = []
            primary_times: list[float] = []
            reference_times: list[float] = []
            primary_score_strings: list[str] = []
            reference_score_strings: list[str] = []
            for _ in range(spec.repeats):
                payload = _run_audit_differential_trial(
                    spec.module,
                    test_dir=spec.test_dir,
                    max_examples=spec.max_examples,
                    workers=spec.workers,
                    validation_mode=spec.validation_mode,
                    compare_validation_mode=spec.compare_validation_mode,
                    python_executable=executable,
                    cwd=workdir,
                )
                seconds.append(float(payload["seconds"]))
                primary_times.append(float(payload["primary_seconds"]))
                reference_times.append(float(payload["reference_seconds"]))
                primary_score = payload.get("primary_score")
                reference_score = payload.get("reference_score")
                if primary_score is not None:
                    primary_scores.append(float(primary_score))
                if reference_score is not None:
                    reference_scores.append(float(reference_score))
                primary_score_text = str(payload.get("primary_mutation_score", ""))
                reference_score_text = str(payload.get("reference_mutation_score", ""))
                if primary_score_text:
                    primary_score_strings.append(primary_score_text)
                if reference_score_text:
                    reference_score_strings.append(reference_score_text)
            primary_median = statistics.median(primary_scores) if primary_scores else None
            reference_median = statistics.median(reference_scores) if reference_scores else None
            details.update(
                {
                    "primary_score": primary_median,
                    "reference_score": reference_median,
                    "score_gap": (
                        max(0.0, reference_median - primary_median)
                        if primary_median is not None and reference_median is not None
                        else None
                    ),
                    "primary_seconds": (
                        statistics.median(primary_times) if primary_times else None
                    ),
                    "reference_seconds": (
                        statistics.median(reference_times) if reference_times else None
                    ),
                    f"{spec.validation_mode}_score": primary_median,
                    f"{spec.compare_validation_mode}_score": reference_median,
                    f"{spec.validation_mode}_mutation_score": (
                        primary_score_strings[-1] if primary_score_strings else ""
                    ),
                    f"{spec.compare_validation_mode}_mutation_score": (
                        reference_score_strings[-1] if reference_score_strings else ""
                    ),
                }
            )
        else:
            assert spec.target is not None
            trials = [
                _run_mutation_benchmark_trial(
                    spec.target,
                    preset=spec.preset,
                    workers=spec.workers,
                    filter_equivalent=spec.filter_equivalent,
                    test_filter=spec.test_filter,
                    python_executable=executable,
                    cwd=workdir,
                )
                for _ in range(spec.repeats)
            ]
            seconds = [trial.seconds for trial in trials]
            details["score"] = trials[0].score
            details["total"] = trials[0].total
            details["killed"] = trials[0].killed

        cases.append(PerfContractCase(spec=spec, seconds=seconds, details=details))

    return PerfContractSuite(cases=cases, contract_path=contract_path)
def benchmark(
    test_class: type | None = None,
    *,
    target_modules: list[str] | None = None,
    max_workers: int | None = None,
    time_per_trial: float = 10.0,
    seed: int = 42,
    steps_per_run: int = 50,
    metric: str = "runs",
    mutate_targets: list[str] | None = None,
    repeats: int = 5,
    workers: int = 1,
    preset: str | None = "standard",
    filter_equivalent: bool = True,
    test_filter: str | None = None,
    python_executable: str | None = None,
    cwd: str | None = None,
) -> ScalingAnalysis | MutationBenchmarkSuite:
    """Benchmark exploration scaling or mutation latency.

    Runs exploration at N=1, 2, 4, ... workers, measures throughput,
    normalizes to C(1)=1, and fits sigma/kappa.

    Args:
        test_class: A ChaosTest subclass to benchmark.
        target_modules: Modules for coverage tracking.
        max_workers: Cap on worker count (default: CPU count).
        time_per_trial: Seconds per trial at each N.
        seed: Base RNG seed.
        steps_per_run: Steps per exploration run.
        metric: ``"runs"`` (runs/sec) or ``"edges"`` (unique edges/sec).
        mutate_targets: Mutation targets to benchmark in fresh subprocesses.
            When provided, mutation latency benchmarking runs instead of
            explorer scaling analysis.
        repeats: Fresh subprocess runs per mutation target.
        workers: Worker count for mutation benchmarking.
        preset: Mutation preset for mutation benchmarking.
        filter_equivalent: Whether to keep equivalence filtering enabled.
        test_filter: Optional pytest ``-k`` expression for mutation runs.
        python_executable: Python interpreter to use for fresh subprocess trials.
        cwd: Working directory for mutation benchmark subprocesses.

    Returns:
        A :class:`ScalingAnalysis` for exploration benchmarks, or a
        :class:`MutationBenchmarkSuite` when *mutate_targets* is provided.
    """
    if mutate_targets:
        return _benchmark_mutations(
            mutate_targets,
            repeats=repeats,
            workers=workers,
            preset=preset,
            filter_equivalent=filter_equivalent,
            test_filter=test_filter,
            python_executable=python_executable,
            cwd=cwd,
        )
    if test_class is None:
        raise ValueError("test_class is required for exploration benchmarks")

    from ordeal.explore import Explorer

    max_w = max_workers or min(os.cpu_count() or 4, 32)

    measurements: list[tuple[int, float]] = []
    n = 1
    while n <= max_w:
        explorer = Explorer(
            test_class,
            target_modules=target_modules,
            seed=seed,
            workers=n,
            max_checkpoints=128,
        )
        result = explorer.run(max_time=time_per_trial, steps_per_run=steps_per_run)
        elapsed = max(result.duration_seconds, 0.001)

        if metric == "edges":
            throughput = result.unique_edges / elapsed
        else:
            throughput = result.total_runs / elapsed

        measurements.append((n, throughput))
        n *= 2

    # Normalize to C(1) = 1
    baseline = measurements[0][1] if measurements else 1.0
    if baseline <= 0:
        baseline = 1.0
    normalized = [(n, t / baseline) for n, t in measurements]

    return analyze(normalized)
def scales_linearly(
    fn: Callable[..., Any] | None = None,
    *,
    n_range: tuple[int, int] = (1, 8),
    max_kappa: float = 0.01,
    max_sigma: float = 0.3,
    samples: int = 4,
    time_per_sample: float = 2.0,
) -> Callable[..., Any]:
    """Decorator: assert that a function scales linearly with concurrency.

    Runs the function with increasing worker counts, fits the USL model,
    and fails if contention (sigma) or coherence (kappa) exceed thresholds::

        from ordeal.scaling import scales_linearly

        @scales_linearly(n_range=(1, 8), max_kappa=0.01)
        def process_batch(items):
            ...

    Args:
        n_range: ``(min_workers, max_workers)`` to test.
        max_kappa: Fail if coherence exceeds this (quadratic overhead).
        max_sigma: Fail if contention exceeds this (serial bottleneck).
        samples: Number of worker counts to test between min and max.
        time_per_sample: Seconds to run at each worker count.
    """
    import concurrent.futures
    import time as _time

    def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def _test(*args: Any, **kwargs: Any) -> Any:
            lo, hi = n_range
            counts = [max(1, lo + i * (hi - lo) // max(samples - 1, 1)) for i in range(samples)]
            counts = sorted(set(counts))

            measurements: list[tuple[int, float]] = []
            for n in counts:
                start = _time.monotonic()
                completed = 0
                deadline = start + time_per_sample
                with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                    while _time.monotonic() < deadline:
                        futs = [pool.submit(func, *args, **kwargs) for _ in range(n)]
                        concurrent.futures.wait(futs)
                        completed += n
                elapsed = _time.monotonic() - start
                throughput = completed / elapsed if elapsed > 0 else 0
                measurements.append((n, throughput))

            if len(measurements) < 2:
                return func(*args, **kwargs)

            baseline = measurements[0][1] if measurements[0][1] > 0 else 1.0
            normalized = [(n, t / baseline) for n, t in measurements]
            result = analyze(normalized)

            if result.fit_status == "inconclusive":
                raise AssertionError(
                    "scales_linearly: USL fit is inconclusive: "
                    f"{result.fit_reason or 'fit quality could not be established'}"
                )

            if result.kappa > max_kappa:
                raise AssertionError(
                    f"scales_linearly: kappa={result.kappa:.4f} exceeds "
                    f"max_kappa={max_kappa} (quadratic coherence overhead)"
                )
            if result.sigma > max_sigma:
                raise AssertionError(
                    f"scales_linearly: sigma={result.sigma:.4f} exceeds "
                    f"max_sigma={max_sigma} (serial contention bottleneck)"
                )

            return func(*args, **kwargs)

        return _test

    if fn is not None:
        return _decorator(fn)
    return _decorator
