---
description: >-
  Fault injection for Python: timeouts, NaN, disk full, network errors.
  PatchFault for external faults, BUGGIFY for inline faults. The
  FoundationDB model in ordeal.
---

# Fault Injection

!!! quote "In plain English"
    Fault injection is controlled sabotage. You pick exactly which parts of your system can break, you break them on purpose, and you watch what happens. It's like a fire drill for your code -- you don't wait for a real fire to find out if the exits work.

    The faults live in `ordeal/faults/`, organized by category: `io.py` for disk and network failures, `numerical.py` for corrupt numbers, `timing.py` for slowdowns and timeouts.

## The idea

Every system depends on things that can fail. The network drops a packet. A disk runs out of space. An API returns garbage. A function takes ten seconds instead of ten milliseconds.

You have two options. You can wait for these failures to happen in production, at 3am, to real users. Or you can cause them yourself, on purpose, in a controlled environment, and watch what happens.

That is fault injection. You deliberately break things to answer one question: **when this component fails, does the rest of the system handle it correctly?**

Think of crash-testing a car. Nobody waits for a real accident to find out if the seatbelts work. You strap a dummy in the driver's seat, accelerate into a wall, and measure what happens. The crash is intentional. The measurement is the point.

The question is never "will this component fail?" -- it will. The question is "when it fails, what does everything else do?"

ordeal gives you two ways to inject faults. They serve different purposes and work well together.

---

## External faults: PatchFault

!!! quote "Think of it this way"
    PatchFault wraps a function from the outside, like putting a detour sign on a road. The real road is still there -- you're just rerouting traffic through your "broken" version. When you remove the sign, traffic flows normally again. You never touch the original code.

External faults target a specific function from the outside. You give ordeal a dotted path -- like `"myapp.api.call"` or `"myservice.db.query"` -- and it replaces that function with a version that misbehaves. When the fault is deactivated, the original function is restored. Nothing permanent. Nothing leaks.

This is for dependencies you don't control: third-party APIs, database drivers, file system operations, network calls. You can't modify their source code to add failure points, but you can intercept them at the boundary.

### Declaring faults in a ChaosTest

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import io, numerical, timing

class PaymentServiceChaos(ChaosTest):
    faults = [
        io.error_on_call("payments.gateway.charge"),
        timing.timeout("payments.gateway.charge"),
        numerical.nan_injection("payments.calculator.total"),
        io.disk_full(),
    ]

    @rule()
    def process_payment(self):
        result = payment_service.process(amount=49.99, card="tok_visa")
        always(result.status in ("success", "declined", "retry"),
               "payment always reaches a valid terminal state")

    @invariant()
    def ledger_balanced(self):
        always(ledger.total_debits() == ledger.total_credits(),
               "ledger stays balanced under faults")

TestPaymentServiceChaos = PaymentServiceChaos.TestCase
```

You list the faults. ordeal's nemesis -- an automatically injected rule -- toggles them on and off during exploration. Hypothesis drives the exploration, choosing which faults fire, when, in what order, interleaved with your application rules. You write what to check. ordeal decides when things break.

### Available fault types

**I/O faults** (`ordeal.faults.io`):

| Function | What it does |
|---|---|
| `error_on_call(target)` | Makes the target raise an exception (default: `IOError`) on every call |
| `return_empty(target)` | Makes the target return `None` on every call |
| `corrupt_output(target)` | Replaces the target's output with random bytes of the same length |
| `truncate_output(target, fraction=0.5)` | Truncates the target's output to a fraction of its length |
| `disk_full()` | Makes write-mode `open()` and `os.write()` fail system-wide with `ENOSPC` |
| `permission_denied()` | Makes write-mode `open()` fail system-wide with `EACCES` |
| `subprocess_timeout(target)` | Makes `subprocess.run` raise `TimeoutExpired` when command matches target |
| `corrupt_stdout(target)` | Makes `subprocess.run` return garbled `stdout` when command matches target |
| `subprocess_delay(target, delay=1.0)` | Adds delay to `subprocess.run` when command matches target — tests timeout handling in Python/Rust/C/Go bridges |

**Numerical faults** (`ordeal.faults.numerical`):

| Function | What it does |
|---|---|
| `nan_injection(target)` | Replaces numeric values in the target's output with `NaN` |
| `inf_injection(target)` | Replaces numeric values in the target's output with `Inf` |
| `wrong_shape(target, expected, actual)` | Makes the target return an array with a different shape |
| `dtype_drift(target, kind="str")` | Coerces numeric outputs into string/int/bool/object leaves |
| `partial_batch(target, fraction=0.5, min_items=1)` | Returns only part of a batch-like output |
| `feature_order_drift(target, shift=1)` | Rotates feature order while keeping the same outer shape |
| `missing_feature(target, key=None, fill=...)` | Drops one feature key, or replaces it with a fill value |
| `corrupted_floats(corrupt_type="nan")` | Provides corrupt float values via `fault.value()` (no patching) |

**Timing faults** (`ordeal.faults.timing`):

| Function | What it does |
|---|---|
| `timeout(target, delay=30.0)` | Makes the target raise `TimeoutError` instantly |
| `slow(target, delay=1.0, mode="simulate")` | Adds delay to the target (`"simulate"` records without sleeping) |
| `intermittent_crash(target, every_n=3)` | Crashes the target every N calls, succeeds otherwise |
| `jitter(target, magnitude=0.01)` | Adds deterministic numeric jitter to the target's return value |

---

## Inline faults: buggify

!!! quote "The key insight"
    PatchFault wraps things from the outside. buggify works from the inside -- you place `if buggify():` directly in your own code, right where you know something could go wrong. It's like an author leaving margin notes in a manuscript: "this part could break here." During testing, those notes come alive. In production, they're invisible.

The second approach comes from FoundationDB. Instead of targeting a function from outside, you place fault injection points directly inside your own code.

```python
from ordeal import buggify, buggify_value

def process(data):
    if buggify():
        time.sleep(random.random() * 5)  # sometimes slow
    result = compute(data)
    return buggify_value(result, float('nan'))  # sometimes corrupt
```

In production, `buggify()` returns `False`. Always. Every time. Zero overhead -- it checks a thread-local boolean and returns. The `if` branch is never taken. The `buggify_value` call returns `result` untouched.

During chaos testing, `buggify()` probabilistically returns `True`. Now your code experiences the faults you designed, exactly where you placed them.

This is powerful because **the fault injection points are the code**. You don't write separate fault definitions somewhere else. When you write `if buggify():`, you're documenting a failure mode right where it matters. A new engineer reading `process()` sees immediately: "this function might be slow, and it might return NaN." The code tells you where it can fail.

### Activating buggify

Three ways:

```python
# 1. Pytest flag (recommended for test suites)
# pytest --chaos --buggify-prob 0.1 --chaos-seed 42

# 2. Programmatic activation (e.g., in conftest.py)
from ordeal import auto_configure
auto_configure(buggify_probability=0.1, seed=42)

# 3. Direct control
from ordeal.buggify import activate, set_seed
activate(probability=0.1)
set_seed(42)
```

### buggify internals

The state is **thread-local**. Each thread has its own active flag, probability, and RNG instance. This means concurrent tests don't interfere with each other.

The RNG is **seed-controlled**. When you set a seed, the sequence of True/False decisions is deterministic. If a chaos test finds a bug with `--chaos-seed 42`, running it again with the same seed reproduces the same fault schedule.

`buggify(probability)` accepts an optional probability override for individual call sites. If omitted, it uses the thread's configured probability (default 0.1). `buggify_value(normal, faulty, probability)` is a convenience -- it returns `faulty` when the coin flip succeeds, `normal` otherwise.

---

## When to use which

!!! quote "How to think about choosing"
    Ask yourself one question: **do I own this code?** If you don't own it (a database driver, a third-party API), use PatchFault to intercept it from the outside. If you do own it and you know where it could break, put a `buggify()` right there. For the strongest testing, use both at the same time.

**External faults (PatchFault)** are for things you don't own. Third-party libraries, system calls, network boundaries, database drivers. You can't (or shouldn't) modify their source code, so you intercept them at the edge.

**Inline faults (buggify)** are for code you own, where you know the failure modes. You know your cache might be cold. You know your parser might receive truncated input. You know your retry logic might hit the maximum. Put a `buggify()` there.

**Both together** is the strongest approach. External faults break the dependencies. Inline faults break the internal assumptions. The nemesis toggles the external faults while buggify fires inside your code. Hypothesis explores the combinations.

```python
class FullChaosTest(ChaosTest):
    faults = [
        io.error_on_call("myapp.db.query"),       # external: DB might fail
        timing.slow("myapp.cache.get"),             # external: cache might be slow
    ]

    @rule()
    def process_request(self):
        # buggify inside the code under test handles internal failure modes
        # while the nemesis toggles external faults from outside
        result = myapp.handle_request({"user": "test"})
        always(result.status_code < 500, "no internal server errors")
```

---

## How PatchFault works under the hood

!!! quote "Why this matters"
    You don't need to understand internals to use faults. But if a fault doesn't fire when you expect it to, or fires when it shouldn't, knowing the lifecycle -- resolve the path, swap the function, restore it later -- lets you debug quickly. The mechanism is simple: save the original, replace it, put it back.

Understanding the mechanism helps when you need to debug unexpected behavior or write custom faults.

### Resolution

When you write `PatchFault("myapp.api.call", wrapper_fn)`, nothing happens immediately. The dotted path is stored as a string. Resolution is **lazy** -- it happens on first activation.

When `activate()` is called, `_resolve_target("myapp.api.call")` runs. It splits the path at the last dot: parent path `"myapp.api"`, attribute name `"call"`. Then it tries to import `myapp.api` as a module. If that works, it has the parent object and the attribute name. If not, it walks backward through the parts, importing the deepest module it can find and traversing attributes for the rest.

This resolution strategy handles both `module.function` and `module.Class.method` paths.

### The patch cycle

```
PatchFault created     target="myapp.api.call"
       |               (path stored, nothing resolved)
       v
   activate()
       |
       +-- _resolve()  (import module, find parent object, save original function)
       |
       +-- wrapper_fn(original) -> replacement
       |
       +-- setattr(parent, "call", replacement)
       |               (myapp.api.call is now the faulty version)
       v
  [fault is active -- calls to myapp.api.call hit the wrapper]
       |
   deactivate()
       |
       +-- setattr(parent, "call", original)
       |               (myapp.api.call is restored)
       v
  [fault is inactive -- calls behave normally]
```

The `wrapper_fn` is a function that receives the original function and returns a replacement. This is where the fault behavior lives. For example, `error_on_call` returns a wrapper that ignores the original and raises an exception. `corrupt_output` returns a wrapper that calls the original, then corrupts the result.

### Reset

`reset()` calls `deactivate()` and then clears the resolved state (parent, attribute name, original function). The next `activate()` will re-resolve from scratch. This matters when modules are reloaded between tests.

---

## LambdaFault

!!! quote "What you can do with this"
    LambdaFault is your escape hatch. PatchFault replaces functions, but what if you need to clear a cache, flip a feature flag, corrupt shared state, or simulate a configuration change? LambdaFault lets you inject *any* custom behavior -- just give it a "do this when activated" and a "do this when deactivated" function. If you can write it in Python, you can inject it as a fault.

Not every fault fits the "patch a function" model. Sometimes you need to clear a cache, flip a feature flag, corrupt some shared state, or toggle a circuit breaker.

`LambdaFault` takes two callables: what to do on activation, what to do on deactivation.

```python
from ordeal.faults import LambdaFault

cache = {}

fault = LambdaFault(
    "kill-cache",
    on_activate=lambda: cache.clear(),
    on_deactivate=lambda: None,
)
```

It participates in the same lifecycle as PatchFault. The nemesis can toggle it. Swarm mode can include or exclude it. It just doesn't patch a function -- it runs arbitrary code.

---

## The Fault lifecycle

!!! quote "In plain English"
    Every fault follows three steps: turn on, run, turn off. It's like a light switch -- flipping it twice doesn't break anything, and when the test ends, everything goes back to exactly how it was. The base class in `ordeal/faults/__init__.py` guarantees this cleanup, so you never have to worry about a fault leaking into the next test.

Every fault -- whether `PatchFault`, `LambdaFault`, or a custom subclass -- follows the same lifecycle managed by the `Fault` base class:

```
         activate()
             |
             +-- already active? -> return (no-op)
             |
             +-- _do_activate()   (subclass implements this)
             |
             +-- self.active = True
             |
        [fault is live]
             |
         deactivate()
             |
             +-- not active? -> return (no-op)
             |
             +-- _do_deactivate() (subclass implements this)
             |
             +-- self.active = False
```

The `active` boolean guard means double-activation and double-deactivation are safe. You never patch twice or restore twice.

**As a context manager**, faults activate on enter and deactivate on exit — useful for scoped fault injection in regular pytest tests:

```python
with io.subprocess_timeout("cargo run"):
    result = run_kernel()
    always(result is not None, "handles timeout")
# fault is automatically deactivated here
```

**During a ChaosTest**, the lifecycle is:

1. `ChaosTest.__init__()` calls `reset()` on every fault (clean slate).
2. In swarm mode, `_swarm_init()` selects a random subset of faults.
3. The `_nemesis` rule toggles faults on and off throughout the test. Hypothesis chooses which fault, and whether to activate or deactivate.
4. `teardown()` calls `reset()` on every fault in the class (not just the swarm subset). Everything is restored.

---

## Production safety

!!! quote "Why you can trust this in production"
    Every fault injection point in ordeal is a no-op when chaos mode is off. Your `buggify()` calls stay in production code, but they do nothing -- one quick check, then normal execution continues. PatchFaults are never activated, so original functions are never replaced. You ship the same code you test, with negligible overhead.

This is the design constraint that makes fault injection practical: **zero overhead when chaos mode is off.**

- `buggify()` checks a thread-local boolean and returns `False`. One attribute lookup. No branching into fault code. No RNG call.
- `buggify_value(normal, faulty)` calls `buggify()`, gets `False`, returns `normal`. The faulty value is never used (though it is evaluated -- if constructing it is expensive, guard it with an `if buggify()` block instead).
- PatchFaults are never activated. The wrapper functions are never called. The original functions are never replaced.
- `always()`, `sometimes()`, `reachable()`, `unreachable()` check if the tracker is active. It isn't. They return immediately.

Nothing to remove before shipping. The `buggify()` calls stay in your production code. They're documentation of failure modes that happens to also be executable during testing.

---

!!! quote "You're ready"
    You know two ways to inject faults: PatchFault for external wrapping and buggify for inline gates. You know when to use each. Browse the full fault library in `ordeal/faults/` — every fault follows the same pattern, so once you've seen one, you've seen them all. Or create your own with LambdaFault.

## Further reading

- [Chaos Testing](chaos-testing.md) -- how the nemesis schedules faults and how swarm mode improves coverage
- [Property Assertions](property-assertions.md) -- how to check that your system behaves correctly under faults
