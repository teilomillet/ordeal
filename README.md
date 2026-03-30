# ordeal

Automated chaos testing for Python. Fault injection, property assertions, coverage-guided exploration, and stateful testing — in one library.

ordeal snaps together ideas from [Antithesis](https://antithesis.com) (deterministic exploration + checkpointing), [FoundationDB](https://apple.github.io/foundationdb/testing.html) (BUGGIFY inline faults), [Jepsen](https://jepsen.io) (nemesis interleaving), [Hypothesis](https://hypothesis.works) (stateful property testing), [Jane Street's QuickCheck](https://blog.janestreet.com/quickcheck-for-core/) (boundary-biased generation), and [Meta's ACH](https://engineering.fb.com) (mutation validation) into a single Python toolkit.

```
pip install ordeal
```

## Quick start

### 1. Write a chaos test

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical

class MyServiceChaos(ChaosTest):
    faults = [
        timing.timeout("myapp.api.call"),
        numerical.nan_injection("myapp.model.predict"),
    ]

    def __init__(self):
        super().__init__()
        self.service = MyService()

    @rule()
    def call_service(self):
        result = self.service.process("input")
        always(result is not None, "process never returns None")

    @invariant()
    def no_corruption(self):
        for item in self.service.results:
            always(not math.isnan(item), "no NaN in output")

# Hypothesis explores rule sequences + fault schedules
TestMyServiceChaos = MyServiceChaos.TestCase
```

### 2. Run with pytest

```bash
pytest --chaos                    # enable chaos mode
pytest --chaos --chaos-seed 42    # reproducible
```

### 3. Or explore with coverage guidance

```bash
ordeal explore                    # reads ordeal.toml
ordeal explore -v --max-time 300  # live progress, 5 minutes
ordeal replay .ordeal/traces/fail-run-42.json  # reproduce a failure
```

## Install

```bash
# From PyPI
pip install ordeal

# With extras
pip install ordeal[atheris]    # coverage-guided fuzzing
pip install ordeal[api]        # API chaos testing via Schemathesis
pip install ordeal[all]        # everything

# As a CLI tool (no venv needed)
uv tool install ordeal         # global install, `ordeal` on PATH
uvx ordeal explore             # ephemeral, no install

# Development
git clone https://github.com/teilomillet/ordeal
cd ordeal
uv sync
uv run pytest                  # 205 tests
uv run ordeal explore          # run the explorer
```

## What's in the box

### Stateful chaos testing

`ChaosTest` extends Hypothesis's `RuleBasedStateMachine`. You declare faults and rules — ordeal auto-injects a **nemesis rule** that toggles faults during exploration. Hypothesis explores which faults fire, when, in what order, interleaved with your application rules.

```python
from ordeal import ChaosTest, rule, invariant
from ordeal.faults import io, numerical, timing

class StorageChaos(ChaosTest):
    faults = [
        io.error_on_call("myapp.storage.save", IOError),
        timing.intermittent_crash("myapp.worker.process", every_n=3),
        numerical.nan_injection("myapp.scoring.predict"),
    ]
    swarm = True  # random fault subsets per run — better coverage

    @rule()
    def write_data(self):
        self.service.save({"key": "value"})

    @rule()
    def read_data(self):
        result = self.service.load("key")
        always(result is not None, "reads never return None after write")
```

### Property assertions (Antithesis model)

Four assertion types, inspired by [Antithesis](https://antithesis.com/docs/properties_assertions/assertions/):

```python
from ordeal import always, sometimes, reachable, unreachable

always(len(results) > 0, "never empty")        # must hold every time
sometimes(cache_hit, "cache is exercised")      # must hold at least once
reachable("error-recovery-path")                # code path must execute
unreachable("silent-data-corruption")           # code path must never execute
```

`always` and `unreachable` raise immediately (triggering Hypothesis shrinking). `sometimes` and `reachable` are checked at the end of the session via the property report.

### Inline fault injection (FoundationDB BUGGIFY)

Place `buggify()` calls in your production code. They're no-ops normally, and probabilistically return `True` during chaos testing:

```python
from ordeal.buggify import buggify, buggify_value

def process(data):
    if buggify():
        time.sleep(random.random() * 5)       # sometimes slow
    result = compute(data)
    return buggify_value(result, float('nan')) # sometimes corrupt
```

Seed-controlled, thread-local, zero-cost in production.

### Coverage-guided exploration

The Explorer is ordeal's answer to Antithesis's exploration engine. It uses AFL-style edge coverage to checkpoint interesting states, then branches from them:

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
#   Run 342, step 15: ValueError: NaN in output (3 steps)
```

When a failure is found, the explorer **shrinks** it to the minimal reproducing sequence — delta debugging + single-step elimination + fault simplification.

### TOML configuration

```toml
# ordeal.toml
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

One file, versionable, shareable between humans and AI agents. See [`ordeal.toml.example`](ordeal.toml.example) for the full schema.

### QuickCheck with boundary bias

`@quickcheck` infers strategies from type hints and biases toward boundary values (0, -1, empty list, max length) where bugs cluster:

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

Works with dataclasses, `Optional`, `Union`, nested types.

### Composable invariants

```python
from ordeal.invariants import no_nan, no_inf, bounded, finite

valid_score = finite & bounded(0, 1)
valid_score(model_output)  # raises AssertionError with clear message
```

### Simulation primitives (no-mock testing)

```python
from ordeal.simulate import Clock, FileSystem

clock = Clock()
fs = FileSystem()
service = MyService(clock=clock, fs=fs)

clock.advance(3600)                      # instant — no real waiting
fs.inject_fault("/data.json", "corrupt") # reads return random bytes
```

### Mutation testing

Validates that your chaos tests actually catch bugs. AST-based operators (arithmetic, comparison, negate, return_none):

```python
from ordeal.mutations import mutate_function_and_test

result = mutate_function_and_test("myapp.scoring.compute", my_tests)
print(result.summary())
# Mutation score: 15/18 (83%)
#   SURVIVED  L42:8 + -> -
#   SURVIVED  L67:4 negate if-condition
```

### Fault primitives

```python
from ordeal.faults import io, numerical, timing

io.error_on_call("mod.func")           # raise IOError
io.return_empty("mod.func")            # return None
io.truncate_output("mod.func", 0.5)    # truncate to half length
io.disk_full()                          # writes fail with ENOSPC
numerical.nan_injection("mod.func")     # output becomes NaN
numerical.inf_injection("mod.func")     # output becomes Inf
numerical.wrong_shape("mod.func", (1,512), (1,256))
timing.timeout("mod.func", delay=30)    # raise TimeoutError
timing.slow("mod.func", delay=2.0)      # add real delay
timing.intermittent_crash("mod.func", every_n=3)
timing.jitter("mod.func", magnitude=0.01)
```

### Integrations

**Atheris** (coverage-guided fuzzing):
```python
from ordeal.integrations.atheris_engine import fuzz
fuzz(my_function, max_time=60)  # coverage guides buggify decisions
```

**Schemathesis** (API chaos testing):
```python
from ordeal.integrations.schemathesis_ext import chaos_api_test
chaos_api_test("http://localhost:8080/openapi.json", faults=[...])
```

## CLI

```bash
ordeal explore                          # run from ordeal.toml
ordeal explore -c ci.toml -v            # custom config, verbose
ordeal explore --max-time 300 --seed 99 # override settings
ordeal replay trace.json                # reproduce a failure
ordeal replay --shrink trace.json       # minimize a failure trace
```

Install the CLI globally with `uv tool install ordeal` or run ephemerally with `uvx ordeal explore`.

## Architecture

```
ordeal/
├── chaos.py           ChaosTest + nemesis + swarm            Hypothesis + Jepsen
├── explore.py         Coverage-guided explorer               Antithesis
├── assertions.py      always/sometimes/reachable             Antithesis
├── buggify.py         Inline fault injection                 FoundationDB
├── quickcheck.py      @quickcheck + boundary bias            Jane Street
├── simulate.py        Clock, FileSystem                      Jane Street
├── invariants.py      Composable: no_nan & bounded(0,1)
├── mutations.py       AST mutation testing                   Meta ACH
├── trace.py           Trace recording + shrinking
├── config.py          TOML configuration
├── cli.py             ordeal explore / replay
├── plugin.py          pytest --chaos
├── strategies.py      Adversarial data generation
├── faults/            io, numerical, timing
└── integrations/      atheris, schemathesis
```

## Design constraint

If an LLM can generate a working chaos test from reading source code alone — no implicit knowledge, no undocumented conventions — then it's automatically easy for humans too. The LLM constraint forces clarity: explicit fault registration, declarative rules, zero hidden setup.

## License

Apache 2.0
