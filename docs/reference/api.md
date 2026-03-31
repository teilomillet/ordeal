---
description: >-
  Complete ordeal API reference: ChaosTest, always, sometimes, buggify,
  faults, invariants, Explorer, quickcheck. Every function, parameter,
  and type.
---

# API Reference

!!! quote "In plain English"
    This is your lookup table -- when you know what you want to do, find the exact function here. Each section maps to a concept you can learn more about in the guides. Whether you're adding ordeal to an existing test suite or starting fresh, the signatures and examples below give you everything you need to wire things up.

Complete public API with signatures, parameters, and usage.

## Discovery

!!! quote "Find everything ordeal offers — programmatically"
    `catalog()` returns every fault, invariant, assertion, strategy, and integration ordeal has, with names, signatures, and documentation. AI assistants and scripts can use this to discover capabilities at runtime without reading files. When new features are added to ordeal, they appear in the catalog automatically.

```python
from ordeal import catalog

c = catalog()
c["faults"]        # all fault factories — timeout, nan_injection, disk_full, ...
c["invariants"]    # composable checks — no_nan, bounded, finite, ...
c["assertions"]    # assertion types — always, sometimes, reachable, unreachable
c["strategies"]    # adversarial data generation strategies
c["integrations"]  # API testing, atheris fuzzing entry points

# Each entry has: name, qualname, signature, doc
for fault in c["faults"]:
    print(f"{fault['name']}{fault['signature']}")
    print(f"  {fault['doc']}")
```

## Core

!!! quote "Stateful chaos testing"
    ChaosTest is the foundation of ordeal. You define rules (things your system does), faults (things that go wrong), and invariants (things that must stay true). Ordeal then explores thousands of interleavings automatically, finding the exact sequence of operations and failures that breaks your system.

### ChaosTest

```python
from ordeal import ChaosTest
```

Base class for stateful chaos tests. Extends Hypothesis's `RuleBasedStateMachine`.

**Class attributes:**

| Attribute | Type | Default | Description |
|---|---|---|---|
| `faults` | `list[Fault]` | `[]` | Faults to inject during testing |
| `swarm` | `bool` | `False` | Random fault subsets per run |

**Methods:**

| Method | Returns | Description |
|---|---|---|
| `active_faults` | `list[Fault]` | Property: currently active faults |
| `teardown()` | `None` | Deactivate all faults, clean up |

```python
class MyServiceChaos(ChaosTest):
    faults = [timing.timeout("myapp.api.call")]
    swarm = True

    @rule()
    def do_something(self):
        ...

TestMyServiceChaos = MyServiceChaos.TestCase
```

### Hypothesis re-exports

These are re-exported from `hypothesis.stateful` for convenience:

```python
from ordeal import rule, invariant, initialize, precondition, Bundle
```

| Import | Description |
|---|---|
| `rule(**kwargs)` | Declare a test rule (decorator) |
| `invariant()` | Declare an invariant check (decorator) |
| `initialize(**kwargs)` | Declare an initialization rule (decorator) |
| `precondition(condition)` | Gate a rule on current state (decorator) |
| `Bundle(name)` | Named collection for data flow between rules |

### auto_configure

```python
auto_configure(
    buggify_probability: float = 0.1,
    seed: int | None = None,
) -> None
```

Enable chaos testing programmatically. Alternative to `--chaos` flag.

```python
from ordeal import auto_configure
auto_configure(buggify_probability=0.2, seed=42)
```

---

## Assertions

!!! quote "The key insight"
    Assertions are how you tell ordeal what "correct" means. `always` and `unreachable` catch violations the instant they happen. `sometimes` and `reachable` are checked at the end of the session -- they verify that something good happened at least once across all your test runs. All four live in `ordeal/assertions.py`.

```python
from ordeal import always, sometimes, reachable, unreachable
```

**Thread safety:** The PropertyTracker is fully lock-guarded — safe for free-threaded Python 3.13+/3.14. All access to `active` and `_properties` is synchronized.

### always

```python
always(
    condition: bool,
    name: str,
    *,
    mute: bool = False,
    **details: Any,
) -> None
```

Assert condition is `True` every time. Raises `AssertionError` immediately on violation — whether or not `--chaos` is active. Violations are never silent by default.

Pass `mute=True` to record the violation without raising. The violation still shows in the property report — tracked, not hidden. Use when a known issue is too loud and you need to focus on something else.

```python
always(result >= 0, "result is non-negative")
always(not math.isnan(score), "score is never NaN", value=score)
always(response.ok, "API healthy", mute=True)  # known flaky, tracked not fatal
```

### sometimes

```python
sometimes(
    condition: bool | Callable[[], bool],
    name: str,
    *,
    attempts: int | None = None,
    **details: Any,
) -> None
```

Assert condition is `True` at least once across the session. Deferred — checked at session end via PropertyTracker.

If `condition` is callable and `attempts` is set, polls the callable up to `attempts` times for standalone use.

```python
sometimes(cache_hit, "cache is exercised")
sometimes(lambda: service.ready(), "service starts", attempts=10)
```

### reachable

```python
reachable(
    name: str,
    **details: Any,
) -> None
```

Record that a code path executed. Deferred — must be hit at least once by session end.

```python
except TimeoutError:
    reachable("timeout-handling-path")
    handle_timeout()
```

### unreachable

```python
unreachable(
    name: str,
    *,
    mute: bool = False,
    **details: Any,
) -> None
```

Assert code path never executes. Raises `AssertionError` immediately — whether or not `--chaos` is active. Violations are never silent by default. Pass `mute=True` to record without raising.

```python
if data is None and not error_occurred:
    unreachable("data-lost-silently")
```

### PropertyTracker

```python
from ordeal.assertions import tracker
```

Global singleton. Accumulates property results across runs.

| Method | Returns | Description |
|---|---|---|
| `reset()` | `None` | Clear all tracked properties |
| `record(name, prop_type, condition, details)` | `None` | Record a property result |
| `record_hit(name, prop_type)` | `None` | Record a hit without condition |
| `results` | `list[Property]` | All tracked properties |
| `failures` | `list[Property]` | Only failed properties |

### Property

```python
from ordeal.assertions import Property
```

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Property name |
| `type` | `str` | `"always"`, `"sometimes"`, `"reachable"`, `"unreachable"` |
| `hits` | `int` | Times evaluated |
| `passes` | `int` | Times condition was True |
| `failures` | `int` | Times condition was False |
| `first_failure_details` | `dict | None` | Details from first failure |
| `passed` | `bool` | Whether property passed (per type semantics) |
| `summary` | `str` | One-line `"PASS ..."` or `"FAIL ..."` |

---

## Buggify

!!! quote "Inline faults for production code"
    Buggify lets you embed fault injection points directly in your application code. In production, `buggify()` is a no-op with negligible overhead. During chaos testing, it fires with configurable probability, letting you simulate failures exactly where they'd happen in real life -- inside your own functions, not just at external boundaries.

```python
from ordeal.buggify import buggify, buggify_value, activate, deactivate, set_seed, is_active
```

### buggify

```python
buggify(probability: float | None = None) -> bool
```

Returns `True` during chaos testing with configurable probability. No-op when inactive (negligible overhead).

```python
if buggify():
    raise ConnectionError("simulated failure")

if buggify(0.5):  # 50% chance when active
    time.sleep(random.random())
```

### buggify_value

```python
buggify_value(normal: _T, faulty: _T, probability: float | None = None) -> _T
```

Returns `faulty` during chaos testing, `normal` otherwise.

```python
return buggify_value(computed_result, float('nan'))
return buggify_value(response, TimeoutError("simulated"), 0.3)
```

### activate / deactivate / set_seed / is_active

```python
activate(probability: float = 0.1) -> None     # enable for current thread
deactivate() -> None                             # disable for current thread
set_seed(seed: int) -> None                      # seed RNG for reproducibility
is_active() -> bool                              # check if enabled
```

---

## Faults

!!! quote "Think of it this way"
    Faults are how you simulate real-world failures -- timeouts, disk errors, network issues, corrupted data. You pick a target function by its dotted path (like `"myapp.db.query"`), and ordeal replaces it with a faulty version when the fault is active. When deactivated, the original function comes back. The base classes live in `ordeal/faults/__init__.py`, with specialized faults in `io.py`, `numerical.py`, `timing.py`, `network.py`, and `concurrency.py`.

### Base classes

```python
from ordeal.faults import Fault, PatchFault, LambdaFault
```

**Thread safety:** The `active` flag and activate/deactivate transitions are lock-guarded. `intermittent_crash` and `jitter` call counters are also lock-protected. Deep-copying faults creates fresh locks (for checkpoint serialization). Safe for free-threaded Python 3.13+.

**Fault** (ABC):

| Method | Description |
|---|---|
| `activate()` | Enable fault injection |
| `deactivate()` | Disable fault injection |
| `reset()` | Deactivate and clear state |
| `name: str` | Human-readable name |
| `active: bool` | Whether currently active |

**PatchFault**:

```python
PatchFault(
    target: str,                                    # dotted path: "myapp.api.call"
    wrapper_fn: Callable[[Callable], Callable],     # receives original, returns replacement
    name: str | None = None,
)
```

Resolves `target` to a function, replaces it with `wrapper_fn(original)` when active, restores on deactivation. Lazy resolution (resolved on first activation).

**LambdaFault**:

```python
LambdaFault(
    name: str,
    on_activate: Callable[[], None],
    on_deactivate: Callable[[], None],
)
```

### I/O faults

```python
from ordeal.faults import io
```

| Function | Signature | Description |
|---|---|---|
| `error_on_call` | `(target: str, error: type = IOError, message: str = "Simulated I/O error") -> PatchFault` | Target raises error on every call |
| `return_empty` | `(target: str) -> PatchFault` | Target returns `None` |
| `corrupt_output` | `(target: str) -> PatchFault` | Target returns random bytes (same length) |
| `truncate_output` | `(target: str, fraction: float = 0.5) -> PatchFault` | Target output truncated to fraction |
| `disk_full` | `() -> Fault` | Global: writes fail with `OSError(ENOSPC)` |
| `permission_denied` | `() -> Fault` | Global: opens fail with `PermissionError` |
| `subprocess_timeout` | `(target: str) -> PatchFault` | `subprocess.run` raises `TimeoutExpired` when command matches target |
| `corrupt_stdout` | `(target: str) -> PatchFault` | `subprocess.run` returns garbled `stdout` when command matches target |
| `subprocess_delay` | `(target: str, *, delay: float = 1.0) -> PatchFault` | Adds delay to `subprocess.run` when command matches target |

```python
faults = [
    io.error_on_call("myapp.storage.save", IOError, "disk unreachable"),
    io.corrupt_output("myapp.cache.read"),
    io.disk_full(),
]
```

### Numerical faults

```python
from ordeal.faults import numerical
```

| Function | Signature | Description |
|---|---|---|
| `nan_injection` | `(target: str) -> PatchFault` | Numeric output becomes NaN |
| `inf_injection` | `(target: str) -> PatchFault` | Numeric output becomes Inf |
| `wrong_shape` | `(target: str, expected: tuple, actual: tuple) -> PatchFault` | Returns array with wrong shape |
| `corrupted_floats` | `(corrupt_type: str = "nan") -> Fault` | Standalone corrupt float source; use `fault.value()` |

```python
faults = [
    numerical.nan_injection("myapp.model.predict"),
    numerical.wrong_shape("myapp.embed", (1, 512), (1, 256)),
]
```

### Timing faults

```python
from ordeal.faults import timing
```

| Function | Signature | Description |
|---|---|---|
| `timeout` | `(target: str, delay: float = 30.0, error: type = TimeoutError) -> PatchFault` | Target raises instantly (no real sleep) |
| `slow` | `(target: str, delay: float = 1.0, mode: str = "simulate") -> PatchFault` | Add delay; `"simulate"` = instant, `"real"` = actual sleep |
| `intermittent_crash` | `(target: str, every_n: int = 3, error: type = RuntimeError) -> Fault` | Crash every Nth call; resets on `reset()` |
| `jitter` | `(target: str, magnitude: float = 0.01) -> Fault` | Add deterministic numeric jitter to return value |

```python
faults = [
    timing.timeout("myapp.api.call"),
    timing.intermittent_crash("myapp.worker.process", every_n=5),
    timing.jitter("myapp.sensor.read", magnitude=0.001),
]
```

### Network faults

```python
from ordeal.faults import network
```

For any code making HTTP/API calls. Simulates real-world network failures without requiring network access.

| Function | Signature | Description |
|---|---|---|
| `http_error` | `(target: str, status_code: int = 500, message: str = "Internal Server Error") -> PatchFault` | Raise `HTTPFaultError` with status code and fake response |
| `connection_reset` | `(target: str) -> PatchFault` | Raise `ConnectionError` |
| `rate_limited` | `(target: str, retry_after: float = 30.0) -> PatchFault` | Raise HTTP 429 with `Retry-After` header |
| `auth_failure` | `(target: str, status_code: int = 401) -> PatchFault` | Raise HTTP 401/403 |
| `dns_failure` | `(target: str) -> PatchFault` | Raise `OSError` (simulated DNS resolution failure) |
| `partial_response` | `(target: str, fraction: float = 0.5) -> PatchFault` | Truncate response to fraction of content |
| `intermittent_http_error` | `(target: str, every_n: int = 3, status_code: int = 503, message: str = "Service Unavailable") -> Fault` | HTTP error every Nth call; resets on `reset()` |

```python
faults = [
    network.http_error("myapp.client.post", status_code=503),
    network.rate_limited("myapp.client.get", retry_after=60),
    network.connection_reset("myapp.client.post"),
    network.dns_failure("myapp.client.resolve"),
]
```

`HTTPFaultError` carries `.status_code` and a duck-typed `.response` object compatible with requests/httpx patterns.

### Concurrency faults

```python
from ordeal.faults import concurrency
```

For testing thread-safety, resource contention, and concurrent access patterns.

| Function | Signature | Description |
|---|---|---|
| `contended_call` | `(target: str, contention: float = 0.05, mode: str = "simulate") -> PatchFault` | Wrap target with a shared lock; simulates resource contention |
| `delayed_release` | `(target: str, delay: float = 0.5, mode: str = "simulate") -> PatchFault` | Add delay after target returns (simulates slow cleanup) |
| `thread_boundary` | `(target: str, timeout: float = 5.0) -> Fault` | Execute target on a background thread (finds thread-local state bugs) |
| `stale_state` | `(obj: Any, attr: str, stale_value: Any) -> Fault` | When active, set `obj.attr = stale_value`; restore on deactivation |

```python
faults = [
    concurrency.contended_call("myapp.pool.acquire", contention=0.1),
    concurrency.thread_boundary("myapp.cache.get"),
    concurrency.stale_state(my_service, "config", old_config),
]
```

---

## Explorer

!!! quote "Coverage-guided exploration"
    The Explorer is ordeal's autopilot. Point it at a ChaosTest, and it runs thousands of rule/fault combinations, tracking which code paths each run reaches. Runs that discover new edges get higher energy, so the explorer automatically focuses on the most productive directions. Use it when manual test cases can't cover the combinatorial space of faults and operations.

```python
from ordeal.explore import Explorer, ExplorationResult, Failure, ProgressSnapshot, CoverageCollector, Checkpoint
```

### Explorer

```python
Explorer(
    test_class: type,                           # ChaosTest subclass
    *,
    target_modules: list[str] | None = None,    # modules to track for coverage
    seed: int = 42,
    max_checkpoints: int = 256,
    checkpoint_prob: float = 0.4,               # probability of starting from checkpoint
    checkpoint_strategy: str = "energy",        # "energy", "uniform", "recent"
    fault_toggle_prob: float = 0.3,
    record_traces: bool = False,
    workers: int = 1,                           # 0 = auto (os.cpu_count())
    share_edges: bool = True,                   # shared-memory edge bitmap for workers
)
```

```python
explorer.run(
    *,
    max_time: float = 60.0,
    max_runs: int | None = None,
    steps_per_run: int = 50,
    shrink: bool = True,
    max_shrink_time: float = 30.0,
    progress: Callable[[ProgressSnapshot], None] | None = None,
    resume_from: str | Path | None = None,    # resume from saved state
    save_state_to: str | Path | None = None,  # save state on completion
) -> ExplorationResult
```

| Method | Returns | Description |
|---|---|---|
| `save_state(path)` | `None` | Save checkpoint corpus, edges, and RNG state to a pickle file for later resumption |
| `load_state(path)` | `dict` | Restore saved state; returns counters (`total_edges`, `checkpoints`) |

```python
explorer = Explorer(
    MyServiceChaos,
    target_modules=["myapp"],
    checkpoint_strategy="energy",
)
result = explorer.run(max_time=120, steps_per_run=100)
print(result.summary())

# Resume a previous run:
result = explorer.run(
    max_time=120,
    resume_from=".ordeal/state.pkl",
    save_state_to=".ordeal/state.pkl",
)
```

### ExplorationResult

| Attribute | Type | Description |
|---|---|---|
| `total_runs` | `int` | Runs completed |
| `total_steps` | `int` | Total steps across all runs |
| `unique_edges` | `int` | Unique control-flow edges discovered |
| `checkpoints_saved` | `int` | Checkpoints in corpus |
| `failures` | `list[Failure]` | Failures found |
| `duration_seconds` | `float` | Wall-clock time |
| `edge_log` | `list[tuple[int, int]]` | `(run_id, cumulative_edges)` |
| `traces` | `list[Trace]` | Recorded traces (if `record_traces=True`) |
| `summary()` | `str` | Human-readable report |

### Failure

| Attribute | Type | Description |
|---|---|---|
| `error` | `Exception` | The exception raised |
| `step` | `int` | Step number when failure occurred |
| `run_id` | `int` | Run that found this failure |
| `active_faults` | `list[str]` | Faults active at failure time |
| `rule_log` | `list[str]` | Sequence of rules/faults leading to failure |
| `trace` | `Trace | None` | Full trace for replay |

### ProgressSnapshot

| Attribute | Type | Description |
|---|---|---|
| `elapsed` | `float` | Seconds since start |
| `total_runs` | `int` | Runs completed |
| `total_steps` | `int` | Steps completed |
| `unique_edges` | `int` | Edges discovered |
| `checkpoints` | `int` | Checkpoints saved |
| `failures` | `int` | Failures found |
| `runs_per_second` | `float` | Throughput |

### CoverageCollector

```python
CoverageCollector(target_paths: list[str])
```

| Method | Returns | Description |
|---|---|---|
| `start()` | `None` | Begin collecting edge coverage via `sys.settrace` |
| `stop()` | `frozenset[int]` | Stop and return observed edges |
| `snapshot()` | `frozenset[int]` | Current edges without stopping |

---

## Trace

```python
from ordeal.trace import Trace, TraceStep, TraceFailure, replay, shrink
```

### Trace

| Attribute | Type | Description |
|---|---|---|
| `run_id` | `int` | Run identifier |
| `seed` | `int` | RNG seed |
| `test_class` | `str` | `"module.path:ClassName"` |
| `from_checkpoint` | `int | None` | Checkpoint run_id, or `None` if fresh |
| `steps` | `list[TraceStep]` | Ordered steps |
| `failure` | `TraceFailure | None` | Failure info if applicable |
| `edges_discovered` | `int` | New edges found |
| `duration` | `float` | Run duration |

| Method | Returns | Description |
|---|---|---|
| `to_dict()` | `dict` | JSON-serializable dict |
| `save(path)` | `None` | Write to JSON file (use `.json.gz` extension for gzip compression) |
| `Trace.from_dict(data)` | `Trace` | Reconstruct from dict |
| `Trace.load(path)` | `Trace` | Load from JSON file (auto-detects `.gz` compression) |

### TraceStep

| Attribute | Type | Description |
|---|---|---|
| `kind` | `str` | `"rule"` or `"fault_toggle"` |
| `name` | `str` | Rule name or `"+fault"` / `"-fault"` |
| `params` | `dict` | Parameters drawn for this step |
| `active_faults` | `list[str]` | Faults active after this step (populated on `fault_toggle` steps; empty on `rule` steps — derive from toggle sequence) |
| `edge_count` | `int` | Cumulative edges at this step |
| `timestamp_offset` | `float` | Time since run start |

### replay

```python
replay(
    trace: Trace,
    test_class: type | None = None,     # auto-resolved from trace.test_class if None
) -> Exception | None
```

Replay a trace step-by-step. Returns the exception if it reproduces, `None` otherwise.

### shrink

```python
shrink(
    trace: Trace,
    test_class: type | None = None,
    *,
    max_time: float = 30.0,
) -> Trace
```

Shrink a failing trace to the minimal reproducing sequence. Three phases: delta debugging, step elimination, fault simplification.

### generate_tests

```python
generate_tests(
    traces: list[Trace],
    *,
    class_path: str | None = None,
) -> str
```

Convert exploration traces into standalone pytest test functions. Each generated test replays the exact rule/fault sequence — failures become regression tests, deep paths become coverage tests.

```python
from ordeal.trace import generate_tests

result = explorer.run(max_time=60, record_traces=True)
test_source = generate_tests(result.traces)
Path("tests/test_generated.py").write_text(test_source)
```

Or from the CLI: `ordeal explore --generate-tests tests/test_generated.py`

---

## QuickCheck

!!! quote "Boundary-biased property testing"
    QuickCheck gives you property-based testing with a twist: instead of purely random inputs, it biases toward boundary values -- zeros, empty strings, max-size lists, powers of two. These are the values most likely to trigger off-by-one errors and edge-case bugs. Just add type hints to your test function and `@quickcheck` handles the rest.

```python
from ordeal.quickcheck import quickcheck, strategy_for_type, biased
```

### quickcheck

```python
@quickcheck
def test_fn(x: int, y: str) -> None:
    ...

@quickcheck(max_examples=500)
def test_fn(x: float) -> None:
    ...

@quickcheck(x=st.integers(min_value=0))  # override specific parameter
def test_fn(x: int, y: str) -> None:
    ...
```

Decorator. Infers strategies from type hints, runs as property test with `max_examples=100` (default).

### strategy_for_type

```python
strategy_for_type(tp: type, *, _depth: int = 0) -> st.SearchStrategy
```

Derive a boundary-biased strategy from a type hint. Results are cached by `(tp, _depth)`. Handles: `int`, `float`, `str`, `bool`, `bytes`, `None`, `list[T]`, `dict[K, V]`, `tuple`, `set`, `Union`, `Optional`, `dataclass`, and **Pydantic `BaseModel`** (v2+ — derives strategies from `model_fields` with constraint support: `ge`/`le`/`gt`/`lt`, `min_length`/`max_length`). Recursion depth limited to 5.

### biased

Namespace of boundary-biased strategies:

```python
biased.integers(min_value=None, max_value=None) -> SearchStrategy[int]
biased.floats(min_value=None, max_value=None, *, allow_nan=False, allow_infinity=False) -> SearchStrategy[float]
biased.strings(min_size=0, max_size=100) -> SearchStrategy[str]
biased.bytes_(min_size=0, max_size=100) -> SearchStrategy[bytes]
biased.lists(elements, min_size=0, max_size=50) -> SearchStrategy[list]
```

Biased toward boundary values: 0, -1, +1, empty, max-length, powers of 2, range endpoints.

---

## Invariants

!!! quote "Composable correctness checks"
    Invariants are reusable validation rules you can compose with `&`. Instead of writing ad-hoc assertions in every test, define what "valid output" means once -- `finite & bounded(0, 1)` -- and apply it everywhere. Reach for these when you have numeric outputs that must satisfy mathematical properties like boundedness, monotonicity, or normalization.

```python
from ordeal.invariants import (
    Invariant, no_nan, no_inf, finite, bounded, monotonic,
    unique, non_empty, unit_normalized, orthonormal, symmetric,
    positive_semi_definite, rank_bounded, mean_bounded, variance_bounded,
)
```

### Invariant

```python
Invariant(name: str, check_fn: Callable[..., None])
```

| Method | Description |
|---|---|
| `__call__(value, *, name=None)` | Run check, raise `AssertionError` on violation |
| `__and__(other)` | Compose: `(a & b)(x)` checks both |

### Built-in invariants

| Invariant | Signature | Description |
|---|---|---|
| `no_nan` | singleton | Reject NaN in scalars, sequences, numpy arrays |
| `no_inf` | singleton | Reject Inf/-Inf |
| `finite` | singleton | `no_nan & no_inf` |
| `bounded` | `(lo: float, hi: float)` | All values in `[lo, hi]` |
| `monotonic` | `(*, strict: bool = False)` | Non-decreasing (or strictly increasing) |
| `unique` | `(*, key: Callable | None = None)` | No duplicates (optionally by key) |
| `non_empty` | `()` | Not empty/falsy |
| `unit_normalized` | `(*, tol: float = 1e-6)` | Row vectors have L2 norm ~1.0 |
| `orthonormal` | `(*, tol: float = 1e-6)` | Rows form orthonormal set |
| `symmetric` | `(*, tol: float = 1e-6)` | Matrix equals its transpose |
| `positive_semi_definite` | `(*, tol: float = 1e-6)` | All eigenvalues >= -tol |
| `rank_bounded` | `(min_rank=0, max_rank=None)` | Matrix rank in range |
| `mean_bounded` | `(lo: float, hi: float)` | Mean in `[lo, hi]` |
| `variance_bounded` | `(lo: float, hi: float)` | Variance in `[lo, hi]` |

```python
valid_score = finite & bounded(0, 1)
valid_score(model_output)

valid_embedding = unit_normalized() & bounded(-1, 1)
valid_embedding(embedding_matrix)
```

---

## Simulate

!!! quote "Deterministic time and filesystem"
    Clock and FileSystem replace real time and real disk with in-memory, deterministic versions. Tests that use `Clock` run instantly regardless of how many hours of simulated time pass. Tests that use `FileSystem` can inject corruption, permission errors, and disk-full conditions without touching actual files. Use these when your code depends on time or I/O and you need tests that are fast and reproducible.

```python
from ordeal.simulate import Clock, FileSystem
```

### Clock

```python
Clock(start: float = 0.0)
```

| Method | Signature | Description |
|---|---|---|
| `time()` | `-> float` | Current simulated time |
| `sleep(seconds)` | `-> None` | Advance by seconds (instant) |
| `advance(seconds)` | `-> None` | Advance, firing timers whose deadline passed |
| `set_timer(delay, callback)` | `-> int` | Schedule callback; returns timer ID |
| `pending_timers` | `-> int` | Property: unfired timer count |
| `patch()` | context manager | Patch `time.time()` and `time.sleep()` |

```python
clock = Clock()
clock.set_timer(10.0, lambda: print("fired"))
clock.advance(15.0)  # timer fires at t=10

with clock.patch():
    import time
    time.sleep(3600)  # instant
```

### FileSystem

```python
FileSystem()
```

| Method | Signature | Description |
|---|---|---|
| `write(path, data)` | `(str, str | bytes) -> None` | Write data, respecting faults |
| `read(path)` | `(str) -> bytes` | Read raw bytes, respecting faults |
| `read_text(path, encoding="utf-8")` | `(str, str) -> str` | Read decoded string |
| `exists(path)` | `(str) -> bool` | True if path exists (no "missing" fault) |
| `delete(path)` | `(str) -> None` | Remove path |
| `list_dir(prefix="/")` | `(str) -> list[str]` | Paths starting with prefix |
| `inject_fault(path, fault)` | `(str, str) -> None` | Inject: `"corrupt"`, `"missing"`, `"readonly"`, `"full"` |
| `clear_fault(path)` | `(str) -> None` | Remove fault on path |
| `clear_all_faults()` | `-> None` | Remove all faults |
| `reset()` | `-> None` | Remove all files and faults |

---

## Mutations

!!! quote "Test quality validation"
    Mutation testing answers a hard question: are your tests actually checking behavior, or just checking that the code runs? It makes small changes to your source code (swapping `+` to `-`, replacing returns with `None`) and checks whether your tests notice. A high kill score means your tests are specific. Surviving mutants point you to exactly where your assertions are too weak.

```python
from ordeal.mutations import mutate_function_and_test, mutate_and_test, validate_mined_properties, generate_mutants, MutationResult, Mutant
```

### validate_mined_properties

```python
validate_mined_properties(
    target: str,                                # dotted path: "myapp.scoring.compute"
    max_examples: int = 100,                    # examples for mine()
    operators: list[str] | None = None,         # None = all operators
) -> MutationResult
```

Mine properties of `target`, then mutate it and check the properties catch the mutations. Bridges mine() and mutation testing. Surviving mutants reveal properties too weak to detect real bugs. Used automatically by `ordeal audit`.

### mutate_function_and_test

```python
mutate_function_and_test(
    target: str,                                # dotted path: "myapp.scoring.compute"
    test_fn: Callable[[], None],                # test to run against each mutant
    operators: list[str] | None = None,         # None = all operators
    *,
    workers: int = 1,                           # parallel workers (1 = sequential)
) -> MutationResult
```

Mutate a single function via PatchFault. Safer than module-level. Recommended. Set `workers > 1` for parallel mutant testing — each mutant is independent, giving near-linear speedup.

### mutate_and_test

```python
mutate_and_test(
    target: str,                                # module path: "myapp.scoring"
    test_fn: Callable[[], None],
    operators: list[str] | None = None,
    *,
    workers: int = 1,                           # parallel workers (1 = sequential)
) -> MutationResult
```

Mutate entire module, swap in `sys.modules`. Only works if tests import the module, not individual functions.

### generate_mutants

```python
generate_mutants(
    source: str,                                # source code string
    operators: list[str] | None = None,
) -> list[tuple[Mutant, ast.Module]]
```

Generate all possible mutants from source. Returns list of `(Mutant, modified_ast)`.

### MutationResult

| Attribute | Type | Description |
|---|---|---|
| `target` | `str` | What was mutated |
| `mutants` | `list[Mutant]` | All generated mutants |
| `total` | `int` | Total mutants |
| `killed` | `int` | Mutants caught by tests |
| `survived` | `list[Mutant]` | Mutants tests missed |
| `score` | `float` | Kill ratio (1.0 = all caught) |
| `summary()` | `str` | Human-readable report |

### Mutant

| Attribute | Type | Description |
|---|---|---|
| `operator` | `str` | `"arithmetic"`, `"comparison"`, `"negate"`, `"return_none"`, `"boundary"`, `"constant"`, `"delete"` |
| `description` | `str` | What changed: `"+ -> -"` |
| `line` | `int` | Source line |
| `col` | `int` | Source column |
| `killed` | `bool` | Whether test caught it |
| `error` | `str | None` | Compilation error if mutant was invalid |
| `location` | `str` | `"L42:8"` |

**Available operators:** `arithmetic`, `comparison`, `negate`, `return_none`, `boundary`, `constant`, `delete`

### mutation_faults

```python
mutation_faults(
    target: str,                    # dotted path: "myapp.scoring.compute"
    operators: list[str] | None = None,
) -> list[tuple[Mutant, PatchFault]]
```

Generate `PatchFault` objects for each mutant. When activated, each fault replaces the target function with a mutated version. Use with ChaosTest to let the nemesis toggle mutations during exploration.

```python
from ordeal.mutations import mutation_faults
faults = [mf for _, mf in mutation_faults("myapp.scoring.compute")]
```

---

## Auto

```python
from ordeal.auto import scan_module, fuzz, chaos_for, register_fixture
```

### scan_module

```python
scan_module(
    module: str | ModuleType,
    *,
    max_examples: int = 50,
    check_return_type: bool = True,
    fixtures: dict[str, SearchStrategy] | None = None,
) -> ScanResult
```

Smoke-test every public function. Generates random inputs from type hints, checks: no crash, return type matches.

```python
result = scan_module("myapp.scoring")
assert result.passed
print(result.summary())
```

### fuzz

```python
fuzz(
    fn: Any,
    *,
    max_examples: int = 1000,
    check_return_type: bool = False,
    **fixtures: SearchStrategy | Any,
) -> FuzzResult
```

Deep-fuzz a single function.

```python
result = fuzz(myapp.scoring.compute, model=model_strategy)
assert result.passed
```

### chaos_for

```python
chaos_for(
    module: str | ModuleType,
    *,
    fixtures: dict[str, SearchStrategy] | None = None,
    invariants: list[Invariant] | None = None,
    faults: list[Fault] | None = None,
    max_examples: int = 50,
    stateful_step_count: int = 30,
) -> type
```

Auto-generate a ChaosTest from a module's public API. Each function becomes a `@rule`.

```python
TestScoring = chaos_for(
    "myapp.scoring",
    invariants=[finite, bounded(0, 1)],
    faults=[timing.timeout("myapp.scoring.predict")],
)
```

### register_fixture

```python
register_fixture(name: str, strategy: SearchStrategy) -> None
```

Register a named fixture for auto-scan. Highest priority after explicit fixtures.

### ScanResult

| Attribute | Type | Description |
|---|---|---|
| `module` | `str` | Module tested |
| `functions` | `list[FunctionResult]` | Per-function results |
| `skipped` | `list[tuple[str, str]]` | `(name, reason)` for skipped functions |
| `passed` | `bool` | All functions passed |
| `total` | `int` | Functions tested |
| `failed` | `int` | Failures |
| `summary()` | `str` | Human-readable report |

### FuzzResult

| Attribute | Type | Description |
|---|---|---|
| `function` | `str` | Function tested |
| `examples` | `int` | Examples run |
| `failures` | `list[Exception]` | Exceptions found |
| `passed` | `bool` | No failures |
| `summary()` | `str` | Human-readable report |

---

## Strategies

```python
from ordeal.strategies import corrupted_bytes, adversarial_strings, nan_floats, edge_integers, mixed_types
```

| Strategy | Signature | Description |
|---|---|---|
| `corrupted_bytes` | `(min_size=0, max_size=1024)` | Edge-case bytes: empty, all-zero, all-0xFF |
| `adversarial_strings` | `(min_size=0, max_size=256)` | SQL injection, XSS, path traversal, null bytes |
| `nan_floats` | `()` | NaN, Inf, -Inf, subnormals, boundaries |
| `edge_integers` | `(bits=64)` | 0, +/-1, min/max for N bits |
| `mixed_types` | `()` | None, bool, int, float, str, bytes, lists, dicts |

```python
from hypothesis import given
from ordeal.strategies import adversarial_strings

@given(s=adversarial_strings())
def test_parser_doesnt_crash(s):
    parse(s)  # should never raise unhandled exception
```

---

## Audit

```python
from ordeal.audit import audit, audit_report, ModuleAudit
```

### audit

```python
audit(
    module: str,                    # dotted path: "myapp.scoring"
    *,
    test_dir: str = "tests",       # directory containing existing tests
    max_examples: int = 20,        # Hypothesis examples per function
) -> ModuleAudit
```

Audit a single module: measure existing test coverage vs ordeal-migrated tests. Every number in the result is either `[verified]` or `FAILED: reason` — the audit never silently returns 0%.

Coverage is measured via coverage.py JSON reports (stable schema), not terminal parsing. Results are cross-checked for consistency. Generated test files are saved to `.ordeal/test_<module>_migrated.py`.

### audit_report

```python
audit_report(
    modules: list[str],
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
) -> str
```

Audit multiple modules and produce a formatted summary report. Every number labeled `[verified]` or `FAILED`.

### ModuleAudit

| Attribute | Type | Description |
|---|---|---|
| `module` | `str` | Module path |
| `current_test_count` | `int` | Existing test count |
| `current_test_lines` | `int` | Lines of existing test code |
| `current_coverage` | `CoverageMeasurement` | Coverage from existing tests (with status) |
| `migrated_test_count` | `int` | Tests in generated migrated file |
| `migrated_lines` | `int` | Lines in generated migrated file |
| `migrated_coverage` | `CoverageMeasurement` | Coverage from migrated tests (with status) |
| `mined_properties` | `list[str]` | Properties with Wilson CI bounds |
| `gap_functions` | `list[str]` | Functions needing fixtures |
| `suggestions` | `list[str]` | Actionable suggestions for uncovered lines |
| `mutation_score` | `str` | e.g. `"8/10 (80%)"` — how many mutations mined properties catch |
| `not_checked` | `list[str]` | Known unknowns — what ordeal structurally cannot verify |
| `warnings` | `list[str]` | Every problem visible here |
| `generated_test` | `str` | Full generated test file content |
| `coverage_preserved` | `bool` | True if migrated >= current - 2% (False if either failed) |
| `summary()` | `str` | Human-readable report with `[verified]`/`FAILED` labels |

### CoverageMeasurement

Every coverage number carries its epistemic status.

```python
from ordeal.audit import CoverageMeasurement, Status
```

| Attribute | Type | Description |
|---|---|---|
| `status` | `Status` | `VERIFIED` or `FAILED` |
| `result` | `CoverageResult | None` | Structured data if verified |
| `error` | `str | None` | Explanation if failed |
| `percent` | `float` | Coverage %, or 0.0 if failed |
| `missing_lines` | `frozenset[int]` | Uncovered lines, or empty if failed |

### CoverageResult

```python
from ordeal.audit import CoverageResult
```

| Attribute | Type | Description |
|---|---|---|
| `percent` | `float` | Coverage percentage |
| `total_statements` | `int` | Total source statements |
| `missing_count` | `int` | Number of uncovered statements |
| `missing_lines` | `frozenset[int]` | Uncovered line numbers |
| `source` | `str` | How measured (e.g. `"coverage.py JSON"`) |

### wilson_lower

```python
wilson_lower(successes: int, total: int, z: float = 1.96) -> float
```

Lower bound of the Wilson score confidence interval. For mined properties: 500/500 at 95% CI gives lower bound ~0.994, meaning "holds with >=99.4% probability" — not "always holds."

---

## Diff

```python
from ordeal.diff import diff, DiffResult, Mismatch
```

Differential testing — compare two implementations on the same random inputs.

### diff

```python
diff(
    fn_a: Callable,                             # reference function
    fn_b: Callable,                             # function to compare
    *,
    max_examples: int = 100,
    rtol: float | None = None,                  # relative tolerance
    atol: float | None = None,                  # absolute tolerance
    compare: Callable[[Any, Any], bool] | None = None,  # custom comparator
    **fixtures: SearchStrategy | Any,
) -> DiffResult
```

Compare two functions for equivalence. Infers strategies from `fn_a`'s type hints. Both functions must accept the same parameters.

```python
# Exact comparison
result = diff(score_v1, score_v2)
assert result.equivalent

# Floating-point tolerance
result = diff(compute_old, compute_new, rtol=1e-6)

# Custom comparator
result = diff(fn_a, fn_b, compare=lambda a, b: a.status == b.status)
```

### DiffResult

| Attribute | Type | Description |
|---|---|---|
| `function_a` | `str` | Name of reference function |
| `function_b` | `str` | Name of compared function |
| `total` | `int` | Examples tested |
| `mismatches` | `list[Mismatch]` | Inputs where outputs differed |
| `equivalent` | `bool` | True if no mismatches |
| `summary()` | `str` | Human-readable report |

### Mismatch

| Attribute | Type | Description |
|---|---|---|
| `args` | `dict` | Input arguments that caused divergence |
| `output_a` | `Any` | Output from `fn_a` |
| `output_b` | `Any` | Output from `fn_b` |

---

## Scaling

```python
from ordeal.scaling import usl, amdahl, optimal_n, peak_throughput, fit_usl, analyze, benchmark
```

Universal Scaling Law (USL) and Amdahl's Law for predicting parallel exploration performance.

### usl

```python
usl(n: float, sigma: float, kappa: float) -> float
```

`C(N) = N / [1 + sigma*(N-1) + kappa*N*(N-1)]`. Returns relative throughput (C(1) = 1).

- `sigma`: contention coefficient — fraction of serialized work
- `kappa`: coherence coefficient — cross-worker sync cost (grows quadratically)

### amdahl / optimal_n / peak_throughput

```python
amdahl(n: float, sigma: float) -> float          # USL with kappa=0
optimal_n(sigma: float, kappa: float) -> float    # worker count at peak throughput
peak_throughput(sigma: float, kappa: float) -> float
```

### fit_usl

```python
fit_usl(measurements: list[tuple[int | float, float]]) -> tuple[float, float]
```

Fit sigma and kappa from `(N, throughput)` pairs via least squares. Requires >= 3 data points.

### analyze

```python
analyze(measurements: list[tuple[int | float, float]]) -> ScalingAnalysis
```

Fit USL and return full analysis with diagnosis.

### benchmark

```python
benchmark(
    test_class: type,
    *,
    target_modules: list[str] | None = None,
    max_workers: int | None = None,       # default: CPU count
    time_per_trial: float = 10.0,
    seed: int = 42,
    steps_per_run: int = 50,
    metric: str = "runs",                 # "runs" or "edges"
) -> ScalingAnalysis
```

Benchmark exploration at N=1, 2, 4, ... workers, measure throughput, fit USL parameters automatically.

```python
from ordeal.scaling import benchmark
analysis = benchmark(MyServiceChaos, target_modules=["myapp"])
print(analysis.summary())
```

### scales_linearly

```python
from ordeal.scaling import scales_linearly

@scales_linearly(n_range=(1, 8), max_kappa=0.01, max_sigma=0.3)
def process_batch(items):
    ...
```

Decorator: assert that a function scales linearly with concurrency. Runs the function with increasing worker counts, fits the USL model, and fails if contention or coherence exceed thresholds.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `n_range` | `tuple[int, int]` | `(1, 8)` | `(min_workers, max_workers)` to test |
| `max_kappa` | `float` | `0.01` | Fail if coherence exceeds this (quadratic overhead) |
| `max_sigma` | `float` | `0.3` | Fail if contention exceeds this (serial bottleneck) |
| `samples` | `int` | `3` | Number of worker counts to test between min and max |
| `time_per_sample` | `float` | `2.0` | Seconds to run at each worker count |

Raises `AssertionError` with diagnostics when thresholds are exceeded. Works as a bare decorator (`@scales_linearly`) or with parameters (`@scales_linearly(max_kappa=0.005)`).

### ScalingAnalysis

| Attribute | Type | Description |
|---|---|---|
| `sigma` | `float` | Contention coefficient |
| `kappa` | `float` | Coherence coefficient |
| `n_optimal` | `float` | Worker count at peak throughput |
| `peak` | `float` | Maximum achievable throughput multiplier |
| `regime` | `str` | `"linear"`, `"amdahl"`, or `"usl"` |
| `efficiency(n)` | `float` | Parallel efficiency C(N)/N at N workers |
| `throughput(n)` | `float` | Predicted relative throughput at N workers |
| `summary()` | `str` | Human-readable report with diagnosis |

---

## Mine

```python
from ordeal.mine import mine, mine_pair, MineResult, MinedProperty
```

### mine

```python
mine(
    fn: Callable,
    *,
    max_examples: int = 500,
    **fixtures: SearchStrategy | Any,
) -> MineResult
```

Discover likely properties of a function by running it many times with random inputs and observing patterns in outputs.

Properties checked: type consistency, never None, no NaN, non-negative, bounded [0,1], never empty, deterministic, idempotent, involution (`f(f(x)) == x`), commutative (`f(a,b) == f(b,a)`), associative (`f(f(a,b),c) == f(a,f(b,c))`), observed range, monotonicity (per numeric input parameter), and length relationships (`len(output) == len(input)`). Float comparisons use `math.isclose` (rel_tol=1e-9, abs_tol=1e-12) so rounding noise doesn't cause false negatives.

```python
result = mine(myapp.scoring.compute, max_examples=500)
for p in result.universal:
    print(p)
# ALWAYS  output type is float (500/500)
# ALWAYS  deterministic (50/50)
# ALWAYS  output in [0, 1] (500/500)
```

### mine_pair

```python
mine_pair(
    f: Callable,
    g: Callable,
    *,
    max_examples: int = 200,
    **fixtures: SearchStrategy | Any,
) -> MineResult
```

Discover relational properties between two functions. Checks roundtrip (`g(f(x)) == x`), reverse roundtrip (`f(g(x)) == x`), and commutative composition (`f(g(x)) == g(f(x))`). Strategies are inferred from `f`'s signature.

```python
result = mine_pair(encode, decode)
# roundtrip decode(encode(x)) == x: ALWAYS
```

### MineResult

Results are separated into three categories: checked and applicable, checked but not relevant, and structurally impossible to check.

| Attribute | Type | Description |
|---|---|---|
| `function` | `str` | Function name |
| `examples` | `int` | Examples run |
| `properties` | `list[MinedProperty]` | Checked and applicable (total > 0) |
| `not_applicable` | `list[str]` | Checked but not relevant (e.g. "bounded [0,1]" for string output) |
| `not_checked` | `list[str]` | Structural limitations — things mine() cannot verify |
| `universal` | `list[MinedProperty]` | Properties that held on every example |
| `likely` | `list[MinedProperty]` | Properties with >= 95% confidence |
| `summary()` | `str` | Human-readable report |

### STRUCTURAL_LIMITATIONS

```python
from ordeal.mine import STRUCTURAL_LIMITATIONS
```

Things mine() fundamentally cannot discover from random sampling — these require domain knowledge:

- Output value correctness (fuzz checks crash safety, not behavior)
- Cross-function consistency (e.g., batch == map of individual)
- Domain-specific invariants (e.g., weighted sum, refusal detection)
- Error handling for intentionally invalid inputs
- Performance and resource usage
- Concurrency and thread safety
- State mutation and side effects

### MinedProperty

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Property description |
| `holds` | `int` | Times property held |
| `total` | `int` | Times property was checked |
| `counterexample` | `dict | None` | First counterexample if not universal |
| `confidence` | `float` | `holds / total` |
| `universal` | `bool` | True if held on every example |

### validate_mined_properties

```python
from ordeal.mutations import validate_mined_properties

validate_mined_properties(
    target: str,                    # dotted path: "myapp.scoring.compute"
    max_examples: int = 100,
    operators: list[str] | None = None,
) -> MutationResult
```

Mine properties of `target`, then mutate the code and check whether the mined properties catch the mutations. Surviving mutants reveal properties that are too weak. Used by `ordeal audit` to report mutation scores.

---

## Metamorphic

```python
from ordeal.metamorphic import Relation, RelationSet, metamorphic
```

Metamorphic testing checks *relationships* between outputs rather than exact values. Define a relation that transforms input and checks how outputs relate, then apply it as a decorator.

### Relation

```python
Relation(
    name: str,                                              # human-readable label
    transform: Callable[[tuple], tuple],                    # transform input args
    check: Callable[[Any, Any], bool],                      # (original_out, transformed_out) -> bool
)
```

Compose with `+`: `(relation_a + relation_b)` checks both.

### metamorphic

```python
@metamorphic(*relations: Relation | RelationSet, max_examples: int = 100)
def test_fn(x: int, y: int):
    return x + y
```

Decorator. For each Hypothesis-generated input, runs the function on original and transformed inputs, then asserts the relation's `check` holds. Strategies inferred from type hints.

```python
commutative = Relation(
    "commutative",
    transform=lambda args: (args[1], args[0]),
    check=lambda a, b: a == b,
)

negate_involution = Relation(
    "negate is involution",
    transform=lambda args: (-args[0],),
    check=lambda a, b: abs(a + b) < 1e-6,
)

@metamorphic(commutative)
def test_add(x: int, y: int):
    return x + y

@metamorphic(negate_involution)
def test_negate(x: float):
    return -x
```

---

## Config

```python
from ordeal.config import load_config, OrdealConfig, ExplorerConfig, TestConfig, ReportConfig, ScanConfig
```

### load_config

```python
load_config(path: str | Path = "ordeal.toml") -> OrdealConfig
```

Load and validate an `ordeal.toml`. Raises `FileNotFoundError` if missing, `ConfigError` on invalid keys/types.

### OrdealConfig

| Attribute | Type | Default |
|---|---|---|
| `explorer` | `ExplorerConfig` | see below |
| `tests` | `list[TestConfig]` | `[]` |
| `scan` | `list[ScanConfig]` | `[]` |
| `report` | `ReportConfig` | see below |

### ExplorerConfig

| Attribute | Type | Default |
|---|---|---|
| `target_modules` | `list[str]` | `[]` |
| `max_time` | `float` | `60.0` |
| `max_runs` | `int | None` | `None` |
| `seed` | `int` | `42` |
| `max_checkpoints` | `int` | `256` |
| `checkpoint_prob` | `float` | `0.4` |
| `checkpoint_strategy` | `str` | `"energy"` |
| `steps_per_run` | `int` | `50` |
| `fault_toggle_prob` | `float` | `0.3` |
| `workers` | `int` | `1` |

### TestConfig

| Attribute | Type | Required |
|---|---|---|
| `class_path` | `str` | Yes |
| `steps_per_run` | `int | None` | No |
| `swarm` | `bool | None` | No |

`resolve() -> type` — import and return the ChaosTest class.

### ReportConfig

| Attribute | Type | Default |
|---|---|---|
| `format` | `str` | `"text"` |
| `output` | `str` | `"ordeal-report.json"` |
| `traces` | `bool` | `False` |
| `traces_dir` | `str` | `".ordeal/traces"` |
| `verbose` | `bool` | `False` |

### ScanConfig

| Attribute | Type | Default |
|---|---|---|
| `module` | `str` | required |
| `max_examples` | `int` | `50` |
| `fixtures` | `dict[str, str]` | `{}` |
