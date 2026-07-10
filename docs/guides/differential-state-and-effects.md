---
title: Differential State and Side Effects
description: Compare mutations, bound receiver state, and selected external effects safely.
---

# Differential state and side effects

Return values are only one way two implementations can differ. This guide adds
the state that exists around a call without pretending ordeal can infer every
external dependency.

## Mutated arguments are automatic

```python
from ordeal import diff

def old(items: list[int]) -> None:
    items.append(1)

def new(items: list[int]) -> None:
    items.append(2)

result = diff(old, new, items=[])
assert result.witness.differences == ("mutated_arguments",)
```

Both functions return `None`, but the post-call lists differ. Each side receives
its own deep copy, and the original list remains unchanged.

## Bound receiver state is automatic

```python
class Counter:
    def __init__(self) -> None:
        self.value = 0

    def old(self, amount: int) -> None:
        self.value += amount

    def new(self, amount: int) -> None:
        self.value += amount + 1

counter = Counter()
result = diff(counter.old, counter.new, amount=2)

print(result.witness.outcome_a.receiver_state)  # {'value': 2}
print(result.witness.outcome_b.receiver_state)  # {'value': 3}
assert counter.value == 0
```

Ordeal reconstructs a fresh receiver for each invocation and captures both
`__dict__` and slots. The object you passed is not used as shared scratch state.

Class-bound methods are different: their state belongs to the class and cannot
be safely cloned as an instance. Select that state explicitly or compare
instance-bound methods; otherwise the result is `inconclusive`.

## External effects must be selected

For a log, fake database, cache, or message buffer, provide capture and restore
hooks:

```python
from ordeal import SideEffect, diff

events: list[str] = []

def capture_events() -> list[str]:
    return list(events)

def restore_events(snapshot: list[str]) -> None:
    events[:] = snapshot

def old(order_id: int) -> None:
    events.append(f"accepted:{order_id}")

def new(order_id: int) -> None:
    events.append(f"queued:{order_id}")

result = diff(
    old,
    new,
    order_id=7,
    side_effects={
        "events": SideEffect(capture=capture_events, restore=restore_events),
    },
)
```

The lifecycle for each generated input is:

1. capture one baseline;
2. restore a copy before A;
3. run A and capture its final effect;
4. restore the baseline before B;
5. run B and capture its final effect;
6. restore the baseline before returning.

If capture, copying, or restoration fails, ordeal returns `inconclusive`. It
does not run B on A's leftovers.

## Choose the smallest useful effect

Capture contract evidence, not an entire process. Prefer “messages published by
this call” over every global, and a temporary test database table over a live
database. Smaller snapshots are easier to copy, compare, minimize, and replay.

Unselected side effects are outside the result's claim. Say that explicitly
when reporting `no_divergence_observed`.

## What custom return comparison does not hide

`compare=` and `normalize=` affect returned values only. They do not suppress a
different exception, mutation, receiver state, or selected side effect. This
prevents a permissive return comparator from masking a state regression.

For multi-step services where one operation intentionally affects the next,
use [Compare System Refactors](system-differential.md). For witness and replay
details, continue with [Differential Evidence](differential-evidence.md).
