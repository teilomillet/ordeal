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
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


def benchmark(
    test_class: type,
    *,
    target_modules: list[str] | None = None,
    max_workers: int | None = None,
    time_per_trial: float = 10.0,
    seed: int = 42,
    steps_per_run: int = 50,
    metric: str = "runs",
) -> ScalingAnalysis:
    """Benchmark exploration scaling and fit USL parameters.

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

    Returns:
        A :class:`ScalingAnalysis` with fitted sigma and kappa.
    """
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
