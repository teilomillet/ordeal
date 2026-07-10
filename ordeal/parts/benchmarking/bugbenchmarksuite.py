from __future__ import annotations
# ruff: noqa
@dataclass(frozen=True)
class BugBenchmarkSuite:
    """Results for a benchmark manifest run, including optional certification."""

    cases: tuple[BugBenchmarkCaseResult, ...]
    manifest_path: str
    selected_tier: str | None = None
    certification_policy: BugBenchmarkCertificationPolicy = field(
        default_factory=BugBenchmarkCertificationPolicy
    )
    manifest_sha256: str | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def case_count(self) -> int:
        """Return the number of executed cases."""
        return len(self.cases)

    @property
    def hit_count(self) -> int:
        """Return the number of true-positive bug cases."""
        return sum(1 for case in self.cases if case.classification == "true_positive")

    @property
    def correct_rejection_count(self) -> int:
        """Return the number of true-negative control cases."""
        return sum(1 for case in self.cases if case.classification == "true_negative")

    @property
    def false_positive_count(self) -> int:
        """Return the number of clean controls incorrectly flagged."""
        return sum(1 for case in self.cases if case.classification == "false_positive")

    @property
    def blocked_count(self) -> int:
        """Return the number of blocked cases."""
        return sum(1 for case in self.cases if case.status == "blocked")

    @property
    def error_count(self) -> int:
        """Return the number of failed-execution cases."""
        return sum(1 for case in self.cases if case.status == "error")

    @property
    def miss_count(self) -> int:
        """Return the number of false-negative bug cases."""
        return sum(1 for case in self.cases if case.classification == "false_negative")

    @property
    def positive_case_count(self) -> int:
        """Return the number of scoreable cases whose oracle says a bug is present."""
        return self.hit_count + self.miss_count

    @property
    def negative_case_count(self) -> int:
        """Return the number of scoreable clean-control cases."""
        return self.correct_rejection_count + self.false_positive_count

    @property
    def precision(self) -> float | None:
        """Return precision over scored positive predictions."""
        return _rate(self.hit_count, self.hit_count + self.false_positive_count)

    @property
    def recall(self) -> float | None:
        """Return recall over scored bug cases."""
        return _rate(self.hit_count, self.hit_count + self.miss_count)

    @property
    def specificity(self) -> float | None:
        """Return specificity over scored clean controls."""
        return _rate(
            self.correct_rejection_count,
            self.correct_rejection_count + self.false_positive_count,
        )

    @property
    def artifact_case_count(self) -> int:
        """Return the number of cases that produced artifacts."""
        return sum(1 for case in self.cases if case.artifacts)

    @property
    def public_case_count(self) -> int:
        """Return the number of public-saturation-risk cases."""
        return sum(1 for case in self.cases if case.spec.saturation_risk == "public")

    @property
    def private_case_count(self) -> int:
        """Return the number of private-saturation-risk cases."""
        return sum(1 for case in self.cases if case.spec.saturation_risk == "private")

    @property
    def optimization_case_count(self) -> int:
        """Return the number of cases explicitly allowed for tuning."""
        return sum(1 for case in self.cases if case.spec.allowed_for_optimization)

    @property
    def report_only_case_count(self) -> int:
        """Return the number of cases reserved for reporting only."""
        return sum(1 for case in self.cases if not case.spec.allowed_for_optimization)

    @property
    def median_seconds(self) -> float:
        """Return the median wall time for executed cases."""
        if not self.cases:
            return 0.0
        return statistics.median(case.seconds for case in self.cases)

    @property
    def hit_rate(self) -> float:
        """Return the legacy share of all cases that were bug hits."""
        if not self.cases:
            return 0.0
        return self.hit_count / len(self.cases)

    @property
    def passed(self) -> bool:
        """Return whether every completed case agreed with its oracle."""
        return bool(self.cases) and all(case.classification_correct for case in self.cases)

    def _provenance_failures(self) -> list[str]:
        """Return failures in the declared oracle and pairing evidence."""
        failures: list[str] = []
        for case in self.cases:
            spec = case.spec
            missing = [
                name
                for name, value in (
                    ("selection_reason", spec.selection_reason),
                    ("oracle_source", spec.oracle_source),
                    ("oracle_url", spec.oracle_url),
                    ("evidence_level", spec.evidence_level),
                    ("fix_commit", spec.fix_commit),
                    ("failure_command", spec.failure_command),
                    ("pair_id", spec.pair_id),
                )
                if not value
            ]
            if missing:
                failures.append(f"case {spec.name!r} lacks {', '.join(missing)}")
            if spec.fix_commit and not re.fullmatch(r"[0-9a-fA-F]{7,64}", spec.fix_commit):
                failures.append(f"case {spec.name!r} has a non-commit fix_commit")
            if spec.oracle_url:
                if not spec.oracle_url.startswith("https://"):
                    failures.append(f"case {spec.name!r} oracle_url is not HTTPS")
                if spec.fix_commit and spec.fix_commit.lower() not in spec.oracle_url.lower():
                    failures.append(f"case {spec.name!r} oracle_url does not bind fix_commit")

        if self.certification_policy.require_paired_controls:
            pairs: dict[str, list[BugBenchmarkSpec]] = {}
            for case in self.cases:
                if case.spec.pair_id:
                    pairs.setdefault(case.spec.pair_id, []).append(case.spec)
            for pair_id, specs in sorted(pairs.items()):
                outcomes = sorted(spec.expected_outcome for spec in specs)
                if outcomes != ["bug", "clean"]:
                    failures.append(
                        f"pair {pair_id!r} must contain exactly one bug and one clean control"
                    )
                    continue
                for field_name in ("project", "bug_id", "fix_commit", "oracle_url"):
                    values = {getattr(spec, field_name) for spec in specs}
                    if len(values) != 1:
                        failures.append(f"pair {pair_id!r} disagrees on {field_name}")
            paired_cases = sum(len(specs) for specs in pairs.values())
            if paired_cases != len(self.cases):
                failures.append("every certified case must belong to a declared pair")
        return failures

    def _evidence_failures(self) -> list[str]:
        """Return failures in executable evidence required for certification."""
        failures: list[str] = []
        for case in self.cases:
            spec = case.spec
            if not spec.evidence_path:
                failures.append(f"case {spec.name!r} has no linked evidence record")
                continue
            verification = case.evidence_verification
            if not isinstance(verification, Mapping):
                failures.append(f"case {spec.name!r} has no evidence verification result")
                continue
            if verification.get("local_verified") is not True:
                failures.append(f"case {spec.name!r} lacks verified local evidence")
            binding = verification.get("manifest_binding")
            if not isinstance(binding, Mapping) or binding.get("passed") is not True:
                failures.append(f"case {spec.name!r} lacks a passing manifest evidence binding")
            online_required = verification.get("online_sources_required")
            if not isinstance(online_required, bool):
                failures.append(
                    f"case {spec.name!r} evidence does not declare its online-source requirement"
                )
            if verification.get("verified") is not True:
                failures.append(f"case {spec.name!r} evidence is not fully verified")
            if online_required is True and verification.get("sources_verified") is not True:
                failures.append(
                    f"case {spec.name!r} requires authoritative online source verification"
                )
        return failures

    def certification_assessment(self) -> dict[str, Any]:
        """Evaluate the manifest's point, uncertainty, and provenance requirements."""
        policy = self.certification_policy
        bounds = {
            "recall": _wilson_lower_bound(
                self.hit_count,
                self.hit_count + self.miss_count,
                policy.confidence_level,
            ),
            "precision": _wilson_lower_bound(
                self.hit_count,
                self.hit_count + self.false_positive_count,
                policy.confidence_level,
            ),
            "specificity": _wilson_lower_bound(
                self.correct_rejection_count,
                self.correct_rejection_count + self.false_positive_count,
                policy.confidence_level,
            ),
        }
        metrics = {
            "true_positives": self.hit_count,
            "false_negatives": self.miss_count,
            "false_positives": self.false_positive_count,
            "true_negatives": self.correct_rejection_count,
            "positive_cases": self.positive_case_count,
            "negative_cases": self.negative_case_count,
            "blocked": self.blocked_count,
            "errors": self.error_count,
            "recall": self.recall,
            "precision": self.precision,
            "specificity": self.specificity,
        }
        failures: list[str] = []
        if policy.enabled:
            failures.extend(self._evidence_failures())
            invalid_classifications = sum(
                case.classification is None and case.status not in {"blocked", "error"}
                for case in self.cases
            )
            if invalid_classifications:
                failures.append("case statuses disagree with their declared outcomes")
            if self.positive_case_count < policy.min_positive_cases:
                failures.append(
                    f"positive cases {self.positive_case_count} < {policy.min_positive_cases}"
                )
            if self.negative_case_count < policy.min_negative_cases:
                failures.append(
                    f"negative cases {self.negative_case_count} < {policy.min_negative_cases}"
                )
            for name, threshold in (
                ("recall", policy.min_recall),
                ("precision", policy.min_precision),
                ("specificity", policy.min_specificity),
            ):
                observed = metrics[name]
                if observed is None or float(observed) + 1e-12 < threshold:
                    failures.append(f"{name} does not meet {threshold:.3f}")
                lower = bounds[name]
                if lower is None or lower + 1e-12 < policy.min_confidence_bound:
                    failures.append(
                        f"{name} Wilson lower bound does not meet "
                        f"{policy.min_confidence_bound:.3f}"
                    )
            if policy.require_complete and (self.blocked_count or self.error_count):
                failures.append("blocked or error cases make the evidence incomplete")
            if policy.require_provenance:
                failures.extend(self._provenance_failures())
            if not self.manifest_sha256:
                failures.append("manifest digest is missing")
        return {
            "enabled": policy.enabled,
            "certified": policy.enabled and not failures,
            "assurance": "self_attested_reproducible_evidence",
            "policy": policy.to_dict(),
            "metrics": metrics,
            "confidence": {
                "level": policy.confidence_level,
                "method": "wilson_score_two_sided",
                "lower_bounds": bounds,
            },
            "failure_reasons": failures,
        }

    @property
    def certified(self) -> bool:
        """Return whether the declared certification contract is satisfied."""
        return bool(self.certification_assessment()["certified"])

    @property
    def check_passed(self) -> bool:
        """Return the fail-closed CLI gate result for this manifest."""
        if self.certification_policy.enabled:
            return self.certified
        return self.passed

    def summary(self) -> str:
        """Return a compact text summary."""
        status = "PASS" if self.passed else "FAIL"
        certificate_status = ""
        if self.certification_policy.enabled:
            certificate_status = " [CERTIFIED]" if self.certified else " [UNCERTIFIED]"
        rates = (
            f"recall={self.recall:.0%}, precision={self.precision:.0%}, "
            f"specificity={self.specificity:.0%}"
            if self.recall is not None
            and self.precision is not None
            and self.specificity is not None
            else "precision/recall/specificity=undefined"
        )
        lines = [
            f"Bug Benchmark [{status}]{certificate_status}",
            f"  manifest={self.manifest_path}",
            (
                f"  cases={self.case_count}, true_positives={self.hit_count}, "
                f"false_negatives={self.miss_count}, false_positives={self.false_positive_count}, "
                f"true_negatives={self.correct_rejection_count}"
            ),
            f"  quality: {rates}",
            (
                f"  incomplete: blocked={self.blocked_count}, errors={self.error_count}; "
                f"artifacts={self.artifact_case_count}/{self.case_count}; "
                f"median_seconds={self.median_seconds:.3f}"
            ),
            (
                f"  epistemics: public={self.public_case_count}, "
                f"private={self.private_case_count}, "
                f"optimize={self.optimization_case_count}, "
                f"report_only={self.report_only_case_count}"
            ),
        ]
        if self.selected_tier is not None:
            lines.append(f"  tier={self.selected_tier}")
        if self.certification_policy.enabled and not self.certified:
            for failure in self.certification_assessment()["failure_reasons"]:
                lines.append(f"  certificate failure: {failure}")
        for case in self.cases:
            lines.append(
                f"  {case.status.upper()} {case.spec.name}: {case.summary} ({case.seconds:.3f}s)"
            )
        return "\n".join(lines)

    def _evidence_dict(self) -> dict[str, Any]:
        """Return the evidence payload covered by the certificate digest."""
        assessment = self.certification_assessment()
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "selected_tier": self.selected_tier,
            "case_count": self.case_count,
            "hit_count": self.hit_count,
            "hit_rate": self.hit_rate,
            "miss_count": self.miss_count,
            "false_positive_count": self.false_positive_count,
            "correct_rejection_count": self.correct_rejection_count,
            "blocked_count": self.blocked_count,
            "error_count": self.error_count,
            "artifact_case_count": self.artifact_case_count,
            "median_seconds": self.median_seconds,
            "precision": self.precision,
            "recall": self.recall,
            "specificity": self.specificity,
            "passed": self.passed,
            "certified": self.certified,
            "certification": assessment,
            "epistemics": {
                "public_case_count": self.public_case_count,
                "private_case_count": self.private_case_count,
                "optimization_case_count": self.optimization_case_count,
                "report_only_case_count": self.report_only_case_count,
            },
            "cases": [case.to_dict() for case in self.cases],
            "summary": self.summary(),
        }

    def _certificate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Build a tamper-evident, explicitly self-attested certificate."""
        try:
            ordeal_version = package_version("ordeal")
        except PackageNotFoundError:
            ordeal_version = "unknown"
        certificate = {
            "schema": "ordeal.bug-benchmark.evidence/v1",
            "assurance": "self_attested_reproducible_evidence",
            "issued_at": self.generated_at,
            "certified": self.certified,
            "subject": {
                "manifest_path": self.manifest_path,
                "manifest_sha256": self.manifest_sha256,
                "selected_tier": self.selected_tier,
                "case_count": self.case_count,
            },
            "claims": self.certification_assessment(),
            "runtime": {
                "ordeal_version": ordeal_version,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
            },
            "integrity": {
                "algorithm": "sha256",
                "evidence_sha256": _sha256_payload(evidence),
            },
            "limitations": [
                "Certifies only the declared cases, controls, thresholds, and captured run.",
                (
                    "Confidence bounds quantify sampling uncertainty but do not guarantee "
                    "unseen results."
                ),
                (
                    "SHA-256 detects evidence changes; it does not prove a third-party "
                    "signer identity."
                ),
            ],
        }
        certificate["integrity"]["certificate_sha256"] = _sha256_payload(certificate)
        return certificate

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly suite payload."""
        evidence = self._evidence_dict()
        if self.certification_policy.enabled:
            evidence["certificate"] = self._certificate(evidence)
        return evidence

    def to_json(self) -> str:
        """Return a stable JSON encoding."""
        return _json_dump(self.to_dict())
def parse_bug_benchmark_certification_policy(path: str) -> BugBenchmarkCertificationPolicy:
    """Parse and validate the optional ``[certification]`` table."""
    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)
    raw = data.get("certification")
    if raw is None:
        return BugBenchmarkCertificationPolicy()
    if not isinstance(raw, dict):
        raise ValueError("[certification] must be a table")

    policy = BugBenchmarkCertificationPolicy(
        enabled=bool(raw.get("enabled", True)),
        confidence_level=float(raw.get("confidence_level", 0.95)),
        min_positive_cases=int(raw.get("min_positive_cases", 1)),
        min_negative_cases=int(raw.get("min_negative_cases", 1)),
        min_recall=float(raw.get("min_recall", 1.0)),
        min_precision=float(raw.get("min_precision", 1.0)),
        min_specificity=float(raw.get("min_specificity", 1.0)),
        min_confidence_bound=float(raw.get("min_confidence_bound", 0.0)),
        require_complete=bool(raw.get("require_complete", True)),
        require_provenance=bool(raw.get("require_provenance", True)),
        require_paired_controls=bool(raw.get("require_paired_controls", True)),
    )
    if not 0.5 < policy.confidence_level < 1:
        raise ValueError("certification confidence_level must be between 0.5 and 1")
    if policy.min_positive_cases < 1 or policy.min_negative_cases < 1:
        raise ValueError("certification minimum case counts must be >= 1")
    for name in ("min_recall", "min_precision", "min_specificity", "min_confidence_bound"):
        value = float(getattr(policy, name))
        if not 0 <= value <= 1:
            raise ValueError(f"certification {name} must be between 0 and 1")
    return policy
