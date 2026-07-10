from __future__ import annotations
# ruff: noqa
def _subprocess_output_truncation_factory(
    target: str,
    *,
    stream: str,
    fraction: float,
) -> Any:
    """Build a ``PatchFault`` factory that truncates subprocess output."""

    def _factory(original: object) -> object:
        def _patched_run(*args: object, **kwargs: object) -> object:
            cmd_str = _subprocess_cmd_string(args, kwargs)
            result = original(*args, **kwargs)  # type: ignore[operator]
            if target not in cmd_str:
                return result

            if stream == "stdout" and hasattr(result, "stdout"):
                result.stdout = _truncate_stream(result.stdout, fraction)
            elif stream == "stderr" and hasattr(result, "stderr"):
                result.stderr = _truncate_stream(result.stderr, fraction)
            return result

        return _patched_run

    return _factory
class _SubprocessTimeoutFault(PatchFault):
    """Patch ``subprocess.run`` while keeping hit state on the active instance."""

    def __init__(self, target: str, timeout_sec: float) -> None:
        super().__init__(
            "subprocess.run",
            lambda original: original,
            name=f"subprocess_timeout({target!r})",
        )
        self.command_match = target
        self.timeout_sec = timeout_sec

    def _do_activate(self) -> None:
        if self._original is None:
            self._resolve()
        self._skipped = False
        original = self._original

        @functools.wraps(original)
        def timeout_run(*args: object, **kwargs: object) -> object:
            cmd_str = _subprocess_cmd_string(args, kwargs)
            if self.command_match in cmd_str:
                self._record_observation_hit()
                raise _sp.TimeoutExpired(cmd_str, self.timeout_sec)
            return original(*args, **kwargs)

        setattr(self._parent, self._attr_name, timeout_run)
def subprocess_timeout(target: str, *, timeout_sec: float = 0.001) -> PatchFault:
    """Make ``subprocess.run``/``subprocess.check_output`` calls matching *target* time out.

    Patches ``subprocess.run`` — when the command string contains *target*,
    raises ``subprocess.TimeoutExpired`` instead of running the process.
    Useful for testing Python↔Rust/C/Go bridges under chaos.

    Works in ChaosTest (nemesis toggles the fault) or as a context manager::

        # In ChaosTest — nemesis toggles automatically
        class KernelChaos(ChaosTest):
            faults = [subprocess_timeout("cargo run")]

            @rule()
            def run_episode(self):
                result = run_kernel(steps=10)  # calls subprocess.run
                always(result.exit_code == 0, "clean exit")

        # As context manager — scoped activation
        with subprocess_timeout("cargo run"):
            result = run_kernel()

    Args:
        target: Substring to match in the command (e.g. ``"cargo run"``).
        timeout_sec: Fake timeout duration for the error.
    """
    return _SubprocessTimeoutFault(target, timeout_sec)
def subprocess_exit(
    target: str,
    *,
    returncode: int = 1,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
) -> PatchFault:
    """Make matching subprocesses exit with *returncode*.

    Patches ``subprocess.Popen`` so the child never starts.  This makes
    ``subprocess.run()``, ``subprocess.check_output()``, and direct
    ``Popen`` callers observe a nonzero exit as if the child had failed.

    Args:
        target: Substring to match in the command string.
        returncode: Exit status to report.  Use a negative value only if
            you want to model signal-style death directly.
        stdout: Optional synthetic stdout for the completed process.
        stderr: Optional synthetic stderr for the completed process.
    """
    return PatchFault(
        "subprocess.Popen",
        _subprocess_failure_factory(
            target,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
        name=f"subprocess_exit({target!r}, returncode={returncode})",
    )
def subprocess_signal(
    target: str,
    *,
    signum: int = signal.SIGTERM,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
) -> PatchFault:
    """Make matching subprocesses die from *signum*.

    The synthetic process reports ``returncode == -signum``, matching
    Python's standard subprocess convention for signal death.

    Args:
        target: Substring to match in the command string.
        signum: Signal number to report as the death cause.
        stdout: Optional synthetic stdout for the completed process.
        stderr: Optional synthetic stderr for the completed process.
    """
    return PatchFault(
        "subprocess.Popen",
        _subprocess_failure_factory(
            target,
            returncode=-abs(signum),
            stdout=stdout,
            stderr=stderr,
        ),
        name=f"subprocess_signal({target!r}, signum={signum})",
    )
def subprocess_truncate_stdout(target: str, fraction: float = 0.5) -> PatchFault:
    """Truncate matching subprocess stdout to *fraction* of its length.

    Patches ``subprocess.run`` so the child process still executes, but
    the captured stdout is shortened after the fact.  Useful for testing
    parsers that assume the child always returns a full payload.
    """
    return PatchFault(
        "subprocess.run",
        _subprocess_output_truncation_factory(target, stream="stdout", fraction=fraction),
        name=f"subprocess_truncate_stdout({target!r}, {fraction})",
    )
def subprocess_truncate_stderr(target: str, fraction: float = 0.5) -> PatchFault:
    """Truncate matching subprocess stderr to *fraction* of its length.

    Patches ``subprocess.run`` so the child process still executes, but
    the captured stderr is shortened after the fact.
    """
    return PatchFault(
        "subprocess.run",
        _subprocess_output_truncation_factory(target, stream="stderr", fraction=fraction),
        name=f"subprocess_truncate_stderr({target!r}, {fraction})",
    )
def corrupt_stdout(target: str) -> PatchFault:
    """Replace the stdout of subprocess calls matching *target* with random bytes.

    Replaces ``stdout`` in the ``CompletedProcess`` with random bytes,
    simulating garbled FFI output.

    Works in ChaosTest (nemesis toggles the fault) or as a context manager::

        # In ChaosTest
        faults = [corrupt_stdout("my_binary")]

        # As context manager
        with corrupt_stdout("my_binary"):
            result = parse_binary_output()

    Args:
        target: Substring to match in the command.
    """
    import os as _os

    def _factory(original: object) -> object:
        def _corrupt_run(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)  # type: ignore[operator]
            cmd = args[0] if args else kwargs.get("args", "")
            cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if target in cmd_str and hasattr(result, "stdout") and result.stdout:
                n = len(result.stdout)
                result.stdout = _os.urandom(n)
            return result

        return _corrupt_run

    return PatchFault("subprocess.run", _factory, name=f"corrupt_stdout({target!r})")
def subprocess_delay(target: str, *, delay: float = 1.0) -> PatchFault:
    """Add *delay* seconds to subprocess calls matching *target*.

    Simulates slow FFI responses — tests timeout handling and
    progress reporting in Python↔Rust/C/Go bridges.

    Works in ChaosTest (nemesis toggles the fault) or as a context manager::

        # In ChaosTest
        faults = [subprocess_delay("cargo run", delay=5.0)]

        # As context manager
        with subprocess_delay("cargo run", delay=5.0):
            result = run_kernel()

    Args:
        target: Substring to match in the command.
        delay: Seconds to sleep before returning the real result.
    """
    import time as _time

    def _factory(original: object) -> object:
        def _slow_run(*args: object, **kwargs: object) -> object:
            cmd = args[0] if args else kwargs.get("args", "")
            cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            result = original(*args, **kwargs)  # type: ignore[operator]
            if target in cmd_str:
                _time.sleep(delay)
            return result

        return _slow_run

    return PatchFault("subprocess.run", _factory, name=f"subprocess_delay({target!r})")
