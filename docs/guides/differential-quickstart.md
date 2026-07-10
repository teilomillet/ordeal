---
title: Differential Testing Quickstart
description: Compare two Python functions and understand the result in ten minutes.
---

# Differential testing quickstart

Use this when you rewrote, optimized, or ported a function and want ordeal to
search for behavior changes.

## 1. Start with two typed functions

```python
from ordeal import diff

def price_old(quantity: int) -> int:
    return quantity * 10

def price_new(quantity: int) -> int:
    if quantity >= 10:
        return quantity * 9
    return quantity * 10

result = diff(price_old, price_new, max_examples=100)
print(result.summary())
```

The type hint tells Hypothesis how to generate `quantity`. Ordeal runs both
functions on independent copies of every generated input.

## 2. Read the status first

```python
match result.status:
    case "divergent":
        print("A reproducible behavior change was found")
    case "no_divergence_observed":
        print("The sampled cases matched; this is not proof")
    case "proven_equivalent":
        print("The supplied full-domain proof succeeded")
    case "inconclusive":
        print("The comparison could not make a sound claim:", result.reason)
```

Do not convert `no_divergence_observed` into “equivalent.” The first phrase
describes measured evidence; the second makes a claim about every possible
input.

## 3. Inspect the one witness

```python
if result.witness is not None:
    print(result.witness.args)
    print(result.witness.differences)
    print(result.witness.outcome_a.return_value)
    print(result.witness.outcome_b.return_value)
```

`args` is the minimized input. `differences` names the envelope fields that
changed, such as `return_value`, `exception`, or `mutated_arguments`.

The witness exists only after exact replay. It is immutable and there is never
a list of Hypothesis's intermediate shrinking candidates.

## 4. Constrain or replace generated inputs

```python
import hypothesis.strategies as st

result = diff(
    price_old,
    price_new,
    quantity=st.integers(min_value=0, max_value=1_000),
)
```

A plain value is also accepted: `quantity=12` checks that one case. Use a
strategy when you want a search.

If ordeal cannot infer an untyped parameter, provide its strategy explicitly.

## Exceptions are outcomes

Two calls agree when both raise the same exception type with the same message.
Returning an exception object is not the same as raising it.

```python
def old(value: int) -> int:
    raise ValueError(f"invalid: {value}")

def new(value: int) -> int:
    raise TypeError(f"invalid: {value}")

assert diff(old, new, value=0).status == "divergent"
```

## Return tolerance and normalization

Use tolerances for numerical drift:

```python
result = diff(old_float, new_float, rtol=1e-6, atol=1e-9)
```

Use `normalize=` to compare a stable representation, or `compare=` for custom
return-value logic. Mutated arguments, receiver state, exceptions, and selected
side effects are still compared independently.

## If the result is inconclusive

Read `result.reason`. Common causes are an object that cannot be deep-copied, a
class-bound receiver with shared state, a failing side-effect restore hook, or
a mismatch that did not replay consistently. Fix the isolation boundary rather
than treating inconclusive as a pass.

Next: [State and Side Effects](differential-state-and-effects.md),
[Differential Evidence](differential-evidence.md), or the exact
[API Reference](../reference/api.md#diff).
