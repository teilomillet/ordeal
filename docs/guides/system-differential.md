---
title: Your First System Comparison
description: A complete old-versus-new store example with timeout recovery.
---

# Your first system comparison

Start with the [plain-language differential model](../concepts/differential-testing.md)
if outcome envelopes, statuses, minimization, or replay are new. This page adds
multi-step state and faults to that same model.

This example compares two in-memory store versions through one realistic story.
Put it in `example_diff.py` and run `python example_diff.py`.

```python
from ordeal.diff import FaultEvent, Operation, PerformanceBudget, diff

class OldStore:
    def __init__(self):
        self.data = {}
        self.events = []
        self.timeout = False

    def put(self, key: str, value: int) -> None:
        self.data[key] = value
        self.events.append(("put", key))

    def get(self, key: str) -> int:
        if self.timeout:
            raise TimeoutError("backend timed out")
        return self.data[key]

    def apply_fault(self, event: FaultEvent) -> None:
        self.timeout = event.action == "activate"

class NewStore(OldStore):
    pass

story = [
    Operation("put", args=("order-7", 42)),
    FaultEvent("timeout", "activate"),
    Operation("get", args=("order-7",)),
    FaultEvent("timeout", "deactivate"),
    Operation("get", args=("order-7",)),
]

result = diff(
    OldStore,
    NewStore,
    sequence=story,
    apply_fault=lambda store, event: store.apply_fault(event),
    state=lambda store: dict(store.data),
    side_effects=lambda store: list(store.events),
    performance=PerformanceBudget(
        max_candidate_seconds=0.1,
        samples=5,
        warmup=1,
    ),
)

print(result.summary())
assert result.status == "no_divergence_observed"
assert result.performance is not None
assert result.performance.within_budget
```

## Read the setup from the outside in

`OldStore` and `NewStore` are zero-argument factories: calling either name makes
a fresh isolated system. Never return the same shared object from both.

`story` is a timeline. The first read happens while the timeout is active, so
both versions must raise the same `TimeoutError`. The second read happens after
recovery, so both must return `42`.

The `state` probe selects business state. It deliberately omits the `timeout`
test knob. The `side_effects` probe selects the event log. If no probe is given,
public instance attributes are compared as state and side effects are reported
as `NOT CHECKED`.

## See Ordeal find a recovery regression

Replace `NewStore` with this version:

```python
class NewStore(OldStore):
    def apply_fault(self, event: FaultEvent) -> None:
        if event.action == "activate":
            self.timeout = True       # bug: never clears the timeout
```

Now the final clean read returns `42` in the old version but still raises in the
new version. `result.status` becomes `divergent`. The report includes:

- the public-interface comparison;
- every measured outcome, state, and side effect;
- the minimized shared sequence and its fault-only view;
- whether operations after recovery matched;
- exact replay attempts and matches;
- a separate performance result.

Use `result.sequence` as the compact debugging story. Use
`result.mismatches[0]` to inspect the first differing observation.

## Choose the right entry point

This page uses the Python API for two factories in one process:
`diff(OldStore, NewStore, sequence=story)`. To compare committed Git revisions
in isolated worktrees, use the separate [`ordeal diff` CLI](revision-diff.md).

Continue with [system recipes](system-differential-recipes.md), consult the
[field reference](../reference/system-differential.md), or diagnose a surprising
result with [troubleshooting](system-differential-troubleshooting.md).
