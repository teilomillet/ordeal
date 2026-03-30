# Auto — Zero-Boilerplate Testing

Point ordeal at your code. Get tests. No scaffolding.

## scan_module

Smoke-test every public function in a module:

```python
from ordeal.auto import scan_module

result = scan_module("myapp.scoring")
print(result.summary())
# scan_module('myapp.scoring'): 8 functions, 1 failed
#   PASS  compute
#   PASS  normalize
#   FAIL  transform: ZeroDivisionError: division by zero
#   PASS  clamp
#   ...
assert result.passed
```

Checks per function: **no crash** with random valid inputs + **return type** matches annotation.

With fixtures for params that can't be inferred from types:

```python
result = scan_module("myapp.scoring", fixtures={"model": model_strategy})
```

## fuzz

Deep-fuzz a single function (1000 examples by default):

```python
from ordeal.auto import fuzz

result = fuzz(myapp.scoring.compute)
assert result.passed

# Override a parameter
result = fuzz(myapp.scoring.compute, model=model_strategy)
```

## chaos_for

Auto-generate a `ChaosTest` from a module's public API. Each function becomes a `@rule`. The nemesis toggles faults. Invariants are checked on every return value.

```python
from ordeal.auto import chaos_for
from ordeal.invariants import finite, bounded
from ordeal.faults import timing

TestScoring = chaos_for("myapp.scoring")

# With depth:
TestScoring = chaos_for(
    "myapp.scoring",
    fixtures={"model": model_strategy},
    invariants=[finite, bounded(0, 1)],
    faults=[timing.timeout("myapp.scoring.predict")],
)
```

Returns a pytest-discoverable `TestCase`. Run with `pytest`.

## How it works

All three primitives:

1. Scan the module for public, non-class callables
2. Infer strategies from type hints (via `ordeal.quickcheck.strategy_for_type`)
3. Accept `fixtures` overrides for params without hints
4. Skip functions that can't be tested (no hints, no fixtures)

Functions starting with `_` are skipped. Functions with defaults for all params work even without type hints.
