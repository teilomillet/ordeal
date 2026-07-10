from __future__ import annotations
# ruff: noqa
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
    def observed_score(self) -> float | None:
        """Return the quality score governed by ``min_score``."""
        key = "primary_score" if self.spec.kind == "audit_compare" else "score"
        score = self.details.get(key)
        return float(score) if score is not None else None

    @property
    def passed(self) -> bool:
        """Whether the observed median stayed within the configured budget."""
        if self.max_seconds is not None and self.median_seconds > self.max_seconds:
            return False
        if self.spec.max_score_gap is not None:
            gap = self.score_gap
            if gap is None or gap > self.spec.max_score_gap:
                return False
        if self.spec.min_score is not None:
            score = self.observed_score
            if score is None or score < self.spec.min_score:
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
                extras.append(f"{self.spec.validation_mode}={float(primary_score):.0%}")
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
        if self.spec.min_score is not None:
            extras.append(f"score_floor={self.spec.min_score:.0%}")
        extra_text = f" ({', '.join(extras)})" if extras else ""
        return (
            f"{status} {self.spec.name}: median={self.median_seconds:.3f}s "
            f"over {len(self.seconds)} run(s), {budget}{extra_text}"
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view of one observed contract case."""
        return {
            "name": self.spec.name,
            "kind": self.spec.kind,
            "passed": self.passed,
            "seconds": [float(second) for second in self.seconds],
            "median_seconds": self.median_seconds,
            "budget_seconds": self.max_seconds,
            "score_gap": self.score_gap,
            "observed_score": self.observed_score,
            "details": dict(self.details),
            "spec": self.spec.to_dict(),
            "summary": self.summary(),
        }
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

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view of the whole contract run."""
        return {
            "contract_path": self.contract_path,
            "passed": self.passed,
            "case_count": len(self.cases),
            "failure_count": len(self.failures),
            "failures": [case.spec.name for case in self.failures],
            "cases": [case.to_dict() for case in self.cases],
            "summary": self.summary(),
        }

    def to_json(self) -> str:
        """Stable JSON encoding for artifact and trend storage."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)
def analyze(measurements: list[tuple[int | float, float]]) -> ScalingAnalysis:
    """Fit USL from (N, throughput) measurements and return full analysis.

    Args:
        measurements: List of ``(worker_count, throughput)`` pairs.
            Throughput should be normalized so C(1) = 1.0.
    """
    fit = _fit_usl(measurements)
    sigma, kappa = fit.sigma, fit.kappa
    n_star = optimal_n(sigma, kappa)
    peak = peak_throughput(sigma, kappa)

    normalized_measurements = [(float(n), float(c)) for n, c in measurements]
    informative = [(n, c) for n, c in normalized_measurements if n > 1 and c > 0]
    residuals = [(n, c - usl(n, sigma, kappa)) for n, c in normalized_measurements]
    informative_residuals = [c - usl(n, sigma, kappa) for n, c in informative]
    residual_sum_squares = sum(residual * residual for residual in informative_residuals)
    rmse = math.sqrt(residual_sum_squares / len(informative))
    observed_mean = statistics.fmean(c for _n, c in informative)
    total_sum_squares = sum((c - observed_mean) ** 2 for _n, c in informative)
    if total_sum_squares <= 1e-15:
        r_squared = 1.0 if residual_sum_squares <= 1e-15 else 0.0
    else:
        r_squared = 1.0 - residual_sum_squares / total_sum_squares
    max_relative_error = max(
        abs(residual) / c for residual, (_n, c) in zip(informative_residuals, informative)
    )

    fit_reasons: list[str] = []
    if len(informative) < 3:
        fit_reasons.append("need at least three informative worker counts to assess fit quality")
    if r_squared < 0.90:
        fit_reasons.append(f"R^2 {r_squared:.3f} is below 0.900")
    if max_relative_error > 0.20:
        fit_reasons.append(f"maximum relative residual {max_relative_error:.1%} exceeds 20.0%")
    fit_status = "inconclusive" if fit_reasons else "conclusive"

    if fit_status == "inconclusive":
        regime = "inconclusive"
    elif sigma < 0.01 and kappa < 0.0001:
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
        measurements=normalized_measurements,
        regime=regime,
        residuals=residuals,
        r_squared=r_squared,
        rmse=rmse,
        max_relative_error=max_relative_error,
        sigma_ci=fit.sigma_ci,
        kappa_ci=fit.kappa_ci,
        fit_status=fit_status,
        fit_reason="; ".join(fit_reasons) or None,
    )
# ============================================================================
# Benchmark
# ============================================================================


_MUTATION_BENCHMARK_MARKER = "__ORDEAL_MUTATION_BENCH__ "
