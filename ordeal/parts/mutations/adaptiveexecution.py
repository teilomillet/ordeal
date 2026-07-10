from __future__ import annotations


# ruff: noqa
@dataclass(frozen=True)
class _MutationTestSelection:
    """Pytest selection derived for a mutation target."""

    paths: tuple[str, ...]
    k_filter: str | None
    ast_scores: tuple[tuple[str, int], ...] = ()

    def pytest_args(self) -> list[str]:
        """Build positional pytest args plus any ``-k`` filter."""
        args = list(self.paths)
        if self.k_filter:
            args.extend(["-k", self.k_filter])
        return args


@dataclass
class _MutationExecutionProfile:
    """Local hints for ordering tests and choosing mutation workers."""

    kill_counts: dict[str, int] = field(default_factory=dict)
    mutant_killers: dict[str, str] = field(default_factory=dict)
    coverage_hits: tuple[str, ...] = ()
    collected_tests: int = 0
    mutant_count: int = 0
    pytest_seconds: float = 0.0
    workers: int = 1


def _mutant_profile_key(mutant: Mutant) -> str:
    """Return a stable key for a mutant's prior killer."""
    return "|".join(
        (
            mutant.operator,
            mutant.description,
            str(mutant.line),
            str(mutant.col),
            mutant.qualname or "",
        )
    )


def _mutation_profile_path(target: str) -> Path:
    """Return the ignored local path for adaptive mutation evidence."""
    safe = target.replace(":", "_").replace(".", "_")
    return Path(".ordeal") / "mutate-profiles" / f"{safe}.json"


def _load_mutation_execution_profile(target: str) -> _MutationExecutionProfile | None:
    """Load prior observations as hints, never as correctness evidence."""
    path = _mutation_profile_path(target)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _MutationExecutionProfile(
            kill_counts={
                str(nodeid): int(count)
                for nodeid, count in dict(payload.get("kill_counts", {})).items()
            },
            mutant_killers={
                str(key): str(nodeid)
                for key, nodeid in dict(payload.get("mutant_killers", {})).items()
            },
            coverage_hits=tuple(str(item) for item in payload.get("coverage_hits", [])),
            collected_tests=max(0, int(payload.get("collected_tests", 0))),
            mutant_count=max(0, int(payload.get("mutant_count", 0))),
            pytest_seconds=max(0.0, float(payload.get("pytest_seconds", 0.0))),
            workers=max(1, int(payload.get("workers", 1))),
        )
    except Exception:
        return None


def _save_mutation_execution_profile(
    target: str,
    profile: _MutationExecutionProfile,
) -> None:
    """Atomically save local adaptive observations."""
    path = _mutation_profile_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "target": target,
        "kill_counts": profile.kill_counts,
        "mutant_killers": profile.mutant_killers,
        "coverage_hits": list(profile.coverage_hits),
        "collected_tests": profile.collected_tests,
        "mutant_count": profile.mutant_count,
        "pytest_seconds": profile.pytest_seconds,
        "workers": profile.workers,
        "timestamp": time.time(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    selection_fn = globals().get("_mutation_test_selection")
    if selection_fn is not None:
        selection_fn.cache_clear()


def _resolve_mutation_worker_count(
    requested: int,
    *,
    mutant_count: int,
    selected_test_count: int,
    profile: _MutationExecutionProfile | None,
    disk_mutation: bool = False,
) -> int:
    """Choose workers from workload size and the prior in-session timing."""
    if requested < 0:
        raise ValueError("workers must be 0 (adaptive) or a positive integer")
    if disk_mutation:
        return 1
    if mutant_count <= 1:
        return 1
    if requested > 0:
        return min(requested, mutant_count)

    cap = min(max(1, os.cpu_count() or 1), 4, mutant_count)
    if cap == 1 or mutant_count < 24:
        return 1

    tests = max(1, selected_test_count)
    if profile is not None and profile.mutant_count > 0 and profile.pytest_seconds > 0:
        comparable_size = 0.5 <= mutant_count / profile.mutant_count <= 2.0
        if comparable_size:
            estimated_serial = profile.pytest_seconds * max(1, profile.workers)
            if estimated_serial < 0.12 or (tests <= 2 and estimated_serial < 0.25):
                return 1
            if estimated_serial >= 0.35 and mutant_count >= 28:
                return min(4, cap)
            if estimated_serial >= 0.20:
                return min(2, cap)

    if mutant_count >= 28 and tests >= 4:
        return min(4, cap)
    if mutant_count >= 24 and tests >= 8:
        return min(2, cap)
    if mutant_count >= 48:
        return min(4, cap)
    return 1


def _nodeid_matches_hint(nodeid: str, hint: str) -> bool:
    """Match a static test node hint to collected parametrized node IDs."""
    return nodeid == hint or nodeid.startswith(f"{hint}[")


def _order_mutation_test_items(
    items: Sequence[Any],
    *,
    mutant: Mutant,
    selection: _MutationTestSelection,
    coverage_hits: set[str],
    profile: _MutationExecutionProfile | None,
) -> list[Any]:
    """Rank likely killers first while retaining every collected fallback."""
    ast_scores = dict(selection.ast_scores)
    prior_counts = profile.kill_counts if profile is not None else {}
    prior_killer = (
        profile.mutant_killers.get(_mutant_profile_key(mutant)) if profile is not None else None
    )
    positions = {id(item): index for index, item in enumerate(items)}

    def _score(item: Any) -> tuple[int, int]:
        nodeid = str(item.nodeid)
        score = prior_counts.get(nodeid, 0) * 10_000
        if prior_killer == nodeid:
            score += 1_000_000
        if nodeid in coverage_hits:
            score += 1_000
        score += max(
            (
                static_score * 10
                for hint, static_score in ast_scores.items()
                if _nodeid_matches_hint(nodeid, hint)
            ),
            default=0,
        )
        return -score, positions[id(item)]

    return sorted(items, key=_score)


def _score_mutation_test_nodes(
    path: str,
    *,
    module_name: str,
    func_name: str | None,
) -> tuple[tuple[str, int], ...]:
    """Return direct-call AST scores for individual pytest test nodes."""
    try:
        source = Path(path).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=path)
    except Exception:
        return ()

    aliases: set[str] = set()
    direct_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    aliases.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom) and node.module == module_name:
            for alias in node.names:
                if alias.name == "*" or func_name is None or alias.name == func_name:
                    direct_names.add(alias.asname or alias.name)

    try:
        display_path = str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        display_path = str(Path(path))

    candidates: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            candidates.append((node.name, node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    child.name.startswith("test_")
                ):
                    candidates.append((f"{node.name}::{child.name}", child))

    scored: list[tuple[str, int]] = []
    for node_name, test_node in candidates:
        score = 0
        for current in ast.walk(test_node):
            if not isinstance(current, ast.Call):
                continue
            call = current.func
            if isinstance(call, ast.Name) and call.id in direct_names:
                score += 12 if func_name is not None else 4
            elif (
                isinstance(call, ast.Attribute)
                and isinstance(call.value, ast.Name)
                and call.value.id in aliases
                and (func_name is None or call.attr == func_name)
            ):
                score += 10 if func_name is not None else 4
        if score:
            scored.append((f"{display_path}::{node_name}", score))
    return tuple(scored)


def _calibrate_mutation_test_coverage(session: Any, target: str) -> set[str]:
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
    items = list(session.items)
    failures_before = session.testsfailed
    for index, item in enumerate(items):
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
            next_item = items[index + 1] if index + 1 < len(items) else None
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=next_item)
        finally:
            sys.setprofile(previous_profiler)
        if entered:
            hits.add(str(item.nodeid))
    session.testsfailed = failures_before
    return hits


def _record_mutation_execution_profile(
    target: str,
    results: Sequence[tuple[Mutant, bool, str | None, str | None]],
    *,
    coverage_hits: set[str],
    collected_tests: int,
    mutant_count: int,
    pytest_seconds: float,
    workers: int,
) -> None:
    """Merge observed killers and calibration into the local profile."""
    profile = _load_mutation_execution_profile(target) or _MutationExecutionProfile()
    for mutant, killed, _error, killer in results:
        if not killed or not killer:
            continue
        profile.kill_counts[killer] = min(1_000_000, profile.kill_counts.get(killer, 0) + 1)
        profile.mutant_killers[_mutant_profile_key(mutant)] = killer
    if coverage_hits:
        profile.coverage_hits = tuple(sorted(coverage_hits))
    if collected_tests > 0:
        profile.collected_tests = collected_tests
    profile.mutant_count = max(0, mutant_count)
    profile.pytest_seconds = max(0.0, pytest_seconds)
    profile.workers = max(1, workers)
    try:
        _save_mutation_execution_profile(target, profile)
    except OSError:
        pass
