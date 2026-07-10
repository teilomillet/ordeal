from __future__ import annotations
# ruff: noqa
def property_strength(self) -> list[dict[str, Any]]:
    """Measure whether exercised mined properties discriminate real mutations.

    A property is ``discriminating`` only when it kills at least one tested
    mutant. An exercised property that kills none is reported as
    ``tautological_or_weak``: mutation testing proves that it added no signal
    for this mutant set, but does not claim a formal logical tautology.
    """
    rows: list[dict[str, Any]] = []
    for observation in self.property_observations:
        name = str(observation.get("name", ""))
        holds = int(observation.get("holds", 0))
        total = int(observation.get("total", 0))
        killed_mutants = [
            mutant
            for mutant in self.mutants
            if name in mutant.metadata.get("killed_by_properties", [])
        ]
        if total <= 0:
            status = "unexercised"
        elif self.total <= 0:
            status = "not_measured"
        elif killed_mutants:
            status = "discriminating"
        else:
            status = "tautological_or_weak"
        rows.append(
            {
                "name": name,
                "holds": holds,
                "total": total,
                "mutants_killed": len(killed_mutants),
                "mutants_tested": self.total,
                "status": status,
            }
        )
    return rows
property_strength.__qualname__ = "MutationResult.property_strength"
MutationResult.property_strength = property_strength
del property_strength
def test_protection_view(self) -> dict[str, Any]:
    """Answer whether the tests protect the target within measured scope.

    Returns a JSON-compatible dict containing the scoped verdict, exact
    mutation score, survivors, kill attribution, property-strength rows,
    and convenience lists for weak or unexercised properties.

    ``weak`` means a mutant survived or a declared property was not
    exercised. ``inconclusive`` means no non-equivalent mutant was tested.
    ``protective_within_measured_scope`` means every tested mutant was
    killed; it is not a universal correctness guarantee.
    """
    properties = self.property_strength()
    unexercised = [item for item in properties if item["status"] == "unexercised"]
    if self.survived:
        status = "weak"
        protects: bool | None = False
        summary = (
            f"{len(self.survived)}/{self.total} mutation(s) survived; "
            "the tests do not protect those behaviors"
        )
    elif unexercised:
        status = "weak"
        protects = False
        summary = f"{len(unexercised)} declared property/properties were not exercised"
    elif self.total <= 0:
        status = "inconclusive"
        protects = None
        summary = "no non-equivalent mutants were tested"
    else:
        status = "protective_within_measured_scope"
        protects = True
        summary = f"all {self.total} tested mutation(s) were killed"
    return {
        "status": status,
        "protects": protects,
        "summary": summary,
        "mutation_score": self.score_text or None,
        "surviving_mutants": len(self.survived),
        "kill_attribution": self.weakest_killers(),
        "property_strength": properties,
        "tautological_or_weak_properties": [
            item["name"] for item in properties if item["status"] == "tautological_or_weak"
        ],
        "unexercised_properties": [item["name"] for item in unexercised],
    }
test_protection_view.__qualname__ = "MutationResult.test_protection_view"
MutationResult.test_protection_view = test_protection_view
del test_protection_view
def epistemic_view(self) -> dict[str, Any]:
    """Return a tight mutation-evidence payload for reports and audit aggregation."""
    contract_note = _contract_context_summary(self.contract_context)
    semantic_clusters = self.semantic_survivor_clusters()
    promoted_clusters = self.promoted_survivor_clusters()
    promoted_keys = {(str(cluster["owner"]), str(cluster["tag"])) for cluster in promoted_clusters}
    exploratory_survivors = sum(
        int(cluster["size"])
        for cluster in semantic_clusters
        if (str(cluster["owner"]), str(cluster["tag"])) not in promoted_keys
    )

    if self.total <= 0:
        status = "no_mutants"
        summary = "no mutants were available to validate"
    elif not self.survived:
        status = "fully_killed"
        summary = f"all {self.total} observed mutant(s) were killed"
    elif promoted_clusters:
        status = "promoted_gaps"
        summary = (
            f"{len(self.survived)} survivor(s) across "
            f"{len(promoted_clusters)} promoted boundary cluster(s)"
        )
    else:
        status = "exploratory_gaps"
        summary = (
            f"{len(self.survived)} exploratory survivor(s) without a promoted boundary cluster"
        )

    return {
        "target": self.target,
        "status": status,
        "summary": summary,
        "score": self.score_text or None,
        "score_fraction": self.score if self.total > 0 else None,
        "killed": self.killed,
        "total": self.total,
        "survived": len(self.survived),
        "contract": contract_note,
        "promoted_boundary_count": len(promoted_clusters),
        "exploratory_survivors": exploratory_survivors,
        "promoted_boundaries": [
            {
                "owner": str(cluster["owner"]),
                "tag": str(cluster["tag"]),
                "label": str(cluster["label"]),
                "size": int(cluster["size"]),
                "operators": list(cluster["operators"]),
                "contract": _contract_context_summary(cluster.get("contract_context")),
            }
            for cluster in promoted_clusters
        ],
        "weakest_killers": self.weakest_killers(limit=3),
        "property_strength": self.property_strength(),
        "test_protection": self.test_protection_view(),
        "validation_sample_matrix_sha256": self.validation_sample_matrix_sha256,
    }
epistemic_view.__qualname__ = "MutationResult.epistemic_view"
MutationResult.epistemic_view = epistemic_view
del epistemic_view
def filter_report(self) -> str:
    """Structured breakdown of the mutation pipeline for AI assistants.

    Shows how many mutants were generated and where they were filtered,
    so the consumer can understand *why* the result looks the way it does.

    Returns an empty string when diagnostics are not populated.
    """
    d = self.diagnostics
    generated = d.get("generated", 0)
    if generated == 0 and self.total == 0:
        return "No mutants were generated from the source code."

    lines = [f"Pipeline: {generated} mutant(s) generated"]
    for key, label in [
        ("skipped_display_method", "skipped (display method)"),
        ("filtered_ast_equivalent", "filtered (AST equivalent)"),
        ("filtered_runtime_equivalent", "filtered (runtime equivalent)"),
        ("filtered_module_equivalent", "filtered (module equivalent)"),
        ("compilation_failed", "dropped (compilation failed)"),
    ]:
        count = d.get(key, 0)
        if count > 0:
            lines.append(f"  - {count} {label}")
    if d.get("generation_timed_out"):
        lines.append("  ⚠ generation timed out — results are partial")
    lines.append(f"  → {d.get('tested', self.total)} tested")
    if self.total > 0:
        lines.append(f"  → {self.killed} killed, {len(self.survived)} survived")
    return "\n".join(lines)
filter_report.__qualname__ = "MutationResult.filter_report"
MutationResult.filter_report = filter_report
del filter_report
def summary(self, remediation: bool = True) -> str:
    """Report with test gaps and per-gap fix guidance.

    Each surviving mutant is a **test gap** — a real code change
    that the test suite fails to detect.  The output names each gap,
    shows the affected source line, and explains the specific fix
    (what kind of test would close the gap).

    Args:
        remediation: If True (default), include per-gap fix guidance
            explaining what test to write.
    """
    target_label, is_method = _mutation_target_display(self.target)
    parts = [f"target: {self.target}"]
    if self.preset_used:
        parts.append(f"preset: {self.preset_used}")
    if self.operators_used:
        parts.append(f"operators: {len(self.operators_used)}/{len(OPERATORS)}")
    if self.concern:
        parts.append(f"concern: {self.concern}")
    contract_note = _contract_context_summary(self.contract_context)
    if contract_note:
        parts.append(f"contract: {contract_note}")
    meta = ", ".join(parts)

    # When no mutants survived filtering, explain why instead of "100%"
    if self.total == 0:
        lines = [f"Mutation score: 0/0 (no mutants to test)  [{meta}]"]
        if is_method:
            lines.append(f"  method target: {target_label}")
        report = self.filter_report()
        if report:
            lines.append(f"  {report}")
        d = self.diagnostics
        generated = d.get("generated", 0)
        filtered = sum(
            d.get(k, 0)
            for k in (
                "filtered_ast_equivalent",
                "filtered_runtime_equivalent",
                "filtered_module_equivalent",
            )
        )
        if generated > 0 and filtered == generated:
            lines.append(
                "  All mutants were filtered as equivalent. "
                "Try filter_equivalent=False to inspect them."
            )
        elif generated == 0:
            lines.append(
                "  No mutation sites found in the source. "
                "Check that the target is correct and contains mutable code."
            )
        return "\n".join(lines)

    lines = [f"Mutation score: {self.score_text}  [{meta}]"]
    if is_method:
        lines.append(f"  method target: {target_label}")
    promoted_clusters = self.promoted_survivor_clusters()
    if self.survived and promoted_clusters:
        lines.append(
            f"  {len(self.survived)} test gap(s); "
            "promoted clusters highlight recurring weak boundaries:"
        )
        for cluster in promoted_clusters:
            ops = ", ".join(cluster["operators"])
            lines.append(
                "    cluster: "
                f"{cluster['owner']} -> {cluster['label']} "
                f"({cluster['size']} survivor(s), ops: {ops})"
            )
            contract_note = _contract_context_summary(cluster.get("contract_context"))
            if contract_note:
                lines.append(f"      contract: {contract_note}")
    elif self.survived:
        lines.append(
            f"  {len(self.survived)} test gap(s) remain, "
            "they are exploratory survivors, "
            "but none cluster strongly enough to promote beyond test-gap guidance."
        )
    for m in self.survived:
        header = f"  GAP {m.report_label}"
        if m.qualname:
            header += f"  @  {m.qualname}"
        lines.append(header)
        context_note = _contract_context_summary(
            _merge_semantic_context(self.contract_context, m.metadata)
        )
        if context_note:
            lines.append(f"    Contract: {context_note}")
        if remediation:
            lines.append(f"    Cause: mutant changes {m.description} and tests still pass.")
            lines.append(f"    Fix: {m.remediation}")
    # Kill attribution — which tests carry their weight
    attr = self.kill_attribution()
    if attr:
        lines.append("")
        lines.append("  Kill attribution (which tests caught which mutations):")
        for test, mutants in sorted(attr.items(), key=lambda x: -len(x[1])):
            ops = ", ".join(sorted({m.operator for m in mutants}))
            lines.append(f"    {test}: {len(mutants)} kill(s) [{ops}]")
    from ordeal.suggest import format_suggestions

    avail = format_suggestions(self)
    if avail:
        lines.append(f"\n{avail}")
    return "\n".join(lines)
summary.__qualname__ = "MutationResult.summary"
MutationResult.summary = summary
del summary
def generate_test_stubs(self) -> str:
    """Generate a Python test file for surviving mutants.

    Produces draft review stubs instead of runnable regressions.
    Each surviving mutant gets a test function with explicit review
    notes, a module-qualified call site, and either a pinned-behavior
    placeholder or a mined-invariant candidate.

    Returns an empty string when all mutants are killed.
    """
    if not self.survived:
        return ""

    target_spec = _resolve_mutation_target(self.target)
    if target_spec.leaf_name is None:
        return ""
    module_path = target_spec.module_name
    func_name = target_spec.leaf_name
    qual_parts = [*target_spec.qualname_parts, func_name]
    call_target = ".".join(qual_parts)
    safe_target = self.target.replace(".", "_")

    # Try to resolve the function signature for better review notes.
    sig_str = _review_signature(self.target)
    _, call_args = _resolve_signature(self.target)

    lines = [
        f'"""Draft review stubs for mutation gaps in {self.target}.',
        "",
        f"Generated by ordeal — {len(self.survived)} surviving mutant(s).",
        "These are review notes, not runnable regressions yet.",
        f"Reviewed signature: {sig_str}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"import {module_path} as _ordeal_target",
        "",
    ]
    contract_note = _contract_context_summary(self.contract_context)
    if contract_note:
        lines.append(f"# Contract context: {contract_note}")
    if target_spec.qualname_parts:
        lines.append(f"# Method target: {module_path}.{call_target}")
        lines.append("")

    for i, m in enumerate(self.survived, 1):
        test_name = f"test_{safe_target}_kill_{m.operator}_{i}"
        context = _merge_semantic_context(self.contract_context, m.metadata)
        boundary_label = _semantic_cluster_label(
            _mutant_semantic_tags(m, target=self.target, metadata=context)[0]
        )
        lines.append("")
        lines.append(f"def {test_name}():")
        lines.append("    # Review this draft before pinning it as a regression.")
        lines.append(f"    # Mutant: {m.report_label}")
        if m.qualname:
            lines.append(f"    # Owner: {m.qualname}")
        contract_note = _contract_context_summary(context)
        if contract_note:
            lines.append(f"    # Contract: {contract_note}")
        lines.append(f"    # Boundary: {boundary_label}")
        if m.source_line:
            lines.extend(_comment_lines(f"Source: {m.source_line}"))
        lines.extend(_comment_lines(f"Fix idea: {m.remediation}"))
        lines.append(f"    result = _ordeal_target.{call_target}({call_args})")
        inv = _suggest_invariant(self.target, func_name)
        if inv:
            lines.append(
                "    # Mined invariant candidate. Confirm this matches the intended contract."
            )
            lines.extend(_comment_lines(inv))
        else:
            lines.append(
                "    # Pinned behavior candidate. Replace this placeholder once reviewed."
            )
            lines.append("    # assert result == ...")
        lines.append("")

    return "\n".join(lines)
generate_test_stubs.__qualname__ = "MutationResult.generate_test_stubs"
MutationResult.generate_test_stubs = generate_test_stubs
del generate_test_stubs
