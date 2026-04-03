"""Deterministic supervisor — control all sources of non-determinism.

The fundamental problem with testing: execution is non-deterministic.
``time.time()`` varies, ``random.random()`` varies, thread scheduling
varies, hash ordering varies.  The same code with the same inputs can
produce different behavior on consecutive runs.

This means:
- Failures may not reproduce (the non-determinism that triggered them is gone)
- State space exploration is inefficient (you revisit "the same" state but
  it behaves differently because of hidden entropy)
- You can't fork from a known state (the fork inherits different entropy)

The ``DeterministicSupervisor`` fixes this by controlling every entropy
source the Python runtime exposes:

1. **RNG seeding** — ``random``, ``buggify``, ``numpy`` (if present) all
   use the same seed.  Given the same seed, fault decisions and generated
   inputs are identical.
2. **Time** — ``time.time()`` and ``time.sleep()`` are replaced with
   ``simulate.Clock``.  Execution timing is deterministic.
3. **Process boundary** — ``subprocess.run`` / ``check_output`` /
   ``Popen`` can be registered against deterministic outputs and the
   supervisor clock.  Child-process interactions become replayable.
4. **Hash randomization** — ``PYTHONHASHSEED`` is logged (can't be changed
   at runtime, but knowing it enables exact reproduction).
5. **Scheduling** — cooperative tasks can ``spawn()``, ``yield_now()``,
   and ``sleep()`` against a seed-driven scheduler.  Same seed = same
   interleaving.  Different seeds explore different schedules.
6. **State trajectory** — every (state_hash, action, next_state_hash)
   transition is logged.  The exploration is a Markov chain that can be
   replayed, forked from any point, and analyzed for unexplored transitions.

This is ordeal's answer to Antithesis's deterministic hypervisor —
scoped to what a Python library can control (no OS scheduling, no VM),
but sufficient for reproducible exploration.

Usage::

    from ordeal.supervisor import DeterministicSupervisor

    with DeterministicSupervisor(seed=42) as sup:
        # All RNGs seeded, time is simulated, trajectory is logged
        result = my_function()
        sup.log_transition("called my_function", state_hash=hash(result))

    with DeterministicSupervisor(seed=42, patch_io=True) as sup:
        sup.register_subprocess(["worker", "--once"], stdout="ok\n", delay=2.0)
        # Child process call advances simulated time, not wall clock
        subprocess.check_output(["worker", "--once"])

    def worker(sup, log):
        log.append("start")
        yield sup.yield_now()
        log.append("resume")
        yield sup.sleep(5.0)
        log.append("done")

    with DeterministicSupervisor(seed=42) as sup:
        log = []
        sup.spawn("worker", worker, sup, log)
        sup.run_until_idle()

    # Replay: same seed → same execution
    with DeterministicSupervisor(seed=42) as sup:
        result2 = my_function()
        assert result2 == result  # deterministic

    # Inspect the exploration trajectory
    print(sup.trajectory)  # [(state0, action, state1), ...]

With ChaosTest::

    with DeterministicSupervisor(seed=42) as sup:
        # Hypothesis, buggify, Clock all share the seed
        # Every fault toggle, rule execution, and state transition is logged
        TestCase = chaos_for("myapp.scoring")
        test = TestCase("runTest")
        test.runTest()

    # The trajectory shows exactly which faults fired when
    for prev, action, next_s in sup.trajectory:
        print(f"  {prev:#06x} → {action} → {next_s:#06x}")

Scales with compute: deterministic execution means parallel workers
with different seeds explore genuinely different regions of the state
space.  No wasted compute on redundant paths.  Every seed is a unique,
reproducible exploration trajectory.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import os
import random
import subprocess as _sp
import unittest.mock
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

from ordeal.simulate import Clock


@dataclass
class Transition:
    """One step in the exploration trajectory."""

    state_before: int
    action: str
    state_after: int
    step: int = 0

    def __str__(self) -> str:
        before = f"{self.state_before:#06x}"
        after = f"{self.state_after:#06x}"
        return f"  [{self.step}] {before} -> {self.action} -> {after}"


@dataclass(frozen=True)
class _RegisteredSubprocess:
    """One deterministic subprocess registration."""

    command: str
    match: str
    stdout: bytes
    stderr: bytes
    returncode: int
    delay: float


def _normalize_command(command: Any) -> str:
    """Normalize subprocess args into one comparable command string."""
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


def _encode_stream(data: str | bytes) -> bytes:
    """Normalize subprocess stream data to bytes."""
    return data.encode() if isinstance(data, str) else data


def _decode_stream(
    data: bytes,
    *,
    text: bool,
    encoding: str | None,
    errors: str | None,
) -> str | bytes:
    """Convert bytes to the caller's requested subprocess stream type."""
    if not text:
        return data
    return data.decode(encoding or "utf-8", errors or "strict")


class _DeterministicPopen:
    """Minimal in-memory ``Popen`` backed by the supervisor clock."""

    def __init__(
        self,
        supervisor: "DeterministicSupervisor",
        args: Any,
        registration: _RegisteredSubprocess,
        *,
        text: bool,
        encoding: str | None,
        errors: str | None,
        stdout_pipe: bool,
        stderr_pipe: bool,
    ) -> None:
        self._supervisor = supervisor
        self.args = args
        self._command = _normalize_command(args)
        self._registration = registration
        self._text = text
        self._encoding = encoding
        self._errors = errors
        self._stdout_pipe = stdout_pipe
        self._stderr_pipe = stderr_pipe
        self._stdout_value = _decode_stream(
            registration.stdout,
            text=text,
            encoding=encoding,
            errors=errors,
        )
        self._stderr_value = _decode_stream(
            registration.stderr,
            text=text,
            encoding=encoding,
            errors=errors,
        )
        self.returncode: int | None = None
        self.pid = supervisor._next_pid
        supervisor._next_pid += 1
        supervisor._subprocess_calls += 1
        self._complete_at = supervisor.clock.time() + registration.delay
        self.stdout = None
        self.stderr = None
        if stdout_pipe:
            self.stdout = (
                io.StringIO(self._stdout_value) if text else io.BytesIO(self._stdout_value)
            )
        if stderr_pipe:
            self.stderr = (
                io.StringIO(self._stderr_value) if text else io.BytesIO(self._stderr_value)
            )
        supervisor.log_transition(f"subprocess.Popen({self._command}) pid={self.pid}")

    def _finish(self) -> int:
        """Mark the subprocess complete and log the exit once."""
        if self.returncode is None:
            self.returncode = self._registration.returncode
            self._supervisor.log_transition(
                f"subprocess.exit({self._command}) -> {self.returncode}"
            )
        return self.returncode

    def poll(self) -> int | None:
        """Return the process return code if it has completed."""
        if self.returncode is not None:
            return self.returncode
        if self._supervisor.clock.time() >= self._complete_at:
            return self._finish()
        return None

    def wait(self, timeout: float | None = None) -> int:
        """Wait for deterministic completion, advancing simulated time."""
        if self.returncode is not None:
            return self.returncode

        remaining = max(0.0, self._complete_at - self._supervisor.clock.time())
        if timeout is not None and remaining > timeout:
            self._supervisor.clock.sleep(timeout)
            raise _sp.TimeoutExpired(self.args, timeout)

        self._supervisor.clock.sleep(remaining)
        return self._finish()

    def communicate(
        self,
        input: Any = None,
        timeout: float | None = None,
    ) -> tuple[str | bytes | None, str | bytes | None]:
        """Wait for completion and return deterministic stdout/stderr."""
        del input
        self.wait(timeout=timeout)
        stdout = self._stdout_value if self._stdout_pipe else None
        stderr = self._stderr_value if self._stderr_pipe else None
        return stdout, stderr

    def kill(self) -> None:
        """Terminate the simulated subprocess immediately."""
        self.returncode = -9
        self._supervisor.log_transition(f"subprocess.kill({self._command})")

    def terminate(self) -> None:
        """Terminate the simulated subprocess gracefully."""
        self.returncode = -15
        self._supervisor.log_transition(f"subprocess.terminate({self._command})")

    def __enter__(self) -> "_DeterministicPopen":
        return self

    def __exit__(self, *exc: object) -> None:
        self.wait()


@dataclass(frozen=True)
class _SchedulerInstruction:
    """One instruction yielded by a cooperative scheduled task."""

    kind: str
    delay: float = 0.0


@dataclass
class _ScheduledTask:
    """Mutable state for one cooperative scheduled task."""

    name: str
    coroutine: Generator[_SchedulerInstruction, None, Any]
    wake_at: float = 0.0
    done: bool = False
    result: Any = None
    steps: int = 0


class DeterministicSupervisor:
    """Control all sources of non-determinism for reproducible exploration.

    A context manager that:

    1. Seeds every RNG in the process (``random``, ``buggify``, ``numpy``)
    2. Replaces ``time.time()``/``time.sleep()`` with a deterministic ``Clock``
    3. Can run cooperative tasks under a deterministic scheduler
    4. Logs every state transition as a Markov chain
    5. Records ``PYTHONHASHSEED`` for full reproduction

    The same seed produces the same exploration trajectory.  Different
    seeds explore different regions.  The trajectory can be replayed
    from any point.

    Attributes:
        seed: The RNG seed controlling this execution.
        clock: The deterministic ``Clock`` replacing ``time.time()``.
        trajectory: List of ``(state_before, action, state_after)`` transitions.
        hash_seed: The ``PYTHONHASHSEED`` value (for reproduction notes).
    """

    def __init__(self, seed: int = 42, *, patch_io: bool = False):
        """Create a deterministic supervisor.

        Args:
            seed: RNG seed for all entropy sources.
            patch_io: If True, also patch ``open()``, ``socket``, and
                ``threading.Thread.start`` for full I/O determinism.
                Default False — only RNGs and time are patched.
                Set True when testing I/O-heavy or multithreaded code.
        """
        self.seed = seed
        self.patch_io = patch_io
        self.clock = Clock()
        self.trajectory: list[Transition] = []
        self.hash_seed: str = os.environ.get("PYTHONHASHSEED", "random")
        self._step = 0
        self._current_state: int = 0
        self._patches: list[Any] = []
        self._saved_random_state: Any = None
        self._registered_subprocesses: list[_RegisteredSubprocess] = []
        self._subprocess_calls = 0
        self._next_pid = 1000
        self._scheduler_rng = random.Random(seed ^ 0x5EED5EED)
        self._scheduled_tasks: dict[str, _ScheduledTask] = {}
        self._task_results: dict[str, Any] = {}
        self._scheduler_steps = 0

    def register_subprocess(
        self,
        command: str | list[str] | tuple[str, ...],
        *,
        stdout: str | bytes = b"",
        stderr: str | bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
        match: str = "exact",
    ) -> None:
        """Register a deterministic subprocess result for ``patch_io=True``.

        This is the first step from process-local determinism toward a
        system substrate: code under test can cross a process boundary
        and still remain reproducible.  Registered commands run against
        the supervisor clock, never the OS scheduler.

        Args:
            command: Exact command or argv to match.
            stdout: Simulated standard output.
            stderr: Simulated standard error.
            returncode: Exit status returned by the child.
            delay: Simulated runtime in seconds.
            match: ``"exact"`` or ``"contains"``.
        """
        if match not in {"exact", "contains"}:
            raise ValueError("match must be 'exact' or 'contains'")
        if delay < 0:
            raise ValueError("delay must be >= 0")
        self._registered_subprocesses.append(
            _RegisteredSubprocess(
                command=_normalize_command(command),
                match=match,
                stdout=_encode_stream(stdout),
                stderr=_encode_stream(stderr),
                returncode=returncode,
                delay=delay,
            )
        )

    def clear_subprocesses(self) -> None:
        """Remove all deterministic subprocess registrations."""
        self._registered_subprocesses.clear()

    def yield_now(self) -> _SchedulerInstruction:
        """Yield control back to the deterministic cooperative scheduler."""
        return _SchedulerInstruction("yield")

    def sleep(self, seconds: float) -> _SchedulerInstruction:
        """Suspend the running cooperative task for *seconds* of simulated time."""
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        return _SchedulerInstruction("sleep", delay=seconds)

    def spawn(
        self,
        name: str,
        task: Callable[..., Any] | Generator[_SchedulerInstruction, None, Any] | Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Register a cooperative task with the deterministic scheduler.

        ``task`` may be either:

        - a generator function that yields ``yield_now()`` / ``sleep()``
        - an already-created generator
        - a plain callable (runs to completion on its first schedule slot)
        """
        if name in self._scheduled_tasks or name in self._task_results:
            raise ValueError(f"Task {name!r} already exists")

        if inspect.isgenerator(task):
            if args or kwargs:
                raise ValueError("Cannot pass args/kwargs when spawning a generator object")
            coroutine = task
        else:
            candidate = task(*args, **kwargs) if callable(task) else task
            if inspect.isgenerator(candidate):
                coroutine = candidate
            else:
                coroutine = self._immediate_task(candidate)

        self._scheduled_tasks[name] = _ScheduledTask(name=name, coroutine=coroutine)
        self.log_transition(f"task.spawn({name})")

    @staticmethod
    def _immediate_task(value: Any) -> Generator[_SchedulerInstruction, None, Any]:
        """Wrap an immediate value in a generator-shaped task."""
        if False:
            yield _SchedulerInstruction("yield")
        return value

    @property
    def task_results(self) -> dict[str, Any]:
        """Results of cooperative tasks that have completed."""
        return dict(self._task_results)

    @property
    def pending_tasks(self) -> list[str]:
        """Names of cooperative tasks that have not finished yet."""
        return [name for name, task in self._scheduled_tasks.items() if not task.done]

    def run_until_idle(self, *, max_steps: int | None = None) -> dict[str, Any]:
        """Run scheduled tasks until all are done or *max_steps* is reached.

        The scheduler is cooperative: tasks run until they yield one of the
        scheduler instructions returned by :meth:`yield_now` or :meth:`sleep`.
        When no tasks are runnable, the supervisor clock advances to the next
        wake-up time deterministically.
        """
        while self.pending_tasks:
            if max_steps is not None and self._scheduler_steps >= max_steps:
                break

            runnable = [
                task
                for task in self._scheduled_tasks.values()
                if not task.done and task.wake_at <= self.clock.time()
            ]

            if not runnable:
                wake_at = min(
                    task.wake_at for task in self._scheduled_tasks.values() if not task.done
                )
                delta = max(0.0, wake_at - self.clock.time())
                self.clock.advance(delta)
                self.log_transition(f"scheduler.advance({delta:.3f})")
                continue

            task = runnable[self._scheduler_rng.randrange(len(runnable))]
            self.log_transition(f"scheduler.run({task.name})")
            self._scheduler_steps += 1
            task.steps += 1

            try:
                instruction = next(task.coroutine)
            except StopIteration as exc:
                task.done = True
                task.result = exc.value
                self._task_results[task.name] = exc.value
                self.log_transition(f"task.done({task.name})")
                continue
            except Exception as exc:
                self.log_transition(f"task.error({task.name}, {type(exc).__name__})")
                raise

            if not isinstance(instruction, _SchedulerInstruction):
                raise TypeError(
                    f"Task {task.name!r} yielded {type(instruction).__name__}, "
                    "expected supervisor.yield_now() or supervisor.sleep()."
                )

            match instruction.kind:
                case "yield":
                    task.wake_at = self.clock.time()
                    self.log_transition(f"task.yield({task.name})")
                case "sleep":
                    task.wake_at = self.clock.time() + instruction.delay
                    self.log_transition(f"task.sleep({task.name}, {instruction.delay:.3f})")
                case _:
                    raise ValueError(f"Unknown scheduler instruction: {instruction.kind!r}")

        return self.task_results

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

            self._thread_patch = unittest.mock.patch.object(
                _threading.Thread, "start", _tracked_start
            )
            self._thread_patch.start()

        return self

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

    @staticmethod
    def _subprocess_stream_flags(kwargs: dict[str, Any]) -> tuple[bool, bool]:
        """Determine whether stdout/stderr are captured for this call."""
        capture_output = bool(kwargs.get("capture_output"))
        stdout_pipe = capture_output or kwargs.get("stdout") == _sp.PIPE
        stderr_pipe = capture_output or kwargs.get("stderr") == _sp.PIPE
        return stdout_pipe, stderr_pipe

    @staticmethod
    def _subprocess_text_mode(kwargs: dict[str, Any]) -> bool:
        """Whether subprocess output should be decoded to text."""
        return bool(
            kwargs.get("text")
            or kwargs.get("universal_newlines")
            or kwargs.get("encoding") is not None
        )

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

    def _deterministic_check_output(self, *args: Any, **kwargs: Any) -> str | bytes:
        """Deterministic ``subprocess.check_output``."""
        kwargs = dict(kwargs)
        kwargs["stdout"] = _sp.PIPE
        kwargs["check"] = True
        result = self._deterministic_run(*args, **kwargs)
        return result.stdout if result.stdout is not None else ("" if kwargs.get("text") else b"")

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

    @property
    def state(self) -> int:
        """Current state hash."""
        return self._current_state

    @property
    def visited_states(self) -> set[int]:
        """All states visited in this trajectory."""
        states = {t.state_before for t in self.trajectory}
        states |= {t.state_after for t in self.trajectory}
        return states

    @property
    def unique_transitions(self) -> int:
        """Number of unique (state, action) pairs explored."""
        return len({(t.state_before, t.action) for t in self.trajectory})

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


class StateTree:
    """Navigable exploration tree with checkpoint, rollback, and branching.

    This is ordeal's answer to "remembering previous states."  The
    exploration is a tree, not a sequence.  At each state, multiple
    actions are possible.  The tree tracks which actions have been
    taken and which remain unexplored.

    The AI assistant navigates the tree::

        tree = StateTree()

        # Checkpoint the current state
        tree.checkpoint(state_id=0, snapshot=my_state)

        # Explore action A → reach state 1
        tree.checkpoint(state_id=1, parent=0, action="action_A",
                        snapshot=new_state)

        # Rollback to state 0
        old_state = tree.rollback(0)

        # Explore action B → reach state 2 (different branch)
        tree.checkpoint(state_id=2, parent=0, action="action_B",
                        snapshot=other_state)

        # What's unexplored?
        tree.frontier()  # states with untaken actions

    The tree is the single source of truth for the exploration.
    The AI reads it to decide where to go next.  ordeal provides
    the checkpoint/rollback/branch operations.  The AI is the
    search strategy.

    Integrates with DeterministicSupervisor: same seed at the same
    checkpoint → same exploration path.  Different seed → different
    branch.  The tree tracks which seeds have been tried at each node.
    """

    def __init__(self) -> None:
        self._nodes: dict[int, StateNode] = {}
        self._current: int | None = None

    def checkpoint(
        self,
        state_id: int,
        *,
        snapshot: Any = None,
        parent: int | None = None,
        action: str | None = None,
        edges: int = 0,
        seed: int = 0,
    ) -> StateNode:
        """Save a state as a node in the tree.

        Args:
            state_id: Unique identifier for this state (e.g. hash).
            snapshot: The Python object to checkpoint (deepcopied).
                Can be a ChaosTest instance, a dict, or any picklable
                object.  ``rollback()`` returns a deepcopy of this.
            parent: The state_id of the parent node (where we came from).
            action: The action that led from parent to this state.
            edges: Edge coverage count at this checkpoint.
            seed: The RNG seed used to reach this state.
        """
        # Deepcopy the snapshot so the checkpoint is independent
        saved = copy.deepcopy(snapshot) if snapshot is not None else None

        depth = 0
        if parent is not None and parent in self._nodes:
            depth = self._nodes[parent].depth + 1
            # Register this as a child of the parent
            if action:
                self._nodes[parent].children[action] = state_id

        node = StateNode(
            state_id=state_id,
            parent_id=parent,
            action_from_parent=action,
            depth=depth,
            edges_at_checkpoint=edges,
            snapshot=saved,
            seed_at_checkpoint=seed,
        )
        self._nodes[state_id] = node
        self._current = state_id
        return node

    def rollback(self, state_id: int) -> Any:
        """Roll back to a previously checkpointed state.

        Returns a deepcopy of the snapshot saved at that checkpoint.
        The tree is not modified — you can rollback and branch as
        many times as you want from any node.

        Args:
            state_id: The state to roll back to.

        Returns:
            A deepcopy of the checkpointed snapshot, or ``None`` if
            no snapshot was saved.

        Raises:
            KeyError: If ``state_id`` was never checkpointed.
        """
        if state_id not in self._nodes:
            raise KeyError(f"State {state_id:#06x} not in tree")
        node = self._nodes[state_id]
        self._current = state_id
        return copy.deepcopy(node.snapshot) if node.snapshot is not None else None

    @property
    def current(self) -> StateNode | None:
        """The current node in the tree."""
        if self._current is None:
            return None
        return self._nodes.get(self._current)

    @property
    def size(self) -> int:
        """Number of checkpointed states."""
        return len(self._nodes)

    @property
    def max_depth(self) -> int:
        """Deepest node in the tree."""
        if not self._nodes:
            return 0
        return max(n.depth for n in self._nodes.values())

    def frontier(self) -> list[StateNode]:
        """Nodes that could be explored further.

        A node is on the frontier if:
        - It has a snapshot (can be rolled back to)
        - It's a leaf (no children yet) OR hasn't been fully explored

        The AI reads this to decide where to branch next.
        """
        return [node for node in self._nodes.values() if node.snapshot is not None]

    def leaves(self) -> list[StateNode]:
        """Leaf nodes — deepest explored states."""
        return [node for node in self._nodes.values() if not node.children]

    def path_to(self, state_id: int) -> list[StateNode]:
        """Return the path from root to the given state.

        Useful for reproducing: the path is the sequence of actions
        that leads to this state.
        """
        path: list[StateNode] = []
        current = state_id
        while current is not None and current in self._nodes:
            node = self._nodes[current]
            path.append(node)
            current = node.parent_id
        path.reverse()
        return path

    def summary(self) -> str:
        """Human-readable tree summary."""
        lines = [
            f"StateTree: {self.size} nodes, depth {self.max_depth}",
            f"  leaves: {len(self.leaves())}",
            f"  frontier: {len(self.frontier())}",
        ]
        if self._current is not None:
            node = self._nodes[self._current]
            lines.append(f"  current: {node.state_id:#06x} (depth {node.depth})")

        # Show tree structure (compact)
        roots = [n for n in self._nodes.values() if n.parent_id is None]
        for root in roots:
            self._print_subtree(root, lines, indent=2)
        return "\n".join(lines)

    def _print_subtree(self, node: StateNode, lines: list[str], indent: int) -> None:
        """Recursively print the tree structure."""
        prefix = " " * indent
        label = node.action_from_parent or "root"
        edges = f" ({node.edges_at_checkpoint} edges)" if node.edges_at_checkpoint else ""
        lines.append(f"{prefix}{label} -> {node.state_id:#06x}{edges}")
        for action, child_id in node.children.items():
            if child_id in self._nodes:
                self._print_subtree(self._nodes[child_id], lines, indent + 2)

    def to_json(self) -> str:
        """Serialize the tree structure (without snapshots) for persistence."""
        nodes = {}
        for sid, node in self._nodes.items():
            nodes[str(sid)] = {
                "state_id": node.state_id,
                "parent_id": node.parent_id,
                "action_from_parent": node.action_from_parent,
                "depth": node.depth,
                "edges_at_checkpoint": node.edges_at_checkpoint,
                "children": node.children,
                "seed_at_checkpoint": node.seed_at_checkpoint,
                "has_snapshot": node.snapshot is not None,
            }
        return json.dumps({"nodes": nodes, "current": self._current}, indent=2)
