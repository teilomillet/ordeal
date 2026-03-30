# Shrinking

How ordeal turns a 100-step failure into a 3-step reproduction you can actually debug.

## Why shrinking matters

The explorer runs thousands of sequences. When it finds a failure, the trace might look like this: 47 rule calls, 5 faults toggling on and off, dozens of irrelevant setup steps. That raw trace tells you *something* is broken, but not *what*.

You need the minimal version: "activate fault X, call rule Y, observe failure." Three steps instead of forty-seven. That is what you hand to someone at 2am.

**Analogy.** You ate 10 things yesterday and now you feel sick. You don't know which food caused it. Shrinking is the process of elimination: try removing items one by one. Skip the soup -- still sick? It wasn't the soup. Skip the salad too -- still sick? Not the salad either. Keep going until you find the minimal set of foods that reproduce the problem. Maybe it was just the oysters.

That is exactly what ordeal does to failing test sequences.

## Three phases of shrinking

Shrinking runs three phases in a loop, each one removing a different kind of noise. The loop repeats until nothing more can be removed (a fixpoint) or the time limit expires (`max_time`, default 30 seconds).

```
while steps got shorter:
    steps = delta_debug(steps)         # Phase 1: remove chunks
    steps = one_by_one(steps)          # Phase 2: remove individuals
    steps = fault_simplify(steps)      # Phase 3: remove fault toggles
```

### Phase 1: Delta debugging

Named after Andreas Zeller's delta debugging algorithm. The idea: remove the largest possible chunks first.

Start by removing the first half of all steps. Does the failure still reproduce? If yes, keep the shorter version -- you just cut the trace in half. If no, try removing the second half. Then try quarters. Then eighths.

```
Original: [A B C D E F G H]  (8 steps)

Try removing [A B C D]:
  [E F G H] -> failure reproduces? YES -> keep [E F G H]

Try removing [E F]:
  [G H] -> failure reproduces? NO

Try removing [G H]:
  [E F] -> failure reproduces? NO

Try removing [E]:
  [F G H] -> failure reproduces? YES -> keep [F G H]

...and so on
```

This is fast. It eliminates large irrelevant sections in O(n log n) replay attempts instead of O(n). Most of the work happens here.

### Phase 2: One-by-one elimination

After the big chunks are gone, try removing each remaining step individually. Walk through the sequence: remove step 0, replay. If the failure reproduces, that step was unnecessary -- drop it permanently. If it doesn't reproduce, that step matters -- keep it and move to step 1.

Some steps are necessary setup (initialize a connection, create an object). Others are noise that happened to be in the trace. This phase separates the two.

### Phase 3: Fault simplification

Fault toggles come in pairs: `+slow_network` activates a fault, `-slow_network` deactivates it. This phase tries removing *all* toggles for each fault. If the failure still reproduces without any `slow_network` toggles, that fault was a bystander -- it happened to be active but didn't contribute to the bug.

This isolates the exact fault combination that matters. If the original trace had 3 faults toggling, you might discover only 1 of them is actually needed.

## How replay works

Before shrinking can remove a step, it needs to check whether the failure still reproduces without it. That is replay.

A `Trace` is a complete record of one exploration run, stored as JSON. It contains a list of `TraceStep` objects, each recording one decision the explorer made:

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"rule"` or `"fault_toggle"` | What type of step this is |
| `name` | string | Rule method name, or `"+fault"` / `"-fault"` for toggles |
| `params` | dict | Parameters drawn for this rule call |
| `active_faults` | list | Which faults were active after this step (populated on `fault_toggle` steps; empty on `rule` steps) |
| `edge_count` | int | Cumulative edge coverage after this step |
| `timestamp_offset` | float | Seconds since the run started |

Replay instantiates a fresh test class, walks through the step list, and re-executes each one. Rule steps call the corresponding method with the recorded parameters. Fault toggle steps activate or deactivate the named fault. Invariants are checked after every step. If any step raises an exception, the failure reproduces.

```python
from ordeal.trace import Trace, replay, shrink

trace = Trace.load("fail-run-42.json")

# Does it reproduce?
error = replay(trace)

# Minimize it
minimal = shrink(trace, MyServiceChaos)
minimal.save("minimal.json")
```

## Traces as JSON

Traces are human-readable JSON files. You can open them in an editor, share them with a teammate, store them in CI artifacts, or parse them with a script.

```json
{
  "run_id": 42,
  "seed": 12345,
  "test_class": "tests.test_service:ServiceChaos",
  "steps": [
    {"kind": "fault_toggle", "name": "+timeout", "params": {},
     "active_faults": ["timeout"], "edge_count": 14, "timestamp_offset": 0.003},
    {"kind": "rule", "name": "create_item", "params": {"n": 5},
     "active_faults": ["timeout"], "edge_count": 18, "timestamp_offset": 0.007},
    {"kind": "rule", "name": "query_items", "params": {},
     "active_faults": ["timeout"], "edge_count": 22, "timestamp_offset": 0.012}
  ],
  "failure": {
    "error_type": "AssertionError",
    "error_message": "invariant 'items_consistent' violated",
    "step": 2
  }
}
```

The CLI commands for working with traces:

```bash
ordeal replay trace.json                     # reproduce a failure
ordeal replay --shrink trace.json            # shrink to minimal
ordeal replay --shrink trace.json -o min.json  # save the result
```

## Shrinking in practice

Here is what shrinking looks like on a real failure.

**Before** (raw explorer output): 47 steps, 3 faults toggling on and off.

```
Step  0: +slow_network
Step  1: create_session
Step  2: add_item(n=3)
Step  3: add_item(n=7)
Step  4: -slow_network
Step  5: query_items
Step  6: +disk_full
Step  7: add_item(n=1)
...
Step 44: +timeout
Step 45: add_item(n=2)
Step 46: query_items          <-- failure here
```

You could stare at 47 steps trying to figure out what went wrong. Or you could shrink.

**After** (shrunk): 3 steps, 1 fault.

```
Step 0: +timeout
Step 1: add_item(n=2)
Step 2: query_items           <-- failure here
```

Now you know exactly what happened. When a timeout fault is active and you add an item then immediately query, the query returns stale data. The slow network and disk full faults were irrelevant. Most of the add/remove/query steps were irrelevant. The specific value `n=2` might matter, or it might not -- but you have a 3-step sequence you can reason about.

## Why ordeal's shrinking is special

Most property-testing frameworks shrink *data*. Hypothesis makes integers smaller. QuickCheck makes lists shorter. That is useful, but it only simplifies the *inputs* to a single function call.

Ordeal shrinks *sequences of operations* combined with *fault schedules*. This is a fundamentally harder problem:

- **Operations depend on each other.** Removing step 3 might invalidate step 7 (which uses an object created in step 3). The shrinker has to check whether the remaining sequence still makes sense by actually replaying it.
- **Faults interact with operations.** A fault active during step 5 might have no effect, or it might be the entire cause of the failure. The only way to know is to try removing it.
- **Order matters.** The same set of operations might fail in one order and pass in another. Shrinking preserves the relative order of surviving steps.

This is what makes ordeal's shrinking useful for real system debugging. The bugs you find in distributed systems, stateful services, and concurrent code are not about bad input values. They are about specific sequences of events happening under specific failure conditions. Shrinking gives you the minimal event sequence.

---

**Next:**
- [Coverage Guidance](coverage-guidance.md) -- how the explorer finds failures in the first place
- [Chaos Testing](chaos-testing.md) -- how test sequences and fault schedules are generated
- [Explorer Guide](../guides/explorer.md) -- using shrinking in practice from the CLI
