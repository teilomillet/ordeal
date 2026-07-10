from __future__ import annotations
# ruff: noqa
def _scan_crash_promoted(
    *,
    category: str | None,
    replayable: bool | None,
    proof_bundle: Mapping[str, Any] | None = None,
    sink_categories: Sequence[str] = (),
) -> bool:
    """Return whether one crash should count as a promoted finding."""
    if category != "likely_bug":
        return False
    critical_sinks = _proof_bundle_critical_sinks(proof_bundle)
    if critical_sinks is None:
        critical_sinks = _critical_security_sinks(sink_categories)
    if not critical_sinks:
        return True
    return isinstance(proof_bundle, Mapping) and _proof_bundle_replayable(proof_bundle, replayable)
def _reportable_crash_category(
    *,
    category: str | None,
    replayable: bool | None,
    proof_bundle: Mapping[str, Any] | None = None,
    sink_categories: Sequence[str] = (),
) -> str:
    """Return the user-facing crash category after proof-based promotion gating."""
    normalized = str(category or "speculative_crash")
    if normalized == "likely_bug" and not _scan_crash_promoted(
        category=normalized,
        replayable=replayable,
        proof_bundle=proof_bundle,
        sink_categories=sink_categories,
    ):
        return "speculative_crash"
    return normalized
def _sink_signal_for_bucket(bucket: str, sink_categories: Sequence[str]) -> float:
    """Return the sink weight for one semantic bucket."""
    weights = [
        _SECURITY_SINK_WEIGHTS.get(category, 0.0)
        for category in _SECURITY_BUCKET_TO_SINKS.get(bucket, ())
        if category in sink_categories
    ]
    return max(weights, default=0.0)
