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
| `with fault:` | Context manager — activates on enter, deactivates on exit |

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
# In ChaosTest — nemesis toggles automatically
faults = [
    io.error_on_call("myapp.storage.save", IOError, "disk unreachable"),
    io.corrupt_output("myapp.cache.read"),
    io.subprocess_timeout("cargo run"),
    io.disk_full(),
]

# As context manager — scoped activation in regular tests
with io.subprocess_timeout("cargo run"):
    result = run_kernel()
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
| `dtype_drift` | `(target: str, kind: str = "str") -> PatchFault` | Coerces numeric output into string/int/bool/object leaves |
| `partial_batch` | `(target: str, fraction: float = 0.5, min_items: int = 1) -> PatchFault` | Truncates batch-like output on the first axis |
| `feature_order_drift` | `(target: str, shift: int = 1) -> PatchFault` | Rotates feature order without changing outer shape |
| `missing_feature` | `(target: str, key: str \| None = None, *, fill: object = ...) -> PatchFault` | Drops one feature key or replaces it with a fill value |
| `corrupted_floats` | `(corrupt_type: str = "nan") -> Fault` | Standalone corrupt float source; use `fault.value()` |

```python
faults = [
    numerical.nan_injection("myapp.model.predict"),
    numerical.partial_batch("myapp.model.predict", fraction=0.5),
    numerical.missing_feature("myapp.features.fetch", "country"),
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
    share_checkpoints: bool = True,             # shared checkpoint ring for workers
    mutation_targets: list[str] | None = None,
    seed_mutation_prob: float | None = None,
    seed_mutation_respect_strategies: bool = False,
    ngram: int = 2,
    corpus_dir: str | Path | None = ".ordeal/seeds",
    rule_swarm: bool = False,
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
    *,
    preset: Literal["essential", "standard", "thorough"] | None = None,
    mine_result: MineResult | None = None,
    validation_mode: Literal["fast", "deep"] = "fast",
) -> MutationResult
```

Mine properties of `target`, then mutate it and check the properties catch the mutations. Bridges mine() and mutation testing. Surviving mutants reveal properties too weak to detect real bugs. `validation_mode="fast"` replays mined inputs against each mutant; `validation_mode="deep"` keeps that replay check and then re-runs `mine()` on each mutant. Used automatically by `ordeal audit`.

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
    workers: int = 1,              # parallel mutation-validation workers
    validation_mode: Literal["fast", "deep"] = "fast",
) -> ModuleAudit
```

Audit a single module: measure existing test coverage vs ordeal-migrated tests. Every number in the result is either `[verified]` or `FAILED: reason` — the audit never silently returns 0%. `validation_mode="fast"` replays mined inputs against mutants. `validation_mode="deep"` keeps that replay check and then re-mines each mutant.

Coverage is measured via coverage.py JSON reports (stable schema), not terminal parsing. Results are cross-checked for consistency. Generated test files are saved to `.ordeal/test_<module>_migrated.py`.

### audit_report

```python
audit_report(
    modules: list[str],
    *,
    test_dir: str = "tests",
    max_examples: int = 20,
    workers: int = 1,
    validation_mode: Literal["fast", "deep"] = "fast",
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
| `validation_mode` | `Literal["fast", "deep"]` | Whether audit used replay or deep re-mining for mutation validation |
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
    test_class: type | None = None,
    *,
    target_modules: list[str] | None = None,
    max_workers: int | None = None,       # default: CPU count
    time_per_trial: float = 10.0,
    seed: int = 42,
    steps_per_run: int = 50,
    metric: str = "runs",                 # "runs" or "edges"
    mutate_targets: list[str] | None = None,
    repeats: int = 5,
    workers: int = 1,
    preset: str | None = "standard",
    filter_equivalent: bool = True,
    test_filter: str | None = None,
) -> ScalingAnalysis | MutationBenchmarkSuite
```

Benchmark exploration at N=1, 2, 4, ... workers, measure throughput, fit USL parameters automatically. When `mutate_targets=[...]` is provided, benchmark mutation latency in fresh subprocesses instead and report median wall time plus per-phase timings.

```python
from ordeal.scaling import benchmark
analysis = benchmark(MyServiceChaos, target_modules=["myapp"])
print(analysis.summary())
```

```python
from ordeal.scaling import benchmark
suite = benchmark(
    mutate_targets=["tests._mutation_bench_target.tiny_add"],
    repeats=5,
    preset="standard",
)
print(suite.summary())
```

### benchmark_perf_contract

```python
from ordeal.scaling import benchmark_perf_contract

suite = benchmark_perf_contract("ordeal.perf.toml")
print(suite.summary())
```

Run a checked-in perf/quality contract. Supports import latency, audit latency, mutation latency, and `audit_compare` cases that fail when one audit validation mode falls too far behind another on mutation score.

When used from the CLI, `--output-json PATH` writes a stable artifact with `passed`, `cases`, `failures`, and per-case timing/score details so agents can consume the result without parsing text.

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
    *,
    preset: Literal["essential", "standard", "thorough"] | None = None,
    mine_result: MineResult | None = None,
    validation_mode: Literal["fast", "deep"] = "fast",
) -> MutationResult
```

Mine properties of `target`, then mutate the code and check whether the mined properties catch the mutations. Surviving mutants reveal properties that are too weak. `validation_mode="fast"` replays mined inputs against mutants. `validation_mode="deep"` keeps that replay check and then re-runs `mine()` for each mutant. Used by `ordeal audit` to report mutation scores.

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
| `seed_mutation_respect_strategies` | `bool` | `False` |

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

---

## Exploration State

!!! quote "Unified view of what ordeal knows about your code"
    Every tool (mine, mutate, scan, chaos) explores one dimension of the state space. `ExplorationState` accumulates their results into a single, persistent, queryable picture. AI assistants read this to understand what's been explored, what's missing, and how confident the results are.

```python
from ordeal.state import explore, ExplorationState
from ordeal.state import explore_mine, explore_scan, explore_mutate, explore_chaos
```

### explore

```python
explore(
    module: str,
    *,
    state: ExplorationState | None = None,  # resume from previous
    time_limit: float | None = None,
    workers: int = 1,                       # parallel mutation testing
    max_examples: int = 50,                 # input space sampling depth
    seed: int = 42,
    patch_io: bool = False,                 # deterministic file/network/subprocess I/O
) -> ExplorationState
```

Runs all exploration strategies in sequence: mine → scan → mutate → chaos. Each step enriches the shared `ExplorationState`. Scales with `workers` (mutation parallelism) and `max_examples` (input sampling depth). Resume from a previous state to accumulate confidence across sessions. Set `patch_io=True` to run the pipeline inside the deterministic supervisor's file/network/subprocess substrate.

Individual steps (`explore_mine`, `explore_scan`, `explore_mutate`, `explore_chaos`) are available for finer control.

### ExplorationState

| Attribute | Type | Description |
|---|---|---|
| `module` | `str` | Module being explored |
| `functions` | `dict[str, FunctionState]` | Per-function exploration state |
| `skipped` | `list[tuple[str, str]]` | Functions skipped during mining with reasons |
| `refreshed` | `list[str]` | Functions invalidated because source changed |
| `confidence` | `float` | Aggregate confidence [0, 1] across all functions |
| `frontier` | `dict[str, list[str]]` | Per-function gaps — what's unexplored |
| `findings` | `list[str]` | Bugs and anomalies found |
| `finding_details` | `list[dict]` | Structured findings for reports and agent handoff |
| `exploration_time` | `float` | Wall-clock time accumulated across runs |
| `supervisor_info` | `dict[str, Any]` | Reproduction info: seed, transitions, states, scheduler/subprocess data |
| `summary()` | `str` | Human-readable exploration report |
| `to_dict()` | `dict` | JSON-friendly state payload for persistence and agents |
| `to_json()` | `str` | Serialize for persistence across sessions |
| `from_json(data)` | `ExplorationState` | Deserialize |
| `refresh()` | `list[str]` | Invalidate stale function results after source changes |

### FunctionState

| Attribute | Type | Description |
|---|---|---|
| `mined` | `bool` | Whether mine() has been run |
| `properties` | `list[dict]` | Discovered properties with confidence |
| `property_violations` | `list[str]` | Suspicious discovered properties summarized as findings |
| `property_violation_details` | `list[dict]` | Structured property-finding details |
| `mutation_score` | `float | None` | Kill ratio from mutation testing |
| `survived_mutants` | `int` | Mutants that survived the current test suite |
| `killed_mutants` | `int` | Mutants killed by the current test suite |
| `hardened` | `bool` | Whether extra tests have been verified against survivors |
| `hardened_kills` | `int` | Additional survivors closed by hardening |
| `crash_free` | `bool | None` | Whether random inputs crashed |
| `scan_error` | `str | None` | Crash/error text from scan_module() |
| `failing_args` | `dict[str, Any] | None` | Shrunk failing arguments from scan/fuzz |
| `chaos_tested` | `bool` | Whether chaos testing has been run |
| `faults_tested` | `list[str]` | Fault names exercised during chaos testing |
| `edges_discovered` | `int` | Unique code paths reached |
| `saturated` | `bool` | True when more mining won't find new paths |
| `confidence` | `float` | Per-function confidence [0, 1] |
| `frontier` | `list[str]` | What's unexplored for this function |

---

## Agent Schema

```python
from ordeal.agent_schema import (
    AgentArtifact,
    AgentEnvelope,
    AgentFinding,
    build_agent_envelope,
)
```

Stable JSON envelope used by CLI `--json` output and other machine consumers.

### AgentFinding

| Attribute | Type | Description |
|---|---|---|
| `kind` | `str` | Finding class such as `crash`, `mutation`, `property`, or `blocked` |
| `summary` | `str` | One-line human-readable statement |
| `confidence` | `float | None` | Optional confidence score |
| `target` | `str | None` | Dotted path or module the finding applies to |
| `location` | `str | None` | Optional file/line or symbolic location |
| `details` | `dict[str, Any]` | Machine-readable structured payload |
| `to_dict()` | `dict` | JSON-friendly representation |

### AgentArtifact

| Attribute | Type | Description |
|---|---|---|
| `kind` | `str` | Artifact type such as `report`, `regression`, `trace`, or `index` |
| `uri` | `str` | Path or URI to the artifact |
| `description` | `str | None` | Short human-readable explanation |
| `metadata` | `dict[str, Any]` | Extra machine-readable metadata |
| `to_dict()` | `dict` | JSON-friendly representation |

### AgentEnvelope

| Attribute | Type | Description |
|---|---|---|
| `schema_version` | `str` | Stable envelope schema version |
| `tool` | `str` | Producing command or subsystem (`scan`, `mine`, `mutate`, ...) |
| `target` | `str` | Primary module/function/trace target |
| `status` | `str` | Overall status such as `ok`, `issue_found`, or `blocked` |
| `summary` | `str` | High-signal one-line summary |
| `recommended_action` | `str` | Best next action for the consumer |
| `suggested_commands` | `list[str]` | Follow-up shell commands |
| `suggested_test_file` | `str | None` | Suggested regression test path |
| `confidence` | `float | None` | Optional confidence score |
| `confidence_basis` | `list[str]` | Short reasons behind the confidence value |
| `blocking_reason` | `str | None` | Why execution was blocked, if applicable |
| `findings` | `list[AgentFinding]` | Structured findings |
| `artifacts` | `list[AgentArtifact]` | Produced or referenced artifacts |
| `raw_details` | `dict[str, Any]` | Tool-specific payload not normalized into top-level fields |
| `to_dict()` | `dict` | Stable machine-readable dict |
| `to_json()` | `str` | Deterministically sorted JSON |

### build_agent_envelope

```python
build_agent_envelope(
    *,
    tool: str,
    target: str,
    status: str,
    summary: str,
    recommended_action: str = "",
    suggested_commands: Sequence[str] = (),
    suggested_test_file: str | None = None,
    confidence: float | None = None,
    confidence_basis: Sequence[str] = (),
    blocking_reason: str | None = None,
    findings: Sequence[AgentFinding | Mapping[str, Any]] = (),
    artifacts: Sequence[AgentArtifact | Mapping[str, Any]] = (),
    raw_details: Mapping[str, Any] | None = None,
    schema_version: str = "1.0",
) -> AgentEnvelope
```

Normalize mixed finding/artifact inputs into a stable `AgentEnvelope`.

---

## Deterministic Supervisor

!!! quote "Control non-determinism for reproducible exploration"
    Execution is non-deterministic: RNG state, time, subprocess timing, and interleavings all vary between runs. The same code can produce different behavior. `DeterministicSupervisor` fixes this by seeding every entropy source, replacing time with a deterministic clock, and optionally running subprocesses and cooperative tasks against a seed-driven scheduler. Same seed = same execution. Different seeds = different exploration trajectories.

```python
from ordeal.supervisor import DeterministicSupervisor, StateTree, StateNode
```

### DeterministicSupervisor

```python
import subprocess

with DeterministicSupervisor(seed=42) as sup:
    # random, buggify, numpy all seeded
    # time.time() and time.sleep() are deterministic
    result = my_function()
    sup.log_transition("called my_function", state_hash=hash(result))

with DeterministicSupervisor(seed=42, patch_io=True) as sup:
    sup.register_subprocess(["worker", "--once"], stdout="ok\n", delay=2.0)
    out = subprocess.check_output(["worker", "--once"], text=True)
    assert out == "ok\n"

with DeterministicSupervisor(seed=42) as sup:
    def worker(name):
        yield sup.yield_now()
        yield sup.sleep(1.0)
        return name

    sup.spawn("a", worker, "a")
    sup.spawn("b", worker, "b")
    results = sup.run_until_idle()
```

| Method | Description |
|---|---|
| `log_transition(action, state_hash=)` | Record a state transition |
| `spawn(name, task, *args, **kwargs)` | Register a cooperative task with the deterministic scheduler |
| `yield_now()` | Yield control back to the scheduler |
| `sleep(seconds)` | Suspend the running task for simulated time |
| `run_until_idle(max_steps=None)` | Run cooperative tasks until completion or a step limit |
| `register_subprocess(command, stdout=, stderr=, returncode=, delay=, match=)` | Register deterministic `subprocess.run` / `check_output` / `Popen` behavior |
| `clear_subprocesses()` | Remove registered deterministic subprocesses |
| `fork(new_seed=)` | Create a new supervisor from current state with different seed |
| `state` | Current state hash |
| `trajectory` | List of `Transition` objects |
| `visited_states` | All states visited |
| `task_results` | Completed cooperative task results keyed by name |
| `pending_tasks` | Cooperative tasks that are still blocked or runnable |
| `reproduction_info()` | Dict with seed, `patch_io`, subprocess count, scheduler steps, hash seed, steps — everything needed to replay |
| `summary()` | Human-readable trajectory report |

### StateTree

Navigable exploration tree with checkpoint and rollback. Each node is a checkpointed state; edges are actions taken. The AI can checkpoint, explore a branch, roll back, and try a different branch.

```python
tree = StateTree()
tree.checkpoint(state_id=0, snapshot=my_state)
tree.checkpoint(state_id=1, parent=0, action="deposit(50)", snapshot=new_state)

old = tree.rollback(0)  # returns deepcopy of checkpointed state
tree.checkpoint(state_id=2, parent=0, action="withdraw(50)", snapshot=other_state)
```

| Method | Description |
|---|---|
| `checkpoint(state_id, snapshot=, parent=, action=, edges=, seed=)` | Save a state as a tree node |
| `rollback(state_id)` | Return deepcopy of a previous checkpoint |
| `frontier()` | Nodes that can be explored further |
| `leaves()` | Deepest explored states |
| `path_to(state_id)` | Sequence of actions from root to a node |
| `summary()` | Visual tree structure |
| `to_json()` | Serialize tree (without snapshots) |

---

## CMPLOG

!!! quote "Crack guarded branches that random testing can't reach"
    When code has `if x == 42 and mode == "admin"`, random testing will almost never generate those exact values. CMPLOG parses the function's AST, extracts literal values from comparisons, and injects them into Hypothesis strategies. This is the Python equivalent of AFL++'s CMPLOG/RedQueen technique.

```python
from ordeal.cmplog import extract_comparison_values, enhance_strategies
```

### extract_comparison_values

```python
extract_comparison_values(fn: Callable) -> dict[str, list[Any]]
```

Returns `{"param_name": [literal_values]}` extracted from `==`, `!=`, `in`, `>=`, etc. in the function source.

### enhance_strategies

```python
enhance_strategies(
    strategies: dict[str, SearchStrategy],
    fn: Callable,
) -> dict[str, SearchStrategy]
```

Merges extracted magic values into Hypothesis strategies. The enhanced strategy generates branch-cracking values alongside random exploration.

Automatically wired into `mine()` — no manual usage needed. Available for custom fuzzing loops.

---

## Mutagen

!!! quote "AFL's bit-flip loop for Python values"
    Real fuzzers don't generate random inputs from scratch — they mutate known-good inputs. `mutagen` applies type-aware perturbation to Python values: bit-flips for ints, mantissa perturbation for floats, character swaps for strings. Combined with coverage feedback, mutations that reach new code paths become seeds for further mutation.

```python
from ordeal.mutagen import mutate_value, mutate_inputs
```

### mutate_value

```python
mutate_value(value: Any, rng: random.Random, intensity: float = 0.3) -> Any
```

Mutate a single value. Type-aware: ints get bit-flips and arithmetic perturbation, floats get mantissa perturbation and special values (NaN, Inf), strings get character swaps and boundary strings, lists/dicts get element mutation.

### mutate_inputs

```python
mutate_inputs(
    inputs: dict[str, Any],
    rng: random.Random,
    intensity: float = 0.3,
) -> dict[str, Any]
```

Mutate a full kwargs dict (like those in `MineResult.collected_inputs`). Returns a new dict with mutated values. Keys are preserved.

Automatically wired into `mine()` Phase 2 — after Hypothesis sampling, productive inputs are mutated to explore nearby state space. Available for custom fuzzing loops.

---

## Cross-Function Mining

!!! quote "Discover relationships between functions automatically"
    Single-function mining finds properties like "output >= 0". Cross-function mining finds relationships like "decode(encode(x)) == x" — roundtrips, composition commutativity, output equivalence. Tests all compatible function pairs automatically.

```python
from ordeal.mine import mine_module, MineModuleResult, CrossFunctionProperty
```

### mine_module

```python
mine_module(
    module: str | ModuleType,
    *,
    max_examples: int = 30,
    mine_per_function: bool = True,
) -> MineModuleResult
```

Discovers per-function properties (via `mine()`) and cross-function relationships for all compatible pairs.

### CrossFunctionProperty

| Attribute | Type | Description |
|---|---|---|
| `function_a` | `str` | First function |
| `function_b` | `str` | Second function |
| `relation` | `str` | `"roundtrip"`, `"commutative_composition"`, or `"equivalent"` |
| `confidence` | `float` | Fraction of inputs where the relation held |
| `holds` | `int` | Number of inputs where it held |
| `total` | `int` | Number of inputs tested |
| `counterexample` | `dict | None` | One failing input if relation doesn't hold universally |

---

## Grammar Strategies

!!! quote "Syntax-valid inputs reach deeper code"
    Random bytes and strings get rejected at the parser level — they never reach the business logic that actually has bugs. Grammar-aware strategies generate syntactically valid inputs (JSON, SQL, URLs, etc.) that pass parsing and exercise the code paths that matter. This is the Python equivalent of libFuzzer's structure-aware custom mutators.

```python
from ordeal.grammar import json_strategy, sql_strategy, url_strategy
from ordeal.grammar import email_strategy, csv_strategy, xml_strategy
from ordeal.grammar import path_strategy, regex_strategy, structured_strategy
```

Each returns a `hypothesis.strategies.SearchStrategy`. Use with `@given`, `@quickcheck`, `mine()`, or any Hypothesis-based tool.

| Strategy | What it generates | Key parameters |
|---|---|---|
| `json_strategy(schema=, max_depth=3)` | Valid JSON values (objects, arrays, primitives) | `schema` dict constrains structure |
| `sql_strategy(dialect=, tables=)` | Valid SELECT/INSERT/UPDATE/DELETE | `tables` dict of `{name: [columns]}` |
| `url_strategy(schemes=)` | Valid URLs with paths, query params, fragments | `schemes` list (default: http, https, ftp) |
| `email_strategy()` | Valid email addresses | — |
| `path_strategy()` | Valid Unix and Windows file paths | — |
| `csv_strategy(columns=, rows=)` | Valid CSV with headers | `columns` list of names |
| `xml_strategy(tag=, max_depth=2)` | Well-formed XML with elements and attributes | `tag` root element name |
| `regex_strategy(pattern)` | Strings matching a regex | `pattern` regex string |
| `structured_strategy(example)` | Values structurally similar to the example | Any Python value |

```python
# Generate valid JSON for API testing
from ordeal.grammar import json_strategy
@given(payload=json_strategy({"type": "object"}))
def test_api(payload):
    response = my_api.post(payload)
    assert response.status_code < 500

# Generate valid SQL for query testing
from ordeal.grammar import sql_strategy
@given(query=sql_strategy(tables={"users": ["id", "name", "email"]}))
def test_query_parser(query):
    parsed = parse_sql(query)
    assert parsed is not None

# Infer strategy from an example
from ordeal.grammar import structured_strategy
example = {"name": "Alice", "scores": [95, 87, 92], "active": True}
@given(data=structured_strategy(example))
def test_process(data):
    result = process_record(data)
    assert result is not None
```

---

## Equivalence Detection

!!! quote "Not all surviving mutants are test gaps"
    Equivalent mutants are code changes that don't change behavior — they always survive mutation testing, inflating the "test gap" count and wasting developer time. Detecting them is one of the hardest problems in mutation testing. ordeal provides three complementary approaches: structural (fast), statistical (medium), and formal (slow, definitive).

```python
from ordeal.equivalence import (
    structural_equivalence,
    statistical_equivalence,
    prove_equivalent,
    classify_mutant,
    filter_equivalent_mutants,
    EquivalenceResult,
)
```

### Three approaches, layered fast → slow

**Structural** — AST comparison after normalization. Catches trivially equivalent mutants (e.g., reordering commutative operations). Fast but conservative.

**Statistical** — Run both versions on random inputs, compare outputs. Uses Wilson score confidence interval to bound equivalence probability. Medium speed, probabilistic.

**Formal** — Z3 SMT solver encodes both functions and checks semantic identity. Definitive proof but slow. Optional: `pip install z3-solver`.

### classify_mutant

```python
classify_mutant(
    original_fn: Callable,
    mutant_fn: Callable,
    original_source: str,
    mutant_source: str,
    *,
    max_seconds: float = 5,
) -> EquivalenceResult
```

Runs all three methods in order (structural → statistical → formal). Returns the first definitive result.

### EquivalenceResult

| Attribute | Type | Description |
|---|---|---|
| `equivalent` | `bool | None` | `True` = equivalent, `False` = different, `None` = inconclusive |
| `confidence` | `float` | 1.0 for proven, 0.0-1.0 for statistical |
| `method` | `str` | `"structural"`, `"statistical"`, `"formal"`, or `"inconclusive"` |
| `counterexample` | `dict | None` | Input where outputs differ (if not equivalent) |
| `time_seconds` | `float` | Time taken for the analysis |

### filter_equivalent_mutants

```python
filter_equivalent_mutants(
    target: str,
    mutant_pairs: list[MutantPair],
    *,
    methods: tuple[str, ...] = ("structural", "statistical"),
) -> list[MutantPair]
```

Drop-in replacement for the existing equivalence filter in mutation testing. Uses the layered approach: structural first (fast), then statistical, optionally formal.

---

## N-gram Coverage

!!! quote "Path context finds deeper bugs"
    Single-edge coverage (the default AFL model) tracks individual transitions: A→B. But the same edge reached via different paths can expose different bugs. N-gram coverage tracks sequences of N edges as a single hash: at ngram=2, the path X→A→B is different from Y→A→B. This captures deeper patterns in control flow without the full overhead of path-sensitive analysis.

The Explorer's `CoverageCollector` supports configurable N-gram depth:

```python
from ordeal.explore import CoverageCollector

# Default: single-edge (backward compatible)
collector = CoverageCollector(["myapp"], ngram=1)

# 2-gram: captures one level of path context
collector = CoverageCollector(["myapp"], ngram=2)
```

Configure via `ordeal.toml`:

```toml
[explorer]
ngram = 2  # path-context depth (default: 1)
```

| N-gram | What it captures | Overhead | Best for |
|---|---|---|---|
| 1 | Single edge transitions | Lowest | Quick exploration |
| 2 | Edge + one predecessor | Low | Most codebases (recommended) |
| 3+ | Deeper path context | Medium | Complex state machines |
