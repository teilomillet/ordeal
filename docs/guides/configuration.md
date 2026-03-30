# Configuration

All exploration settings live in `ordeal.toml`. Copy from [`ordeal.toml.example`](https://github.com/teilomillet/ordeal/blob/main/ordeal.toml.example) and edit.

## Schema

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

## Minimal example

```toml
[explorer]
target_modules = ["myapp"]
max_time = 60

[[tests]]
class = "tests.test_chaos:MyServiceChaos"
```

## CI example

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

## Loading from Python

```python
from ordeal.config import load_config

cfg = load_config()               # reads ./ordeal.toml
cfg = load_config("ci.toml")     # custom path
cfg.explorer.max_time             # 60.0
cfg.tests[0].resolve()           # imports the class
```
