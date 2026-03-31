---
description: >-
  Mutation testing for Python with ordeal. 14 AST operators verify your
  tests catch real bugs. Presets for fast or thorough analysis. CI-ready
  with ordeal mutate.
---

# Mutation Testing

!!! quote "In plain English"
    How do you know your tests are actually good? Mutation testing answers that question. Ordeal makes small, deliberate changes to your code -- like swapping a `+` to a `-` -- and checks whether your tests catch the change. If they don't, you've found a blind spot. This is about test quality, not code quality.

## Why mutation testing

Your chaos tests found zero bugs. That's either very good news or very bad news. Mutation testing tells you which.

The idea is simple: change the code (mutate it) and run the tests. If the tests still pass after the change, they have a blind spot. That surviving mutant represents a class of real bugs your tests would not catch.

This is how Meta validates test quality at scale. Write the tests, then prove they work by checking that they fail when the code is wrong. If they don't fail, the tests are incomplete.

### The question coverage can't answer

!!! quote "Think of it this way"
    Imagine a security guard who walks through every room but never checks if the doors are locked. That's code coverage without mutation testing. The guard was there (the line ran), but nothing was actually verified. Mutation testing checks whether the guard would notice if someone swapped a lock.

Code coverage tells you "this line ran." Mutation testing tells you "if this line were wrong, would any test notice?" A line can have 100% coverage and still have zero meaningful assertions checking its output. Consider:

```python
def compute(a, b):
    return a + b

def test_compute():
    compute(3, 4)  # 100% coverage, 0% checking
```

The test runs the function — coverage says 100%. But swap `+` to `-` and the test still passes. The mutation reveals: nobody checks the result.

### Where it fits in the ordeal standard

Ordeal's goal is certification: when ordeal passes, the code works. But that only holds if the tests themselves are trustworthy. Mutation testing is the meta-test — it validates the validators:

```
Code ← tested by → Chaos tests ← validated by → Mutation testing
```

If your chaos tests have a 95%+ mutation score, you can trust them. If not, the surviving mutants tell you exactly where to add assertions.

## Quick start

!!! quote "What you can do with this"
    In three lines of code, you can find out exactly where your tests are weak. Each surviving mutant points you to a specific line, a specific change, and a specific missing assertion. You don't have to guess what to test next -- the survivors tell you.

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

!!! quote "In plain English"
    Each operator is a different way of breaking your code on purpose. Swapping `+` to `-` tests whether you check math results. Replacing a return value with `None` tests whether you check what functions give back. Together, they cover the most common categories of real bugs. The operators live in `ordeal/mutations.py`.

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

## Performance and parallelism

Module-level mutation testing can be slow because each mutant needs a full test run. ordeal optimizes this in three ways:

**Batch mode** (automatic): When auto-discovering tests, ordeal runs all mutants in a single pytest session instead of starting a new session per mutant. This eliminates repeated startup overhead.

**Equivalence filtering** (default on): Before testing, ordeal runs each mutant on random inputs and skips mutants that produce identical outputs. These "equivalent mutants" can never be killed and waste testing time.

**Parallel workers**: Distribute mutants across multiple processes. Each worker runs a batched pytest session on its chunk.

```bash
ordeal mutate myapp.scoring --workers 4           # 4 parallel workers
ordeal mutate myapp.scoring --no-filter            # disable equivalence filtering
```

```python
result = mutate_and_test("myapp.scoring", workers=4, filter_equivalent=True)
```

**Decorated functions**: `@ray.remote`, `@functools.wraps`, and similar decorators are auto-unwrapped — ordeal reaches the original function for source inspection.

**ChaosTest visibility**: The mutation runner passes `--chaos` to pytest, so ChaosTest classes and their `always()`/`sometimes()` assertions are exercised during mutation scoring.

**No tests found**: If auto-discovery finds no matching tests, ordeal raises `NoTestsFoundError` instead of reporting a misleading 0% score.

## Function-level vs module-level

!!! quote "Why this matters"
    You can mutate one function at a time (safe, precise) or a whole module at once (fast, broad). Start with `mutate_function_and_test` for the functions you care about most, then use `mutate_and_test` when you want a quick sweep of an entire file.

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

!!! quote "The key insight"
    A high mutation score means your tests actually verify behavior, not just that code runs. A low score means your tests would still pass even if the code were broken. The survivors are not failures to fix in your code -- they are gaps to fill in your tests.

!!! quote "What mutation scores mean for your team"
    For a startup shipping fast: focus on the surviving mutants. Each one is a specific blind spot — a bug that could ship and your tests wouldn't catch it. Fix the top 3 survivors and your test suite gets meaningfully stronger in 15 minutes.

    For an established team: track mutation scores over time. A score that climbs from 75% to 92% over a quarter means your testing culture is improving. A score that drops after a refactor means the new code shipped without adequate assertions. The number measures testing discipline, not just test count.

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

!!! quote "What this unlocks"
    This is where auto-testing and mutation testing meet. `mine()` discovers properties of your function. `validate_mined_properties` then checks whether those discovered properties are strong enough to catch bugs. If a mutant survives, the mined properties missed it -- and you know exactly what kind of assertion to add.

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

## mutation_faults — mutations as faults for the Explorer

!!! quote "How to explore this"
    Instead of testing one mutant at a time in a loop, you can turn mutants into faults and let the Explorer combine them with other faults during chaos testing. The nemesis toggles mutations on and off alongside network timeouts, disk failures, and everything else. This finds bugs that only appear when a mutation interacts with a fault -- something sequential mutation testing would never catch.

`mutation_faults()` bridges mutation testing with the Explorer. Instead of running mutations in a loop, it generates PatchFault objects — one per mutant. You can add these to a ChaosTest's fault list, and the nemesis will toggle them during exploration:

```python
from ordeal.mutations import mutation_faults

# Generate faults from mutants
mutant_faults = mutation_faults("myapp.scoring.compute", operators=["arithmetic", "comparison"])

# Each entry is (Mutant, PatchFault)
for mutant, fault in mutant_faults:
    print(f"{mutant.location} {mutant.description} -> {fault.name}")
# L42:8 + -> - -> mutant(arithmetic@L42:+ -> -)
# L67:4 < -> <= -> mutant(comparison@L67:< -> <=)
```

Use these faults in a ChaosTest to let the Explorer's nemesis toggle mutations during coverage-guided exploration:

```python
from ordeal import ChaosTest, rule, always
from ordeal.mutations import mutation_faults

class ScoreChaos(ChaosTest):
    faults = [mf for _, mf in mutation_faults("myapp.scoring.compute")]
    swarm = True  # random subset of mutants per run

    @rule()
    def score(self):
        result = self.service.compute(3, 4)
        always(result == 7, "compute is correct")
```

This is powerful: instead of testing mutants one at a time, the Explorer combines mutation faults with your normal faults, exploring which mutation + fault combinations break invariants. It turns mutation testing from a validation step into an exploration strategy.

### When to use mutation_faults vs mutate_function_and_test

| Approach | Speed | Depth | Use when |
|---|---|---|---|
| `mutate_function_and_test` | Fast (one mutant at a time) | Shallow (each mutant tested in isolation) | Quick validation, CI gate |
| `mutation_faults` + Explorer | Slower (coverage-guided) | Deep (mutations combined with faults + interleavings) | Pre-release, finding subtle interaction bugs |

## Workflow

!!! quote "Think of it this way"
    Chaos tests find bugs in your code. Mutation testing finds bugs in your tests. The workflow is a loop: write tests, mutate, fix the gaps the survivors reveal, mutate again. Each cycle makes your test suite stronger. When you hit 95%+ mutation score, you can trust your tests to catch real regressions.

Mutation testing fits into a validation loop:

1. **Write chaos tests** -- use ChaosTest, quickcheck, or standard pytest.
2. **Run mutations** -- `mutate_function_and_test` on the critical functions.
3. **Read the survivors** -- each one is a specific gap.
4. **Add the missing assertion** -- the survivor tells you exactly what to check.
5. **Run mutations again** -- confirm the gap is closed.
6. **Repeat** -- until the mutation score meets your threshold.

This loop is the difference between "we have tests" and "we have tests that work." The chaos tests find bugs in your code. Mutation testing finds bugs in your tests.
