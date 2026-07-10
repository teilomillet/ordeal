from __future__ import annotations
# ruff: noqa
def _artifact_certification_is_earned(evidence: dict[str, Any]) -> bool:
    """Re-evaluate certificate eligibility from serialized cases and policy."""
    assessment = evidence.get("certification")
    if not isinstance(assessment, dict):
        return False
    policy = assessment.get("policy")
    if not isinstance(policy, dict) or not bool(policy.get("enabled")):
        return False
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return False
    if not _artifact_case_oracles_are_consistent(evidence):
        return False
    specs = [case.get("spec", {}) for case in cases if isinstance(case, dict)]
    if len(specs) != len(cases) or any(not isinstance(spec, dict) for spec in specs):
        return False
    for case, spec in zip(cases, specs, strict=True):
        if not spec.get("evidence_path"):
            return False
        verification = case.get("evidence_verification")
        if not isinstance(verification, dict):
            return False
        binding = verification.get("manifest_binding")
        if (
            verification.get("local_verified") is not True
            or verification.get("verified") is not True
            or not isinstance(binding, dict)
            or binding.get("passed") is not True
        ):
            return False
        online_required = verification.get("online_sources_required")
        if not isinstance(online_required, bool):
            return False
        if online_required and verification.get("sources_verified") is not True:
            return False
    metrics = _artifact_classification_metrics(evidence)
    positives = int(metrics["hit_count"] or 0) + int(metrics["miss_count"] or 0)
    negatives = int(metrics["correct_rejection_count"] or 0) + int(
        metrics["false_positive_count"] or 0
    )
    if positives < int(policy.get("min_positive_cases", 1)):
        return False
    if negatives < int(policy.get("min_negative_cases", 1)):
        return False
    for name in ("recall", "precision", "specificity"):
        value = metrics[name]
        if value is None or float(value) + 1e-12 < float(policy.get(f"min_{name}", 1.0)):
            return False
    if bool(policy.get("require_complete", True)) and (
        int(metrics["blocked_count"] or 0) or int(metrics["error_count"] or 0)
    ):
        return False

    confidence_level = float(policy.get("confidence_level", 0.95))
    min_bound = float(policy.get("min_confidence_bound", 0.0))
    counts = {
        "recall": (
            int(metrics["hit_count"] or 0),
            int(metrics["hit_count"] or 0) + int(metrics["miss_count"] or 0),
        ),
        "precision": (
            int(metrics["hit_count"] or 0),
            int(metrics["hit_count"] or 0) + int(metrics["false_positive_count"] or 0),
        ),
        "specificity": (
            int(metrics["correct_rejection_count"] or 0),
            int(metrics["correct_rejection_count"] or 0)
            + int(metrics["false_positive_count"] or 0),
        ),
    }
    for successes, total in counts.values():
        lower = _wilson_lower_bound(successes, total, confidence_level)
        if lower is None or lower + 1e-12 < min_bound:
            return False

    if bool(policy.get("require_provenance", True)):
        required = (
            "selection_reason",
            "oracle_source",
            "oracle_url",
            "evidence_level",
            "fix_commit",
            "failure_command",
            "pair_id",
        )
        for spec in specs:
            if any(not spec.get(name) for name in required):
                return False
            fix_commit = str(spec["fix_commit"])
            oracle_url = str(spec["oracle_url"])
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", fix_commit):
                return False
            if (
                not oracle_url.startswith("https://")
                or fix_commit.lower() not in oracle_url.lower()
            ):
                return False
        if bool(policy.get("require_paired_controls", True)):
            pairs: dict[str, list[dict[str, Any]]] = {}
            for spec in specs:
                pairs.setdefault(str(spec["pair_id"]), []).append(spec)
            if sum(len(pair) for pair in pairs.values()) != len(specs):
                return False
            for pair in pairs.values():
                if sorted(str(spec.get("expected_outcome")) for spec in pair) != ["bug", "clean"]:
                    return False
                for field_name in ("project", "bug_id", "fix_commit", "oracle_url"):
                    if len({spec.get(field_name) for spec in pair}) != 1:
                        return False
    return bool(evidence.get("manifest_sha256"))
def verify_bug_benchmark_certificate(
    artifact_path: str,
    *,
    manifest_path: str | None = None,
) -> BugBenchmarkCertificateVerification:
    """Verify certificate digests, claims, metrics, and optional manifest bytes."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return BugBenchmarkCertificateVerification(
            valid=False,
            certified=False,
            evidence_digest_valid=False,
            certificate_digest_valid=False,
            claims_consistent=False,
            manifest_digest_valid=None,
            errors=(f"could not read certificate artifact: {exc}",),
        )
    if not isinstance(payload, dict):
        errors.append("artifact root must be a JSON object")
        payload = {}
    raw_certificate = payload.pop("certificate", None)
    if not isinstance(raw_certificate, dict):
        return BugBenchmarkCertificateVerification(
            valid=False,
            certified=False,
            evidence_digest_valid=False,
            certificate_digest_valid=False,
            claims_consistent=False,
            manifest_digest_valid=None,
            errors=("artifact does not contain a certificate object",),
        )

    certificate = dict(raw_certificate)
    integrity = certificate.get("integrity")
    if not isinstance(integrity, dict):
        integrity = {}
        errors.append("certificate integrity block is missing")
    expected_evidence_digest = str(integrity.get("evidence_sha256", ""))
    evidence_digest_valid = bool(expected_evidence_digest) and (
        _sha256_payload(payload) == expected_evidence_digest
    )
    if not evidence_digest_valid:
        errors.append("evidence SHA-256 digest does not match")

    certificate_for_digest = json.loads(json.dumps(certificate))
    digest_integrity = certificate_for_digest.get("integrity", {})
    expected_certificate_digest = str(digest_integrity.pop("certificate_sha256", ""))
    certificate_digest_valid = bool(expected_certificate_digest) and (
        _sha256_payload(certificate_for_digest) == expected_certificate_digest
    )
    if not certificate_digest_valid:
        errors.append("certificate SHA-256 digest does not match")

    claims_consistent = True
    if certificate.get("schema") != "ordeal.bug-benchmark.evidence/v1":
        claims_consistent = False
        errors.append("unsupported certificate schema")
    if certificate.get("assurance") != "self_attested_reproducible_evidence":
        claims_consistent = False
        errors.append("certificate assurance type is not recognized")
    if certificate.get("claims") != payload.get("certification"):
        claims_consistent = False
        errors.append("certificate claims differ from the evidence assessment")
    if bool(certificate.get("certified")) != bool(payload.get("certified")):
        claims_consistent = False
        errors.append("certificate status differs from the evidence status")

    subject = certificate.get("subject")
    if not isinstance(subject, dict):
        subject = {}
        claims_consistent = False
        errors.append("certificate subject is missing")
    for key in ("manifest_path", "manifest_sha256", "selected_tier", "case_count"):
        if subject.get(key) != payload.get(key):
            claims_consistent = False
            errors.append(f"certificate subject differs on {key}")

    computed_metrics = _artifact_classification_metrics(payload)
    if not _artifact_case_oracles_are_consistent(payload):
        claims_consistent = False
        errors.append("serialized case statuses disagree with their declared outcomes")
    for key, computed in computed_metrics.items():
        declared = payload.get(key)
        if isinstance(computed, float) and isinstance(declared, (float, int)):
            agrees = abs(float(declared) - computed) <= 1e-12
        else:
            agrees = declared == computed
        if not agrees:
            claims_consistent = False
            errors.append(f"serialized case evidence disagrees with {key}")
    earned_certification = _artifact_certification_is_earned(payload)
    if bool(payload.get("certified")) != earned_certification:
        claims_consistent = False
        errors.append("serialized evidence does not earn its declared certification status")

    manifest_candidate: Path | None = None
    if manifest_path:
        manifest_candidate = Path(manifest_path)
    else:
        declared_manifest_path = payload.get("manifest_path")
        if declared_manifest_path and Path(str(declared_manifest_path)).exists():
            manifest_candidate = Path(str(declared_manifest_path))
    manifest_digest_valid: bool | None = None
    if manifest_candidate is not None:
        try:
            manifest_digest_valid = _sha256_file(manifest_candidate) == payload.get(
                "manifest_sha256"
            )
        except OSError as exc:
            errors.append(f"could not read manifest for verification: {exc}")
            manifest_digest_valid = False
        if not manifest_digest_valid:
            errors.append("manifest SHA-256 digest does not match")
    else:
        errors.append("manifest bytes were unavailable; exact manifest verification is required")

    valid = (
        evidence_digest_valid
        and certificate_digest_valid
        and claims_consistent
        and manifest_digest_valid is True
        and not errors
    )
    return BugBenchmarkCertificateVerification(
        valid=valid,
        certified=bool(certificate.get("certified")),
        evidence_digest_valid=evidence_digest_valid,
        certificate_digest_valid=certificate_digest_valid,
        claims_consistent=claims_consistent,
        manifest_digest_valid=manifest_digest_valid,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
