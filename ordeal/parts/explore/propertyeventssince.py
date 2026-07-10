from __future__ import annotations
# ruff: noqa
def _property_events_since(
    before: dict[str, tuple[str, int, int, int]],
    after: dict[str, tuple[str, int, int, int]],
) -> list[dict[str, Any]]:
    """Return property deltas observed between two tracker snapshots."""
    events: list[dict[str, Any]] = []
    for name, (prop_type, hits, passes, failures) in after.items():
        _, before_hits, before_passes, before_failures = before.get(name, (prop_type, 0, 0, 0))
        delta_hits = hits - before_hits
        delta_passes = passes - before_passes
        delta_failures = failures - before_failures
        if delta_hits <= 0 and delta_passes <= 0 and delta_failures <= 0:
            continue
        events.append(
            {
                "name": name,
                "type": prop_type,
                "delta_hits": delta_hits,
                "delta_passes": delta_passes,
                "delta_failures": delta_failures,
            }
        )
    return events
def _signal_name(signum: int) -> str:
    """Best-effort symbolic signal name."""
    try:
        return signal.Signals(signum).name
    except Exception:
        return f"SIG{signum}"
def _normalize_command(cmd: Any) -> str:
    """Compact command formatting for subprocess-oriented findings."""
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)
