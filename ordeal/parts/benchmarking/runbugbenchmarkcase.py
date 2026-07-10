from __future__ import annotations
# ruff: noqa
def run_bug_benchmark_case(
    spec: BugBenchmarkSpec,
    *,
    workspace: str,
    python_executable: str | None = None,
    ordeal_root: str | None = None,
) -> BugBenchmarkCaseResult:
    """Run one benchmark case against the real `ordeal scan --json` surface."""
    workspace_path = Path(workspace).resolve()
    executable = python_executable or sys.executable
    required_python = _resolve_required_python_version(spec, workspace_path=workspace_path)
    requirement_spec = spec
    if required_python and not spec.requires_python and not spec.python_version:
        requirement_spec = replace(spec, python_version=required_python)
    mismatch = None
    if requirement_spec.requires_python or requirement_spec.python_version:
        try:
            actual_python = _interpreter_version(executable)
            mismatch = _python_requirement_error(requirement_spec, actual_python)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
            mismatch = f"could not inspect benchmark interpreter {executable!r}: {exc}"
    if mismatch:
        return BugBenchmarkCaseResult(
            spec=spec,
            status="blocked",
            seconds=0.0,
            exit_code=1,
            summary="benchmark case requires a different Python version",
            workspace=str(workspace_path),
            command=(executable, "-m", "ordeal.cli", *_scan_command(spec)),
            findings=(),
            artifacts=(),
            raw_result={},
            error=mismatch,
        )
    command = [executable, "-m", "ordeal.cli", *_scan_command(spec)]
    env = dict(os.environ)
    pythonpath_entries: list[str] = []
    if ordeal_root:
        pythonpath_entries.append(str(Path(ordeal_root).resolve()))
    if spec.dataset == "bugsinpy":
        pythonpath_entries.extend(_workspace_site_packages(workspace_path))
    pythonpath_entries.extend(_resolve_pythonpath_entries(spec, workspace_path=workspace_path))
    if pythonpath_entries:
        current = env.get("PYTHONPATH", "")
        prefix = os.pathsep.join(pythonpath_entries)
        env["PYTHONPATH"] = prefix if not current else f"{prefix}{os.pathsep}{current}"

    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=str(workspace_path),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started

    raw_result: dict[str, Any] = {}
    if not proc.stdout.strip():
        return BugBenchmarkCaseResult(
            spec=spec,
            status="error",
            seconds=elapsed,
            exit_code=proc.returncode,
            summary="benchmark command did not emit JSON output",
            workspace=str(workspace_path),
            command=tuple(command),
            findings=(),
            artifacts=(),
            raw_result={},
            error=proc.stderr.strip() or None,
        )
    try:
        raw_result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return BugBenchmarkCaseResult(
            spec=spec,
            status="error",
            seconds=elapsed,
            exit_code=proc.returncode,
            summary="benchmark command did not emit valid JSON",
            workspace=str(workspace_path),
            command=tuple(command),
            findings=(),
            artifacts=(),
            raw_result={},
            error=f"{exc}: {proc.stdout[:400]}",
        )

    findings = list(raw_result.get("findings", []))
    artifacts = list(raw_result.get("artifacts", []))
    oracle_findings = [
        finding for finding in findings if _finding_matches_expected_evidence(finding, spec=spec)
    ]
    matched_targets = _matching_targets(
        oracle_findings,
        expected_targets=spec.expected_targets,
    )
    has_exact_oracle = any(
        (
            spec.expected_error_type,
            spec.expected_error_message,
            spec.expected_witness_sha256,
        )
    )
    matched_files = (
        _matching_files(raw_result, expected_files=spec.expected_files)
        if oracle_findings or not has_exact_oracle
        else ()
    )
    if raw_result.get("status") == "blocked":
        status = "blocked"
        summary = str(raw_result.get("summary", "")).strip() or "benchmark case was blocked"
    elif proc.returncode not in {0, 1}:
        status = "error"
        summary = "benchmark command failed before producing a scored result"
    elif matched_targets or matched_files:
        status = "hit" if spec.expected_outcome == "bug" else "false_positive"
        summary = (
            f"matched {len(matched_targets)} target(s) and {len(matched_files)} file(s) "
            f"across {len(findings)} finding(s)"
        )
    else:
        status = "miss" if spec.expected_outcome == "bug" else "correct_rejection"
        summary = f"no scoped targets matched across {len(findings)} finding(s)"

    error = None
    if status in {"blocked", "error"}:
        error = str(raw_result.get("blocking_reason") or proc.stderr or "").strip() or None

    return BugBenchmarkCaseResult(
        spec=spec,
        status=status,
        seconds=elapsed,
        exit_code=proc.returncode,
        summary=summary,
        workspace=str(workspace_path),
        command=tuple(command),
        matched_targets=matched_targets,
        matched_files=matched_files,
        findings=tuple(dict(item) for item in findings),
        artifacts=tuple(dict(item) for item in artifacts),
        raw_result=raw_result,
        error=error,
    )
def _evidence_binding_checks(
    spec: BugBenchmarkSpec,
    verification: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return exact manifest-to-evidence binding checks for one case."""
    backing = verification.get("backing_values", {})
    upstream = backing.get("upstream", {}) if isinstance(backing, dict) else {}
    oracle = backing.get("oracle", {}) if isinstance(backing, dict) else {}
    revision = "buggy" if spec.expected_outcome == "bug" else "fixed"
    revision_oracle = oracle.get(revision, {}) if isinstance(oracle, dict) else {}
    expected_observation = (
        revision_oracle.get("expected", {}) if isinstance(revision_oracle, dict) else {}
    )
    callable_name = (
        str(revision_oracle.get("callable", "")) if isinstance(revision_oracle, dict) else ""
    )
    expected_target = f"{spec.module}.{callable_name}" if callable_name else ""
    values: list[tuple[str, Any, Any]] = [
        ("evidence_id", spec.pair_id, verification.get("evidence_id")),
        ("project", spec.project, upstream.get("project")),
        ("bug_id", spec.bug_id, upstream.get("bug_id")),
        ("fixed_commit", spec.fix_commit, upstream.get("fixed_commit")),
        ("module", spec.module, revision_oracle.get("module")),
        (
            "expected_target",
            expected_target,
            expected_target if expected_target in spec.expected_targets else None,
        ),
    ]
    if spec.expected_outcome == "bug":
        values.extend(
            [
                (
                    "expected_error_type",
                    spec.expected_error_type,
                    expected_observation.get("exception_type"),
                ),
                (
                    "expected_error_message",
                    spec.expected_error_message,
                    expected_observation.get("exception_message"),
                ),
                (
                    "expected_witness_sha256",
                    spec.expected_witness_sha256,
                    oracle.get("kwargs_sha256"),
                ),
            ]
        )
    checks: list[dict[str, Any]] = []
    for name, expected, actual in values:
        checks.append(
            {
                "name": name,
                "passed": expected is not None and expected == actual,
                "expected": expected,
                "actual": actual,
                "category": "manifest_binding",
            }
        )
    return checks
def benchmark_bug_manifest(
    manifest_path: str,
    *,
    python_executable: str | None = None,
    ordeal_root: str | None = None,
    tier: str | None = None,
    bugsinpy_root: str | None = None,
    checkout_root: str | None = None,
    online_sources: bool = False,
) -> BugBenchmarkSuite:
    """Run one benchmark manifest and return the scored suite."""
    from ordeal.evidence import verify_bug_evidence

    specs = list(parse_bug_benchmark_manifest(manifest_path))
    certification_policy = parse_bug_benchmark_certification_policy(manifest_path)
    if tier is not None:
        specs = [spec for spec in specs if spec.tier == tier]

    cases: list[BugBenchmarkCaseResult] = []
    evidence_cache: dict[str, dict[str, Any]] = {}
    for spec in specs:
        evidence_payload: dict[str, Any] | None = None
        evidence_errors: list[str] = []
        if certification_policy.enabled and not spec.evidence_path:
            evidence_errors.append("certification requires a linked evidence record")
        if spec.evidence_path:
            evidence_path = str(
                (Path(manifest_path).resolve().parent / spec.evidence_path).resolve()
            )
            if evidence_path not in evidence_cache:
                try:
                    verification = verify_bug_evidence(
                        evidence_path,
                        online_sources=online_sources,
                        python_executable=python_executable,
                    )
                    evidence_cache[evidence_path] = verification.to_dict()
                except (OSError, ValueError) as exc:
                    evidence_cache[evidence_path] = {
                        "verified": False,
                        "local_verified": False,
                        "sources_verified": False,
                        "errors": [str(exc)],
                    }
            evidence_payload = dict(evidence_cache[evidence_path])
            evidence_payload["record_path"] = spec.evidence_path
            binding_checks = _evidence_binding_checks(spec, evidence_payload)
            evidence_payload["manifest_binding"] = {
                "passed": all(check["passed"] for check in binding_checks),
                "checks": binding_checks,
            }
            if not evidence_payload.get("local_verified"):
                evidence_errors.append("linked evidence did not pass local verification")
            if certification_policy.enabled and not evidence_payload.get("verified"):
                evidence_errors.append("certification requires fully verified linked evidence")
            if online_sources and not evidence_payload.get("verified"):
                evidence_errors.append("linked evidence did not pass online source verification")
            evidence_errors.extend(
                f"evidence binding failed: {check['name']}"
                for check in binding_checks
                if not check["passed"]
            )
        if evidence_errors:
            cases.append(
                BugBenchmarkCaseResult(
                    spec=spec,
                    status="blocked",
                    seconds=0.0,
                    exit_code=1,
                    summary="benchmark evidence could not be verified",
                    workspace=spec.workspace or "",
                    command=(),
                    evidence_verification=evidence_payload,
                    error="; ".join(evidence_errors),
                )
            )
            continue
        try:
            workspace = _resolve_workspace(
                spec,
                manifest_path=manifest_path,
                bugsinpy_root=bugsinpy_root,
                checkout_root=checkout_root,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
            cases.append(
                BugBenchmarkCaseResult(
                    spec=spec,
                    status="blocked",
                    seconds=0.0,
                    exit_code=1,
                    summary="benchmark workspace could not be prepared",
                    workspace=spec.workspace or "",
                    command=(),
                    findings=(),
                    artifacts=(),
                    raw_result={},
                    evidence_verification=evidence_payload,
                    error=str(exc),
                )
            )
            continue

        case = run_bug_benchmark_case(
            spec,
            workspace=str(workspace),
            python_executable=python_executable,
            ordeal_root=ordeal_root,
        )
        cases.append(replace(case, evidence_verification=evidence_payload))

    return BugBenchmarkSuite(
        cases=tuple(cases),
        manifest_path=manifest_path,
        selected_tier=tier,
        certification_policy=certification_policy,
        manifest_sha256=_sha256_file(manifest_path),
    )
@dataclass(frozen=True)
class BugBenchmarkCertificateVerification:
    """Independent verification result for one benchmark JSON artifact."""

    valid: bool
    certified: bool
    evidence_digest_valid: bool
    certificate_digest_valid: bool
    claims_consistent: bool
    manifest_digest_valid: bool | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Return whether the artifact is valid and carries a positive certificate."""
        return self.valid and self.certified

    def summary(self) -> str:
        """Return a compact human-readable verification report."""
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"Bug Benchmark Certificate Verification [{status}]",
            f"  valid={self.valid}, certified={self.certified}",
            (
                f"  evidence_digest={self.evidence_digest_valid}, "
                f"certificate_digest={self.certificate_digest_valid}, "
                f"claims_consistent={self.claims_consistent}"
            ),
            f"  manifest_digest={self.manifest_digest_valid}",
        ]
        lines.extend(f"  error: {error}" for error in self.errors)
        lines.extend(f"  warning: {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly verification payload."""
        return {
            "passed": self.passed,
            "valid": self.valid,
            "certified": self.certified,
            "evidence_digest_valid": self.evidence_digest_valid,
            "certificate_digest_valid": self.certificate_digest_valid,
            "claims_consistent": self.claims_consistent,
            "manifest_digest_valid": self.manifest_digest_valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "summary": self.summary(),
        }

    def to_json(self) -> str:
        """Return stable JSON for verification automation."""
        return _json_dump(self.to_dict())
def _artifact_classification_metrics(evidence: dict[str, Any]) -> dict[str, int | float | None]:
    """Recompute confusion-matrix metrics from serialized case evidence."""
    raw_cases = [case for case in evidence.get("cases", []) if isinstance(case, dict)]
    classified = [
        (
            str(case.get("spec", {}).get("expected_outcome", "bug")),
            str(case.get("status", "")),
        )
        for case in raw_cases
        if isinstance(case.get("spec"), dict)
    ]
    true_positives = classified.count(("bug", "hit"))
    false_negatives = classified.count(("bug", "miss"))
    false_positives = classified.count(("clean", "false_positive"))
    true_negatives = classified.count(("clean", "correct_rejection"))
    statuses = [status for _, status in classified]
    return {
        "hit_count": true_positives,
        "miss_count": false_negatives,
        "false_positive_count": false_positives,
        "correct_rejection_count": true_negatives,
        "blocked_count": statuses.count("blocked"),
        "error_count": statuses.count("error"),
        "precision": _rate(true_positives, true_positives + false_positives),
        "recall": _rate(true_positives, true_positives + false_negatives),
        "specificity": _rate(true_negatives, true_negatives + false_positives),
    }
def _artifact_case_oracles_are_consistent(evidence: dict[str, Any]) -> bool:
    """Return whether every completed status belongs to its declared oracle class."""
    allowed = {
        "bug": {"hit", "miss", "blocked", "error"},
        "clean": {"false_positive", "correct_rejection", "blocked", "error"},
    }
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return False
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("spec"), dict):
            return False
        outcome = str(case["spec"].get("expected_outcome", "bug"))
        if str(case.get("status", "")) not in allowed.get(outcome, set()):
            return False
    return True
