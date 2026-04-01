---
description: >-
  Configure ordeal with ordeal.toml: explorer settings, test classes,
  report format, scan targets, mutation presets. Full schema reference
  with examples.
---

# Configuration

!!! quote "In plain English"
    `ordeal.toml` is your single source of truth. Everything your team needs to reproduce a test run -- seed, time budget, faults, report format -- lives in one file. Check it into your repo and everyone gets the same behavior. No guessing, no "it works on my machine."

All exploration settings live in `ordeal.toml`. Copy from [`ordeal.toml.example`](https://github.com/teilomillet/ordeal/blob/main/ordeal.toml.example) and edit.

## Why TOML

ordeal uses a single `ordeal.toml` file because configuration should be data, not code.

TOML is human-readable, machine-parseable, and version-controllable. You can review changes in a diff, generate the file from a script, or have an AI agent produce it after scanning your codebase. There is no Python import machinery involved, no subclassing, no registration -- just a flat file that describes what to explore, how long to run, and where to report.

One file, checked into your repo, that anyone (or anything) can read and modify.

## Schema

!!! quote "Think of it this way"
    The config file has four sections. `[explorer]` controls how the engine explores (how long, how deep, how many workers). `[[tests]]` lists which ChaosTest classes to run. `[report]` decides what output you get. `[[scan]]` lets you auto-test modules without writing any test code at all.

### `[explorer]`

| Key | Type | Default | Description |
|---|---|---|---|
| `target_modules` | `list[str]` | `[]` | Modules to track for edge coverage |
| `max_time` | `float` | `60` | Wall-clock time limit (seconds) |
| `max_runs` | `int?` | `null` | Run count limit (null = time-only) |
| `seed` | `int` | `42` | RNG seed |
| `max_checkpoints` | `int` | `256` | Checkpoint corpus size |
| `checkpoint_prob` | `float` | `0.4` | Probability of starting from checkpoint |
| `checkpoint_strategy` | `str` | `"energy"` | `"energy"`, `"uniform"`, or `"recent"` |
| `steps_per_run` | `int` | `50` | Max rule steps per run |
| `fault_toggle_prob` | `float` | `0.3` | Nemesis action probability per step |
| `workers` | `int` | `1` | Parallel workers (0 = auto: `os.cpu_count()`) |
| `ngram` | `int` | `2` | N-gram depth for edge hashing (1=AFL classic, 2+=path context) |
| `rule_swarm` | `bool` | `false` | Unified swarm: random rule+fault subsets per run |

### `[[tests]]`

| Key | Type | Required | Description |
|---|---|---|---|
| `class` | `str` | Yes | `"module.path:ClassName"` |
| `steps_per_run` | `int?` | No | Override per test |
| `swarm` | `bool?` | No | Override swarm mode |

### `[report]`

| Key | Type | Default | Description |
|---|---|---|---|
| `format` | `str` | `"text"` | `"text"`, `"json"`, or `"both"` |
| `output` | `str` | `"ordeal-report.json"` | JSON report path |
| `traces` | `bool` | `false` | Save full traces for replay |
| `traces_dir` | `str` | `".ordeal/traces"` | Trace output directory |
| `verbose` | `bool` | `false` | Live progress to stderr |
| `corpus_dir` | `str` | `".ordeal/seeds"` | Persistent seed corpus directory |

### `[[scan]]`

!!! quote "What you can do with this"
    Auto-scan is the fastest way to get value from ordeal. Point it at a module and it smoke-tests every public function automatically -- no test code needed. Functions with type hints get random inputs generated for free. Add `fixtures` for anything the type system can't describe.

Declare modules for auto-scan testing. The pytest plugin auto-collects these and runs `scan_module()` on each.

| Key | Type | Default | Description |
|---|---|---|---|
| `module` | `str` | required | Dotted module path to scan |
| `max_examples` | `int` | `50` | Hypothesis examples per function |
| `fixtures` | `dict` | `{}` | Strategy overrides for untyped parameters |

```toml
[[scan]]
module = "myapp.scoring"
max_examples = 100

[[scan]]
module = "myapp.pipeline"
fixtures = { model = "sampled_from(['gpt-4', 'claude'])" }
```

When you run `pytest --chaos`, ordeal auto-discovers these entries and smoke-tests every public function in each module. Functions without type hints are skipped unless fixtures are provided.

## Tuning guide

!!! quote "Why this matters"
    Tuning is how you tell the explorer where to spend its time. The defaults work out of the box, but understanding these knobs lets you trade speed for depth, breadth for focus, and quick feedback for thorough validation. Start with defaults, then adjust based on what the report tells you.

The defaults are reasonable for a first run. Once you have something working, the parameters below are the ones worth adjusting.

### `target_modules`

Controls which Python modules the explorer instruments for edge coverage. The explorer uses AFL-style edge hashing via `sys.settrace` -- it only tracks control-flow transitions in the modules you list here.

Start with your main application module (e.g., `["myapp"]`). Add more as you want broader coverage. Submodules are included automatically: `"myapp"` covers `myapp.api`, `myapp.db`, and so on.

Too many modules means tracing overhead slows down each run, reducing the number of runs per second. Too few modules means the explorer is blind to coverage in code you care about, so it cannot checkpoint effectively. If you are unsure, start narrow and widen after looking at the edge count in the report.

### `max_time`

Wall-clock time limit for the entire exploration run. The explorer loops until this limit is reached (or `max_runs` is hit, if set).

- **60s** -- good for local development and quick feedback.
- **300s** -- good for CI. Catches most shallow and medium-depth bugs.
- **3600s+** -- pre-release or nightly runs. Longer runs find deeper bugs because the explorer has more time to branch from rare checkpoints.

The relationship is roughly logarithmic: doubling the time does not double the bugs found, but it does explore states that shorter runs never reach. Start short, increase as your confidence requirements grow.

### `checkpoint_prob`

The probability that a new exploration run starts from a saved checkpoint rather than from a fresh machine state. This controls the balance between depth and diversity.

- **0.4 (default)** -- a good balance. 40% of runs branch from interesting prior states, 60% start fresh.
- **0.6 - 0.8** -- deep-state exploration. Use this for systems with deep state machines where bugs hide behind many prerequisite steps.
- **0.1 - 0.2** -- high diversity. Use this early on, or for systems where bugs tend to appear in the first few steps regardless of prior state.

If you see the edge count plateauing quickly, try increasing this value to let the explorer dig deeper from known interesting states.

### `checkpoint_strategy`

How the explorer picks which checkpoint to branch from when it does use a checkpoint.

- **`"energy"` (default)** -- checkpoints that led to new coverage discoveries get higher energy and are selected more often. Energy decays over time (decay factor 0.95, minimum 0.01), so stale checkpoints gradually lose priority. This works well for most systems.
- **`"uniform"`** -- pick a checkpoint at random with equal probability. Try this if energy scheduling seems stuck on a small cluster of checkpoints.
- **`"recent"`** -- favor recently created checkpoints with linearly increasing weights. Good for systems where newer states matter more than older ones, such as systems with monotonically growing state.

### `steps_per_run`

The maximum number of rule steps (including fault toggles) in a single exploration run. Each run picks a random number of steps between 1 and this value.

- **50 (default)** -- good for most services and typical ChaosTest classes.
- **100 - 200** -- for systems with deep state machines where the interesting behavior requires many sequential operations (e.g., a database that needs a series of writes before a compaction triggers).
- **20 - 30** -- for fast iteration. Shorter runs complete faster, so the explorer gets more runs per second and more chances to try different checkpoint branches.

Higher values mean each individual run takes longer. Lower values mean more runs, but each run explores less deeply. If your ChaosTest rules are expensive (e.g., they call external services), lean toward fewer steps.

### `fault_toggle_prob`

The probability that any given step is a fault toggle (nemesis action) rather than a regular rule execution. When a fault toggle fires, the explorer randomly activates or deactivates one of the registered faults.

- **0.3 (default)** -- roughly 30% of steps are fault toggles. This gives a good mix of normal operation and fault injection.
- **0.5 - 0.7** -- highly chaotic. Faults flip on and off frequently, testing rapid recovery and cascading failures.
- **0.05 - 0.15** -- long fault-free windows with occasional disruptions. Better for testing sustained degraded operation rather than rapid fault cycling.

### `seed`

The RNG seed for the entire exploration. Same seed + same code = same exploration path.

Set this explicitly in CI for reproducibility. When a failure is found, the seed (along with the trace) lets you replay the exact same sequence. Use different seeds across parallel CI jobs to explore different paths.

### `max_checkpoints`

The maximum number of checkpoints kept in the corpus. When the limit is reached, the lowest-energy checkpoint is evicted (under the `"energy"` strategy) or a random one is removed.

256 is generous for most use cases. Increase it if you have long runs where many distinct interesting states accumulate. Decrease it if your machine state is large (each checkpoint stores a snapshot of the user state dict).

### `workers`

Number of parallel worker processes. Default 1 (sequential). Each worker gets a unique seed (`base + i*7919`) and explores independently.

Set to the number of available CPU cores for maximum throughput. In CI, match your runner's core count. Locally, leave headroom for other work (e.g., `workers = 6` on an 8-core machine).

Workers share discoveries via a shared-memory edge bitmap and a shared checkpoint pool. Edges are deduplicated across workers in real time; checkpoints from significant discoveries are published for other workers to branch from. Use `ordeal.scaling.benchmark()` to measure actual efficiency for your test.

```toml
[explorer]
workers = 4    # 4 parallel processes
```

## Examples

!!! quote "How to explore this"
    These examples show real configurations for different stages of development. Copy the one closest to your situation and adjust from there. The pattern is always the same: short runs for fast feedback while coding, longer runs in CI for confidence, and thorough runs before a release.

### Local development (quick iteration)

Fast feedback during development. Short runs, text output, no traces.

```toml
[explorer]
target_modules = ["myapp"]
max_time = 30
steps_per_run = 30

[[tests]]
class = "tests.test_chaos:MyServiceChaos"
```

### CI

Longer runs, fixed seed for reproducibility, JSON report for tooling.

```toml
[explorer]
target_modules = ["myapp"]
max_time = 300
seed = 0

[[tests]]
class = "tests.chaos.test_api:APIChaos"

[[tests]]
class = "tests.chaos.test_scoring:ScoringChaos"

[report]
format = "json"
output = "ordeal-report.json"
```

### Pre-release validation

Thorough exploration. Long time budget, deep state exploration, full traces saved for replay.

```toml
[explorer]
target_modules = ["myapp", "myapp.db", "myapp.cache"]
max_time = 3600
seed = 0
checkpoint_prob = 0.7
steps_per_run = 150
max_checkpoints = 512

[[tests]]
class = "tests.chaos.test_api:APIChaos"
steps_per_run = 200

[[tests]]
class = "tests.chaos.test_scoring:ScoringChaos"

[[tests]]
class = "tests.chaos.test_persistence:PersistenceChaos"
steps_per_run = 200

[report]
format = "both"
output = "ordeal-report.json"
traces = true
traces_dir = ".ordeal/traces"
verbose = true
```

### Multi-service with per-test overrides

Multiple ChaosTest classes with different tuning per test. The API test gets more steps because it has a deeper state machine. The cache test uses swarm mode to randomize which faults are active.

```toml
[explorer]
target_modules = ["ordering", "inventory", "payments"]
max_time = 600
seed = 7

[[tests]]
class = "tests.chaos.test_ordering:OrderingChaos"
steps_per_run = 100

[[tests]]
class = "tests.chaos.test_inventory:InventoryChaos"
steps_per_run = 50
swarm = true

[[tests]]
class = "tests.chaos.test_payments:PaymentsChaos"
steps_per_run = 80

[report]
format = "both"
output = "ordeal-report.json"
verbose = true
```

## Loading from Python

```python
from ordeal.config import load_config

cfg = load_config()               # reads ./ordeal.toml
cfg = load_config("ci.toml")     # custom path
cfg.explorer.max_time             # 60.0
cfg.tests[0].resolve()           # imports the class
```

The `load_config` function validates the TOML against the schema and raises `ConfigError` with a clear message if any key is unknown or any value is out of range.

## For AI agents

!!! quote "The key insight"
    The config format is intentionally boring -- flat TOML, no inheritance, no conditionals. That means any tool, script, or AI agent can read it, write it, or generate it. You never need Python to produce a valid config.

`ordeal.toml` is designed to be generated programmatically. The format is intentionally flat and predictable -- no inheritance, no imports, no conditional logic.

A typical workflow for an AI agent:

1. Scan the codebase for `ChaosTest` subclasses.
2. Identify their module paths and class names.
3. Determine which application modules should be traced for coverage.
4. Generate an `ordeal.toml` with reasonable defaults.

Here is what a generated config might look like after scanning a project:

```toml
# Auto-generated by agent. Target: myapp (3 ChaosTest classes found).

[explorer]
target_modules = ["myapp"]
max_time = 300
seed = 0

[[tests]]
class = "tests.chaos.test_api:APIChaos"

[[tests]]
class = "tests.chaos.test_db:DatabaseChaos"

[[tests]]
class = "tests.chaos.test_cache:CacheChaos"

[report]
format = "json"
output = "ordeal-report.json"
```

The class path format is always `"module.path:ClassName"` -- the same format used by Python entry points. An agent can derive this from any ChaosTest subclass it finds in the test tree.

No Python code is needed to produce or consume this file. A shell script, a CI step, or an LLM can generate it from a template.
