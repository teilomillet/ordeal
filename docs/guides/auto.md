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

## mine — discover properties automatically

`mine()` runs a function many times with random inputs and observes patterns in outputs — type consistency, boundedness, determinism, idempotency. You confirm which are real and turn them into tested invariants:

```python
from ordeal.mine import mine

result = mine(myapp.scoring.compute, max_examples=500)
for p in result.universal:
    print(p)
# ALWAYS  output type is float (500/500)
# ALWAYS  deterministic (50/50)
# ALWAYS  output in [0, 1] (500/500)
```

Properties are probabilistic — the confidence is stated, not assumed. `500/500` doesn't mean "always holds"; it means "holds with >= 99.4% probability at 95% CI" (Wilson score interval).

`mine()` also tells you what it *cannot* check — see `result.not_checked` for structural limitations (correctness, concurrency, domain-specific invariants). These are the tests you need to write manually.

`ordeal audit` uses `mine()` internally to generate migrated test files with mined property descriptions.

## diff — compare two implementations

Differential testing: run two functions on the same random inputs and check their outputs match. Catches regressions, validates refactors, and verifies backend ports:

```python
from ordeal.diff import diff

# Exact comparison — are v1 and v2 identical?
result = diff(score_v1, score_v2, max_examples=200)
assert result.equivalent, result.summary()

# Floating-point tolerance — are old and new close enough?
result = diff(compute_old, compute_new, rtol=1e-6)

# Custom comparator — only care about specific fields
result = diff(api_v1, api_v2, compare=lambda a, b: a.status == b.status)
```

When outputs differ, `result.mismatches` contains the exact inputs and both outputs so you can debug the divergence. Strategies are inferred from `fn_a`'s type hints — both functions must accept the same parameters.

Use cases:
- **Refactoring**: verify the new implementation matches the old
- **Porting**: compare a Python prototype against a Rust/C extension
- **Regression testing**: ensure a bugfix doesn't change other outputs

## register_fixture — teach ordeal your types

When your codebase has domain-specific types that ordeal can't infer from hints, register a fixture once and every auto tool picks it up:

```python
from ordeal.auto import register_fixture
import hypothesis.strategies as st

# Register once at import time (e.g., in conftest.py)
register_fixture("model", st.sampled_from(["gpt-4", "claude-3", "llama-70b"]))
register_fixture("api_key", st.just("sk-test-key-12345"))
register_fixture("config", st.fixed_dictionaries({
    "temperature": st.floats(0.0, 2.0),
    "max_tokens": st.integers(1, 4096),
}))
```

Now `scan_module`, `fuzz`, `mine`, and `diff` all know how to generate these parameters without explicit `fixtures=` overrides:

```python
# These "just work" because the fixtures are registered
result = scan_module("myapp.llm")        # uses registered "model" and "api_key"
result = fuzz(myapp.llm.generate)         # same
result = mine(myapp.llm.generate)         # same
```

**Priority order** when resolving a parameter:

1. Explicit `fixtures={"model": ...}` passed to the function (highest)
2. `register_fixture("model", ...)` global registry
3. Type hint inference via `strategy_for_type`
4. Parameter name heuristics (e.g., `"seed"` → integers, `"probability"` → floats 0-1)

Register fixtures for: API clients, database connections, model objects, configuration dicts, authentication tokens — anything that can't be generated from a type hint alone.

## How it works

All auto primitives (`scan_module`, `fuzz`, `chaos_for`, `mine`, `diff`):

1. Scan the module for public, non-class callables
2. Infer strategies from type hints (via `ordeal.quickcheck.strategy_for_type`)
3. Check registered fixtures for unresolved parameters
4. Fall back to parameter name heuristics (`"threshold"` → floats, `"count"` → integers)
5. Accept explicit `fixtures` overrides (highest priority)
6. Skip functions that can't be tested (no hints, no fixtures, no heuristics)

Functions starting with `_` are skipped. Functions with defaults for all params work even without type hints.
