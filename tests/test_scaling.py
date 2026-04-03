"""Tests for ordeal.scaling — USL and Amdahl models."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

import ordeal.scaling as scaling
from ordeal.scaling import (
    amdahl,
    analyze,
    benchmark,
    fit_usl,
    optimal_n,
    peak_throughput,
    usl,
)

# ============================================================================
# usl() formula
# ============================================================================


class TestUSL:
    def test_single_worker_is_baseline(self):
        assert usl(1, sigma=0.1, kappa=0.01) == 1.0

    def test_zero_workers(self):
        assert usl(0, sigma=0.1, kappa=0.01) == 0.0

    def test_linear_scaling(self):
        """sigma=0, kappa=0 -> C(N)=N."""
        for n in [1, 2, 4, 8, 16, 64]:
            assert usl(n, sigma=0, kappa=0) == pytest.approx(n)

    def test_amdahl_regime(self):
        """kappa=0 -> Amdahl's Law."""
        sigma = 0.1
        for n in [2, 4, 8]:
            expected = n / (1 + sigma * (n - 1))
            assert usl(n, sigma=sigma, kappa=0) == pytest.approx(expected)

    def test_retrograde_throughput(self):
        """High kappa -> throughput decreases at large N."""
        c4 = usl(4, sigma=0.05, kappa=0.02)
        c64 = usl(64, sigma=0.05, kappa=0.02)
        assert c64 < c4, "Should show retrograde throughput"

    def test_symmetry_with_amdahl_function(self):
        for n in [1, 2, 4, 8, 16]:
            assert usl(n, sigma=0.1, kappa=0) == pytest.approx(amdahl(n, sigma=0.1))


# ============================================================================
# amdahl()
# ============================================================================


class TestAmdahl:
    def test_asymptote(self):
        """Amdahl's Law asymptote is 1/sigma."""
        sigma = 0.05
        c_large = amdahl(10_000, sigma)
        assert c_large == pytest.approx(1 / sigma, rel=0.01)

    def test_half_serial(self):
        """50% serial -> max 2x speedup."""
        c_large = amdahl(10_000, sigma=0.5)
        assert c_large == pytest.approx(2.0, rel=0.01)


# ============================================================================
# optimal_n()
# ============================================================================


class TestOptimalN:
    def test_formula(self):
        sigma, kappa = 0.05, 0.002
        expected = math.sqrt((1 - sigma) / kappa)
        assert optimal_n(sigma, kappa) == pytest.approx(expected)

    def test_no_coherence_is_infinite(self):
        assert math.isinf(optimal_n(sigma=0.1, kappa=0))

    def test_peak_is_actual_maximum(self):
        """C(N*) should be >= C(N) for all N."""
        sigma, kappa = 0.05, 0.002
        n_star = optimal_n(sigma, kappa)
        c_star = usl(n_star, sigma, kappa)
        for n in [1, 2, 4, 8, 16, 32, 64, 128, 256]:
            assert usl(n, sigma, kappa) <= c_star + 1e-9


# ============================================================================
# peak_throughput()
# ============================================================================


class TestPeakThroughput:
    def test_amdahl_peak(self):
        assert peak_throughput(sigma=0.1, kappa=0) == pytest.approx(10.0)

    def test_usl_peak(self):
        sigma, kappa = 0.05, 0.002
        n_star = optimal_n(sigma, kappa)
        assert peak_throughput(sigma, kappa) == pytest.approx(usl(n_star, sigma, kappa))


# ============================================================================
# fit_usl()
# ============================================================================


class TestFitUSL:
    def test_recover_known_parameters(self):
        """Generate data from known sigma/kappa, fit should recover them."""
        true_sigma, true_kappa = 0.08, 0.003
        data = [(n, usl(n, true_sigma, true_kappa)) for n in [1, 2, 4, 8, 16, 32]]
        sigma, kappa = fit_usl(data)
        assert sigma == pytest.approx(true_sigma, abs=1e-6)
        assert kappa == pytest.approx(true_kappa, abs=1e-6)

    def test_recover_amdahl(self):
        """When data follows Amdahl (no kappa), kappa should be ~0."""
        true_sigma = 0.1
        data = [(n, amdahl(n, true_sigma)) for n in [1, 2, 4, 8, 16]]
        sigma, kappa = fit_usl(data)
        assert sigma == pytest.approx(true_sigma, abs=1e-4)
        assert kappa == pytest.approx(0.0, abs=1e-6)

    def test_recover_linear(self):
        """Perfect linear scaling -> sigma~0, kappa~0."""
        data = [(n, float(n)) for n in [1, 2, 4, 8]]
        sigma, kappa = fit_usl(data)
        assert sigma == pytest.approx(0.0, abs=1e-6)
        assert kappa == pytest.approx(0.0, abs=1e-6)

    def test_too_few_points_raises(self):
        with pytest.raises(ValueError, match="at least 3"):
            fit_usl([(1, 1.0), (2, 1.8)])

    def test_only_n1_raises(self):
        with pytest.raises(ValueError, match="at least 3"):
            fit_usl([(1, 1.0)])


# ============================================================================
# analyze()
# ============================================================================


class TestAnalyze:
    def test_linear_regime(self):
        data = [(n, float(n)) for n in [1, 2, 4, 8]]
        result = analyze(data)
        assert result.regime == "linear"
        assert result.sigma < 0.01
        assert result.kappa < 0.0001

    def test_amdahl_regime(self):
        sigma = 0.15
        data = [(n, amdahl(n, sigma)) for n in [1, 2, 4, 8, 16]]
        result = analyze(data)
        assert result.regime == "amdahl"
        assert result.sigma == pytest.approx(sigma, abs=0.01)

    def test_usl_regime(self):
        sigma, kappa = 0.05, 0.005
        data = [(n, usl(n, sigma, kappa)) for n in [1, 2, 4, 8, 16, 32]]
        result = analyze(data)
        assert result.regime == "usl"
        assert result.n_optimal < float("inf")

    def test_summary_runs(self):
        data = [(n, usl(n, 0.05, 0.002)) for n in [1, 2, 4, 8, 16]]
        result = analyze(data)
        text = result.summary()
        assert "sigma" in text
        assert "kappa" in text
        assert "Predicted scaling" in text

    def test_efficiency(self):
        data = [(n, usl(n, 0.1, 0.001)) for n in [1, 2, 4, 8, 16]]
        result = analyze(data)
        # Efficiency at N=1 is always 100%
        assert result.efficiency(1) == pytest.approx(1.0)
        # Efficiency decreases with N
        assert result.efficiency(8) < result.efficiency(2)

    def test_throughput(self):
        sigma, kappa = 0.05, 0.002
        data = [(n, usl(n, sigma, kappa)) for n in [1, 2, 4, 8, 16]]
        result = analyze(data)
        assert result.throughput(4) == pytest.approx(usl(4, sigma, kappa))


class TestMutationBenchmark:
    def test_benchmark_mutations_aggregates_trials(self, monkeypatch):
        payloads = iter(
            [
                {
                    "seconds": 0.30,
                    "total": 4,
                    "killed": 3,
                    "score": 0.75,
                    "timings": {"generate_seconds": 0.01, "pytest_seconds": 0.26},
                    "diagnostics": {"selected_test_files": 2, "collected_tests": 5},
                },
                {
                    "seconds": 0.28,
                    "total": 4,
                    "killed": 3,
                    "score": 0.75,
                    "timings": {"generate_seconds": 0.01, "pytest_seconds": 0.24},
                    "diagnostics": {"selected_test_files": 2, "collected_tests": 5},
                },
                {
                    "seconds": 0.34,
                    "total": 4,
                    "killed": 3,
                    "score": 0.75,
                    "timings": {"generate_seconds": 0.02, "pytest_seconds": 0.29},
                    "diagnostics": {"selected_test_files": 3, "collected_tests": 6},
                },
            ]
        )

        def fake_run(args, cwd, text, capture_output, check):
            payload = next(payloads)
            return SimpleNamespace(
                stdout="noise\n"
                + scaling._MUTATION_BENCHMARK_MARKER
                + json.dumps(payload)
                + "\n",
                stderr="",
                returncode=0,
            )

        monkeypatch.setattr(scaling.subprocess, "run", fake_run)

        suite = benchmark(
            mutate_targets=["pkg.mod.compute"],
            repeats=3,
            workers=2,
            preset="essential",
            cwd="/tmp",
        )

        case = suite.cases[0]
        assert suite.repeats == 3
        assert case.median_seconds == pytest.approx(0.30)
        assert case.phase_medians["pytest_seconds"] == pytest.approx(0.26)
        assert case.median_selected_test_files == 2
        assert case.median_collected_tests == 5

    def test_benchmark_mutations_summary(self, monkeypatch):
        payload = {
            "seconds": 0.25,
            "total": 2,
            "killed": 2,
            "score": 1.0,
            "timings": {"total_seconds": 0.25, "pytest_seconds": 0.20},
            "diagnostics": {"selected_test_files": 1, "collected_tests": 2},
        }

        def fake_run(args, cwd, text, capture_output, check):
            return SimpleNamespace(
                stdout=scaling._MUTATION_BENCHMARK_MARKER + json.dumps(payload) + "\n",
                stderr="",
                returncode=0,
            )

        monkeypatch.setattr(scaling.subprocess, "run", fake_run)

        suite = benchmark(mutate_targets=["pkg.mod.compute"], repeats=1, cwd="/tmp")
        text = suite.summary()
        assert "Mutation Benchmark" in text
        assert "pkg.mod.compute" in text
        assert "pytest_seconds" in text
