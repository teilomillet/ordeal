from __future__ import annotations


# ruff: noqa
_PytestItemIdentity = tuple[str, str, str]


def _pytest_item_identity(
    nodeid: str,
    *,
    path: object | None = None,
) -> _PytestItemIdentity:
    """Return raw and canonical identities for one pytest node ID."""
    node_path, separator, remainder = nodeid.partition("::")
    canonical_path = Path(path) if path is not None else Path(node_path)
    return nodeid, str(canonical_path.resolve()), remainder if separator else ""


def _current_pytest_item_identity() -> _PytestItemIdentity | None:
    """Return the outer pytest item that invoked mutation testing, if any."""
    current = os.environ.get("PYTEST_CURRENT_TEST")
    if not current:
        return None
    for phase in (" (setup)", " (call)", " (teardown)"):
        if current.endswith(phase):
            current = current[: -len(phase)]
            break
    return _pytest_item_identity(current)


def _eligible_mutation_test_items(
    items: Sequence[Any],
    excluded: _PytestItemIdentity | None,
) -> list[Any]:
    """Exclude only the active outer pytest item from a nested mutation run."""
    if excluded is None:
        return list(items)
    return [
        item
        for item in items
        if not (
            (identity := _pytest_item_identity(
                str(item.nodeid),
                path=getattr(item, "path", None),
            ))[0]
            == excluded[0]
            or identity[1:] == excluded[1:]
        )
    ]


def _calibrate_mutation_test_coverage(
    session: Any,
    target: str,
    *,
    items: Sequence[Any] | None = None,
) -> set[str]:
    """Observe target entry per test inside the already-open pytest session."""
    target_spec = _resolve_mutation_target(target)
    target_code = None
    target_file = None
    if target_spec.leaf_name is not None:
        try:
            target_code = _unwrap_func(_resolved_target_callable(target_spec)).__code__
        except Exception:
            target_code = None
    else:
        source_path = getattr(target_spec.module, "__file__", None)
        if source_path is not None:
            target_file = str(Path(source_path).resolve())
    if target_code is None and target_file is None:
        return set()

    hits: set[str] = set()
    selected_items = list(session.items if items is None else items)
    failures_before = session.testsfailed
    for index, item in enumerate(selected_items):
        entered = False
        previous_profiler = sys.getprofile()

        def _profile(frame: types.FrameType, event: str, arg: object) -> None:
            nonlocal entered
            if previous_profiler is not None:
                previous_profiler(frame, event, arg)
            if event != "call":
                return
            if target_code is not None and frame.f_code is target_code:
                entered = True
            elif target_file is not None:
                entered = entered or (
                    frame.f_globals.get("__name__") == target_spec.module_name
                    and str(Path(frame.f_code.co_filename).resolve()) == target_file
                )

        try:
            sys.setprofile(_profile)
            next_item = (
                selected_items[index + 1]
                if index + 1 < len(selected_items)
                else None
            )
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=next_item)
        finally:
            sys.setprofile(previous_profiler)
        if entered:
            hits.add(str(item.nodeid))
    session._ordeal_mutation_baseline_failed = session.testsfailed > failures_before
    session.testsfailed = failures_before
    return hits


def _mutation_test_baseline_fails(
    session: Any,
    *,
    items: Sequence[Any] | None = None,
) -> bool:
    """Run every selected original test once and restore pytest failure state."""
    failures_before = session.testsfailed
    selected_items = list(session.items if items is None else items)
    for index, item in enumerate(selected_items):
        next_item = (
            selected_items[index + 1]
            if index + 1 < len(selected_items)
            else None
        )
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=next_item)
    failed = session.testsfailed > failures_before
    session.testsfailed = failures_before
    return failed
