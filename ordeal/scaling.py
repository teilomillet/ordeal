"""Universal Scaling Law (USL) and Amdahl's Law for parallel exploration.

The USL quantifies how throughput changes as workers increase::

    C(N) = N / [1 + sigma*(N-1) + kappa*N*(N-1)]

- **sigma** captures contention — serialized work (locks, shared I/O).
- **kappa** captures coherence — cross-worker sync cost (grows quadratically).
- When kappa=0 this reduces to Amdahl's Law.

Usage::

    from ordeal.scaling import usl, fit_usl, analyze

    # Predict throughput with 8 workers
    c = usl(8, sigma=0.05, kappa=0.002)

    # Fit from benchmark measurements
    sigma, kappa = fit_usl([(1, 1.0), (2, 1.9), (4, 3.4), (8, 5.2)])

    # Full analysis with diagnosis
    analysis = analyze([(1, 1.0), (2, 1.9), (4, 3.4), (8, 5.2)])
    print(analysis.summary())

    # Benchmark the explorer automatically
    from ordeal.scaling import benchmark
    analysis = benchmark(MyServiceChaos, target_modules=["myapp"])
"""

from __future__ import annotations

import functools
import json
import math
import os
import statistics
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10
    import tomli as tomllib  # type: ignore[no-redefine]

# ============================================================================
# Core formulas
# ============================================================================


def usl(n: float, sigma: float, kappa: float) -> float:
    """Universal Scaling Law: ``C(N) = N / [1 + sigma*(N-1) + kappa*N*(N-1)]``.

    Args:
        n: Number of workers/processors.
        sigma: Contention coefficient (0 <= sigma < 1). Fraction of work
            that must be serialized.
        kappa: Coherence coefficient (kappa >= 0). Cost of maintaining
            consistency across workers — grows quadratically with N.

    Returns:
        Relative throughput normalized so that C(1) = 1.
    """
    if n <= 0:
        return 0.0
    return n / (1.0 + sigma * (n - 1) + kappa * n * (n - 1))


def amdahl(n: float, sigma: float) -> float:
    """Amdahl's Law — USL with kappa=0 (no coherence penalty).

    Throughput is bounded by ``1/sigma`` as N grows.
    """
    return usl(n, sigma, kappa=0.0)


def optimal_n(sigma: float, kappa: float) -> float:
    """Worker count at peak throughput: ``N* = sqrt((1 - sigma) / kappa)``.

    Beyond this point, adding workers *decreases* throughput
    (retrograde scaling from coherence costs).

    Returns ``float('inf')`` when kappa=0 (Amdahl regime).
    """
    if kappa <= 0:
        return float("inf")
    return math.sqrt((1.0 - sigma) / kappa)


def peak_throughput(sigma: float, kappa: float) -> float:
    """Maximum achievable throughput ``C(N*)``.

    Returns ``1/sigma`` when kappa=0 (Amdahl asymptote).
    """
    n_star = optimal_n(sigma, kappa)
    if math.isinf(n_star):
        return 1.0 / sigma if sigma > 0 else float("inf")
    return usl(n_star, sigma, kappa)


# ============================================================================
# Fitting
# ============================================================================


def fit_usl(
    measurements: list[tuple[int | float, float]],
) -> tuple[float, float]:
    """Fit sigma and kappa from (N, throughput) pairs via least squares.

    Linearizes the USL equation::

        N/C(N) - 1 = sigma*(N-1) + kappa*N*(N-1)

    Requires at least 3 data points (including N=1).
    Throughput should be normalized so C(1) ~ 1.0.

    Returns:
        ``(sigma, kappa)`` tuple.

    Raises:
        ValueError: If fewer than 3 usable measurements or degenerate data.
    """
    # N=1 gives 0=0, no information — filter it out
    points = [(float(n), float(c)) for n, c in measurements if n > 1 and c > 0]

    if len(points) < 2:
        raise ValueError("Need at least 3 measurements (including N=1) to fit USL")

    # Normal equations for y = sigma*x1 + kappa*x2
    s11 = s12 = s22 = sy1 = sy2 = 0.0
    for n, c in points:
        x1 = n - 1
        x2 = n * (n - 1)
        y = n / c - 1.0
        s11 += x1 * x1
        s12 += x1 * x2
        s22 += x2 * x2
        sy1 += x1 * y
        sy2 += x2 * y

    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-15:
        raise ValueError("Degenerate data — cannot fit USL parameters")

    sigma = max(0.0, min((s22 * sy1 - s12 * sy2) / det, 1.0))
    kappa = max(0.0, (s11 * sy2 - s12 * sy1) / det)
    return sigma, kappa


# ============================================================================
# Analysis
# ============================================================================


@dataclass
class ScalingAnalysis:
    """Results from USL analysis of scaling measurements."""

    sigma: float
    kappa: float
    n_optimal: float
    peak: float
    measurements: list[tuple[float, float]]
    regime: str  # "linear", "amdahl", "usl"

    def summary(self) -> str:
        """Human-readable scaling report with diagnosis."""
        lines = [
            "Scaling Analysis (Universal Scaling Law)",
            f"  sigma (contention):  {self.sigma:.6f}",
            f"  kappa (coherence):   {self.kappa:.6f}",
            f"  Regime:              {self.regime}",
        ]

        if math.isinf(self.n_optimal):
            lines.append(f"  Optimal workers:     unbounded (Amdahl limit: {self.peak:.1f}x)")
        else:
            lines.append(f"  Optimal workers:     {self.n_optimal:.1f}")
            lines.append(f"  Peak throughput:     {self.peak:.2f}x")

        lines.append("")
        lines.append("  Predicted scaling:")
        for n in [1, 2, 4, 8, 16, 32, 64]:
            c = usl(n, self.sigma, self.kappa)
            eff = c / n * 100
            marker = ""
            if not math.isinf(self.n_optimal) and abs(n - self.n_optimal) < n * 0.3:
                marker = " <-- peak"
            lines.append(f"    N={n:3d}: {c:6.2f}x throughput ({eff:5.1f}% efficient){marker}")

        lines.append("")
        lines.append("  Diagnosis:")
        if self.sigma < 0.01 and self.kappa < 0.0001:
            lines.append("    Near-linear scaling. Minimal contention and coherence costs.")
        elif self.kappa < 0.0001:
            lines.append(f"    Amdahl-bounded: {self.sigma:.1%} of work is serialized.")
            lines.append(f"    Scaling ceiling: {1 / self.sigma:.1f}x regardless of worker count.")
        else:
            lines.append(f"    Contention (sigma): {self.sigma:.1%} serialized fraction.")
            lines.append(f"    Coherence (kappa):  {self.kappa:.6f} cross-worker sync cost.")
            if self.n_optimal < 4:
                lines.append("    Coherence dominates — parallelism barely helps.")
                lines.append("    Bottleneck: cross-worker synchronization cost.")
            elif self.n_optimal < 16:
                lines.append("    Moderate scaling — sync frequency limits throughput.")

        return "\n".join(lines)

    def efficiency(self, n: int) -> float:
        """Parallel efficiency at N workers: C(N)/N."""
        return usl(n, self.sigma, self.kappa) / n

    def throughput(self, n: int) -> float:
        """Predicted relative throughput at N workers."""
        return usl(n, self.sigma, self.kappa)


@dataclass
class MutationBenchmarkTrial:
    """One fresh-process mutation benchmark run."""

    seconds: float
    total: int
    killed: int
    score: float
    timings: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class MutationBenchmarkCase:
    """Aggregated timings for one mutation target."""

    target: str
    trials: list[MutationBenchmarkTrial]

    @property
    def median_seconds(self) -> float:
        """Median wall time across fresh subprocess runs."""
        return statistics.median(trial.seconds for trial in self.trials)

    @property
    def phase_medians(self) -> dict[str, float]:
        """Median per-phase wall times across trials."""
        keys: set[str] = set()
        for trial in self.trials:
            keys.update(trial.timings.keys())
        return {
            key: statistics.median(trial.timings.get(key, 0.0) for trial in self.trials)
            for key in sorted(keys)
        }

    @property
    def median_selected_test_files(self) -> int:
        """Median count of heuristic test files handed to pytest."""
        values = [int(trial.diagnostics.get("selected_test_files", 0)) for trial in self.trials]
        return int(statistics.median(values)) if values else 0

    @property
    def median_collected_tests(self) -> int:
        """Median collected pytest item count across trials."""
        values = [int(trial.diagnostics.get("collected_tests", 0)) for trial in self.trials]
        return int(statistics.median(values)) if values else 0

    def summary(self) -> str:
        """Human-readable mutation latency report."""
        first = self.trials[0]
        lines = [
            f"{self.target}",
            (
                f"  Median: {self.median_seconds:.3f}s over {len(self.trials)} run(s), "
                f"score={first.killed}/{first.total} ({first.score:.0%})"
            ),
            (
                f"  Selection: files={self.median_selected_test_files}, "
                f"collected_tests={self.median_collected_tests}"
            ),
        ]
        phases = self.phase_medians
        if phases:
            lines.append("  Phases:")
            for name, seconds in phases.items():
                lines.append(f"    {name}: {seconds:.3f}s")
        return "\n".join(lines)


@dataclass
class MutationBenchmarkSuite:
    """Benchmark results for one or more mutation targets."""

    cases: list[MutationBenchmarkCase]
    repeats: int
    workers: int
    preset: str | None

    def summary(self) -> str:
        """Human-readable summary for the whole mutation suite."""
        lines = [
            "Mutation Benchmark",
            f"  repeats={self.repeats}, workers={self.workers}, preset={self.preset or 'all'}",
        ]
        for case in self.cases:
            lines.append("")
            lines.append(case.summary())
        return "\n".join(lines)


@dataclass(frozen=True)
class PerfContractSpec:
    """One benchmark case from a checked-in perf/quality contract."""

    name: str
    kind: str
    repeats: int = 3
    max_seconds: float | None = None
    module: str | None = None
    target: str | None = None
    mode: str = "cold"
    validation_mode: str = "fast"
    compare_validation_mode: str | None = None
    workers: int = 1
    preset: str | None = "standard"
    test_filter: str | None = None
    filter_equivalent: bool = True
    test_dir: str = "tests"
    max_examples: int = 20
    max_score_gap: float | None = None


@dataclass
class PerfContractCase:
    """Observed timings and quality metrics for one contract case."""

    spec: PerfContractSpec
    seconds: list[float]
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def median_seconds(self) -> float:
        """Median wall time for the case."""
        return statistics.median(self.seconds)

    @property
    def max_seconds(self) -> float | None:
        """Configured upper bound for this case."""
        return self.spec.max_seconds

    @property
    def score_gap(self) -> float | None:
        """Observed score gap for differential audit cases."""
        gap = self.details.get("score_gap")
        return float(gap) if gap is not None else None

    @property
    def passed(self) -> bool:
        """Whether the observed median stayed within the configured budget."""
        if self.max_seconds is not None and self.median_seconds > self.max_seconds:
            return False
        if self.spec.max_score_gap is not None:
            gap = self.score_gap
            if gap is None or gap > self.spec.max_score_gap:
                return False
        return True

    def summary(self) -> str:
        """Human-readable one-line summary."""
        budget = "no budget"
        if self.max_seconds is not None:
            budget = f"budget={self.max_seconds:.3f}s"
        status = "PASS" if self.passed else "FAIL"
        extras: list[str] = []
        if self.spec.kind == "audit":
            extras.append(f"mode={self.spec.mode}")
            extras.append(f"validation={self.spec.validation_mode}")
        if self.spec.kind == "audit_compare":
            primary_score = self.details.get("primary_score")
            reference_score = self.details.get("reference_score")
            if primary_score is not None and reference_score is not None:
                extras.append(
                    f"{self.spec.validation_mode}={float(primary_score):.0%}"
                )
                extras.append(
                    f"{self.spec.compare_validation_mode or 'deep'}={float(reference_score):.0%}"
                )
            if self.score_gap is not None:
                extras.append(f"gap={self.score_gap:.0%}")
            if self.spec.max_score_gap is not None:
                extras.append(f"gap_budget={self.spec.max_score_gap:.0%}")
        if self.spec.kind == "mutate":
            score = self.details.get("score")
            if score is not None:
                extras.append(f"score={float(score):.0%}")
        extra_text = f" ({', '.join(extras)})" if extras else ""
        return (
            f"{status} {self.spec.name}: median={self.median_seconds:.3f}s "
            f"over {len(self.seconds)} run(s), {budget}{extra_text}"
        )


@dataclass
class PerfContractSuite:
    """Results for a checked-in perf/quality contract."""

    cases: list[PerfContractCase]
    contract_path: str

    @property
    def passed(self) -> bool:
        """True when every benchmark case stayed within budget."""
        return all(case.passed for case in self.cases)

    @property
    def failures(self) -> list[PerfContractCase]:
        """Cases that exceeded their configured budget."""
        return [case for case in self.cases if not case.passed]

    def summary(self) -> str:
        """Human-readable summary for the entire contract."""
        status = "PASS" if self.passed else "FAIL"
        lines = [f"Performance Contract [{status}]", f"  file={self.contract_path}"]
        for case in self.cases:
            lines.append(f"  {case.summary()}")
        return "\n".join(lines)


def analyze(measurements: list[tuple[int | float, float]]) -> ScalingAnalysis:
    """Fit USL from (N, throughput) measurements and return full analysis.

    Args:
        measurements: List of ``(worker_count, throughput)`` pairs.
            Throughput should be normalized so C(1) = 1.0.
    """
    sigma, kappa = fit_usl(measurements)
    n_star = optimal_n(sigma, kappa)
    peak = peak_throughput(sigma, kappa)

    if sigma < 0.01 and kappa < 0.0001:
        regime = "linear"
    elif kappa < 0.0001:
        regime = "amdahl"
    else:
        regime = "usl"

    return ScalingAnalysis(
        sigma=sigma,
        kappa=kappa,
        n_optimal=n_star,
        peak=peak,
        measurements=[(float(n), float(c)) for n, c in measurements],
        regime=regime,
    )


# ============================================================================
# Benchmark
# ============================================================================


_MUTATION_BENCHMARK_MARKER = "__ORDEAL_MUTATION_BENCH__ "


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
    """Run one fresh-process fast-vs-deep audit comparison."""
    _clear_audit_artifacts(module, cwd)
    script = textwrap.dedent(
        f"""
        import json
        import time

        from ordeal.audit import audit

        started = time.perf_counter()
        primary = audit(
            {module!r},
            test_dir={test_dir!r},
            max_examples={max_examples},
            workers={workers},
            validation_mode={validation_mode!r},
        )
        middle = time.perf_counter()
        reference = audit(
            {module!r},
            test_dir={test_dir!r},
            max_examples={max_examples},
            workers={workers},
            validation_mode={compare_validation_mode!r},
        )
        ended = time.perf_counter()
        payload = {{
            "seconds": ended - started,
            "primary_seconds": middle - started,
            "reference_seconds": ended - middle,
            "primary_score": primary.mutation_score_fraction,
            "reference_score": reference.mutation_score_fraction,
            "primary_mutation_score": primary.mutation_score,
            "reference_mutation_score": reference.mutation_score,
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

        spec = PerfContractSpec(
            name=name,
            kind=kind,
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
        )

        if spec.repeats <= 0:
            raise ValueError(f"Case {name!r} must have repeats >= 1")
        if spec.max_seconds is not None and spec.max_seconds <= 0:
            raise ValueError(f"Case {name!r} must have max_seconds > 0")
        if spec.max_score_gap is not None and not (0 <= spec.max_score_gap <= 1):
            raise ValueError(f"Case {name!r} must have max_score_gap between 0 and 1")
        if spec.validation_mode not in {"fast", "deep"}:
            raise ValueError(f"Case {name!r} has unsupported validation_mode {spec.validation_mode!r}")
        if (
            spec.compare_validation_mode is not None
            and spec.compare_validation_mode not in {"fast", "deep"}
        ):
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


def benchmark_perf_contract(
    contract_path: str,
    *,
    python_executable: str | None = None,
    cwd: str | None = None,
) -> PerfContractSuite:
    """Run a checked-in perf/quality contract and return the observed medians."""
    executable = python_executable or sys.executable
    workdir = cwd or os.getcwd()
    cases: list[PerfContractCase] = []

    for spec in _parse_perf_contract(contract_path):
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
    samples: int = 3,
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
