# Core Concepts

Six concepts, each with one example.

## ChaosTest

Stateful test with automatic fault injection. Declare faults + rules + invariants:

```python
class MyTest(ChaosTest):
    faults = [timing.timeout("myapp.api.call")]
    swarm = True  # random fault subsets per run

    @rule()
    def call_api(self): ...

    @invariant()
    def healthy(self): assert self.service.ok
```

The **nemesis** (auto-injected) toggles faults during exploration. `swarm = True` means each run uses a random subset for better coverage.

## Faults

| Module | Examples |
|---|---|
| `faults.io` | `error_on_call`, `return_empty`, `truncate_output`, `disk_full` |
| `faults.numerical` | `nan_injection`, `inf_injection`, `wrong_shape` |
| `faults.timing` | `timeout`, `slow`, `intermittent_crash`, `jitter` |

Custom: subclass `Fault` or use `LambdaFault("name", on_activate, on_deactivate)`.

All faults work the same way: `activate()` / `deactivate()` / `reset()`. The nemesis calls these automatically.

## Assertions

```python
always(cond, "name")       # must be true every time — raises immediately
sometimes(cond, "name")    # must be true at least once — checked at end
reachable("name")          # code path must execute — checked at end
unreachable("name")        # must never execute — raises immediately
```

## Invariants

Composable named checks with clear failure messages:

```python
from ordeal.invariants import finite, bounded
valid_score = finite & bounded(0, 1)
valid_score(model_output)  # "Invariant 'bounded(0, 1)' violated: 1.5 not in [0, 1]"
```

Built-in: `no_nan`, `no_inf`, `finite`, `bounded(lo, hi)`, `monotonic()`, `unique()`, `non_empty()`.

## Buggify

Inline fault injection — no-op in production, active during testing:

```python
from ordeal.buggify import buggify, buggify_value

if buggify():                                    # sometimes True during testing
    time.sleep(5)
return buggify_value(result, float('nan'))       # sometimes NaN during testing
```

Seed-controlled, thread-local, zero-cost when inactive.

## QuickCheck

`@quickcheck` infers strategies from type hints with boundary bias:

```python
from ordeal.quickcheck import quickcheck

@quickcheck
def test_sort_idempotent(xs: list[int]):
    assert sorted(sorted(xs)) == sorted(xs)
```

Works with `int`, `float`, `str`, `list[T]`, `dict[K,V]`, `Optional[T]`, dataclasses.
