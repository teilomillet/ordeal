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
    The config file now covers the main CLI workflows too. `[explorer]` and `[[tests]]` drive stateful exploration, `[report]` controls output, `[fixtures]`, `[[scan]]`, `[[objects]]`, and `[[contracts]]` tune exploratory module scans, `[audit]` sets test-quality defaults, and `[init]` sets bootstrap defaults for starter tests and gap-closing passes.

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

### `[fixtures]`

Shared fixture registries are imported for their `register_fixture()` side effects before scan commands run. Use this for project-wide registries that should apply to every scan target.

| Key | Type | Default | Description |
|---|---|---|---|
| `registries` | `list[str]` | `[]` | Importable modules that call `register_fixture()` |

```toml
[fixtures]
registries = ["tests.support.shared_fixtures"]
```

### `[[scan]]`

!!! quote "What you can do with this"
    Auto-scan is the fastest way to get value from ordeal, but treat it as exploratory output first. Point it at a module and it smoke-tests every public function automatically -- no test code needed. Functions with type hints get random inputs generated for free. Add `fixtures` for anything the type system can't describe, and use suppression knobs when you already know a relation is noisy rather than a bug.

Declare modules for auto-scan testing. The pytest plugin auto-collects these and runs `scan_module()` on each.
Pack aliases in `auto_contracts` expand to concrete checks, so names like
`shell_path_safety`, `protected_env_vars`, `cleanup_teardown`,
`cancellation_safety`, and `json_tool_call_normalization` are first-class.

| Key | Type | Default | Description |
|---|---|---|---|
| `module` | `str` | required | Dotted module path to scan |
| `max_examples` | `int` | `50` | Hypothesis examples per function |
| `security_focus` | `bool` | `false` | Expand sink inference and trust-boundary probes for security-oriented scans |
| `targets` | `list[str]` | `[]` | Optional callable selectors such as `score`, `Env.*`, or `pkg.mod:Env.build_env_vars` |
| `include_private` | `bool` | `false` | Include single-underscore callables |
| `fixtures` | `dict[str, str]` | `{}` | Strategy specs for untyped parameters, such as comma-separated `sampled_from` values |
| `expected_failures` | `list[str]` | `[]` | Function names whose failure is expected behavior |
| `expected_preconditions` | `list[str]` or `dict[str, list[str]]` | `[]` | Global or per-function precondition patterns that should stay visible but demoted |
| `fixture_registries` | `list[str]` | `[]` | Importable modules that call `register_fixture()` for project-specific strategies |
| `ignore_contracts` | `list[str]` | `[]` | Contract check names to suppress from scan feedback |
| `ignore_properties` | `list[str]` | `[]` | Property names to suppress from mined warnings |
| `ignore_relations` | `list[str]` | `[]` | Relation names to suppress from mined relation checks |
| `expected_properties` | `list[str]` or `dict[str, list[str]]` | `[]` | Global or per-function property names to treat as expected semantics |
| `expected_relations` | `list[str]` or `dict[str, list[str]]` | `[]` | Global or per-function relation names to treat as expected semantics |
| `contract_overrides` | `dict[str, list[str]]` | `{}` | Per-function contract suppressions or overrides |
| `property_overrides` | `dict[str, list[str]]` | `{}` | Per-function property suppressions or overrides |
| `relation_overrides` | `dict[str, list[str]]` | `{}` | Per-function relation suppressions or overrides |

```toml
[[scan]]
module = "myapp.scoring"
max_examples = 100
security_focus = true
targets = ["myapp.scoring:Scorer.score"]
fixture_registries = ["tests.support.fixtures"]
ignore_contracts = ["quoted_paths"]
ignore_properties = ["commutative"]
ignore_relations = ["commutative_composition"]
expected_failures = ["validate_input"]
expected_preconditions = { "*" = ["ValueError"], build_env_vars = ["protected key"] }
expected_properties = { "*" = ["ordered_arguments"] }
expected_relations = { "*" = ["equivalent"] }
contract_overrides = { build_env_vars = ["protected_env_keys"] }

[[scan]]
module = "myapp.pipeline"
fixtures = { model = "sampled_from(['gpt-4', 'claude'])" }
property_overrides = { normalize = ["idempotent"] }
relation_overrides = { normalize = ["equivalent"] }
```

When you run `pytest --chaos`, ordeal auto-discovers these entries and smoke-tests every public function in each module. Functions without type hints are skipped unless fixtures are provided or a registry supplies them. Known preconditions stay separate from candidate issue ranking so the output stays epistemic.

`security_focus = true` is the opt-in trust-boundary review setting. It widens the sink taxonomy to include import/deserialization/filesystem-write/IPC paths, adds deterministic probes for pure path/symlink shapers, and synthesizes small artifact/config mutations for deserialization- and IPC-shaped parameters without creating a second scan command.

`targets` now acts as a first-class selector list, not just an exact-callable allowlist. Exact names still work, and glob patterns let package-root scans focus on a subset of exported callables without rewriting the module target.

The `expected_*` and `ignore_*` knobs are schema-level policy hints for the scan feedback layer. They let teams declare that some preconditions, contracts, properties, or relations are already understood in this module, without changing the underlying scan target or fixture setup.

When `proof_bundles = true` (the default), promoted crash findings also carry a structured witness, contract basis, confidence breakdown, minimal reproduction, failure path, and likely-impact summary in the saved report and JSON bundle.

### `[[objects]]`

Reusable object factories for bound instance methods. `scan` and `audit` use these hooks so methods show up as callable targets instead of disappearing behind constructor setup.

| Key | Type | Default | Description |
|---|---|---|---|
| `target` | `str` | required | Class target such as `pkg.mod:Env` |
| `factory` | `str?` | `null` | Import path to a sync or async factory |
| `state_factory` | `str?` | `null` | Optional state builder for harnesses that need a separate state object |
| `setup` | `str?` | `null` | Optional sync or async hook run after factory creation |
| `teardown` | `str?` | `null` | Optional sync or async cleanup hook run after audit or chaos execution |
| `harness` | `str` | `"fresh"` | `fresh` creates a new instance per call, `stateful` reuses one instance across a machine run |
| `scenarios` | `list[str | inline-table]` | `[]` | Repeatable sync/async hooks or inline collaborator scenario specs applied after setup |
| `methods` | `list[str]` | `[]` | Optional method subset for audit-target expansion |
| `include_private` | `bool` | `false` | Include single-underscore methods when expanded |

```toml
[[objects]]
target = "myapp.envs:ComposableEnv"
factory = "tests.support.factories:make_composable_env"
state_factory = "tests.support.factories:build_composable_state"
setup = "tests.support.factories:prime_composable_env"
teardown = "tests.support.factories:cleanup_composable_env"
harness = "stateful"
scenarios = ["subprocess", "sandbox"]
methods = ["build_env_vars"]
```

Use `factory` for construction, `state_factory` when the object needs a separate state payload, `setup` for one-time preparation, `teardown` for cleanup, and `scenarios` for collaborator behavior that should be layered on top of the object before the listed methods are exercised. `harness = "stateful"` tells ordeal to reuse the same instance across a stateful run instead of rebuilding it for every call. Built-in scenario libraries now work directly in TOML, and `scan --save-artifacts` will write a `.scenarios.md` note when it infers a good pack for the current target:

```toml
scenarios = ["subprocess", "sandbox", "upload_download", "http", "state_store"]
```

Alias names like `subprocess_runner`, `sandbox_client`, `upload_download_client`, and `http_client` still resolve to the same built-in libraries when you need the more explicit spelling.

Inline scenario tables still cover small collaborator tweaks directly in TOML:

```toml
scenarios = [
  { kind = "setattr", path = "sandbox_client.mode", value = "offline" },
  { kind = "stub_return", path = "sandbox_client.execute_command", value = { returncode = 0, stdout = "ok" } },
  { kind = "stub_raise", path = "upload_content", error = "RuntimeError: denied" },
]
```

### `[[contracts]]`

Explicit semantic probes for scan targets. Use these for shell/path/env helpers where plain fuzzing is too weak.
Pack aliases such as `shell_path_safety`, `protected_env_vars`,
`cleanup_teardown`, `cancellation_safety`, and
`json_tool_call_normalization` expand to concrete built-ins.

| Key | Type | Default | Description |
|---|---|---|---|
| `target` | `str` | required | Callable target such as `pkg.mod:Env.build_env_vars` |
| `checks` | `list[str]` | `[]` | Built-ins and pack aliases: `shell_safe`, `quoted_paths`, `command_arg_stability`, `protected_env_keys`, `shell_path_safety`, `protected_env_vars`, `cleanup_teardown`, `cancellation_safety`, `json_tool_call_normalization` |
| `kwargs` | `dict[str, object]` | `{}` | Concrete probe inputs |
| `tracked_params` | `list[str]` | `[]` | String params to track in shell/path checks |
| `protected_keys` | `list[str]` | `[]` | Env keys that must survive updates |
| `env_param` | `str?` | `null` | Which kwarg carries the input env mapping |
| `phase` | `str?` | `null` | Lifecycle phase under test, such as `setup`, `rollout`, `cleanup`, or `teardown` |
| `followup_phases` | `list[str]` | `[]` | Lifecycle phases that must still run after an injected fault |
| `fault` | `str?` | `null` | Injected lifecycle fault name, such as `cancel_rollout` or `raise_setup_hook` |
| `handler_name` | `str?` | `null` | Optional preferred lifecycle handler to target during the probe |

```toml
[[contracts]]
target = "myapp.envs:ComposableEnv.build_env_vars"
checks = ["shell_path_safety", "protected_env_vars", "json_tool_call_normalization"]
kwargs = { path = "tmp/my binary", env_vars = { PATH = "/bin", HOME = "/tmp/home" } }
tracked_params = ["path"]
protected_keys = ["PATH", "HOME"]
env_param = "env_vars"

[[contracts]]
target = "myapp.envs:ComposableEnv.rollout"
checks = ["cancellation_safety"]
kwargs = { marker = "demo" }
phase = "rollout"
followup_phases = ["cleanup", "teardown"]
fault = "cancel_rollout"
```

### `[audit]`

Set defaults for `ordeal audit`. CLI flags still win, but when omitted the command can read modules and policy directly from `ordeal.toml`.

| Key | Type | Default | Description |
|---|---|---|---|
| `modules` | `list[str]` | `[]` | Module paths to audit when the CLI omits them |
| `test_dir` | `str` | `"tests"` | Directory containing existing tests |
| `max_examples` | `int` | `20` | Hypothesis examples per function |
| `workers` | `int` | `1` | Parallel workers for mutation validation |
| `validation_mode` | `str` | `"fast"` | `"fast"` replay or `"deep"` replay + re-mine |
| `min_fixture_completeness` | `float` | `0.0` | Minimum runnable-target ratio before audit reports a blocked target |
| `show_generated` | `bool` | `false` | Print generated ordeal tests during audit |
| `save_generated` | `str?` | `null` | Save generated ordeal tests to this path |
| `write_gaps_dir` | `str?` | `null` | Write draft gap stubs to this directory |
| `include_exploratory_function_gaps` | `bool` | `false` | Surface indirect-only function coverage gaps |
| `require_direct_tests` | `bool` | `false` | Exit 1 when any function is still exploratory or uncovered |

```toml
[audit]
modules = ["myapp.scoring"]
validation_mode = "deep"
write_gaps_dir = "tests/gaps"
require_direct_tests = true
min_fixture_completeness = 0.5
```

Use `[[audit.targets]]` when one class needs an audit-specific factory or a narrower method subset:

`save_generated` and `write_gaps_dir` are workspace-local output paths. Ordeal rejects values that escape the current repo root.

```toml
[[audit.targets]]
target = "myapp.envs:ComposableEnv"
factory = "tests.support.factories:make_composable_env"
state_factory = "tests.support.factories:build_composable_state"
setup = "tests.support.factories:prime_composable_env"
teardown = "tests.support.factories:cleanup_composable_env"
harness = "stateful"
scenarios = ["sandbox"]
methods = ["build_env_vars", "post_sandbox_setup"]
```

### `[init]`

Set defaults for `ordeal init`. This is useful when you want bootstrap behavior to be reproducible in CI or by coding agents.

| Key | Type | Default | Description |
|---|---|---|---|
| `target` | `str?` | `null` | Package to bootstrap when the CLI omits it |
| `output_dir` | `str` | `"tests"` | Directory where generated tests are written |
| `ci` | `bool` | `false` | Generate `.github/workflows/<name>.yml` |
| `ci_name` | `str` | `"ordeal"` | Workflow filename stem |
| `install_skill` | `bool` | `false` | Install the bundled AI-agent skill |
| `close_gaps` | `bool` | `false` | Run audit-guided draft gap generation after bootstrap |
| `gap_output_dir` | `str?` | `null` | Override where `close_gaps` writes draft stubs |
| `mutation_preset` | `str` | `"essential"` | Preset used for the quick mutation pass |
| `scan_max_examples` | `int` | `10` | Example budget for the lightweight read-only scan summary |

```toml
[init]
target = "myapp"
close_gaps = true
gap_output_dir = "tests/gaps"
scan_max_examples = 12
```

`output_dir` and `gap_output_dir` are also workspace-local output paths; values outside the current repo root are rejected.

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
