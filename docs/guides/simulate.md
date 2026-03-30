# Simulation Primitives

No-mock, fast, deterministic. Inject these instead of mocking real infrastructure.

## Clock

```python
from ordeal.simulate import Clock

clock = Clock()
service = MyService(clock=clock)  # inject instead of time.time

clock.advance(3600)               # instant — no real waiting
assert clock.time() == 3600.0

# Timers
clock.set_timer(10.0, lambda: print("fired"))
clock.advance(15.0)  # timer fires at t=10

# Patch stdlib (when you can't inject)
with clock.patch():
    import time
    time.sleep(60)           # instant
    assert time.time() == 60.0
```

## FileSystem

```python
from ordeal.simulate import FileSystem

fs = FileSystem()
fs.write("/data.json", '{"ok": true}')
assert fs.read("/data.json") == b'{"ok": true}'
assert fs.exists("/data.json")
fs.delete("/data.json")

# Fault injection
fs.inject_fault("/data.json", "corrupt")   # reads return random bytes
fs.inject_fault("/data.json", "missing")   # reads raise FileNotFoundError
fs.inject_fault("/data.json", "readonly")  # writes raise PermissionError
fs.inject_fault("/data.json", "full")      # writes raise OSError(ENOSPC)
fs.clear_fault("/data.json")               # back to normal
```

## With ChaosTest

```python
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
```
