from __future__ import annotations
# ruff: noqa
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
    """Fit non-negative sigma and kappa from (N, throughput) pairs.

    Linearizes the USL equation::

        N/C(N) - 1 = sigma*(N-1) + kappa*N*(N-1)

    The constrained least-squares optimum is selected from the feasible
    interior and the ``sigma=0``, ``sigma=1``, and ``kappa=0`` boundaries.
    Boundary coefficients are refitted instead of independently clamping an
    unconstrained solution.

    Requires at least 3 data points (including N=1).
    Throughput should be normalized so C(1) ~ 1.0.

    Returns:
        ``(sigma, kappa)`` tuple.

    Raises:
        ValueError: If fewer than 3 usable measurements or degenerate data.
    """
    fit = _fit_usl(measurements)
    return fit.sigma, fit.kappa
@dataclass(frozen=True)
class _USLFit:
    """Internal constrained USL fit in the linearized response space."""

    sigma: float
    kappa: float
    points: list[tuple[float, float, float, float]]
    residual_sum_squares: float
    sigma_ci: tuple[float, float] | None
    kappa_ci: tuple[float, float] | None
def _fit_usl(measurements: list[tuple[int | float, float]]) -> _USLFit:
    """Return the exact two-variable box-constrained least-squares fit."""
    points: list[tuple[float, float, float, float]] = []
    for raw_n, raw_c in measurements:
        n, c = float(raw_n), float(raw_c)
        if n <= 1 or c <= 0 or not (math.isfinite(n) and math.isfinite(c)):
            continue
        x1 = n - 1.0
        x2 = n * (n - 1.0)
        points.append((n, x1, x2, n / c - 1.0))

    if len(points) < 2:
        raise ValueError("Need at least 3 measurements (including N=1) to fit USL")

    # Normal equations for y = sigma*x1 + kappa*x2
    s11 = s12 = s22 = sy1 = sy2 = 0.0
    for _n, x1, x2, y in points:
        s11 += x1 * x1
        s12 += x1 * x2
        s22 += x2 * x2
        sy1 += x1 * y
        sy2 += x2 * y

    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-15:
        raise ValueError("Degenerate data — cannot fit USL parameters")

    def objective(sigma: float, kappa: float) -> float:
        return sum((y - sigma * x1 - kappa * x2) ** 2 for _n, x1, x2, y in points)

    candidates = [
        (max(0.0, min(sy1 / s11, 1.0)), 0.0),
        (0.0, max(0.0, sy2 / s22)),
        (1.0, max(0.0, (sy2 - s12) / s22)),
    ]
    unconstrained = (
        (s22 * sy1 - s12 * sy2) / det,
        (s11 * sy2 - s12 * sy1) / det,
    )
    if 0.0 <= unconstrained[0] <= 1.0 and unconstrained[1] >= 0.0:
        candidates.append(unconstrained)

    sigma, kappa = min(candidates, key=lambda candidate: objective(*candidate))
    residual_sum_squares = objective(sigma, kappa)
    sigma_ci, kappa_ci = _coefficient_confidence_intervals(
        points,
        residual_sum_squares=residual_sum_squares,
        s11=s11,
        s22=s22,
        determinant=det,
        sigma=sigma,
        kappa=kappa,
    )
    return _USLFit(
        sigma=sigma,
        kappa=kappa,
        points=points,
        residual_sum_squares=residual_sum_squares,
        sigma_ci=sigma_ci,
        kappa_ci=kappa_ci,
    )
_T_975 = (
    float("inf"), 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365,
    2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
    2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069,
    2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)
def _coefficient_confidence_intervals(
    points: list[tuple[float, float, float, float]],
    *,
    residual_sum_squares: float,
    s11: float,
    s22: float,
    determinant: float,
    sigma: float,
    kappa: float,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Approximate 95% Wald intervals, truncated to the fit constraints."""
    degrees_of_freedom = len(points) - 2
    if degrees_of_freedom <= 0:
        return None, None

    residual_variance = residual_sum_squares / degrees_of_freedom
    sigma_error = math.sqrt(max(0.0, residual_variance * s22 / determinant))
    kappa_error = math.sqrt(max(0.0, residual_variance * s11 / determinant))
    critical = _T_975[min(degrees_of_freedom, len(_T_975) - 1)]
    sigma_margin = critical * sigma_error
    kappa_margin = critical * kappa_error
    return (
        (max(0.0, sigma - sigma_margin), min(1.0, sigma + sigma_margin)),
        (max(0.0, kappa - kappa_margin), kappa + kappa_margin),
    )
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
    regime: str  # "linear", "amdahl", "usl", or "inconclusive"
    residuals: list[tuple[float, float]] = field(default_factory=list)
    r_squared: float = float("nan")
    rmse: float = float("nan")
    max_relative_error: float = float("nan")
    sigma_ci: tuple[float, float] | None = None
    kappa_ci: tuple[float, float] | None = None
    fit_status: str = "inconclusive"
    fit_reason: str | None = None

    def summary(self) -> str:
        """Human-readable scaling report with diagnosis."""
        lines = [
            "Scaling Analysis (Universal Scaling Law)",
            f"  sigma (contention):  {self.sigma:.6f}",
            f"  kappa (coherence):   {self.kappa:.6f}",
            f"  Regime:              {self.regime}",
            f"  Fit status:          {self.fit_status.upper()}",
        ]

        if self.sigma_ci is None or self.kappa_ci is None:
            lines.append("  Approx. 95% CIs:     unavailable (need more worker counts)")
        else:
            lines.append(
                f"  Approx. sigma 95% CI: [{self.sigma_ci[0]:.6f}, {self.sigma_ci[1]:.6f}]"
            )
            lines.append(
                f"  Approx. kappa 95% CI: [{self.kappa_ci[0]:.6f}, {self.kappa_ci[1]:.6f}]"
            )
        lines.extend(
            [
                f"  R^2 (throughput):    {self.r_squared:.4f}",
                f"  RMSE (throughput):   {self.rmse:.4f}x",
                f"  Max rel. residual:   {self.max_relative_error:.1%}",
            ]
        )
        if self.fit_reason:
            lines.append(f"  Fit note:            {self.fit_reason}")

        if self.fit_status == "inconclusive":
            lines.append("  Optimal workers:     inconclusive")
        elif math.isinf(self.n_optimal):
            lines.append(f"  Optimal workers:     unbounded (Amdahl limit: {self.peak:.1f}x)")
        else:
            lines.append(f"  Optimal workers:     {self.n_optimal:.1f}")
            lines.append(f"  Peak throughput:     {self.peak:.2f}x")

        lines.append("")
        lines.append("  Observed vs fitted scaling:")
        for n, observed in self.measurements:
            fitted = usl(n, self.sigma, self.kappa)
            observed_efficiency = observed / n * 100 if n > 0 else 0.0
            fitted_efficiency = fitted / n * 100 if n > 0 else 0.0
            residual = observed - fitted
            marker = ""
            if (
                self.fit_status == "conclusive"
                and not math.isinf(self.n_optimal)
                and abs(n - self.n_optimal) < n * 0.3
            ):
                marker = " <-- peak"
            lines.append(
                f"    N={n:3.0f}: observed {observed:6.2f}x "
                f"({observed_efficiency:5.1f}% efficient), "
                f"fitted {fitted:6.2f}x ({fitted_efficiency:5.1f}% efficient), "
                f"residual {residual:+.2f}x{marker}"
            )

        lines.append("")
        lines.append("  Diagnosis:")
        if self.fit_status == "inconclusive":
            lines.append(
                "    Fit is inconclusive; use the observed measurements, not USL projections."
            )
        elif self.sigma < 0.01 and self.kappa < 0.0001:
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
    tier: str = "pr"
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
    min_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view of the contract case specification."""
        return {
            "name": self.name,
            "kind": self.kind,
            "tier": self.tier,
            "repeats": self.repeats,
            "max_seconds": self.max_seconds,
            "module": self.module,
            "target": self.target,
            "mode": self.mode,
            "validation_mode": self.validation_mode,
            "compare_validation_mode": self.compare_validation_mode,
            "workers": self.workers,
            "preset": self.preset,
            "test_filter": self.test_filter,
            "filter_equivalent": self.filter_equivalent,
            "test_dir": self.test_dir,
            "max_examples": self.max_examples,
            "max_score_gap": self.max_score_gap,
            "min_score": self.min_score,
        }
