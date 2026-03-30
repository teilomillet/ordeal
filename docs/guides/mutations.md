# Mutation Testing

## Why mutation testing

Your chaos tests found zero bugs. That is either very good news or very bad news. Mutation testing tells you which.

The idea is simple: change the code (mutate it) and run the tests. If the tests still pass after the change, they have a blind spot. That surviving mutant represents a class of real bugs your tests would not catch.

This is how Meta validates test quality at scale. Write the tests, then prove they work by checking that they fail when the code is wrong. If they do not fail, the tests are incomplete.

Mutation testing answers a question that code coverage cannot: "If this line were wrong, would any test notice?" A line can have 100% coverage and still have zero meaningful assertions checking its output.

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
#   SURVIVED  L81:12 return None
```

Each SURVIVED line is a specific test gap. Line 42 had a `+` swapped to `-` and no test noticed. That means no test checks the sign of that computation.

## Operators

ordeal ships seven mutation operators. Each targets a different class of bugs.

| Operator | What it does | Example |
|---|---|---|
| `arithmetic` | Swap `+`/`-`, `*`/`/`, `%`/`*` | `a + b` becomes `a - b` |
| `comparison` | Swap `<`/`<=`, `>`/`>=`, `==`/`!=` | `x > 0` becomes `x >= 0` |
| `negate` | Negate `if`/`while` conditions | `if x:` becomes `if not x:` |
| `return_none` | Replace return value with None | `return x` becomes `return None` |
| `boundary` | Shift integer constants by one | `n` becomes `n + 1` |
| `constant` | Replace numeric constants with 0, 1, or -1 | `100` becomes `0` |
| `delete` | Replace statements with `pass` | `total += x` becomes `pass` |

### What each operator catches

**arithmetic** -- Finds missing tests for mathematical correctness. If swapping `+` to `-` survives, no test verifies the direction of the computation. Add an assertion that checks the actual numeric result, not just that the function returned something.

**comparison** -- Finds missing boundary tests. If `<` to `<=` survives, no test exercises the exact boundary value. The fix is almost always a test case at the boundary: if the code says `x > 0`, test with `x = 0` and `x = 1`.

**negate** -- Finds missing condition tests. If negating an `if` condition survives, either the condition is dead code or the test never exercises both branches. Add a test that triggers the opposite branch.

**return_none** -- Finds missing return value assertions. If replacing a return value with `None` survives, the caller never checks what it gets back. This is one of the most common test gaps.

**boundary** -- Finds off-by-one errors. If changing `10` to `11` survives, no test is sensitive to that exact value. Common in loop bounds, slice indices, and range limits.

**constant** -- Finds hardcoded magic numbers that are untested. If replacing `100` with `0` survives, the constant might as well be anything. Tests should verify that the specific value matters.

**delete** -- Finds unnecessary or untested statements. If deleting an assignment and replacing it with `pass` survives, either the statement has no observable effect or the tests do not observe it. This catches dead code and undertested side effects.

## Function-level vs module-level

ordeal provides two entry points. They test the same mutations but differ in how they swap the code.

### mutate_function_and_test (recommended)

```python
from ordeal.mutations import mutate_function_and_test

result = mutate_function_and_test(
    "myapp.scoring.compute",
    lambda: test_scoring(),
)
```

This mutates a single function and uses PatchFault to swap it at the module level. Callers that reference the function through the module (`myapp.scoring.compute(...)`) will see each mutant. This is the safer option because it has a smaller blast radius -- only one function changes at a time.

Use this when:
- You want to validate tests for a specific function.
- The function is called through its module (the common case).
- You want reliable, isolated mutation testing.

### mutate_and_test

```python
from ordeal.mutations import mutate_and_test

result = mutate_and_test("myapp.scoring", lambda: run_all_tests())
```

This mutates the entire module and swaps `sys.modules`. All functions in the module are mutated together. This is faster for broad coverage because it generates mutants across all functions at once.

The trade-off: code that imported individual functions before the swap (`from myapp.scoring import compute`) will still reference the original, unmutated version. For this to work, the tests must import the module, not its functions.

Use this when:
- You want a broad sweep of an entire module.
- Your tests import the module object (not individual functions).
- You are doing an initial assessment of test quality across a whole file.

## Interpreting results

The mutation score is the percentage of mutants your tests killed. Here is how to read it.

**100% score**: Every mutant was caught. Your tests are strong for this function. No change to the code can slip past unnoticed (within the scope of the operators tested).

**80-99% score**: Most mutants caught, but some gaps remain. Look at the SURVIVED lines to find them. These are usually boundary conditions, specific branches, or return values that are not asserted.

**Below 80% score**: Significant test gaps. The tests are likely checking that the code runs without errors, but not checking that it produces the right results. Start with the return_none survivors -- if replacing a return value with None does not break a test, the test is not checking outputs at all.

### Reading survivor output

Each SURVIVED line has this format:

```
SURVIVED  L42:8 + -> -
```

This means: on line 42, column 8, the `+` operator was changed to `-`, and all tests still passed. The line and column point directly to the code that needs better testing.

### Common survivors and what to add

**Arithmetic survivors** (`+ -> -`, `* -> /`): Your tests are not checking numeric correctness. Add assertions that verify the exact result, not just that the result is "truthy" or "not None". For example, assert `compute(3, 4) == 7` instead of just calling `compute(3, 4)`.

**Comparison survivors** (`< -> <=`, `== -> !=`): Your tests skip the boundary. If `x > 0` mutates to `x >= 0` and survives, add a test with `x = 0`. Boundary tests are the single most effective addition you can make.

**Negate survivors** (`negate if-condition`): One branch of the condition is untested. Write a test that exercises the branch that was never reached. This often reveals dead code or error paths that were never tested.

**Return None survivors**: The test calls the function but ignores what it returns. Add `assert result == expected_value` or `assert result is not None` at minimum.

**Boundary survivors** (`10 -> 11`): Off-by-one changes go unnoticed. Test with values at the exact boundary. If the code uses `range(10)`, test that the 10th element is excluded and the 9th is included.

**Constant survivors** (`100 -> 0`): A magic number has no coverage. Either the constant is dead, or the test does not exercise the path where it matters. Add a test that would fail if the constant were different.

**Delete survivors** (`delete statement`): A statement can be removed with no test impact. Either it is dead code (remove it) or the test does not observe its effect (add an assertion for the side effect).

## validate_mined_properties — close the loop with mine()

`mine()` discovers properties. Mutation testing checks whether those properties actually catch bugs. `validate_mined_properties` does both in one call:

```python
from ordeal.mutations import validate_mined_properties

result = validate_mined_properties("myapp.scoring.compute", max_examples=100)
print(result.summary())
# Mutation score: 8/10 (80%)
#   SURVIVED  L42:8 + -> -
#   SURVIVED  L67:4 negate if-condition
```

It mines the original function, then mutates it and re-mines each mutant. If a mined property no longer holds on the mutant, the mutant is killed. Surviving mutants mean the mined properties are too weak to detect that class of bug.

`ordeal audit` runs this automatically and reports the score in the summary:

```
  mutation: 14/18 (78%)
```

This answers the question: "are the properties mine() found strong enough to be useful as tests?"

## Workflow

Mutation testing fits into a validation loop:

1. **Write chaos tests** -- use ChaosTest, quickcheck, or standard pytest.
2. **Run mutations** -- `mutate_function_and_test` on the critical functions.
3. **Read the survivors** -- each one is a specific gap.
4. **Add the missing assertion** -- the survivor tells you exactly what to check.
5. **Run mutations again** -- confirm the gap is closed.
6. **Repeat** -- until the mutation score meets your threshold.

This loop is the difference between "we have tests" and "we have tests that work." The chaos tests find bugs in your code. Mutation testing finds bugs in your tests.
