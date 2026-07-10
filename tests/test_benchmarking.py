"""Tests for bug benchmark manifests and scoring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ordeal.benchmarking as benchmarking


def test_parse_bug_benchmark_manifest_supports_defaults_and_bugsinpy_metadata(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "bugs.toml"
    manifest.write_text(
        """
[defaults]
dataset = "bugsinpy"
protocol = "scan"
mode = "candidate"
max_examples = 12
compile_checkout = false
requires_python = ">=3.12"
oracle_python_version = "3.8.1"
oracle_source = "upstream failing tests"
evidence_level = "benchmark_curated"
saturation_risk = "public"
allowed_for_optimization = false

[[cases]]
name = "youtube_dl_2"
project = "youtube-dl"
bug_id = "2"
module = "youtube_dl.extractor.common"
expected_targets = ["youtube_dl.extractor.common.InfoExtractor.extract"]
selection_reason = "Public anchor case for reproducible bug discovery reporting."

[[cases]]
name = "private_case"
dataset = "rolling"
tier = "private"
workspace = "work/private-case"
module = "pkg.mod"
targets = ["Runner.run"]
expected_targets = ["pkg.mod.Runner.run"]
selection_reason = "Recent holdout bug reserved for tuning and nightly tracking."
oracle_source = "fixed commit replay plus local failing regression"
evidence_level = "local_replay"
saturation_risk = "private"
allowed_for_optimization = true
""",
        encoding="utf-8",
    )

    specs = benchmarking.parse_bug_benchmark_manifest(str(manifest))

    assert len(specs) == 2
    assert specs[0].dataset == "bugsinpy"
    assert specs[0].project == "youtube-dl"
    assert specs[0].bug_id == "2"
    assert specs[0].workspace is None
    assert specs[0].max_examples == 12
    assert specs[0].compile_checkout is False
    assert specs[0].requires_python == ">=3.12"
    assert specs[0].oracle_python_version == "3.8.1"
    assert specs[0].expected_outcome == "bug"
    assert specs[0].selection_reason.startswith("Public anchor case")
    assert specs[0].allowed_for_optimization is False
    assert specs[1].dataset == "rolling"
    assert specs[1].tier == "private"
    assert specs[1].workspace == "work/private-case"
    assert specs[1].targets == ("Runner.run",)
    assert specs[1].saturation_risk == "private"
    assert specs[1].allowed_for_optimization is True


def test_parse_bug_benchmark_manifest_rejects_public_optimization_case(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "invalid.toml"
    manifest.write_text(
        """
[[cases]]
name = "bad_public_case"
dataset = "bugsinpy"
tier = "public"
project = "youtube-dl"
bug_id = "2"
module = "youtube_dl.extractor.common"
expected_targets = ["youtube_dl.extractor.common.InfoExtractor.extract"]
selection_reason = "Invalid because it would be used for tuning."
oracle_source = "upstream failing tests"
evidence_level = "benchmark_curated"
saturation_risk = "public"
allowed_for_optimization = true
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allowed_for_optimization"):
        benchmarking.parse_bug_benchmark_manifest(str(manifest))


@pytest.mark.parametrize(
    ("version_fields", "message"),
    [
        ('requires_python = "not-a-specifier"', "invalid requires_python"),
        (
            'requires_python = ">=3.12"\npython_version = "3.12.0"',
            "cannot define both",
        ),
    ],
)
def test_parse_bug_benchmark_manifest_validates_runner_versions(
    tmp_path: Path,
    version_fields: str,
    message: str,
) -> None:
    manifest = tmp_path / "invalid-version.toml"
    manifest.write_text(
        f"""
[[cases]]
name = "bad_version"
workspace = "."
module = "pkg.mod"
expected_targets = ["pkg.mod.run"]
selection_reason = "Unit test for invalid runner version metadata."
oracle_source = "synthetic"
evidence_level = "manual_review"
{version_fields}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        benchmarking.parse_bug_benchmark_manifest(str(manifest))


def test_run_bug_benchmark_case_scores_hit_from_expected_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "case"
    workspace.mkdir()
    spec = benchmarking.BugBenchmarkSpec(
        name="demo_case",
        module="pkg.mod",
        workspace=str(workspace),
        selection_reason="Unit test case for hit scoring.",
        oracle_source="synthetic CLI payload",
        evidence_level="manual_review",
        saturation_risk="private",
        allowed_for_optimization=True,
        expected_targets=("pkg.mod.decode",),
        max_examples=5,
        mode="candidate",
    )

    payload = {
        "status": "findings",
        "summary": "findings found",
        "findings": [
            {
                "kind": "crash",
                "summary": "decode crashed",
                "target": "pkg.mod.decode",
                "details": {"module": "pkg.mod", "function": "decode"},
            }
        ],
        "artifacts": [{"kind": "report", "uri": ".ordeal/findings/pkg/mod.md"}],
    }

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(benchmarking.subprocess, "run", fake_run)

    result = benchmarking.run_bug_benchmark_case(spec, workspace=str(workspace))

    assert result.status == "hit"
    assert result.hit is True
    assert result.matched_targets == ("pkg.mod.decode",)
    assert len(result.artifacts) == 1
    assert result.exit_code == 1


def test_run_bug_benchmark_case_rejects_wrong_crash_on_the_right_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "case"
    workspace.mkdir()
    expected_kwargs = {"request_headers": {"X-Ordeal": None}}
    spec = benchmarking.BugBenchmarkSpec(
        name="exact_oracle",
        module="pkg.mod",
        workspace=str(workspace),
        expected_targets=("pkg.mod.update_headers",),
        expected_error_type="AttributeError",
        expected_error_message="'NoneType' object has no attribute 'decode'",
        expected_witness_sha256=benchmarking._sha256_payload(expected_kwargs),
        selection_reason="Reject unrelated failures on the same callable.",
        oracle_source="synthetic exact oracle",
        evidence_level="executable",
    )
    payload = {
        "status": "findings",
        "findings": [
            {
                "kind": "crash",
                "target": "pkg.mod.update_headers",
                "details": {
                    "error": "different failure",
                    "failing_args": expected_kwargs,
                    "proof_bundle": {"failing_path": {"error_type": "ValueError"}},
                },
            }
        ],
    }
    monkeypatch.setattr(
        benchmarking.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = benchmarking.run_bug_benchmark_case(spec, workspace=str(workspace))

    assert result.status == "miss"
    assert result.matched_targets == ()


@pytest.mark.parametrize(
    ("findings", "expected_status", "classification"),
    [
        ([], "correct_rejection", "true_negative"),
        (
            [
                {
                    "kind": "crash",
                    "target": "pkg.fixed.decode",
                    "details": {"module": "pkg.fixed", "function": "decode"},
                }
            ],
            "false_positive",
            "false_positive",
        ),
    ],
)
def test_run_bug_benchmark_case_scores_clean_controls(
    monkeypatch,
    tmp_path: Path,
    findings: list[dict[str, object]],
    expected_status: str,
    classification: str,
) -> None:
    workspace = tmp_path / "case"
    workspace.mkdir()
    spec = benchmarking.BugBenchmarkSpec(
        name="fixed_control",
        module="pkg.fixed",
        workspace=str(workspace),
        expected_outcome="clean",
        pair_id="demo-pair",
        selection_reason="Unit test fixed sibling.",
        oracle_source="synthetic fixed sibling",
        evidence_level="manual_review",
        expected_targets=("pkg.fixed.decode",),
    )
    monkeypatch.setattr(
        benchmarking.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1 if findings else 0,
            stdout=json.dumps({"status": "findings" if findings else "ok", "findings": findings}),
            stderr="",
        ),
    )

    result = benchmarking.run_bug_benchmark_case(spec, workspace=str(workspace))

    assert result.status == expected_status
    assert result.classification == classification
    assert result.classification_correct is (expected_status == "correct_rejection")


def test_run_bug_benchmark_case_uses_workspace_site_packages_and_pythonpath_for_bugsinpy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "case"
    site_packages = workspace / "env" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (workspace / "bugsinpy_bug.info").write_text(
        'pythonpath="build/lib/;src/"\n',
        encoding="utf-8",
    )
    spec = benchmarking.BugBenchmarkSpec(
        name="bugsinpy_case",
        module="pkg.mod",
        dataset="bugsinpy",
        workspace=str(workspace),
        selection_reason="Unit test case for workspace python selection.",
        oracle_source="synthetic CLI payload",
        evidence_level="manual_review",
        saturation_risk="public",
        allowed_for_optimization=False,
        expected_targets=("pkg.mod.decode",),
    )

    calls: dict[str, object] = {}

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["env"] = kwargs["env"]
        calls["cwd"] = kwargs["cwd"]
        return SimpleNamespace(
            returncode=0,
            stdout='{"status": "ok", "findings": [], "artifacts": []}',
            stderr="",
        )

    monkeypatch.setattr(benchmarking.subprocess, "run", fake_run)

    result = benchmarking.run_bug_benchmark_case(
        spec,
        workspace=str(workspace),
        python_executable="/usr/bin/python3",
        ordeal_root=str(tmp_path / "ordeal-root"),
    )

    command = calls["command"]
    assert command[0] == "/usr/bin/python3"
    assert calls["cwd"] == str(workspace.resolve())
    pythonpath = calls["env"]["PYTHONPATH"]
    assert str((tmp_path / "ordeal-root").resolve()) in pythonpath
    assert str(site_packages.resolve()) in pythonpath
    assert str((workspace / "build/lib").resolve()) in pythonpath
    assert str((workspace / "src").resolve()) in pythonpath
    assert result.status == "miss"


def test_run_bug_benchmark_case_empty_stdout_is_error(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "case"
    workspace.mkdir()
    spec = benchmarking.BugBenchmarkSpec(
        name="bad_case",
        module="pkg.mod",
        workspace=str(workspace),
        selection_reason="Unit test case for invalid benchmark output.",
        oracle_source="synthetic CLI payload",
        evidence_level="manual_review",
        saturation_risk="private",
        allowed_for_optimization=True,
        expected_targets=("pkg.mod.decode",),
    )

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="module import failed",
        )

    monkeypatch.setattr(benchmarking.subprocess, "run", fake_run)

    result = benchmarking.run_bug_benchmark_case(spec, workspace=str(workspace))

    assert result.status == "error"
    assert result.summary == "benchmark command did not emit JSON output"
    assert result.error == "module import failed"


def test_run_bug_benchmark_case_blocks_on_python_version_mismatch(tmp_path: Path) -> None:
    workspace = tmp_path / "case"
    workspace.mkdir()
    spec = benchmarking.BugBenchmarkSpec(
        name="versioned_case",
        module="pkg.mod",
        workspace=str(workspace),
        selection_reason="Unit test case for Python version gating.",
        oracle_source="synthetic CLI payload",
        evidence_level="manual_review",
        saturation_risk="public",
        allowed_for_optimization=False,
        python_version="3.8.1",
        expected_targets=("pkg.mod.decode",),
    )

    result = benchmarking.run_bug_benchmark_case(spec, workspace=str(workspace))

    assert result.status == "blocked"
    assert result.summary == "benchmark case requires a different Python version"
    assert "requires Python 3.8.1" in (result.error or "")


def test_run_bug_benchmark_case_checks_selected_interpreter_version(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "case"
    workspace.mkdir()
    spec = benchmarking.BugBenchmarkSpec(
        name="version_range_case",
        module="pkg.mod",
        workspace=str(workspace),
        selection_reason="Unit test for selected interpreter gating.",
        oracle_source="synthetic CLI payload",
        evidence_level="manual_review",
        requires_python=">=3.12",
        expected_targets=("pkg.mod.decode",),
    )
    calls: list[str] = []

    def fake_interpreter_version(executable: str) -> str:
        calls.append(executable)
        return "3.11.9"

    monkeypatch.setattr(benchmarking, "_interpreter_version", fake_interpreter_version)

    result = benchmarking.run_bug_benchmark_case(
        spec,
        workspace=str(workspace),
        python_executable="/custom/python",
    )

    assert calls == ["/custom/python"]
    assert result.status == "blocked"
    assert "requires Python >=3.12" in (result.error or "")


def test_benchmark_bug_manifest_filters_tier(monkeypatch, tmp_path: Path) -> None:
    public_workspace = tmp_path / "public"
    private_workspace = tmp_path / "private"
    public_workspace.mkdir()
    private_workspace.mkdir()
    manifest = tmp_path / "suite.toml"
    manifest.write_text(
        f"""
[defaults]
oracle_source = "curated regression replay"
evidence_level = "benchmark_curated"

[[cases]]
name = "public_case"
tier = "public"
workspace = "{public_workspace}"
module = "pkg.public"
expected_targets = ["pkg.public.issue"]
selection_reason = "Public anchor case for comparability."
saturation_risk = "public"
allowed_for_optimization = false

[[cases]]
name = "private_case"
tier = "private"
workspace = "{private_workspace}"
module = "pkg.private"
expected_targets = ["pkg.private.issue"]
selection_reason = "Private holdout case for optimization tracking."
saturation_risk = "private"
allowed_for_optimization = true
""",
        encoding="utf-8",
    )

    def fake_run_case(spec, **kwargs):
        return benchmarking.BugBenchmarkCaseResult(
            spec=spec,
            status="hit",
            seconds=0.25,
            exit_code=1,
            summary=f"matched {spec.name}",
            workspace=spec.workspace or "",
            command=("python", "-m", "ordeal.cli"),
            matched_targets=spec.expected_targets,
            findings=(),
            artifacts=(),
            raw_result={},
        )

    monkeypatch.setattr(benchmarking, "run_bug_benchmark_case", fake_run_case)

    suite = benchmarking.benchmark_bug_manifest(str(manifest), tier="private")

    assert suite.case_count == 1
    assert suite.cases[0].spec.name == "private_case"
    assert suite.hit_count == 1
    assert suite.selected_tier == "private"


def test_find_bugsinpy_checkout_root_returns_nested_project_root(tmp_path: Path) -> None:
    outer = tmp_path / "workspace"
    project = outer / "PySnooper"
    project.mkdir(parents=True)
    (project / "bugsinpy_bug.info").write_text("", encoding="utf-8")

    resolved = benchmarking._find_bugsinpy_checkout_root(outer)

    assert resolved == project


@pytest.mark.parametrize("case_name", ["../victim", "nested/victim", r"nested\victim"])
def test_prepare_bugsinpy_workspace_rejects_path_like_case_names(
    case_name: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    spec = benchmarking.BugBenchmarkSpec(
        name=case_name,
        module="pkg.mod",
        dataset="bugsinpy",
        project="demo",
        bug_id="1",
    )
    monkeypatch.setattr(
        benchmarking,
        "_find_bugsinpy_executable",
        lambda *args, **kwargs: pytest.fail("unsafe name must fail before executable lookup"),
    )

    with pytest.raises(ValueError, match="Unsafe BugsInPy case name"):
        benchmarking._prepare_bugsinpy_workspace(
            spec,
            bugsinpy_root=None,
            checkout_root=str(tmp_path / "checkouts"),
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_prepare_bugsinpy_workspace_rejects_absolute_case_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spec = benchmarking.BugBenchmarkSpec(
        name=str(tmp_path / "victim"),
        module="pkg.mod",
        dataset="bugsinpy",
        project="demo",
        bug_id="1",
    )
    monkeypatch.setattr(
        benchmarking,
        "_find_bugsinpy_executable",
        lambda *args, **kwargs: pytest.fail("unsafe name must fail before executable lookup"),
    )

    with pytest.raises(ValueError, match="Unsafe BugsInPy case name"):
        benchmarking._prepare_bugsinpy_workspace(
            spec,
            bugsinpy_root=None,
            checkout_root=str(tmp_path / "checkouts"),
        )


def test_prepare_bugsinpy_workspace_skips_compiler_when_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spec = benchmarking.BugBenchmarkSpec(
        name="demo-1",
        module="pkg.mod",
        dataset="bugsinpy",
        project="demo",
        bug_id="1",
        compile_checkout=False,
    )
    executable_lookups: list[str] = []

    def fake_find(name: str, **kwargs) -> str:
        executable_lookups.append(name)
        if name == "bugsinpy-compile":
            pytest.fail("compile executable must not be resolved")
        return name

    def fake_run(command, **kwargs):
        workspace = Path(command[command.index("-w") + 1])
        workspace.mkdir(parents=True)
        (workspace / "bugsinpy_bug.info").write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(benchmarking, "_find_bugsinpy_executable", fake_find)
    monkeypatch.setattr(benchmarking.subprocess, "run", fake_run)

    workspace = benchmarking._prepare_bugsinpy_workspace(
        spec,
        bugsinpy_root=None,
        checkout_root=str(tmp_path / "checkouts"),
    )

    assert workspace == (tmp_path / "checkouts" / "demo-1").resolve()
    assert executable_lookups == ["bugsinpy-checkout"]


def test_bug_benchmark_suite_json_contains_hit_counts() -> None:
    spec = benchmarking.BugBenchmarkSpec(
        name="demo_case",
        module="pkg.mod",
        workspace="workspace",
        selection_reason="Unit test case for suite JSON.",
        oracle_source="synthetic CLI payload",
        evidence_level="manual_review",
        saturation_risk="private",
        allowed_for_optimization=True,
        expected_targets=("pkg.mod.issue",),
    )
    suite = benchmarking.BugBenchmarkSuite(
        cases=(
            benchmarking.BugBenchmarkCaseResult(
                spec=spec,
                status="hit",
                seconds=0.4,
                exit_code=1,
                summary="matched 1 target(s)",
                workspace="workspace",
                command=("python", "-m", "ordeal.cli"),
                matched_targets=("pkg.mod.issue",),
                findings=(),
                artifacts=(),
                raw_result={},
            ),
        ),
        manifest_path="benchmarks/bugs.toml",
        selected_tier="public",
    )

    payload = json.loads(suite.to_json())

    assert payload["passed"] is True
    assert payload["hit_count"] == 1
    assert payload["hit_rate"] == pytest.approx(1.0)
    assert payload["epistemics"]["private_case_count"] == 1
    assert payload["epistemics"]["optimization_case_count"] == 1
    assert payload["cases"][0]["status"] == "hit"


def test_certification_policy_rejects_invalid_confidence(tmp_path: Path) -> None:
    manifest = tmp_path / "invalid.toml"
    manifest.write_text(
        """
[certification]
confidence_level = 1.0

[[cases]]
name = "case"
workspace = "."
module = "pkg.mod"
expected_targets = ["pkg.mod.run"]
selection_reason = "Invalid policy test."
oracle_source = "synthetic"
evidence_level = "manual_review"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="confidence_level"):
        benchmarking.parse_bug_benchmark_manifest(str(manifest))


def test_certificate_fails_closed_without_enough_controls() -> None:
    spec = benchmarking.BugBenchmarkSpec(
        name="positive_only",
        module="pkg.mod",
        workspace="workspace",
        expected_outcome="bug",
        pair_id="demo",
        selection_reason="Positive-only test case.",
        oracle_source="synthetic",
        oracle_url="https://example.com/abcdef1",
        evidence_level="manual_review",
        fix_commit="abcdef1",
        failure_command="pytest -q",
        expected_targets=("pkg.mod.issue",),
    )
    suite = benchmarking.BugBenchmarkSuite(
        cases=(
            benchmarking.BugBenchmarkCaseResult(
                spec=spec,
                status="hit",
                seconds=0.1,
                exit_code=1,
                summary="matched",
                workspace="workspace",
                command=("python",),
            ),
        ),
        manifest_path="manifest.toml",
        manifest_sha256="0" * 64,
        certification_policy=benchmarking.BugBenchmarkCertificationPolicy(enabled=True),
    )

    assert suite.passed is True
    assert suite.certified is False
    assert suite.check_passed is False
    assert any(
        "negative cases" in reason
        for reason in suite.certification_assessment()["failure_reasons"]
    )


def _fully_scored_certificate_suite(
    evidence: dict[str, object] | None,
) -> benchmarking.BugBenchmarkSuite:
    """Return one perfect bug/control pair for certificate-boundary tests."""
    common = {
        "project": "demo",
        "bug_id": "1",
        "pair_id": "demo-1",
        "evidence_path": "evidence/demo-1.toml" if evidence is not None else None,
        "selection_reason": "Certificate evidence boundary test.",
        "oracle_source": "upstream regression",
        "oracle_url": "https://example.test/commit/abcdef1",
        "evidence_level": "executable",
        "fix_commit": "abcdef1",
        "failure_command": "pytest -q",
    }
    bug = benchmarking.BugBenchmarkSpec(
        name="bug",
        module="pkg.buggy",
        expected_outcome="bug",
        expected_targets=("pkg.buggy.run",),
        **common,
    )
    control = benchmarking.BugBenchmarkSpec(
        name="control",
        module="pkg.fixed",
        expected_outcome="clean",
        expected_targets=("pkg.fixed.run",),
        **common,
    )
    return benchmarking.BugBenchmarkSuite(
        cases=(
            benchmarking.BugBenchmarkCaseResult(
                spec=bug,
                status="hit",
                seconds=0.1,
                exit_code=1,
                summary="matched",
                workspace="workspace",
                command=("python",),
                evidence_verification=evidence,
            ),
            benchmarking.BugBenchmarkCaseResult(
                spec=control,
                status="correct_rejection",
                seconds=0.1,
                exit_code=0,
                summary="clean",
                workspace="workspace",
                command=("python",),
                evidence_verification=evidence,
            ),
        ),
        manifest_path="manifest.toml",
        manifest_sha256="0" * 64,
        certification_policy=benchmarking.BugBenchmarkCertificationPolicy(enabled=True),
    )


def test_certificate_fails_closed_without_executable_evidence() -> None:
    suite = _fully_scored_certificate_suite(None)

    assert suite.passed is True
    assert suite.certified is False
    assert suite.check_passed is False
    assert any(
        "no linked evidence record" in reason
        for reason in suite.certification_assessment()["failure_reasons"]
    )


def test_certificate_requires_online_sources_when_evidence_declares_them() -> None:
    evidence: dict[str, object] = {
        "local_verified": True,
        "sources_verified": False,
        "verified": False,
        "online_sources_required": True,
        "manifest_binding": {"passed": True},
    }
    suite = _fully_scored_certificate_suite(evidence)

    assert suite.certified is False
    reasons = suite.certification_assessment()["failure_reasons"]
    assert any("not fully verified" in reason for reason in reasons)
    assert any("authoritative online source" in reason for reason in reasons)


def test_certificate_and_artifact_require_fully_verified_bound_evidence() -> None:
    evidence: dict[str, object] = {
        "local_verified": True,
        "sources_verified": True,
        "verified": True,
        "online_sources_required": True,
        "manifest_binding": {"passed": True},
    }
    suite = _fully_scored_certificate_suite(evidence)

    assert suite.certified is True
    payload = json.loads(suite.to_json())
    payload.pop("certificate")
    assert benchmarking._artifact_certification_is_earned(payload) is True

    payload["cases"][0]["evidence_verification"]["manifest_binding"]["passed"] = False
    assert benchmarking._artifact_certification_is_earned(payload) is False


def test_checked_in_public_manifest_parses() -> None:
    manifest = Path(__file__).resolve().parent.parent / "benchmarks" / "bug-benchmark.public.toml"

    specs = benchmarking.parse_bug_benchmark_manifest(str(manifest))

    policy = benchmarking.parse_bug_benchmark_certification_policy(str(manifest))

    assert len(specs) == 6
    assert policy.enabled is False
    assert policy.min_confidence_bound == pytest.approx(0.40)
    assert all(spec.dataset == "bugsinpy-reproduction" for spec in specs)
    assert all(spec.requires_python == ">=3.12" for spec in specs)
    assert all(spec.oracle_python_version for spec in specs)
    assert all(spec.allowed_for_optimization is False for spec in specs)
    httpie_bug = next(spec for spec in specs if spec.name == "httpie_3_reproduction")
    httpie_control = next(spec for spec in specs if spec.name == "httpie_3_fixed_control")
    pysnooper_bug = next(spec for spec in specs if spec.name == "pysnooper_3_reproduction")
    pysnooper_control = next(spec for spec in specs if spec.name == "pysnooper_3_fixed_control")
    tornado_bug = next(spec for spec in specs if spec.name == "tornado_14_reproduction")
    tornado_control = next(spec for spec in specs if spec.name == "tornado_14_fixed_control")
    assert httpie_bug.evidence_path == "evidence/httpie-3.toml"
    assert httpie_control.evidence_path == "evidence/httpie-3.toml"
    assert httpie_bug.expected_error_type == "AttributeError"
    assert httpie_bug.expected_witness_sha256 == (
        "bdd0c82f4c058188fce09292f1530bfa3665fb9b544189251ec6c3942a65d296"
    )
    assert pysnooper_bug.evidence_path == "evidence/pysnooper-3.toml"
    assert pysnooper_control.evidence_path == "evidence/pysnooper-3.toml"
    assert pysnooper_bug.expected_error_type == "NameError"
    assert pysnooper_bug.expected_witness_sha256 == (
        "aa8a92b273e3465d2712da546a934467fc4de7a635d79465989775853efc993d"
    )
    assert tornado_bug.evidence_path == "evidence/tornado-14.toml"
    assert tornado_control.evidence_path == "evidence/tornado-14.toml"
    assert tornado_bug.expected_error_type == "RuntimeError"
    assert tornado_bug.expected_witness_sha256 == (
        "c49dbffe64d4cfcab1f753a0c6fade4928fef28f66bda199d3845e900dfa6972"
    )
    assert sorted(spec.expected_outcome for spec in specs) == [
        "bug",
        "bug",
        "bug",
        "clean",
        "clean",
        "clean",
    ]


def test_checked_in_public_manifest_executes_without_blocks() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = root / "benchmarks" / "bug-benchmark.public.toml"

    suite = benchmarking.benchmark_bug_manifest(
        str(manifest),
        ordeal_root=str(root),
        tier="public",
    )

    assert suite.passed is True
    assert suite.case_count == 6
    assert suite.hit_count == 3
    assert suite.correct_rejection_count == 3
    assert suite.false_positive_count == 0
    assert suite.blocked_count == 0
    assert suite.recall == pytest.approx(1.0)
    assert suite.precision == pytest.approx(1.0)
    assert suite.specificity == pytest.approx(1.0)
    assert suite.certified is False
    assert suite.check_passed is True


def test_checked_in_public_cases_embed_verified_local_evidence() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = root / "benchmarks" / "bug-benchmark.public.toml"
    suite = benchmarking.benchmark_bug_manifest(
        str(manifest),
        ordeal_root=str(root),
        tier="public",
    )
    assert len(suite.cases) == 6
    for case in suite.cases:
        verification = case.evidence_verification
        assert verification is not None
        assert verification["local_verified"] is True
        assert verification["sources_verified"] is False
        assert verification["manifest_binding"]["passed"] is True
        assert verification["backing_values"]["oracle"]["buggy"]["matching_replays"] == 5
        assert verification["backing_values"]["oracle"]["fixed"]["matching_replays"] == 5
        assert verification["backing_values"]["oracle"]["requires_python"] == ">=3.12"
    assert "certificate" not in json.loads(suite.to_json())


def test_manifest_blocks_case_when_linked_evidence_binding_disagrees(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "fixtures"
    workspace.mkdir()
    manifest = tmp_path / "manifest.toml"
    witness_sha256 = "0" * 64
    manifest.write_text(
        f'''
[[cases]]
name = "bound_case"
workspace = "{workspace}"
module = "pkg.buggy"
project = "demo"
bug_id = "1"
pair_id = "demo-1"
fix_commit = "abcdef1"
evidence_path = "evidence.toml"
expected_targets = ["pkg.buggy.run"]
expected_error_type = "ValueError"
expected_error_message = "boom"
expected_witness_sha256 = "{witness_sha256}"
selection_reason = "Binding failure test."
oracle_source = "synthetic"
evidence_level = "executable"
''',
        encoding="utf-8",
    )

    class _Verification:
        def to_dict(self) -> dict[str, object]:
            return {
                "evidence_id": "different-pair",
                "local_verified": True,
                "verified": True,
                "backing_values": {
                    "upstream": {
                        "project": "demo",
                        "bug_id": "1",
                        "fixed_commit": "abcdef1",
                    },
                    "oracle": {
                        "kwargs_sha256": "0" * 64,
                        "buggy": {
                            "module": "pkg.buggy",
                            "callable": "run",
                            "expected": {
                                "exception_type": "ValueError",
                                "exception_message": "boom",
                            },
                        },
                    },
                },
            }

    monkeypatch.setattr(
        "ordeal.evidence.verify_bug_evidence",
        lambda *args, **kwargs: _Verification(),
    )
    monkeypatch.setattr(
        benchmarking,
        "run_bug_benchmark_case",
        lambda *args, **kwargs: pytest.fail("scan must not run with an invalid binding"),
    )

    suite = benchmarking.benchmark_bug_manifest(str(manifest))

    assert suite.blocked_count == 1
    assert suite.cases[0].summary == "benchmark evidence could not be verified"
    assert "evidence_id" in (suite.cases[0].error or "")


def test_checked_in_private_template_parses() -> None:
    manifest = (
        Path(__file__).resolve().parent.parent
        / "benchmarks"
        / "bug-benchmark.private.template.toml"
    )

    specs = benchmarking.parse_bug_benchmark_manifest(str(manifest))

    assert len(specs) == 2
    assert all(spec.tier == "private" for spec in specs)
    assert all(spec.allowed_for_optimization is True for spec in specs)
    assert {spec.expected_outcome for spec in specs} == {"bug", "clean"}
