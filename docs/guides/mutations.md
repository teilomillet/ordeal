# Mutation Testing

Validates that your tests actually catch bugs. Generates mutated code, runs your tests, reports what slipped through.

## Quick start

```python
from ordeal.mutations import mutate_function_and_test

result = mutate_function_and_test(
    "myapp.scoring.compute",
    lambda: test_scoring(),
)
print(result.summary())
# Mutation score: 15/18 (83%)
#   SURVIVED  L42:8 + -> -
#   SURVIVED  L67:4 negate if-condition
```

## Operators

| Operator | What it does | Example |
|---|---|---|
| `arithmetic` | Swap `+`/`-`, `*`/`/` | `a + b` becomes `a - b` |
| `comparison` | Swap `<`/`<=`, `==`/`!=` | `x > 0` becomes `x >= 0` |
| `negate` | Negate `if`/`while` conditions | `if x:` becomes `if not x:` |
| `return_none` | Replace return value with None | `return x` becomes `return None` |

## Module-level testing

For whole-module mutation (swaps `sys.modules`):

```python
from ordeal.mutations import mutate_and_test

result = mutate_and_test("myapp.scoring", lambda: run_all_tests())
```

## Interpreting results

- **Score = 100%**: every mutant was caught. Tests are strong.
- **Survivors**: mutants your tests missed. Each one is a potential real bug your tests wouldn't catch.
- **SURVIVED L42:8 + -> -**: changing `+` to `-` on line 42 didn't break any test.
