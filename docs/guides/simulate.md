# Simulation Primitives

!!! quote "In plain English"
    Simulation means you control time and the filesystem entirely from your test. Same inputs produce the same outputs, every single run. No flaky tests, no waiting for real time to pass, no touching real files on disk. The simulation primitives live in `ordeal/simulate.py`.

No-mock, fast, deterministic. Inject these instead of mocking real infrastructure.

## Why simulate instead of mock

!!! quote "The key insight"
    Mocks check that you called the right function. Simulations check that your system does the right thing. When you refactor internal code, mocks break even if the behavior is still correct. Simulations only break when actual behavior changes -- which is exactly when you want a test to fail.

Mocks verify that you called a function with the right arguments. Simulations verify that your system behaves correctly. The difference matters.

A mock for `time.sleep(60)` asserts that sleep was called with 60. A simulated clock advances 60 seconds of simulated time, fires any timers scheduled in that window, and lets you check the resulting state. The mock tests the contract between components. The simulation tests the actual behavior.

When the contract changes -- a function gets renamed, parameters shift, an internal call is restructured -- mocks break even if the behavior is still correct. Simulations do not. They only break when the behavior changes, which is when you want them to break.

There is also a speed difference. `Clock.advance(3600)` does not wait an hour. It is a single function call that updates an integer. This makes simulated tests orders of magnitude faster than real-time tests and significantly faster than mocked tests that still go through mock machinery, argument recording, and call verification.

## Clock

!!! quote "What this unlocks"
    You can control time itself in your tests. Need to test what happens after an hour? `clock.advance(3600)` -- instant, no waiting. Need to verify a timer fires at the right moment? Set it, advance to that moment, and check. The Clock makes time-dependent code completely deterministic and testable.

```python
from ordeal.simulate import Clock

clock = Clock()
service = MyService(clock=clock)  # inject instead of time.time

clock.advance(3600)               # instant -- no real waiting
assert clock.time() == 3600.0
```

### Timers

```python
clock = Clock()
fired = []
clock.set_timer(10.0, lambda: fired.append("ten"))
clock.set_timer(5.0, lambda: fired.append("five"))

clock.advance(7.0)   # fires the 5-second timer
assert fired == ["five"]

clock.advance(5.0)   # fires the 10-second timer
assert fired == ["five", "ten"]
```

Timers use a heap-based priority queue internally. When you call `clock.advance(seconds)`, the clock steps forward through time, stopping at each timer deadline to fire its callback before continuing. Timers fire in chronological order regardless of the order they were registered. This means you can schedule timers in any order and they will fire correctly.

The `pending_timers` property tells you how many timers have not yet fired, which is useful for assertions:

```python
clock = Clock()
clock.set_timer(10.0, lambda: None)
clock.set_timer(20.0, lambda: None)
assert clock.pending_timers == 2

clock.advance(15.0)
assert clock.pending_timers == 1
```

### Patching stdlib

!!! quote "Why this matters"
    Sometimes you can't change the code you're testing -- it calls `time.time()` directly, and you can't pass in a clock. `clock.patch()` handles this by temporarily replacing the real `time.time` and `time.sleep` with the simulated versions. Everything inside the `with` block uses fake time, even third-party libraries.

When you cannot inject the clock directly -- for example, when testing third-party code that calls `time.time()` -- use `clock.patch()`:

```python
import time
from ordeal.simulate import Clock

clock = Clock()
with clock.patch():
    assert time.time() == 0.0
    time.sleep(60)            # instant
    assert time.time() == 60.0
```

`clock.patch()` is a context manager that replaces `time.time` and `time.sleep` in the stdlib `time` module with the clock's own `time()` and `sleep()` methods. Inside the context, all code that calls `time.time()` or `time.sleep()` -- including code in third-party libraries -- will use the simulated clock. Outside the context, the original functions are restored.

This is useful when you cannot modify the code under test to accept a clock parameter. However, when you can inject the clock directly, prefer that approach -- it is more explicit and avoids patching global state.

## FileSystem

!!! quote "Think of it this way"
    The simulated FileSystem is a tiny in-memory filesystem that your code reads and writes to as if it were real. No actual files are created on disk. You can write data, read it back, then inject faults like corruption or permission errors to see how your code handles them. It's a sandbox where you control everything.

```python
from ordeal.simulate import FileSystem

fs = FileSystem()
fs.write("/data.json", '{"ok": true}')
assert fs.read("/data.json") == b'{"ok": true}'
assert fs.exists("/data.json")
fs.delete("/data.json")
```

The filesystem is entirely in-memory. No disk I/O occurs. Reads return bytes; use `read_text()` if you need a decoded string. `list_dir(prefix)` returns sorted paths under a given prefix.

### Fault injection

!!! quote "What you can do with this"
    Real filesystems fail in specific ways: files get corrupted, disks fill up, permissions change. The simulated FileSystem lets you trigger each of these failures on demand. You can test how your code handles a corrupted config file, a full disk during a write, or a file that vanishes between checking and reading -- all without touching a real filesystem.

```python
fs.inject_fault("/data.json", "corrupt")
fs.inject_fault("/config.yaml", "missing")
fs.inject_fault("/output.log", "readonly")
fs.inject_fault("/db.sqlite", "full")
```

Each fault type simulates a specific real-world failure:

**"corrupt"** -- Reads return random bytes of the same length as the original file content. Simulates bit rot, filesystem corruption, or incomplete writes. Use this to test that your code validates data after reading it, rather than trusting the filesystem blindly.

**"missing"** -- Reads raise `FileNotFoundError` even if the file exists in the simulated filesystem. Simulates file deletion by another process, race conditions where a file disappears between an existence check and a read, or failed mounts.

**"readonly"** -- Writes raise `PermissionError`. Simulates permission changes, read-only filesystems, or security policy enforcement. Tests that your code handles write failures gracefully rather than crashing.

**"full"** -- Writes raise `OSError` with errno 28 (ENOSPC). Simulates a full disk. This is one of the most common production failures and one of the least tested. Code that writes to disk should handle this case.

### Cleanup

```python
fs.clear_fault("/data.json")    # remove fault from one path
fs.clear_all_faults()           # remove all faults, keep files
fs.reset()                      # remove all files and all faults
```

`clear_fault` removes the fault from a single path, restoring normal behavior for that file. `clear_all_faults` removes every injected fault but leaves the file contents intact. `reset` wipes everything -- files and faults -- returning the filesystem to its initial empty state. Use `reset()` in test teardown to ensure a clean slate.

## With ChaosTest

!!! quote "How to explore this"
    When you combine simulation primitives with ChaosTest, the explorer can find failure sequences a human would never think to test. It might advance time past a cache expiry, then corrupt the backing file, then trigger a read -- all in one test run. Each rule becomes a lever the explorer can pull in any order, and the invariant is checked after every step.

```python
from ordeal import ChaosTest, rule, invariant
from ordeal.faults import timing
from ordeal.simulate import Clock, FileSystem

class MyServiceChaos(ChaosTest):
    faults = [timing.timeout("myapp.api.call")]

    def __init__(self):
        super().__init__()
        self.clock = Clock()
        self.fs = FileSystem()
        self.service = MyService(clock=self.clock, fs=self.fs)

    @rule()
    def advance_time(self):
        self.clock.advance(30)

    @rule()
    def corrupt_data(self):
        self.fs.inject_fault("/cache.bin", "corrupt")

    @rule()
    def heal_data(self):
        self.fs.clear_fault("/cache.bin")

    @invariant()
    def service_never_crashes(self):
        assert self.service.is_healthy()
```

Simulation primitives become part of the ChaosTest state. Rules can advance time, inject filesystem faults, and check invariants. Hypothesis explores interleavings of these rules -- it might advance time, then corrupt a file, then advance time again, then heal the file, all while the nemesis toggles network faults. The invariant is checked after every step.

This is powerful because the explorer can find sequences that a human would not think to test. For example: advance time past a cache TTL, then corrupt the underlying file, then trigger a cache miss. The combination matters even if each individual operation is safe.

## When to use simulations

!!! quote "In plain English"
    If your code cares about time or touches files, simulation is almost always better than mocking. It's faster, more realistic, and won't break when you refactor internals. The lists below give you concrete situations where Clock and FileSystem shine -- if your code does any of these things, reach for `ordeal.simulate`.

Use the simulated Clock for any test that depends on time:

- **Timeouts**: verify that your code times out after the right duration without waiting for it.
- **Caching with TTL**: advance past the TTL and confirm the cache expires.
- **Scheduled tasks**: set timers, advance to their deadlines, and verify they fire.
- **Rate limiting**: advance time between requests to test rate limit windows.
- **Debouncing**: verify that rapid calls are collapsed and the final callback fires at the right time.

Use the simulated FileSystem for any test that depends on file I/O:

- **File-based storage**: test read/write cycles without touching disk.
- **Logging**: verify log output by reading from the simulated filesystem.
- **Serialization**: write serialized data, inject corruption, verify deserialization handles it.
- **Configuration loading**: test behavior when config files are missing, corrupted, or read-only.
- **Backup and recovery**: inject faults during writes to test recovery logic.
