# ordeal

[![CI](https://github.com/teilomillet/ordeal/actions/workflows/ci.yml/badge.svg)](https://github.com/teilomillet/ordeal/actions/workflows/ci.yml)
[![Docs](https://github.com/teilomillet/ordeal/actions/workflows/docs.yml/badge.svg)](https://docs.byordeal.com/)
[![PyPI](https://img.shields.io/pypi/v/ordeal)](https://pypi.org/project/ordeal/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/ordeal)](https://pypi.org/project/ordeal/)
[![License](https://img.shields.io/github/license/teilomillet/ordeal)](LICENSE)

**Your tests pass. Your code still breaks.**

Traditional tests verify what you thought of. Ordeal explores what you didn't — thousands of operation sequences, faults injected at every level, property assertions that hold across all of them. When ordeal passes, the code works. Not just on the happy path. Under adversity.

```
pip install ordeal
```

## 30-second example

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical

class MyServiceChaos(ChaosTest):
    faults = [
        timing.timeout("myapp.api.call"),         # API times out
        numerical.nan_injection("myapp.predict"),  # model returns NaN
    ]

    @rule()
    def call_service(self):
        result = self.service.process("input")
        always(result is not None, "process never returns None")

    @invariant()
    def no_corruption(self):
        for item in self.service.results:
            always(not math.isnan(item), "no NaN in output")

TestMyServiceChaos = MyServiceChaos.TestCase
```

```bash
pytest --chaos                    # explore fault interleavings
pytest --chaos --chaos-seed 42    # reproduce exactly
ordeal explore                    # coverage-guided, reads ordeal.toml
```

You declare what can go wrong (faults), what your system does (rules), and what must stay true (assertions). Ordeal explores the combinations.

## Why ordeal

Testing catches bugs you can imagine. The dangerous bugs are the ones you can't — a timeout *during* a retry, a NaN *inside* a recovery path, a permission error *after* the cache warmed up. These come from combinations, and the space of combinations is too large to explore by hand.

Ordeal automates this. It brings ideas from the most rigorous engineering cultures in the world to Python:

| What | Idea | From |
|---|---|---|
| Stateful chaos testing with nemesis | An adversary toggles faults while Hypothesis explores interleavings | [Jepsen](https://jepsen.io) + [Hypothesis](https://hypothesis.works) |
| Coverage-guided exploration | Save checkpoints at new code paths, branch from productive states | [Antithesis](https://antithesis.com) |
| Property assertions | `always`, `sometimes`, `reachable`, `unreachable` — accumulate evidence across runs | [Antithesis](https://antithesis.com/docs/properties_assertions/) |
| Inline fault injection | `buggify()` — no-op in production, probabilistic fault in testing | [FoundationDB](https://apple.github.io/foundationdb/testing.html) |
| Boundary-biased generation | Test at 0, -1, empty, max-length — where bugs actually cluster | [Jane Street QuickCheck](https://blog.janestreet.com/quickcheck-for-core/) |
| Mutation testing | Flip `+` to `-`, `<` to `<=` — verify your tests actually catch real bugs | [Meta ACH](https://engineering.fb.com) |
| Differential testing | Compare two implementations on random inputs — catches regressions | Equivalence testing |
| Property mining | Discover invariants from execution traces — type, bounds, monotonicity | Specification mining |

**Read the full [philosophy](https://docs.byordeal.com/philosophy) to understand why this matters.**

## The ordeal standard

When a project uses ordeal and passes, it means something:

- An explorer ran thousands of operation sequences with coverage guidance
- Faults were injected in combinations no human would write
- Property assertions held across all runs
- Mutations were caught

That's not "the tests pass." That's a certification that the code handles adversity. Ordeal-tested code is code you can trust.

## Install

```bash
# From PyPI
pip install ordeal

# With extras
pip install ordeal[atheris]    # coverage-guided fuzzing via Atheris
pip install ordeal[api]        # API chaos testing via Schemathesis
pip install ordeal[all]        # everything

# As a CLI tool
uv tool install ordeal         # global install
uvx ordeal explore             # ephemeral, no install

# Development
git clone https://github.com/teilomillet/ordeal
cd ordeal && uv sync
uv run pytest                  # 205 tests
```

## What's in the box

### Stateful chaos testing

`ChaosTest` extends Hypothesis's `RuleBasedStateMachine`. You declare faults and rules — ordeal auto-injects a **nemesis** that toggles faults during exploration. The nemesis is just another Hypothesis rule, so the engine explores fault schedules like it explores any other state transition. Shrinking works automatically.

```python
from ordeal import ChaosTest, rule, invariant
from ordeal.faults import io, numerical, timing

class StorageChaos(ChaosTest):
    faults = [
        io.error_on_call("myapp.storage.save", IOError),
        timing.intermittent_crash("myapp.worker.process", every_n=3),
        numerical.nan_injection("myapp.scoring.predict"),
    ]
    swarm = True  # random fault subsets per run — better aggregate coverage

    @rule()
    def write_data(self):
        self.service.save({"key": "value"})

    @rule()
    def read_data(self):
        result = self.service.load("key")
        always(result is not None, "reads never return None after write")
```

Swarm mode: each run activates a random subset of faults. Over many runs, this covers more fault combinations than always-all-on. Hypothesis handles the subset selection, so shrinking isolates the exact fault combination that triggers a failure.

### Property assertions

Four types, each with different semantics:

```python
from ordeal import always, sometimes, reachable, unreachable

always(len(results) > 0, "never empty")          # must hold every time — fails immediately
sometimes(cache_hit, "cache is used")             # must hold at least once — checked at session end
reachable("error-recovery-path")                  # code path must execute at least once
unreachable("silent-data-corruption")             # code path must never execute — fails immediately
```

`always` and `unreachable` fail instantly, triggering Hypothesis shrinking. `sometimes` and `reachable` accumulate evidence across the full session — they're checked when testing ends. This is the [Antithesis assertion model](https://antithesis.com/docs/properties_assertions/assertions/), brought to Python.

### Inline fault injection (BUGGIFY)

Place `buggify()` gates in your production code. They return `False` normally. During chaos testing, they probabilistically return `True`:

```python
from ordeal.buggify import buggify, buggify_value

def process(data):
    if buggify():                                    # sometimes inject delay
        time.sleep(random.random() * 5)
    result = compute(data)
    return buggify_value(result, float('nan'))        # sometimes corrupt output
```

Seed-controlled. Thread-local. Zero-cost in production. This is [FoundationDB's BUGGIFY](https://apple.github.io/foundationdb/testing.html) for Python — the code under test *is* the test harness.

### Coverage-guided exploration

The Explorer tracks which code paths each run discovers (AFL-style edge hashing). When a run finds new coverage, it saves a checkpoint. Future runs branch from high-value checkpoints, systematically exploring the state space:

```python
from ordeal.explore import Explorer

explorer = Explorer(
    MyServiceChaos,
    target_modules=["myapp"],
    checkpoint_strategy="energy",  # favor productive checkpoints
)
result = explorer.run(max_time=60)
print(result.summary())
# Exploration: 5000 runs, 52000 steps, 60.0s
# Coverage: 287 edges, 43 checkpoints
# Failures found: 2
#   Run 342, step 15: ValueError (3 steps after shrinking)
```

Failures are **shrunk** — delta debugging removes unnecessary steps, then fault simplification removes unnecessary faults. You get the minimal sequence that reproduces the bug.

Scale with `workers` — each process gets a unique seed for independent exploration, results are aggregated:

```python
explorer = Explorer(MyServiceChaos, target_modules=["myapp"], workers=8)
```

### Configuration

```toml
# ordeal.toml — one file, human and machine readable
[explorer]
target_modules = ["myapp"]
max_time = 60
seed = 42
checkpoint_strategy = "energy"

[[tests]]
class = "tests.test_chaos:MyServiceChaos"

[report]
format = "both"
traces = true
verbose = true
```

See [`ordeal.toml.example`](ordeal.toml.example) for the full schema with every option documented.

### QuickCheck with boundary bias

`@quickcheck` infers strategies from type hints. It biases toward boundary values — 0, -1, empty list, max length — where implementation bugs cluster:

```python
from ordeal.quickcheck import quickcheck

@quickcheck
def test_sort_idempotent(xs: list[int]):
    assert sorted(sorted(xs)) == sorted(xs)

@quickcheck
def test_score_bounded(x: float, y: float):
    result = score(x, y)
    assert 0 <= result <= 1
```

### Composable invariants

```python
from ordeal.invariants import no_nan, no_inf, bounded, finite

valid_score = finite & bounded(0, 1)
valid_score(model_output)  # raises AssertionError with clear message
```

Invariants compose with `&`. Works with scalars, sequences, and numpy arrays.

### Simulation primitives

Deterministic Clock and FileSystem — no mocks, no real I/O, instant:

```python
from ordeal.simulate import Clock, FileSystem

clock = Clock()
fs = FileSystem()

clock.advance(3600)                      # instant — no real waiting
fs.inject_fault("/data.json", "corrupt") # reads return random bytes
```

### Differential testing

Compare two implementations on the same random inputs — catches regressions and validates refactors:

```python
from ordeal.diff import diff

result = diff(score_v1, score_v2, rtol=1e-6)
assert result.equivalent, result.summary()
# diff(score_v1, score_v2): 100 examples, EQUIVALENT
```

### Mutation testing

Validates that your chaos tests actually catch bugs. If you flip `+` to `-` in the code and your tests still pass, your tests have a blind spot:

```python
from ordeal.mutations import mutate_function_and_test

result = mutate_function_and_test("myapp.scoring.compute", my_tests)
print(result.summary())
# Mutation score: 15/18 (83%)
#   SURVIVED  L42:8 + -> -
#   SURVIVED  L67:4 negate if-condition
```

### Fault library

```python
from ordeal.faults import io, numerical, timing, network, concurrency

# I/O faults
io.error_on_call("mod.func")            # raise IOError
io.disk_full()                           # writes fail with ENOSPC
io.corrupt_output("mod.func")           # return random bytes
io.truncate_output("mod.func", 0.5)     # truncate to half

# Numerical faults
numerical.nan_injection("mod.func")      # output becomes NaN
numerical.inf_injection("mod.func")      # output becomes Inf
numerical.wrong_shape("mod.func", (1,512), (1,256))

# Timing faults
timing.timeout("mod.func")              # raise TimeoutError
timing.slow("mod.func", delay=2.0)      # add delay
timing.intermittent_crash("mod.func", every_n=3)
timing.jitter("mod.func", magnitude=0.01)

# Network faults
network.http_error("mod.client.post", status_code=503)
network.connection_reset("mod.client.post")
network.rate_limited("mod.client.get", retry_after=60)
network.dns_failure("mod.client.resolve")

# Concurrency faults
concurrency.contended_call("mod.pool.acquire", contention=0.1)
concurrency.thread_boundary("mod.cache.get")
concurrency.stale_state(service, "config", old_config)
```

### Integrations

```python
# Atheris — coverage-guided fuzzing steers buggify() decisions
from ordeal.integrations.atheris_engine import fuzz
fuzz(my_function, max_time=60)

# Schemathesis — API chaos testing
from ordeal.integrations.schemathesis_ext import chaos_api_test
chaos_api_test("http://localhost:8080/openapi.json", faults=[...])
```

### Audit — justify adoption with data

```bash
ordeal audit myapp.scoring --test-dir tests/
```

```
myapp.scoring
  current:   33 tests |   343 lines | 98% coverage [verified]
  migrated:  12 tests |   130 lines | 96% coverage [verified]
  saving:   64% fewer tests | 62% less code | same coverage
  mined:    compute: output in [0, 1] (500/500, >=99% CI)
  mutation: 14/18 (78%)
  suggest:
    - L42 in compute(): test when x < 0
    - L67 in normalize(): test that ValueError is raised
```

Every number is either `[verified]` (measured via coverage.py JSON, cross-checked) or `FAILED: reason` — the audit never silently returns 0%. Mined properties include Wilson confidence intervals. When there are coverage gaps, it reads the source and tells you exactly what to test. Use `--show-generated` to inspect the test file, `--save-generated` to keep it.

## CLI

```bash
ordeal audit myapp.scoring              # compare existing tests vs ordeal
ordeal explore                          # run from ordeal.toml
ordeal explore -w 8                     # parallel with 8 workers
ordeal explore -c ci.toml -v            # custom config, verbose
ordeal explore --max-time 300 --seed 99 # override settings
ordeal replay trace.json                # reproduce a failure
ordeal replay --shrink trace.json       # minimize a failure trace
```

## Documentation

- **[Philosophy](https://docs.byordeal.com/philosophy)** — Why ordeal exists and what it means for code quality
- **[Getting Started](https://docs.byordeal.com/getting-started)** — Your first chaos test in 5 minutes
- **[Core Concepts](https://docs.byordeal.com/concepts/chaos-testing)** — How ordeal thinks
- **[Guides](https://docs.byordeal.com/guides/explorer)** — Explorer, simulation, mutations, integrations
- **[API Reference](https://docs.byordeal.com/reference/api)** — Every function, every parameter
- **[Full docs](https://docs.byordeal.com/)**

## Architecture

```
ordeal/
├── chaos.py           ChaosTest + nemesis + swarm            Hypothesis + Jepsen
├── explore.py         Coverage-guided explorer               Antithesis
├── assertions.py      always/sometimes/reachable             Antithesis
├── buggify.py         Inline fault injection                 FoundationDB
├── quickcheck.py      @quickcheck + boundary bias            Jane Street
├── simulate.py        Clock, FileSystem                      Deterministic sim
├── invariants.py      Composable: no_nan & bounded(0,1)
├── mutations.py       AST mutation testing                   Meta ACH
├── trace.py           Trace recording + shrinking
├── config.py          TOML configuration
├── cli.py             ordeal explore / replay
├── plugin.py          pytest --chaos
├── strategies.py      Adversarial data generation
├── faults/            io, numerical, timing, network, concurrency
└── integrations/      atheris, schemathesis
```

## License

Apache 2.0
