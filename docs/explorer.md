# Explorer

Coverage-guided exploration with checkpointing. Finds bugs that random testing misses.

## The idea

Random testing starts fresh every run. To find a deep bug, it has to re-discover the rare state each time. The Explorer **saves** interesting states and **branches** from them.

```
Run → track edge coverage → new edge? → checkpoint → branch from it later
```

## Quick start

```python
from ordeal.explore import Explorer

explorer = Explorer(MyServiceChaos, target_modules=["myapp"])
result = explorer.run(max_time=60)
print(result.summary())
```

Or via CLI:

```bash
ordeal explore -v          # reads ordeal.toml, live progress
```

```
[4s] runs=17354 edges=31 cps=23 fails=0 (4334 runs/s)
```

## Checkpoint scheduling

Three strategies — set in `ordeal.toml` or constructor:

| Strategy | Behavior | Default |
|---|---|---|
| `energy` | Favor checkpoints that led to discoveries | Yes |
| `uniform` | Pick randomly | |
| `recent` | Favor newest | |

Energy decays when a checkpoint stops producing new edges and grows when it does.

## Shrinking

When a failure is found, the Explorer minimizes it:

1. Delta debugging — remove large chunks
2. Single-step elimination — remove individual steps
3. Fault simplification — remove irrelevant fault toggles

A 50-step trace might shrink to 3.

## Traces

Every run is recorded as JSON. Replay or shrink later:

```bash
ordeal replay .ordeal/traces/fail-run-42.json
ordeal replay --shrink trace.json -o minimal.json
```

```python
from ordeal.trace import Trace, replay, shrink

trace = Trace.load("fail-run-42.json")
error = replay(trace)                    # reproduce
minimal = shrink(trace, MyServiceChaos)  # minimize
```

## Explorer vs Hypothesis

| | Hypothesis | Explorer |
|---|---|---|
| Exploration | Random | Coverage-guided |
| Best for | Everyday testing | Deep interaction bugs |
| Speed | Fast | Slower (tracing overhead) |
| Integration | pytest | CLI / TOML / Python |

Use both: Hypothesis for CI, Explorer for finding what Hypothesis can't.
