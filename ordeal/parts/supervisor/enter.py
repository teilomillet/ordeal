from __future__ import annotations
# ruff: noqa
def __enter__(self) -> DeterministicSupervisor:
    """Activate deterministic mode: seed RNGs, patch I/O, start logging.

    Controls every entropy source a Python library can reach:

    **RNG seeding** (same seed = identical random sequences):
    - ``random`` module — all functions
    - buggify — thread-local fault RNG
    - numpy — if installed
    - Hypothesis — database disabled (no cross-run leakage),
      but internal RNG uses source hash, not our seed.
      Mine() property confidence is APPROXIMATE, not exact.

    **I/O patching** (deterministic, no real disk/network/time):
    - ``time.time()`` / ``time.sleep()`` → ``simulate.Clock``
    - ``builtins.open()`` → ``simulate.FileSystem`` (in-memory)
    - ``socket.create_connection`` → raises ``ConnectionRefusedError``
    - ``subprocess.run`` / ``check_output`` / ``Popen`` → deterministic,
      registered child processes on the supervisor clock

    **Thread tracking** (observable, logged):
    - ``threading.Thread.start`` → wrapped to log thread creation
      in the trajectory. Thread scheduling is still OS-controlled
      but thread creation is deterministic and visible.
    - ``spawn()`` / ``yield_now()`` / ``sleep()`` → cooperative tasks
      scheduled deterministically from a seed-derived scheduler RNG

    **What remains non-deterministic** (OS-level, cannot control):
    - Thread interleaving ORDER within the OS scheduler (unless you
      model the work as cooperative scheduled tasks)
    - GC finalization timing
    - ``ctypes`` / C extension side effects
    """
    self._active_patches: list[Any] = []

    # -- 1. Seed Python's random module --
    self._saved_random_state = random.getstate()
    random.seed(self.seed)

    # -- 2. Hypothesis --
    # Hypothesis has its own internal RNG. In supervisor mode we force
    # derandomized execution and disable the example database so
    # property mining and scan phases are reproducible across runs.
    #
    # Important nuance: Hypothesis's derandomized trajectory is tied to
    # the test source, not to ``self.seed``. The supervisor seed still
    # controls Python RNGs, buggify, and mutation loops; Hypothesis gets
    # exact replay, but not seed-based exploration diversity.
    try:
        from hypothesis import settings

        self._saved_hypothesis_settings = settings.default
        settings.default = settings(
            settings.default,
            database=None,
            derandomize=True,
        )
    except Exception:
        self._saved_hypothesis_settings = None

    # -- 3. Seed buggify --
    try:
        from ordeal.buggify import activate, set_seed

        activate()
        set_seed(self.seed)
    except Exception:
        pass

    # -- 4. Seed numpy --
    try:
        import numpy as np

        np.random.seed(self.seed)  # type: ignore[attr-defined]
    except ImportError:
        pass

    # -- 5. Patch time → deterministic Clock --
    self._time_patch = unittest.mock.patch("time.time", side_effect=self.clock.time)
    self._sleep_patch = unittest.mock.patch("time.sleep", side_effect=self.clock.sleep)
    self._time_patch.start()
    self._sleep_patch.start()

    # -- 6-8. I/O patching (opt-in via patch_io=True) --
    # When patch_io is True, all I/O is deterministic:
    #   - open() routes to in-memory FileSystem
    #   - socket connections are refused (no network)
    #   - subprocess calls use registered deterministic outputs
    #   - thread creation is logged in the trajectory
    # When patch_io is False (default), only RNGs and time are
    # patched — real I/O works normally. Use patch_io=True when
    # testing I/O-heavy or multithreaded user code.
    self.filesystem = None
    if self.patch_io:
        from ordeal.simulate import FileSystem

        self.filesystem = FileSystem()
        self._open_patch = unittest.mock.patch(
            "builtins.open", side_effect=self._deterministic_open
        )
        self._open_patch.start()

        self._socket_patch = unittest.mock.patch(
            "socket.create_connection",
            side_effect=ConnectionRefusedError("DeterministicSupervisor: network disabled"),
        )
        self._socket_patch.start()

        self._subprocess_run_patch = unittest.mock.patch(
            "subprocess.run",
            side_effect=self._deterministic_run,
        )
        self._subprocess_run_patch.start()

        self._check_output_patch = unittest.mock.patch(
            "subprocess.check_output",
            side_effect=self._deterministic_check_output,
        )
        self._check_output_patch.start()

        self._popen_patch = unittest.mock.patch(
            "subprocess.Popen",
            side_effect=self._deterministic_popen,
        )
        self._popen_patch.start()

        import threading as _threading

        self._original_thread_start = _threading.Thread.start
        self._thread_count = 0

        def _tracked_start(thread_self: Any) -> None:
            self._thread_count += 1
            self.log_transition(f"thread_start({thread_self.name})")
            self._original_thread_start(thread_self)

        self._thread_patch = unittest.mock.patch.object(_threading.Thread, "start", _tracked_start)
        self._thread_patch.start()

    return self
__enter__.__qualname__ = "DeterministicSupervisor.__enter__"
DeterministicSupervisor.__enter__ = __enter__
del __enter__
def _resolve_subprocess(self, command: Any) -> _RegisteredSubprocess:
    """Find the deterministic registration for a subprocess command."""
    command_str = _normalize_command(command)
    for entry in reversed(self._registered_subprocesses):
        if entry.match == "exact" and command_str == entry.command:
            return entry
        if entry.match == "contains" and entry.command in command_str:
            return entry
    raise FileNotFoundError(
        "DeterministicSupervisor: subprocess not registered: "
        f"{command_str!r}. Register it with supervisor.register_subprocess()."
    )
_resolve_subprocess.__qualname__ = "DeterministicSupervisor._resolve_subprocess"
DeterministicSupervisor._resolve_subprocess = _resolve_subprocess
del _resolve_subprocess
def _subprocess_stream_flags(kwargs: dict[str, Any]) -> tuple[bool, bool]:
    """Determine whether stdout/stderr are captured for this call."""
    capture_output = bool(kwargs.get("capture_output"))
    stdout_pipe = capture_output or kwargs.get("stdout") == _sp.PIPE
    stderr_pipe = capture_output or kwargs.get("stderr") == _sp.PIPE
    return stdout_pipe, stderr_pipe
_subprocess_stream_flags.__qualname__ = "DeterministicSupervisor._subprocess_stream_flags"
DeterministicSupervisor._subprocess_stream_flags = staticmethod(_subprocess_stream_flags)
del _subprocess_stream_flags
def _subprocess_text_mode(kwargs: dict[str, Any]) -> bool:
    """Whether subprocess output should be decoded to text."""
    return bool(
        kwargs.get("text")
        or kwargs.get("universal_newlines")
        or kwargs.get("encoding") is not None
    )
_subprocess_text_mode.__qualname__ = "DeterministicSupervisor._subprocess_text_mode"
DeterministicSupervisor._subprocess_text_mode = staticmethod(_subprocess_text_mode)
del _subprocess_text_mode
def _deterministic_run(self, *args: Any, **kwargs: Any) -> _sp.CompletedProcess[Any]:
    """Deterministic ``subprocess.run`` backed by registered outputs."""
    command = args[0] if args else kwargs.get("args")
    registration = self._resolve_subprocess(command)
    command_str = _normalize_command(command)
    timeout = kwargs.get("timeout")
    text_mode = self._subprocess_text_mode(kwargs)
    encoding = kwargs.get("encoding")
    errors = kwargs.get("errors")
    stdout_pipe, stderr_pipe = self._subprocess_stream_flags(kwargs)
    stdout_value = _decode_stream(
        registration.stdout,
        text=text_mode,
        encoding=encoding,
        errors=errors,
    )
    stderr_value = _decode_stream(
        registration.stderr,
        text=text_mode,
        encoding=encoding,
        errors=errors,
    )

    self._subprocess_calls += 1
    if timeout is not None and registration.delay > timeout:
        self.clock.sleep(timeout)
        self.log_transition(f"subprocess.run({command_str}) -> timeout")
        raise _sp.TimeoutExpired(
            command,
            timeout,
            output=stdout_value if stdout_pipe else None,
            stderr=stderr_value if stderr_pipe else None,
        )

    self.clock.sleep(registration.delay)
    self.log_transition(f"subprocess.run({command_str}) -> {registration.returncode}")

    result = _sp.CompletedProcess(
        args=command,
        returncode=registration.returncode,
        stdout=stdout_value if stdout_pipe else None,
        stderr=stderr_value if stderr_pipe else None,
    )

    if kwargs.get("check") and registration.returncode != 0:
        raise _sp.CalledProcessError(
            registration.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )

    return result
_deterministic_run.__qualname__ = "DeterministicSupervisor._deterministic_run"
DeterministicSupervisor._deterministic_run = _deterministic_run
del _deterministic_run
def _deterministic_check_output(self, *args: Any, **kwargs: Any) -> str | bytes:
    """Deterministic ``subprocess.check_output``."""
    kwargs = dict(kwargs)
    kwargs["stdout"] = _sp.PIPE
    kwargs["check"] = True
    result = self._deterministic_run(*args, **kwargs)
    return result.stdout if result.stdout is not None else ("" if kwargs.get("text") else b"")
_deterministic_check_output.__qualname__ = "DeterministicSupervisor._deterministic_check_output"
DeterministicSupervisor._deterministic_check_output = _deterministic_check_output
del _deterministic_check_output
def _deterministic_popen(self, *args: Any, **kwargs: Any) -> _DeterministicPopen:
    """Deterministic ``subprocess.Popen`` with virtual time semantics."""
    command = args[0] if args else kwargs.get("args")
    registration = self._resolve_subprocess(command)
    text_mode = self._subprocess_text_mode(kwargs)
    encoding = kwargs.get("encoding")
    errors = kwargs.get("errors")
    stdout_pipe, stderr_pipe = self._subprocess_stream_flags(kwargs)
    return _DeterministicPopen(
        self,
        command,
        registration,
        text=text_mode,
        encoding=encoding,
        errors=errors,
        stdout_pipe=stdout_pipe,
        stderr_pipe=stderr_pipe,
    )
_deterministic_popen.__qualname__ = "DeterministicSupervisor._deterministic_popen"
DeterministicSupervisor._deterministic_popen = _deterministic_popen
del _deterministic_popen
def _deterministic_open(self, path: Any, mode: str = "r", *a: Any, **kw: Any) -> Any:
    """Route open() through the in-memory FileSystem.

    Supports read/write for text and binary modes.  For paths not
    in the FileSystem, raises FileNotFoundError (deterministic).
    """
    import io

    path_str = str(path)
    if "w" in mode:
        # Write mode — capture to filesystem
        buf = io.BytesIO() if "b" in mode else io.StringIO()
        original_close = buf.close

        def _close_and_save() -> None:
            data = buf.getvalue()
            if isinstance(data, str):
                data = data.encode()
            self.filesystem.write(path_str, data)
            original_close()

        buf.close = _close_and_save  # type: ignore[assignment]
        return buf
    # Read mode — serve from filesystem
    try:
        data = self.filesystem.read(path_str)
    except FileNotFoundError:
        raise FileNotFoundError(  # noqa: B904
            f"DeterministicSupervisor: {path_str} not in filesystem. "
            "Pre-populate via supervisor.filesystem.write() for determinism."
        )
    if "b" in mode:
        return io.BytesIO(data)
    return io.StringIO(data.decode())
_deterministic_open.__qualname__ = "DeterministicSupervisor._deterministic_open"
DeterministicSupervisor._deterministic_open = _deterministic_open
del _deterministic_open
def __exit__(self, *exc: object) -> None:
    """Restore all patched functions and RNG states."""
    # Stop all patches in reverse order
    for patch_name in (
        "_thread_patch",
        "_popen_patch",
        "_check_output_patch",
        "_subprocess_run_patch",
        "_socket_patch",
        "_open_patch",
        "_sleep_patch",
        "_time_patch",
    ):
        patch = getattr(self, patch_name, None)
        if patch is not None:
            try:
                patch.stop()
            except RuntimeError:
                pass  # patch wasn't started

    # Restore random state
    if self._saved_random_state is not None:
        random.setstate(self._saved_random_state)

    # Restore Hypothesis settings
    if getattr(self, "_saved_hypothesis_settings", None) is not None:
        try:
            from hypothesis import settings

            settings.default = self._saved_hypothesis_settings
        except Exception:
            pass

    # Deactivate buggify
    try:
        from ordeal.buggify import deactivate

        deactivate()
    except Exception:
        pass
__exit__.__qualname__ = "DeterministicSupervisor.__exit__"
DeterministicSupervisor.__exit__ = __exit__
del __exit__
def log_transition(self, action: str, *, state_hash: int | None = None) -> None:
    """Record a state transition in the exploration trajectory.

    Args:
        action: Human-readable description of what happened
            (e.g. ``"toggle timeout fault"``, ``"call process()"``)
        state_hash: Hash of the current state after the action.
            If ``None``, auto-increments from the previous state.
    """
    prev = self._current_state
    if state_hash is not None:
        self._current_state = state_hash
    else:
        # Auto-hash: combine previous state with action for a unique
        # deterministic next-state when the caller doesn't provide one
        h = hashlib.md5(f"{prev}:{action}:{self._step}".encode()).hexdigest()  # noqa: S324
        self._current_state = int(h[:8], 16)

    self.trajectory.append(
        Transition(
            state_before=prev,
            action=action,
            state_after=self._current_state,
            step=self._step,
        )
    )
    self._step += 1
log_transition.__qualname__ = "DeterministicSupervisor.log_transition"
DeterministicSupervisor.log_transition = log_transition
del log_transition
def fork(self, new_seed: int | None = None) -> DeterministicSupervisor:
    """Create a new supervisor forked from the current state.

    The forked supervisor:
    - Starts from the current state (not from zero)
    - Uses a different seed (for exploring a different branch)
    - Inherits the trajectory up to this point

    This is how the Explorer can branch from a checkpoint:
    fork with a different seed → each fork explores a different
    path from the same known state.
    """
    fork_seed = new_seed if new_seed is not None else self.seed + self._step + 1
    forked = DeterministicSupervisor(seed=fork_seed, patch_io=self.patch_io)
    forked._current_state = self._current_state
    forked._step = self._step
    forked.trajectory = list(self.trajectory)  # copy history
    forked._scheduler_steps = self._scheduler_steps
    forked._task_results = dict(self._task_results)
    forked._registered_subprocesses = list(self._registered_subprocesses)
    forked._scheduler_rng.setstate(self._scheduler_rng.getstate())
    return forked
fork.__qualname__ = "DeterministicSupervisor.fork"
DeterministicSupervisor.fork = fork
del fork
def state(self) -> int:
    """Current state hash."""
    return self._current_state
state.__qualname__ = "DeterministicSupervisor.state"
DeterministicSupervisor.state = property(state)
del state
def visited_states(self) -> set[int]:
    """All states visited in this trajectory."""
    states = {t.state_before for t in self.trajectory}
    states |= {t.state_after for t in self.trajectory}
    return states
visited_states.__qualname__ = "DeterministicSupervisor.visited_states"
DeterministicSupervisor.visited_states = property(visited_states)
del visited_states
def unique_transitions(self) -> int:
    """Number of unique (state, action) pairs explored."""
    return len({(t.state_before, t.action) for t in self.trajectory})
unique_transitions.__qualname__ = "DeterministicSupervisor.unique_transitions"
DeterministicSupervisor.unique_transitions = property(unique_transitions)
del unique_transitions
def summary(self) -> str:
    """Human-readable exploration trajectory summary."""
    lines = [
        f"DeterministicSupervisor(seed={self.seed})",
        f"  PYTHONHASHSEED: {self.hash_seed}",
        f"  steps: {self._step}",
        f"  unique states: {len(self.visited_states)}",
        f"  unique transitions: {self.unique_transitions}",
        f"  clock: {self.clock.time():.1f}s simulated",
        f"  patch_io: {self.patch_io}",
        f"  subprocess calls: {self._subprocess_calls}",
        f"  scheduler steps: {self._scheduler_steps}",
        f"  pending tasks: {len(self.pending_tasks)}",
    ]
    if self.trajectory:
        lines.append("  trajectory (last 10):")
        for t in self.trajectory[-10:]:
            lines.append(str(t))
    return "\n".join(lines)
summary.__qualname__ = "DeterministicSupervisor.summary"
DeterministicSupervisor.summary = summary
del summary
def reproduction_info(self) -> dict[str, Any]:
    """Return everything needed to reproduce this exact execution.

    An AI assistant can save this and replay later::

        info = sup.reproduction_info()
        # Save to file, pass to another run, etc.
        # To reproduce: DeterministicSupervisor(seed=info["seed"])
    """
    return {
        "seed": self.seed,
        "hash_seed": self.hash_seed,
        "patch_io": self.patch_io,
        "steps": self._step,
        "unique_states": len(self.visited_states),
        "unique_transitions": self.unique_transitions,
        "final_state": self._current_state,
        "subprocess_calls": self._subprocess_calls,
        "scheduler_steps": self._scheduler_steps,
        "pending_tasks": len(self.pending_tasks),
        "completed_tasks": len(self._task_results),
    }
reproduction_info.__qualname__ = "DeterministicSupervisor.reproduction_info"
DeterministicSupervisor.reproduction_info = reproduction_info
del reproduction_info
# ============================================================================
# State Tree — navigable exploration tree with checkpoint and rollback
# ============================================================================


@dataclass
class StateNode:
    """A node in the exploration tree — one checkpointed state.

    Each node stores:
    - The state identity (hash) and a snapshot of the Python objects
    - Which actions have been taken from this state (children)
    - Which actions are POSSIBLE but untaken (the frontier)
    - The edge coverage at this point

    The tree grows as exploration proceeds.  The AI can navigate it:
    go deeper (explore a child), roll back (return to parent), or
    branch (try an untaken action from any visited node).
    """

    state_id: int
    parent_id: int | None = None
    action_from_parent: str | None = None
    depth: int = 0
    edges_at_checkpoint: int = 0
    children: dict[str, int] = field(default_factory=dict)
    snapshot: Any = field(default=None, repr=False)
    seed_at_checkpoint: int = 0
