---
description: >-
  Zero-boilerplate Python testing: ordeal mine discovers properties,
  fuzz finds crashes, scan_module smoke-tests every function. No test
  code required.
---

# Auto — Zero-Boilerplate Testing

Point ordeal at your code. Get tests. No scaffolding.

!!! quote "In plain English"
    What if you didn't have to write the test at all? Auto-testing means you give ordeal a module, and it generates chaos tests, fuzzes your functions, and discovers properties for you. You can go from zero tests to comprehensive chaos coverage in one command.

!!! quote "The fastest path to real findings"
    From install to your first real bug finding: about 30 seconds. No test code to write. No configuration to set up. Just `ordeal mine mymodule` and read the output. If it finds something, you have a real bug report with the exact input that triggers it. If it doesn't, you have evidence that your functions handle random inputs correctly — and you can go deeper with `scan_module`, `fuzz`, or a full `ChaosTest`.

## scan_module

!!! quote "Think of it this way"
    `scan_module` is like running a health check on every function in a file. It calls each one with random valid inputs and checks that nothing crashes and the return types match. One line of code, and you know which functions are fragile.

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

For security-oriented triage, keep the same API and opt into trust-boundary bias:

```python
result = scan_module("myapp.scoring", mode="candidate", security_focus=True)
```

## fuzz

!!! quote "What this unlocks"
    When `scan_module` finds a function that looks suspicious, `fuzz` lets you zoom in. It hammers a single function with thousands of random inputs, looking for crashes, wrong return types, and unexpected exceptions. Think of it as a stress test for one function.

Deep-fuzz a single function (1000 examples by default):

```python
from ordeal.auto import fuzz

result = fuzz(myapp.scoring.compute)
assert result.passed

# Override a parameter
result = fuzz(myapp.scoring.compute, model=model_strategy)
```

## chaos_for

!!! quote "Why this matters"
    This is the full power of ordeal, fully automated. `chaos_for` takes a module and builds a complete chaos test: every function becomes a rule, faults get toggled by the nemesis, and invariants are checked after every step. You get the same exploration that a hand-written `ChaosTest` provides, without writing any test code yourself.

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

!!! quote "The key insight"
    You might not know what properties your function should have. That's fine -- `mine` figures it out for you. It runs your function hundreds of times and watches what happens: does it always return the same type? Is the output always in a range? Is it deterministic? You review the discoveries and decide which ones are real contracts worth testing.

`mine()` runs a function many times with random inputs and observes patterns in outputs. It checks: type consistency, never None, no NaN, non-negative, bounded [0,1], never empty, deterministic, idempotent, involution (`f(f(x)) == x`), commutative (`f(a,b) == f(b,a)`), associative (`f(f(a,b),c) == f(a,f(b,c))`), observed range, monotonicity, and length relationships. Float comparisons use `math.isclose` so rounding noise doesn't cause false negatives.

You confirm which properties are real and turn them into tested invariants:

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

### CLI

Mine from the terminal without writing Python:

```bash
ordeal mine myapp.scoring.compute           # single function
ordeal mine myapp.scoring                   # all public functions in module
ordeal mine myapp.scoring.compute -n 1000   # more examples for tighter CI
```

### Generated assertions

`ordeal audit` uses `mine()` to generate real `@quickcheck` assertion tests (not just comments) when your functions have type hints:

```python
# Auto-generated by ordeal audit
@quickcheck
def test_compute_properties(x: float):
    """Mined properties for myapp.scoring.compute."""
    result = myapp.scoring.compute(x)
    assert result is not None  # >=93.0% CI
    assert 0 <= result <= 1  # >=93.0% CI
    assert myapp.scoring.compute(x) == result  # >=93.0% CI
```

Functions without type hints fall back to informational comments with confidence bounds.

## mine_pair — discover cross-function properties

!!! quote "What you can do with this"
    If you have an `encode` and a `decode`, a `serialize` and a `deserialize`, or any pair of functions that should undo each other, `mine_pair` will verify that automatically. It finds roundtrip properties, commutativity, and inverse relationships without you having to specify them.

Check if two functions are inverses, roundtrip-safe, or commutative under composition:

```python
from ordeal.mine import mine_pair

result = mine_pair(encode, decode, max_examples=200)
for p in result.universal:
    print(p)
# ALWAYS  roundtrip decode(encode(x)) == x (48/48)
# ALWAYS  roundtrip encode(decode(x)) == x (45/45)
```

Properties checked:

- **Roundtrip**: `g(f(x)) == x` — the composition is the identity
- **Reverse roundtrip**: `f(g(x)) == x` — the other direction
- **Commutative composition**: `f(g(x)) == g(f(x))` — order doesn't matter

Strategies are inferred from `f`'s type hints. Both functions must accept each other's output as input for roundtrip checks to apply.

```bash
ordeal mine-pair myapp.encode myapp.decode    # CLI equivalent
```

## diff — compare two implementations

!!! quote "How to explore this"
    Rewriting a function? Porting to a faster implementation? `diff` runs both versions on the same random inputs and tells you exactly where they disagree. You don't need to write comparison logic -- ordeal generates the inputs and checks the outputs for you.

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

!!! quote "In plain English"
    Ordeal is smart about generating random inputs from type hints, but it can't invent your custom types. `register_fixture` lets you teach it once -- "a `model` is one of these strings, an `api_key` looks like this" -- and then every auto tool knows how to generate those values. Register in `conftest.py` and forget about it.

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

!!! quote "The key insight"
    Every auto tool follows the same pipeline: scan for functions, figure out what inputs they need (from type hints, registered fixtures, or name-based guesses), generate those inputs, and run. The logic lives in `ordeal/auto.py` and `ordeal/quickcheck.py`. If a function has type hints, ordeal can test it. If it doesn't, you can teach ordeal with fixtures.

All auto primitives (`scan_module`, `fuzz`, `chaos_for`, `mine`, `diff`):

1. Scan the module for public, non-class callables
2. Infer strategies from type hints (via `ordeal.quickcheck.strategy_for_type`)
3. Check registered fixtures for unresolved parameters
4. Fall back to parameter name heuristics (`"threshold"` → floats, `"count"` → integers)
5. Accept explicit `fixtures` overrides (highest priority)
6. Skip functions that can't be tested (no hints, no fixtures, no heuristics)

Functions starting with `_` are skipped. Functions with defaults for all params work even without type hints.
