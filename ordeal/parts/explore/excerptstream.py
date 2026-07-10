from __future__ import annotations
# ruff: noqa
def _excerpt_stream(value: Any, limit: int = 160) -> str | None:
    """Return a short human-readable stdout/stderr excerpt."""
    if value in (None, b"", ""):
        return None
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", "replace")
        except Exception:
            text = repr(value)
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return None
    if len(text) > limit:
        return text[:limit] + "..."
    return text
def _extract_native_boundary(error: BaseException) -> dict[str, Any] | None:
    """Classify subprocess/native-boundary failures into structured metadata."""
    try:
        import subprocess as _sp
    except Exception:
        return None

    if isinstance(error, _sp.TimeoutExpired):
        return {
            "boundary": "subprocess",
            "mode": "timeout",
            "command": _normalize_command(getattr(error, "cmd", "")),
            "timeout": getattr(error, "timeout", None),
            "stdout": _excerpt_stream(getattr(error, "output", None)),
            "stderr": _excerpt_stream(getattr(error, "stderr", None)),
        }

    if isinstance(error, _sp.CalledProcessError):
        returncode = int(getattr(error, "returncode", 0))
        if returncode < 0:
            signum = abs(returncode)
            return {
                "boundary": "subprocess",
                "mode": "signal",
                "command": _normalize_command(getattr(error, "cmd", "")),
                "signal": signum,
                "signal_name": _signal_name(signum),
                "returncode": returncode,
                "stdout": _excerpt_stream(getattr(error, "output", None)),
                "stderr": _excerpt_stream(getattr(error, "stderr", None)),
            }
        return {
            "boundary": "subprocess",
            "mode": "exit_code",
            "command": _normalize_command(getattr(error, "cmd", "")),
            "returncode": returncode,
            "stdout": _excerpt_stream(getattr(error, "output", None)),
            "stderr": _excerpt_stream(getattr(error, "stderr", None)),
        }

    return None
def _format_native_boundary(boundary: dict[str, Any]) -> str:
    """Human-readable native-boundary summary."""
    mode = str(boundary.get("mode", "unknown"))
    command = boundary.get("command")
    if mode == "timeout":
        timeout = boundary.get("timeout")
        base = f"subprocess timeout ({timeout}s)" if timeout is not None else "subprocess timeout"
    elif mode == "signal":
        signame = boundary.get("signal_name") or _signal_name(int(boundary.get("signal", 0) or 0))
        base = f"subprocess died by {signame}"
    elif mode == "exit_code":
        base = f"subprocess exited with code {boundary.get('returncode')}"
    else:
        base = f"subprocess failure ({mode})"
    if command:
        base += f" [{command}]"
    stderr = boundary.get("stderr")
    if stderr:
        base += f": {stderr}"
    return base
def _format_swarm_config_summary(row: dict[str, Any]) -> str:
    """Compact one-line summary for one swarm configuration."""
    rule_count = len(row.get("active_rules", []))
    fault_count = len(row.get("active_faults", []))
    uses = int(row.get("times_used", 0))
    edges = int(row.get("edges_found", 0))
    failures = int(row.get("failure_count", 0))
    energy = float(row.get("energy", 0.0))
    return (
        f"rules={rule_count}, faults={fault_count}, uses={uses}, "
        f"edges={edges}, failures={failures}, energy={energy:.2f}"
    )
def _format_counter_summary(counter: Counter[str]) -> str:
    """Render a compact ``label xN`` summary."""
    if not counter:
        return ""
    return ", ".join(f"{label} x{count}" for label, count in counter.most_common(3))
def _top_property_stress(property_stress: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """Return the highest-signal property/fault stress hotspots."""
    rows: list[dict[str, Any]] = []
    for prop, fault_hits in property_stress.items():
        for fault_sig, hits in fault_hits.items():
            rows.append({"property": prop, "faults": fault_sig, "hits": int(hits)})
    rows.sort(key=lambda row: (-row["hits"], row["property"], row["faults"]))
    return rows
def _resolve_worker_count(workers: int) -> int:
    """Resolve requested workers to a safe concrete process count."""
    if workers > 0:
        return max(1, min(workers, _POOL_NUM_SLOTS))
    auto = os.cpu_count() or 1
    return max(1, min(auto, _AUTO_WORKER_CAP, _POOL_NUM_SLOTS))
def _format_exception_traceback(error: BaseException) -> str:
    """Best-effort formatted traceback for later inspection."""
    return "".join(_traceback.format_exception(type(error), error, error.__traceback__))
def _serialize_failure_payload(
    error: BaseException,
    *,
    worker_id: int,
    run_id: int,
    step: int,
    active_faults: list[str],
    rule_log: list[str],
    trace: Trace | None,
    error_traceback: str | None = None,
) -> dict[str, Any]:
    """Convert a worker-side failure into a transport-safe payload."""
    native_boundary = _extract_native_boundary(error)
    payload = {
        "worker_id": worker_id,
        "run_id": run_id,
        "step": step,
        "active_faults": list(active_faults),
        "rule_log": list(rule_log),
        "error_type": type(error).__name__,
        "error_module": type(error).__module__,
        "error_qualname": type(error).__qualname__,
        "error_message": str(error)[:1000],
        "error_traceback": (error_traceback or _format_exception_traceback(error))[:12000],
    }
    if native_boundary is not None:
        payload["native_boundary"] = native_boundary
    if trace is not None:
        payload["trace"] = trace.to_dict()
        payload["trace_hash"] = trace.content_hash()
    return payload
def _load_exception_type(module_name: str, qualname: str) -> type[Exception] | None:
    """Import an exception type when the worker serialized a real class."""
    if "<locals>" in qualname:
        return None
    try:
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        if isinstance(obj, type) and issubclass(obj, Exception):
            return obj
    except Exception:
        return None
    return None
def _deserialize_worker_exception(payload: dict[str, Any]) -> Exception:
    """Reconstruct the most faithful exception object we can in the parent."""
    module_name = str(payload.get("error_module", "builtins"))
    qualname = str(payload.get("error_qualname", payload.get("error_type", "RuntimeError")))
    message = str(payload.get("error_message", ""))
    exc_type = _load_exception_type(module_name, qualname)
    if exc_type is None:
        remote_name = payload.get("error_type", qualname)
        error = RuntimeError(f"{remote_name}: {message}" if message else str(remote_name))
    else:
        try:
            error = exc_type(message)
        except Exception:
            remote_name = payload.get("error_type", qualname)
            error = RuntimeError(f"{remote_name}: {message}" if message else str(remote_name))
    setattr(error, "__ordeal_remote_worker_id__", payload.get("worker_id"))
    setattr(error, "__ordeal_remote_traceback__", payload.get("error_traceback"))
    setattr(error, "error_type", payload.get("error_type", qualname))
    setattr(error, "remote_traceback", payload.get("error_traceback"))
    return error
def _deserialize_failure_payload(payload: dict[str, Any]) -> Failure:
    """Rebuild a worker failure, preserving type, traceback, and trace payload."""
    trace_payload = payload.get("trace")
    trace = Trace.from_dict(trace_payload) if isinstance(trace_payload, dict) else None
    return Failure(
        error=_deserialize_worker_exception(payload),
        step=int(payload.get("step", 0)),
        run_id=int(payload.get("run_id", -1)),
        active_faults=list(payload.get("active_faults", [])),
        rule_log=list(payload.get("rule_log", [])),
        trace=trace,
        error_traceback=payload.get("error_traceback"),
        native_boundary=payload.get("native_boundary"),
    )
def _parallel_failure_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    """Stable dedup key for crash spam from parallel workers."""
    trace_hash = payload.get("trace_hash")
    if trace_hash:
        return ("trace", trace_hash)
    return (
        payload.get("error_module"),
        payload.get("error_qualname"),
        payload.get("error_message"),
        payload.get("step"),
        tuple(payload.get("active_faults", [])),
        tuple(payload.get("rule_log", [])[-4:]),
    )
# ============================================================================
# Explorer
# ============================================================================


def _qualified_name(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"
class Explorer:
    """Coverage-guided stateful exploration with checkpoints, energy scheduling, and seed mutation.

    The core exploration engine.  Runs ChaosTest rule sequences while tracking
    edge coverage (AFL-style), checkpointing interesting states, and branching
    from them with three orthogonal exploration dimensions:

    - **Swarm**: each run uses a random fault subset (different failure environments)
    - **Energy**: checkpoints that led to new edges are selected more often
    - **Seed mutation**: rule parameters that led to new coverage are stored on
      checkpoints and mutated (via ``ordeal.mutagen``) on the next branch —
      the AFL closed-loop adapted for typed stateful testing

    Compared to Hypothesis (random search + shrinking), the Explorer finds bugs
    at the intersection of features — the class of bugs that random testing
    almost never reaches.

    Example::

        from ordeal.explore import Explorer

        explorer = Explorer(MyChaosTest, target_modules=["myapp"])
        result = explorer.run(max_time=60)
        print(result.summary())
        # → Exploration: 500 runs, 25000 steps, 60.0s
        # → Coverage: 142 edges, 38 checkpoints
        # → Seed mutations: 312 used, 47 productive
    """
def __init__(
    self,
    test_class: type,
    *,
    target_modules: list[str] | None = None,
    seed: int = 42,
    max_checkpoints: int = 256,
    checkpoint_prob: float = 0.4,
    checkpoint_strategy: str = "energy",
    fault_toggle_prob: float = 0.3,
    record_traces: bool = False,
    workers: int = 1,
    share_edges: bool = True,
    share_checkpoints: bool = True,
    mutation_targets: list[str] | None = None,
    seed_mutation_prob: float | None = None,
    seed_mutation_respect_strategies: bool = False,
    ngram: int = 2,
    corpus_dir: str | Path | None = None,
    rule_swarm: bool = False,
) -> None:
    """Initialize the exploration engine.

    Args:
        test_class: A ChaosTest subclass to explore.
        target_modules: Dotted module names for coverage (e.g. ``["myapp"]``).
        seed: RNG seed for reproducible runs.
        max_checkpoints: Checkpoint corpus size limit.
        checkpoint_prob: Probability of starting from a checkpoint.
        checkpoint_strategy: ``"energy"`` | ``"uniform"`` | ``"recent"``.
        fault_toggle_prob: Probability of nemesis action per step.
        record_traces: If True, keep full traces in the result.
        workers: Number of parallel worker processes. ``0`` means auto
            (uses ``os.cpu_count()``). Default ``1`` (sequential).
        mutation_targets: Dotted paths to functions to mutate
            (e.g. ``["myapp.service.process"]``).  Mutations become
            faults that the nemesis toggles during exploration.
            Killed mutants = your tests catch the bug.  Surviving
            mutants = test gap found.
        share_edges: When ``workers > 1``, use a shared-memory edge
            bitmap so workers skip edges already found by others.
            AFL-style: one byte per edge hash, single-byte atomic
            writes, zero locks.  Default ``True``.
        share_checkpoints: When ``workers > 1``, share checkpoints
            between workers via a shared-memory ring buffer.  Workers
            publish discoveries and subscribe to others' finds with
            global energy propagation — a checkpoint that leads to
            new edges for any worker gets higher priority for all.
            Default ``True``.
        seed_mutation_prob: Probability of mutating a productive seed
            instead of generating fresh parameters when branching from
            a checkpoint.  Default ``0.25`` (25%), matching the ratio
            used in ``mine()``'s Phase 2.  Set to ``0.0`` to disable
            seed mutation entirely.  Higher values (up to ``1.0``)
            make the explorer more exploitation-focused — useful when
            the rule parameter space is large relative to the state
            space.  See ``ordeal.mutagen`` for the mutation engine.
        seed_mutation_respect_strategies: If True, mutate productive
            seeds but keep values within common bounds implied by the
            rule's declared Hypothesis strategies.  Useful for control-
            plane systems where "nearby but still valid" mutations are
            more informative than unconstrained fuzzing.
        ngram: N-gram depth for edge coverage hashing.  ``1`` gives
            classic AFL single-edge hashing (``prev_loc XOR cur_loc``).
            ``2`` (the default) hashes the last 2 locations with the
            current one, capturing which branch led to each edge.
            Higher values capture deeper path context but have
            diminishing returns for Python's coarse line-level tracing.
            See :class:`CoverageCollector` for the full rationale.
        rule_swarm: When True, each exploration run includes each
            rule with independent probability 0.5 (fair coin flip),
            per the swarm testing algorithm (Groce et al., ISSTA
            2012).  Disabling some rules per run forces others to
            accumulate state (e.g. only inserts, no deletes → cache
            grows large → GC triggers).  At least one rule is always
            kept.  Default ``False``.
        corpus_dir: Directory for the persistent seed corpus.  Failing
            traces are saved here and replayed automatically on the
            next run for instant regression detection.  Default
            ``".ordeal/seeds"``.  Set to ``None`` to disable.
    """
    self.test_class = test_class
    self.target_paths = [m.replace(".", "/") for m in (target_modules or [])]
    self.target_modules = target_modules
    self.rng = random.Random(seed)
    self.seed = seed
    self.max_checkpoints = max_checkpoints
    self.checkpoint_prob = checkpoint_prob
    self.checkpoint_strategy = checkpoint_strategy
    self.fault_toggle_prob = fault_toggle_prob
    self.record_traces = record_traces
    self.workers = _resolve_worker_count(workers)
    self.share_edges = share_edges
    self.share_checkpoints = share_checkpoints
    self.ngram = ngram
    self.mutation_targets = mutation_targets or []
    self.seed_mutation_prob = (
        seed_mutation_prob if seed_mutation_prob is not None else _SEED_MUTATION_PROB
    )
    self.seed_mutation_respect_strategies = seed_mutation_respect_strategies
    self.corpus_dir: Path | None = Path(corpus_dir) if corpus_dir is not None else None
    self.rule_swarm = rule_swarm

    # Shared-memory edge bitmap (set by _run_parallel / _worker_fn)
    # 65536 bytes — one byte per possible 16-bit edge hash.
    # Single-byte writes are atomic on all architectures.
    self._shared_bitmap: memoryview | None = None

    # Shared-memory state bitmap (set by _run_parallel / _worker_fn)
    self._shared_state_bitmap: memoryview | None = None

    # Shared-memory ring buffer for checkpoint exchange
    self._pool_ring: memoryview | None = None
    self._worker_id: int = 0
    self._pool_num_workers: int = 1
    self._pool_slots_per_worker: int = _POOL_NUM_SLOTS
    self._pool_next_slot: int = 0  # next slot to write (within our range)
    self._pool_write_seq: int = 0  # per-worker monotonic sequence
    self._pool_seen_seq: dict[int, int] = {}  # slot → last seen sequence
    self._pool_last_sync: float = 0.0
    self._pool_sync_interval: float = 0.5  # 500ms (was 2s for file-based)
    self._pool_auth_key: bytes | None = None

    # Internal state
    self._total_edges: set[int] = set()
    self._total_states: set[int] = set()
    self._satisfied_properties: set[str] = set()
    self._checkpoints: list[Checkpoint] = []
    self._rules: list[_RuleInfo] = []
    self._invariant_names: list[str] = []
    self._last_step_rule: tuple[str, dict[str, Any]] | None = None
    self._last_step_used_mutation: bool = False
    self._last_generated_params: dict[str, Any] = {}
    self._active_rules: list[_RuleInfo] = []  # set per run (swarm or full)
    self._active_fault_names: list[str] | None = None  # set per run (swarm or all)
    self._current_swarm_config: SwarmConfig | None = None  # current run's config
    self._swarm_configs: dict[
        tuple[tuple[str, ...], tuple[str, ...]], SwarmConfig
    ] = {}  # energy-tracked configs
    self._fault_pair_hits: Counter[tuple[str, str]] = Counter()
    self._current_run_properties: set[str] = set()
    self._rule_file_coverage: dict[str, set[str]] = {}  # rule_name -> {filenames}
    self._gap_files: set[str] = set()  # files with uncovered branches
    self._strategy_failures: dict[str, int] = {}
    self._checkpoint_feedback: deque[tuple[float, float, bool]] = deque(
        maxlen=_CHECKPOINT_FEEDBACK_WINDOW
    )
    self._effective_checkpoint_prob = checkpoint_prob
    self._checkpoint_restore_share = 0.0
__init__.__qualname__ = "Explorer.__init__"
Explorer.__init__ = __init__
del __init__
def _snapshot_machine(self, machine: ChaosTest) -> _MachineSnapshot:
    """Create a lightweight snapshot, with optional user-defined filtering."""
    if "clone_checkpoint_state" in type(machine).__dict__:
        state = machine.clone_checkpoint_state()
        if not isinstance(state, dict):
            raise TypeError("clone_checkpoint_state() must return a dict")
        fault_active = {f.name: f.active for f in machine._faults}
        return _MachineSnapshot(state_dict=state, fault_active=fault_active)

    snapshot_filter = getattr(machine, "checkpoint_snapshot_filter", None)
    legacy_snapshot_filter = getattr(machine, "snapshot_filter", None)
    state: dict[str, Any] = {}
    for k, v in machine.__dict__.items():
        if k == "_faults":
            continue
        if callable(snapshot_filter) and not snapshot_filter(k, v):
            continue
        if callable(legacy_snapshot_filter) and not legacy_snapshot_filter(k, v):
            continue
        try:
            state[k] = copy.deepcopy(v)
        except Exception:
            pass
    fault_active = {f.name: f.active for f in machine._faults}
    return _MachineSnapshot(state_dict=state, fault_active=fault_active)
_snapshot_machine.__qualname__ = "Explorer._snapshot_machine"
Explorer._snapshot_machine = _snapshot_machine
del _snapshot_machine
def _restore_machine(self, snapshot: _MachineSnapshot) -> ChaosTest:
    """Restore a fresh machine from a snapshot, with optional user hook."""
    machine = self.test_class()
    owns_checkpoint_isolation = (
        "clone_checkpoint_state" in type(machine).__dict__
        and "restore_checkpoint_state" in type(machine).__dict__
    )
    restore_checkpoint = None
    if "restore_checkpoint_state" in type(machine).__dict__:
        restore_checkpoint = getattr(machine, "restore_checkpoint_state", None)
    legacy_restore = getattr(machine, "restore_snapshot", None)
    if callable(restore_checkpoint):
        restore_checkpoint(
            snapshot.state_dict if owns_checkpoint_isolation else copy.deepcopy(snapshot.state_dict)
        )
    elif callable(legacy_restore):
        legacy_restore(copy.deepcopy(snapshot.state_dict))
    else:
        for k, v in snapshot.state_dict.items():
            try:
                machine.__dict__[k] = copy.deepcopy(v)
            except Exception:
                machine.__dict__[k] = v
    for f in machine._faults:
        was_active = snapshot.fault_active.get(f.name, False)
        if was_active and not f.active:
            f.activate()
        elif not was_active and f.active:
            f.deactivate()
    return machine
_restore_machine.__qualname__ = "Explorer._restore_machine"
Explorer._restore_machine = _restore_machine
del _restore_machine
