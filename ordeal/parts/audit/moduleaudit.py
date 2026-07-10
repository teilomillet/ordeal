from __future__ import annotations
# ruff: noqa
@dataclass
class ModuleAudit:
    """Audit result for one module.

    Every numeric field is accompanied by a status indicator.
    If a measurement failed, the corresponding field is 0 and the
    ``warnings`` list explains why.
    """

    module: str

    # Current state (from existing tests)
    current_test_count: int = 0
    current_test_lines: int = 0
    current_coverage: CoverageMeasurement = field(
        default_factory=lambda: CoverageMeasurement(Status.FAILED, error="not measured yet"),
    )

    # Migrated state (ordeal auto + mined properties)
    migrated_test_count: int = 0
    migrated_lines: int = 0
    migrated_coverage: CoverageMeasurement = field(
        default_factory=lambda: CoverageMeasurement(Status.FAILED, error="not measured yet"),
    )

    # What ordeal discovered (with confidence bounds)
    mined_properties: list[str] = field(default_factory=list)
    mutation_score: str = ""  # e.g. "8/10 (80%)" or "" if not run
    validation_mode: AuditValidationMode = "fast"
    gap_functions: list[str] = field(default_factory=list)
    total_functions: int = 0
    function_audits: list[FunctionAudit] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    suggested_relations: list[dict[str, str]] = field(default_factory=list)
    mutation_gaps: list[dict[str, str]] = field(default_factory=list)
    weakest_tests: list[dict[str, int | str]] = field(default_factory=list)
    mutation_gap_stubs: list[dict[str, str]] = field(default_factory=list)
    mutation_targets: list[dict[str, Any]] = field(default_factory=list)
    contract_findings: list[dict[str, Any]] = field(default_factory=list)
    blocking_reason: str | None = None
    harness_hints: list[dict[str, Any]] = field(default_factory=list)
    surface: list[dict[str, Any]] = field(default_factory=list)

    # Known unknowns — what ordeal structurally cannot verify
    not_checked: list[str] = field(default_factory=list)

    # Audit health — every problem is visible here
    warnings: list[str] = field(default_factory=list)

    # Generated test file content (for inspection/debugging)
    generated_test: str = ""

    @property
    def coverage_preserved(self) -> bool:
        """True if migrated coverage >= current coverage - tolerance.

        Returns False if either measurement failed.
        """
        if self.current_coverage.status == Status.FAILED:
            return False
        if self.migrated_coverage.status == Status.FAILED:
            return False
        return (
            self.migrated_coverage.percent
            >= self.current_coverage.percent - COVERAGE_TOLERANCE_PCT
        )

    def summary(self) -> str:
        """Human-readable one-module report with epistemic labels."""
        lines = [f"\n  {self.module}"]
        views = self.evidence_views()

        current = views["current_suite"]
        lines.append(
            f"    current suite: {current['tests']:>4} tests "
            f"| {current['lines']:>5} lines "
            f"| {current['coverage']}"
        )

        generated = views["generated_suite"]
        lines.append(
            f"    generated incremental: {generated['tests']:>4} tests "
            f"| {generated['lines']:>5} lines "
            f"| {generated['coverage']}"
        )
        mutation = views["mutation_validation"]
        if mutation["score"]:
            lines.append(f"    mutation: {mutation['score']}")
        if mutation["validation"]:
            lines.append(f"    validation: {mutation['validation']}")
        lines.append(f"    mutation view: {mutation['summary']}")
        combined = views["combined_view"]
        lines.append(f"    combined view: {combined['label']}: {combined['summary']}")
        protection = views["test_protection"]
        lines.append(
            f"    protection: {str(protection['status']).upper()}: {protection['summary']}"
        )

        if self.mined_properties:
            grouped = _group_mined_properties(self.mined_properties)
            lines.append(f"    mined:    {grouped}")
        if self.mutation_gaps:
            lines.append("    surviving mutants:")
            for gap in self.mutation_gaps[:DISPLAY_CAP]:
                lines.append(f"      - {gap['target']}: {gap['location']} {gap['description']}")
        if self.weakest_tests:
            lines.append("    weakest killers:")
            for item in self.weakest_tests[:DISPLAY_CAP]:
                lines.append(f"      - {item['test']}: {item['kills']} kill(s)")
        if self.mutation_gap_stubs:
            lines.append(f"    stubs:    {len(self.mutation_gap_stubs)} draft review stub file(s)")
        if self.contract_findings:
            lines.append(
                f"    contracts: {len(self.contract_findings)} explicit contract finding(s)"
            )

        if self.function_audits:
            counts = self.function_audit_counts
            lines.append(
                "    functions:"
                f" {counts['exercised']} exercised [verified],"
                f" {counts['exploratory']} exploratory [inferred],"
                f" {counts['uncovered']} no effective tests [none]"
            )
            for status in ("exercised", "exploratory", "uncovered"):
                entries = [item for item in self.function_audits if item.status == status]
                if not entries:
                    continue
                names = ", ".join(item.name for item in entries[:DISPLAY_CAP])
                lines.append(f"      - {entries[0].summary_label()}: {names}")
                if entries[0].evidence:
                    first = entries[0].evidence[0]
                    lines.append(f"        evidence: {first['kind']} — {first['detail']}")

        if self.gap_functions:
            lines.append(
                f"    gaps:     {len(self.gap_functions)} functions need fixtures: "
                f"{', '.join(self.gap_functions[:DISPLAY_CAP])}"
            )
        if self.blocking_reason:
            lines.append(f"    blocked:  {self.blocking_reason}")
        if self.harness_hints:
            lines.append("    harness hints:")
            for hint in self.harness_hints[:DISPLAY_CAP]:
                lines.append(f"      - {hint['function']}: {hint['kind']} -> {hint['suggestion']}")

        if self.suggestions:
            lines.append("    suggest:")
            for s in self.suggestions:
                lines.append(f"      - {s}")

        if self.suggested_relations:
            lines.append("    metamorphic relations (auto-discovered):")
            for rel in self.suggested_relations:
                lines.append(f"      - {rel['function']}: {rel['code']}")

        if self.not_checked:
            lines.append("    NOT verified (requires manual tests):")
            for item in self.not_checked:
                lines.append(f"      - {item}")

        if self.warnings:
            lines.append(f"    warnings: {len(self.warnings)}")

        return "\n".join(lines)

    def mutation_validation_view(self) -> dict[str, object]:
        """Return a compact mutation-validation view for audit consumers."""
        targets = [dict(item) for item in self.mutation_targets]
        statuses = {str(item.get("status", "")) for item in targets}
        promoted_boundary_count = sum(
            int(item.get("promoted_boundary_count", 0) or 0) for item in targets
        )
        exploratory_survivors = sum(
            int(item.get("exploratory_survivors", 0) or 0) for item in targets
        )
        promoted_boundaries: list[dict[str, Any]] = []
        for item in targets:
            for cluster in item.get("promoted_boundaries", []):
                promoted_boundaries.append(dict(cluster))

        score = self.mutation_score or ""
        validation = (
            self._format_validation_mode()
            if score or targets or self.validation_mode == "deep"
            else ""
        )
        if self.blocking_reason:
            status = "blocked"
            summary = "mutation validation was skipped because audit lacked runnable leverage"
        elif not score and not targets:
            status = "not_measured"
            summary = "mutation validation not measured"
        elif "promoted_gaps" in statuses:
            status = "promoted_gaps"
            summary = (
                f"{score}; {promoted_boundary_count} promoted boundary cluster(s)"
                if score
                else f"{promoted_boundary_count} promoted boundary cluster(s)"
            )
        elif "exploratory_gaps" in statuses:
            status = "exploratory_gaps"
            summary = (
                f"{score}; {exploratory_survivors} exploratory survivor(s)"
                if score
                else f"{exploratory_survivors} exploratory survivor(s)"
            )
        elif statuses == {"no_mutants"} and targets:
            status = "no_mutants"
            summary = "no mutants were available to validate generated checks"
        else:
            status = "fully_killed"
            summary = (
                f"{score}; observed mutants were killed by the current/generated suite"
                if score
                else "observed mutants were killed by the current/generated suite"
            )
        return {
            "label": "contract-aware mutation",
            "status": status,
            "score": score or None,
            "validation": validation,
            "summary": summary,
            "targets": targets,
            "promoted_boundary_count": promoted_boundary_count,
            "exploratory_survivors": exploratory_survivors,
            "promoted_boundaries": promoted_boundaries[:DISPLAY_CAP],
            "weakest_killers": list(self.weakest_tests),
        }

    def test_protection_view(self) -> dict[str, object]:
        """Decide whether the resulting checks protect the measured module behavior.

        Mutation survival is decisive even at 100% line coverage. Coverage and
        property evidence expose what was not exercised or did not discriminate,
        while failed measurements remain inconclusive instead of becoming zeroes.

        This module-level view describes the generated/migrated checks: their
        verified line coverage plus aggregated mined-property mutation results.
        Use ``mutate()`` and ``MutationResult.test_protection_view()`` to judge
        the selected existing test suite directly.
        """
        counts = self.mutation_score_counts
        killed, total = counts if counts is not None else (0, 0)
        survivors = max(total - killed, 0)
        coverage = self.migrated_coverage
        coverage_verified = coverage.status == Status.VERIFIED
        line_percent = coverage.percent if coverage_verified else None
        missing_lines = sorted(coverage.missing_lines) if coverage_verified else []
        missing_count = (
            coverage.result.missing_count
            if coverage_verified and coverage.result is not None
            else 0
        )

        property_strength: list[dict[str, object]] = []
        for target in self.mutation_targets:
            target_name = str(target.get("target", ""))
            for item in target.get("property_strength", []):
                row = dict(item)
                row["target"] = target_name
                property_strength.append(row)

        tautological_or_weak = [
            item for item in property_strength if item.get("status") == "tautological_or_weak"
        ]
        unexercised = [item for item in property_strength if item.get("status") == "unexercised"]

        if survivors > 0:
            status = "weak"
            protects: bool | None = False
            if line_percent == 100.0:
                summary = f"100% line coverage but {survivors}/{total} mutation(s) survived"
            else:
                summary = f"{survivors}/{total} mutation(s) survived"
        elif unexercised:
            status = "weak"
            protects = False
            summary = f"{len(unexercised)} declared property/properties were not exercised"
        elif coverage_verified and (missing_count > 0 or line_percent != 100.0):
            status = "weak"
            protects = False
            summary = (
                f"{missing_count} executable line(s) were not covered"
                if missing_count > 0
                else f"line coverage was {line_percent:.0f}%"
            )
        elif total <= 0:
            status = "inconclusive"
            protects = None
            summary = "mutation strength was not measured"
        elif not coverage_verified:
            status = "inconclusive"
            protects = None
            summary = f"mutation score was {killed}/{total}, but line coverage was not verified"
        else:
            status = "protective_within_measured_scope"
            protects = True
            summary = (
                f"all {total} tested mutation(s) were killed and all executable lines were covered"
            )

        return {
            "label": "resulting test protection",
            "status": status,
            "protects": protects,
            "summary": summary,
            "mutation_score": self.mutation_score or None,
            "killed_mutants": killed,
            "tested_mutants": total,
            "surviving_mutants": survivors,
            "kill_attribution": list(self.weakest_tests),
            "line_coverage_percent": line_percent,
            "coverage_gaps": missing_lines,
            "coverage_gap_count": missing_count,
            "property_strength": property_strength,
            "tautological_or_weak_properties": tautological_or_weak,
            "unexercised_properties": unexercised,
        }

    def evidence_views(self) -> dict[str, dict[str, object]]:
        """Return normalized current/generated/combined audit evidence views."""
        current = {
            "label": "current suite",
            "tests": self.current_test_count,
            "lines": self.current_test_lines,
            "coverage": self._format_coverage(self.current_coverage),
            "status": self.current_coverage.status.value,
        }
        generated = {
            "label": "generated incremental",
            "tests": self.migrated_test_count,
            "lines": self.migrated_lines,
            "coverage": self._format_coverage(self.migrated_coverage),
            "status": self.migrated_coverage.status.value,
            "mutation": self.mutation_score or "",
            "validation": self._format_validation_mode()
            if self.mutation_score or self.validation_mode == "deep"
            else "",
        }
        mutation = self.mutation_validation_view()
        combined = {"label": "coverage delta", "summary": "not measured"}
        cur = self.current_coverage
        mig = self.migrated_coverage
        if (
            cur.status == Status.VERIFIED
            and mig.status == Status.VERIFIED
            and self.current_test_count > 0
            and self.current_test_lines > 0
        ):
            delta = mig.percent - cur.percent
            label, summary = _format_change_summary(
                self.current_test_count,
                self.migrated_test_count,
                self.current_test_lines,
                self.migrated_lines,
                coverage_delta=delta,
            )
            combined = {
                "label": label,
                "summary": (
                    f"{summary} | {mutation['label']}: {mutation['summary']}"
                    if mutation["status"] != "not_measured"
                    else summary
                ),
                "coverage_delta": delta,
            }
        elif mutation["status"] != "not_measured":
            combined = {
                "label": "mutation convergence",
                "summary": f"{mutation['label']}: {mutation['summary']}",
            }
        return {
            "current_suite": current,
            "generated_suite": generated,
            "mutation_validation": mutation,
            "combined_view": combined,
            "test_protection": self.test_protection_view(),
        }

    @staticmethod
    def _format_coverage(m: CoverageMeasurement) -> str:
        """Format a coverage measurement with its epistemic label."""
        if m.status == Status.FAILED:
            return f"FAILED: {m.error}"
        return f"{m.percent:.0f}% coverage [{m.status.value}]"

    def _format_validation_mode(self) -> str:
        """Describe how mutation validation was performed."""
        if self.validation_mode == "deep":
            return "deep replay + re-mine"
        return "fast replay"

    @property
    def mutation_score_counts(self) -> tuple[int, int] | None:
        """Exact ``(killed, total)`` counts parsed from ``mutation_score``."""
        return _parse_mutation_score(self.mutation_score)

    @property
    def mutation_score_fraction(self) -> float | None:
        """Exact mutation score as a fraction, or ``None`` when unavailable."""
        counts = self.mutation_score_counts
        if counts is None:
            return None
        killed, total = counts
        if total <= 0:
            return None
        return killed / total

    @property
    def function_audit_counts(self) -> dict[str, int]:
        """Count function audits by epistemic status."""
        counts = {"exercised": 0, "exploratory": 0, "uncovered": 0}
        for item in self.function_audits:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    @property
    def direct_test_gap_counts(self) -> dict[str, int]:
        """Count function audits that do not satisfy the direct-test gate."""
        counts = {"exploratory": 0, "uncovered": 0}
        for item in self.function_audits:
            if item.status in counts:
                counts[item.status] += 1
        return counts

    @property
    def direct_test_gaps(self) -> list[FunctionAudit]:
        """Return functions that still lack direct verified tests."""
        return [item for item in self.function_audits if item.status != "exercised"]

    @property
    def has_direct_test_gaps(self) -> bool:
        """True when any function is only exploratory or fully uncovered."""
        return bool(self.direct_test_gaps)

    @property
    def fixture_completeness(self) -> float:
        """Fraction of discovered functions that ordeal could execute directly."""
        if self.total_functions <= 0:
            return 0.0
        return max(self.total_functions - len(self.gap_functions), 0) / self.total_functions
def _coverage_result_to_dict(result: CoverageResult | None) -> dict[str, object] | None:
    """Serialize a coverage result for the audit cache."""
    if result is None:
        return None
    return {
        "percent": result.percent,
        "total_statements": result.total_statements,
        "missing_count": result.missing_count,
        "missing_lines": sorted(result.missing_lines),
        "source": result.source,
    }
def _coverage_result_from_dict(data: dict[str, object] | None) -> CoverageResult | None:
    """Deserialize a cached coverage result."""
    if data is None:
        return None
    return CoverageResult(
        percent=float(data["percent"]),
        total_statements=int(data["total_statements"]),
        missing_count=int(data["missing_count"]),
        missing_lines=frozenset(int(line) for line in data["missing_lines"]),
        source=str(data["source"]),
    )
def _coverage_measurement_to_dict(measurement: CoverageMeasurement) -> dict[str, object]:
    """Serialize a coverage measurement for the audit cache."""
    return {
        "status": measurement.status.value,
        "result": _coverage_result_to_dict(measurement.result),
        "error": measurement.error,
    }
def _coverage_measurement_from_dict(data: dict[str, object]) -> CoverageMeasurement:
    """Deserialize a cached coverage measurement."""
    return CoverageMeasurement(
        status=Status(str(data["status"])),
        result=_coverage_result_from_dict(data.get("result")),
        error=data.get("error"),
    )
def _function_audit_to_dict(result: FunctionAudit) -> dict[str, object]:
    """Serialize one function audit result for the on-disk cache."""
    return {
        "name": result.name,
        "status": result.status,
        "epistemic": result.epistemic,
        "covered_body_lines": result.covered_body_lines,
        "total_body_lines": result.total_body_lines,
        "evidence": result.evidence,
    }
def _function_audit_from_dict(data: dict[str, object]) -> FunctionAudit:
    """Deserialize one cached function audit result."""
    return FunctionAudit(
        name=str(data["name"]),
        status=_normalize_function_audit_status(str(data["status"])),
        epistemic=_normalize_evidence_label(str(data["epistemic"])),
        covered_body_lines=int(data.get("covered_body_lines", 0)),
        total_body_lines=int(data.get("total_body_lines", 0)),
        evidence=[dict(item) for item in data.get("evidence", [])],
    )
