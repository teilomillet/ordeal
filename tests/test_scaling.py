"""Tests for ordeal.scaling — USL and Amdahl models."""

from __future__ import annotations

import json
import math
from pathlib import Path
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
                stdout="noise\n" + scaling._MUTATION_BENCHMARK_MARKER + json.dumps(payload) + "\n",
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


class TestPerfContract:
    def test_parse_perf_contract(self, tmp_path: Path):
        contract = tmp_path / "perf.toml"
        contract.write_text(
            """
[[cases]]
name = "import_cli"
kind = "import"
module = "ordeal.cli"
repeats = 5
max_seconds = 0.08

[[cases]]
name = "audit_demo"
kind = "audit"
module = "ordeal.demo"
mode = "warm"
repeats = 2
validation_mode = "deep"
max_seconds = 0.30

[[cases]]
name = "audit_demo_compare"
kind = "audit_compare"
module = "ordeal.demo"
repeats = 1
max_score_gap = 0.10

[[cases]]
name = "mutate_demo"
kind = "mutate"
target = "pkg.mod.compute"
repeats = 3
workers = 2
max_seconds = 0.25
"""
        )

        specs = scaling._parse_perf_contract(str(contract))

        assert [spec.name for spec in specs] == [
            "import_cli",
            "audit_demo",
            "audit_demo_compare",
            "mutate_demo",
        ]
        assert specs[1].mode == "warm"
        assert specs[1].validation_mode == "deep"
        assert specs[2].kind == "audit_compare"
        assert specs[2].compare_validation_mode == "deep"
        assert specs[2].max_score_gap == pytest.approx(0.10)
        assert specs[3].target == "pkg.mod.compute"
        assert specs[3].workers == 2

    def test_benchmark_perf_contract_aggregates_cases(self, monkeypatch, tmp_path: Path):
        contract = tmp_path / "perf.toml"
        contract.write_text(
            """
[[cases]]
name = "import_cli"
kind = "import"
module = "ordeal.cli"
repeats = 3
max_seconds = 0.05

[[cases]]
name = "audit_demo_cold"
kind = "audit"
module = "ordeal.demo"
mode = "cold"
repeats = 2
max_seconds = 2.0

[[cases]]
name = "mutate_demo"
kind = "mutate"
target = "pkg.mod.compute"
repeats = 2
max_seconds = 0.25

[[cases]]
name = "audit_demo_compare"
kind = "audit_compare"
module = "ordeal.demo"
repeats = 2
max_score_gap = 0.10
"""
        )

        import_times = iter([0.02, 0.03, 0.025])
        audit_times = iter([1.5, 1.6])
        differential_trials = iter(
            [
                {
                    "seconds": 0.80,
                    "primary_seconds": 0.30,
                    "reference_seconds": 0.50,
                    "primary_score": 0.80,
                    "reference_score": 0.85,
                    "primary_mutation_score": "8/10 (80%)",
                    "reference_mutation_score": "17/20 (85%)",
                },
                {
                    "seconds": 0.90,
                    "primary_seconds": 0.35,
                    "reference_seconds": 0.55,
                    "primary_score": 0.82,
                    "reference_score": 0.84,
                    "primary_mutation_score": "9/11 (82%)",
                    "reference_mutation_score": "21/25 (84%)",
                },
            ]
        )
        mutation_trials = iter(
            [
                scaling.MutationBenchmarkTrial(0.10, total=2, killed=2, score=1.0),
                scaling.MutationBenchmarkTrial(0.12, total=2, killed=2, score=1.0),
            ]
        )

        monkeypatch.setattr(
            scaling,
            "_run_import_benchmark_trial",
            lambda *args, **kwargs: next(import_times),
        )
        monkeypatch.setattr(
            scaling,
            "_run_audit_benchmark_trial",
            lambda *args, **kwargs: next(audit_times),
        )
        monkeypatch.setattr(
            scaling,
            "_run_mutation_benchmark_trial",
            lambda *args, **kwargs: next(mutation_trials),
        )
        monkeypatch.setattr(
            scaling,
            "_run_audit_differential_trial",
            lambda *args, **kwargs: next(differential_trials),
        )

        suite = scaling.benchmark_perf_contract(
            str(contract),
            python_executable="python",
            cwd=str(tmp_path),
        )

        assert suite.passed
        assert [case.spec.name for case in suite.cases] == [
            "import_cli",
            "audit_demo_cold",
            "mutate_demo",
            "audit_demo_compare",
        ]
        assert suite.cases[0].median_seconds == pytest.approx(0.025)
        assert suite.cases[1].median_seconds == pytest.approx(1.55)
        assert suite.cases[2].details["score"] == pytest.approx(1.0)
        assert suite.cases[3].details["primary_score"] == pytest.approx(0.81)
        assert suite.cases[3].details["reference_score"] == pytest.approx(0.845)
        assert suite.cases[3].score_gap == pytest.approx(0.035)

    def test_perf_contract_summary_reports_failure(self):
        case = scaling.PerfContractCase(
            spec=scaling.PerfContractSpec(
                name="import_cli",
                kind="import",
                module="ordeal.cli",
                max_seconds=0.05,
            ),
            seconds=[0.07, 0.08, 0.09],
        )
        suite = scaling.PerfContractSuite(cases=[case], contract_path="ordeal.perf.toml")

        assert not suite.passed
        assert suite.failures == [case]
        assert "Performance Contract [FAIL]" in suite.summary()

    def test_perf_contract_suite_json_contains_case_status_and_failures(self):
        case = scaling.PerfContractCase(
            spec=scaling.PerfContractSpec(
                name="audit_demo_compare",
                kind="audit_compare",
                module="ordeal.demo",
                validation_mode="fast",
                compare_validation_mode="deep",
                max_score_gap=0.05,
            ),
            seconds=[0.80, 0.85],
            details={
                "primary_score": 0.70,
                "reference_score": 0.85,
                "score_gap": 0.15,
            },
        )
        suite = scaling.PerfContractSuite(cases=[case], contract_path="ordeal.perf.toml")

        payload = json.loads(suite.to_json())

        assert payload["passed"] is False
        assert payload["failure_count"] == 1
        assert payload["failures"] == ["audit_demo_compare"]
        assert payload["cases"][0]["passed"] is False
        assert payload["cases"][0]["score_gap"] == pytest.approx(0.15)
        assert payload["cases"][0]["spec"]["validation_mode"] == "fast"

    def test_perf_contract_fails_on_audit_score_gap(self):
        case = scaling.PerfContractCase(
            spec=scaling.PerfContractSpec(
                name="audit_demo_compare",
                kind="audit_compare",
                module="ordeal.demo",
                validation_mode="fast",
                compare_validation_mode="deep",
                max_score_gap=0.05,
            ),
            seconds=[0.80],
            details={
                "primary_score": 0.70,
                "reference_score": 0.85,
                "score_gap": 0.15,
            },
        )

        assert not case.passed
        assert "gap=15%" in case.summary()
        assert "gap_budget=5%" in case.summary()

    def test_parse_perf_contract_tier_field(self, tmp_path: Path):
        contract = tmp_path / "perf.toml"
        contract.write_text(
            """
[[cases]]
name = "fast_check"
kind = "import"
module = "ordeal.cli"
max_seconds = 0.1

[[cases]]
name = "slow_compare"
kind = "audit_compare"
tier = "nightly"
module = "ordeal.demo"
repeats = 1
max_score_gap = 0.10
"""
        )

        specs = scaling._parse_perf_contract(str(contract))
        assert specs[0].tier == "pr"
        assert specs[1].tier == "nightly"

    def test_benchmark_perf_contract_tier_filter(self, monkeypatch, tmp_path: Path):
        contract = tmp_path / "perf.toml"
        contract.write_text(
            """
[[cases]]
name = "pr_import"
kind = "import"
module = "ordeal.cli"
max_seconds = 1.0

[[cases]]
name = "nightly_compare"
kind = "audit_compare"
tier = "nightly"
module = "ordeal.demo"
repeats = 1
max_score_gap = 0.10
"""
        )

        monkeypatch.setattr(
            scaling,
            "_run_import_benchmark_trial",
            lambda *args, **kwargs: 0.01,
        )

        suite = scaling.benchmark_perf_contract(str(contract), tier="pr")
        assert len(suite.cases) == 1
        assert suite.cases[0].spec.name == "pr_import"

    def test_suite_to_json(self):
        import json

        case = scaling.PerfContractCase(
            spec=scaling.PerfContractSpec(
                name="test_case",
                kind="import",
                module="ordeal.cli",
                max_seconds=0.1,
            ),
            seconds=[0.05, 0.06],
        )
        suite = scaling.PerfContractSuite(cases=[case], contract_path="test.toml")
        data = json.loads(suite.to_json())
        assert data["passed"] is True
        assert len(data["cases"]) == 1
        assert data["cases"][0]["name"] == "test_case"
        assert data["cases"][0]["spec"]["tier"] == "pr"
        assert "seconds" in data["cases"][0]
