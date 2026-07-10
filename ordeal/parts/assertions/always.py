from __future__ import annotations
# ruff: noqa
def always(
    condition: bool,
    name: str,
    *,
    mute: bool = False,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Assert *condition* every time — raises immediately on violation.

    Raises ``AssertionError`` immediately on violation — whether or not
    the tracker is active.  Violations are never silent by default.

    Pass ``mute=True`` to record the violation without raising.  The
    violation still appears in the property report (when ``--chaos`` is
    active) — it is tracked, not hidden.  Use this when a known issue
    is too loud and you need to focus on something else::

        always(not math.isnan(x), "no NaN", mute=True)  # tracked, not fatal

    When the tracker IS active (``--chaos``), the result is also recorded
    for the property report regardless of ``mute``.

    Args:
        condition: The boolean condition that must hold.
        name: Human-readable label for this assertion.
        mute: If ``True``, record violation without raising.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context included in the error message.
    """
    tracker.record(
        name,
        "always",
        condition,
        details or None,
        operation=operation,
        fault=fault,
    )
    if not condition and not mute:
        msg = f"always violated: {name}"
        if details:
            msg += f" | {details}"
        raise AssertionError(msg)
def sometimes(
    condition: bool | Callable[[], bool],
    name: str,
    *,
    attempts: int | None = None,
    warn: bool = False,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Assert *condition* at least once — deferred, checked at session end.

    Simple — deferred, checked at session end::

        sometimes(score > 0.5, "high scores exist")

    With ``warn=True`` — visible in normal pytest (no --chaos needed)::

        sometimes(score > 0.5, "high scores exist", warn=True)

    With ``attempts`` — immediate, standalone, no tracker needed::

        sometimes(lambda: cache.hit_rate() > 0, "cache warms up", attempts=100)

    Args:
        condition: Boolean or callable condition that should eventually hold.
        name: Human-readable label for this assertion.
        attempts: Polling attempts for immediate standalone use.
        warn: If True, print to stderr even when tracker is inactive.
            Useful for pre-flight checklists where findings should be
            visible in normal test runs.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context stored with a first failure.
    """
    if attempts is not None and callable(condition):
        for _ in range(attempts):
            if condition():
                tracker.record(
                    name,
                    "sometimes",
                    True,
                    details or None,
                    operation=operation,
                    fault=fault,
                )
                return
        tracker.record(
            name,
            "sometimes",
            False,
            details or None,
            operation=operation,
            fault=fault,
        )
        raise AssertionError(f"sometimes: never true in {attempts} attempts: {name}")

    cond = condition() if callable(condition) else condition
    was_active = tracker.record(
        name,
        "sometimes",
        cond,
        details or None,
        operation=operation,
        fault=fault,
    )
    if not was_active:
        if warn:
            # Use print (stdout) so pytest captures and shows it visibly
            status = "PASS" if cond else "OBSERVE"
            detail_str = f" | {details}" if details else ""
            print(f"  ordeal: sometimes({name!r}): {status}{detail_str}")
        else:
            warnings.warn(
                f"sometimes({name!r}) called but tracker is inactive — this is a no-op. "
                "Run with --chaos or call auto_configure() to enable property tracking.",
                stacklevel=2,
            )
def reachable(
    name: str,
    *,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Assert this code path executes at least once — deferred, checked at session end.

    Args:
        name: Human-readable label for this reachability assertion.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context for the property report.
    """
    was_active = tracker.record_hit(
        name,
        "reachable",
        operation=operation,
        fault=fault,
    )
    if not was_active:
        warnings.warn(
            f"reachable({name!r}) called but tracker is inactive — this is a no-op. "
            f"Run with --chaos or call auto_configure() to enable property tracking.",
            stacklevel=2,
        )
def unreachable(
    name: str,
    *,
    mute: bool = False,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Assert this code path never executes — raises immediately if reached.

    Raises ``AssertionError`` immediately — whether or not the tracker
    is active.  Violations are never silent by default.

    Pass ``mute=True`` to record the hit without raising.  The hit
    still appears in the property report — it is tracked, not hidden.

    Args:
        name: Human-readable label for this assertion.
        mute: If ``True``, record the hit without raising.
        operation: Optional operation dimension for reliability coverage.
        fault: Optional fault dimension. Must be provided with ``operation``.
        **details: Extra context included in the error message.
    """
    tracker.record_hit(
        name,
        "unreachable",
        operation=operation,
        fault=fault,
    )
    if not mute:
        msg = f"unreachable code reached: {name}"
        if details:
            msg += f" | {details}"
        raise AssertionError(msg)
def catalog() -> list[dict[str, str]]:
    """Discover all assertion functions via runtime introspection.

    Returns a list of dicts with ``name``, ``signature``, and ``doc``.
    Fully automatic — scans all public functions in this module.
    New assertion functions appear without registration.
    """
    import inspect as _inspect
    import sys

    mod = sys.modules[__name__]
    entries: list[dict[str, str]] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_") or attr_name in ("catalog", "report"):
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or _inspect.isclass(obj):
            continue
        # Only include functions defined in this module
        if getattr(obj, "__module__", None) != __name__:
            continue
        try:
            sig = str(_inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(...)"
        entries.append(
            {
                "name": attr_name,
                "qualname": f"ordeal.assertions.{attr_name}",
                "signature": sig,
                "doc": (_inspect.getdoc(obj) or "").split("\n")[0],
            }
        )
    return entries
