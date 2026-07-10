from __future__ import annotations
# ruff: noqa
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
__init__.__qualname__ = "DeterministicSupervisor.__init__"
DeterministicSupervisor.__init__ = __init__
del __init__
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
register_subprocess.__qualname__ = "DeterministicSupervisor.register_subprocess"
DeterministicSupervisor.register_subprocess = register_subprocess
del register_subprocess
def clear_subprocesses(self) -> None:
    """Remove all deterministic subprocess registrations."""
    self._registered_subprocesses.clear()
clear_subprocesses.__qualname__ = "DeterministicSupervisor.clear_subprocesses"
DeterministicSupervisor.clear_subprocesses = clear_subprocesses
del clear_subprocesses
def yield_now(self) -> _SchedulerInstruction:
    """Yield control back to the deterministic cooperative scheduler."""
    return _SchedulerInstruction("yield")
yield_now.__qualname__ = "DeterministicSupervisor.yield_now"
DeterministicSupervisor.yield_now = yield_now
del yield_now
def sleep(self, seconds: float) -> _SchedulerInstruction:
    """Suspend the running cooperative task for *seconds* of simulated time."""
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    return _SchedulerInstruction("sleep", delay=seconds)
sleep.__qualname__ = "DeterministicSupervisor.sleep"
DeterministicSupervisor.sleep = sleep
del sleep
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
spawn.__qualname__ = "DeterministicSupervisor.spawn"
DeterministicSupervisor.spawn = spawn
del spawn
def _immediate_task(value: Any) -> Generator[_SchedulerInstruction, None, Any]:
    """Wrap an immediate value in a generator-shaped task."""
    if False:
        yield _SchedulerInstruction("yield")
    return value
_immediate_task.__qualname__ = "DeterministicSupervisor._immediate_task"
DeterministicSupervisor._immediate_task = staticmethod(_immediate_task)
del _immediate_task
def task_results(self) -> dict[str, Any]:
    """Results of cooperative tasks that have completed."""
    return dict(self._task_results)
task_results.__qualname__ = "DeterministicSupervisor.task_results"
DeterministicSupervisor.task_results = property(task_results)
del task_results
def pending_tasks(self) -> list[str]:
    """Names of cooperative tasks that have not finished yet."""
    return [name for name, task in self._scheduled_tasks.items() if not task.done]
pending_tasks.__qualname__ = "DeterministicSupervisor.pending_tasks"
DeterministicSupervisor.pending_tasks = property(pending_tasks)
del pending_tasks
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
            wake_at = min(task.wake_at for task in self._scheduled_tasks.values() if not task.done)
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
run_until_idle.__qualname__ = "DeterministicSupervisor.run_until_idle"
DeterministicSupervisor.run_until_idle = run_until_idle
del run_until_idle
