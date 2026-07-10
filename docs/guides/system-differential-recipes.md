---
title: System Comparison Recipes
description: Patterns for state, side effects, APIs, faults, and performance.
---

# System comparison recipes

Start with two fresh factories and a list of `Operation` or `FaultEvent`
objects. Add only the probes and budgets your contract actually needs.

## Compare only meaningful state

Public attributes are captured automatically. Prefer an explicit probe when
objects contain caches, clients, locks, clocks, or fault-controller state:

```python
result = diff(
    OldCart, NewCart,
    sequence=story,
    state=lambda cart: {"items": dict(cart.items), "total": cart.total},
)
```

The probe runs after every event and must return a deep-copyable value.

## Compare selected side effects

Side effects are never guessed. Expose a stable observation owned by each
system instance:

```python
result = diff(
    OldMailer, NewMailer,
    sequence=[Operation("send", args=("a@example.com",))],
    side_effects=lambda mailer: list(mailer.outbox),
)
```

For databases, queues, or HTTP calls, use a test adapter or recorder so each
factory sees its own isolated evidence.

## Use keyword arguments

```python
Operation("create_order", kwargs={"sku": "A7", "quantity": 2})
```

Arguments are deep-copied independently before each version receives them.

## Express a fault and its recovery

```python
story = [
    FaultEvent("corrupt_response", "activate", {"field": "price"}),
    Operation("refresh"),
    FaultEvent("corrupt_response", "deactivate"),
    Operation("refresh"),
]

result = diff(
    OldClient, NewClient,
    sequence=story,
    apply_fault=lambda client, event: client.faults.apply(event),
)
```

Actions named `deactivate`, `recover`, `restart`, or `clear` begin the recovery
phase. Later operations determine `result.recovery_parity`.

## Wrap an HTTP or process service

```python
class ShopAdapter:
    def __init__(self, client):
        self.client = client

    def create(self, sku: str):
        return self.client.post("/orders", json={"sku": sku}).json()

result = diff(
    lambda: ShopAdapter(old_client()),
    lambda: ShopAdapter(new_client()),
    sequence=[Operation("create", args=("A7",))],
)
```

Factories must isolate ports, databases, files, and queues. Ordeal controls the
shared story, not infrastructure that both adapters accidentally share.

## Add an absolute or relative speed limit

```python
budget = PerformanceBudget(
    max_slowdown=1.20,
    max_candidate_seconds=0.050,
    samples=9,
    warmup=2,
)
result = diff(OldSearch, NewSearch, sequence=story, performance=budget)
```

Performance measures the original story, with setup and observation probes
outside the timed region. Check `result.status` and
`result.performance.within_budget` separately.

## Make a CI gate

```python
assert result.status == "no_divergence_observed", result.summary()
assert result.performance is None or result.performance.within_budget
```

This is a bounded regression gate, not a proof of universal equivalence. Add
stories for materially different workflows and fault plans.
