from __future__ import annotations
# ruff: noqa
def declare(
    name: str,
    prop_type: str,
    *,
    operation: str | None = None,
    fault: str | None = None,
    **details: Any,
) -> None:
    """Register a deferred property or reliability expectation before observations.

    ``sometimes()`` and ``reachable()`` are observational by default: if the
    call site is never reached, there is nothing to track.  ``declare()``
    makes the expectation explicit so the property can fail at session end
    even when it was never observed.  With ``operation`` and ``fault``, all
    four property types can be declared as reliability cells; an unseen cell
    reports ``NOT EXERCISED`` rather than pretending it passed or failed.

    Typical use::

        declare("timeout handler runs", "reachable")
        declare("cache warms up", "sometimes")
        declare(
            "eventual_commit",
            "always",
            operation="create_order",
            fault="worker_restart",
        )

    Then, elsewhere in the code under test::

        reachable("timeout handler runs")
        sometimes(cache_hit, "cache warms up")

    Args:
        name: Human-readable property label.
        prop_type: ``"reachable"`` or ``"sometimes"`` for a plain
            declaration; any assertion type for a reliability cell.
        operation: Operation dimension for reliability coverage.
        fault: Fault dimension for reliability coverage. Must be provided
            together with ``operation``.
        **details: Optional metadata stored with the property.
    """
    contextual = operation is not None or fault is not None
    if not contextual and prop_type not in _DEFERRED_PROPERTY_TYPES:
        raise ValueError(
            "declare() only supports deferred property types: 'sometimes' or 'reachable'"
        )
    if contextual and prop_type not in {"always", "sometimes", "reachable", "unreachable"}:
        raise ValueError(
            "contextual declare() type must be 'always', 'sometimes', 'reachable', "
            "or 'unreachable'"
        )
    if contextual:
        if operation is None or fault is None:
            raise ValueError("operation and fault must be provided together")
        was_active = tracker.declare_reliability(
            name,
            prop_type,
            operation=operation,
            fault=fault,
            details=details or None,
        )
    else:
        with tracker._lock:
            if not tracker._active:
                was_active = False
            else:
                prop = tracker._properties.get(name)
                if prop is None:
                    tracker._properties[name] = Property(
                        name=name,
                        type=prop_type,
                        first_failure_details=details or None,
                    )
                elif prop.type != prop_type:
                    raise ValueError(
                        f"Property {name!r} already declared as {prop.type!r}, "
                        f"cannot redeclare as {prop_type!r}"
                    )
                elif prop.first_failure_details is None and details:
                    prop.first_failure_details = details
                was_active = True
    if not was_active:
        warnings.warn(
            f"declare({name!r}, {prop_type!r}) called but tracker is inactive — this is a no-op. "
            "Run with --chaos or call auto_configure() to enable property tracking.",
            stacklevel=2,
        )
