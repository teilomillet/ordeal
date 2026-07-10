# Test protection FAQ

## Does 100% mutation score prove there are no bugs?

No. It means all tested, non-equivalent mutants were killed. Other bug classes,
inputs, integrations, concurrency schedules, or requirements may remain outside
the measured scope.

## Does 100% line coverage mean the tests are meaningful?

No. Coverage proves execution, not observation. A test can call every branch
without asserting a result. Ordeal includes an executable example with 100% line
coverage and a 0% mutation score.

## Why can a property be weak while the suite is protective?

Properties can overlap. “Not `None`” may kill nothing while an exact-value
property kills every mutant. Keep the weak property if it communicates a useful
contract, but do not count it as discriminating evidence.

## Does `tautological_or_weak` mean the statement is logically true?

No. It means the property killed none of the measured mutants. Formal tautology
proof would require a different analysis. The label intentionally preserves that
uncertainty.

## What is an unexercised property?

A declared property with zero observations. It represents a known requirement
that the run never tested. Ordeal surfaces it instead of silently treating it as
passing.

## Why did Ordeal report no mutants?

The target may contain no supported mutation sites, all generated mutants may
have been filtered as equivalent, compilation may have failed, or generation may
have timed out. Treat the verdict as `inconclusive`, then inspect diagnostics.

## Are surviving mutants always test bugs?

No. A survivor may be behaviorally equivalent to the original code. Ordeal
filters likely equivalents, but equivalence is difficult in general. Review the
source change before adding a test.

## What does kill attribution prove?

It records the test or property observed killing a mutant. Property evaluation
can name multiple rejecting properties. Pytest usually stops at the first failing
test, so attribution is not a complete redundancy map of every test that could
have killed the mutant.

## Why do `audit` and `mutate` answer slightly different questions?

`mutate` runs mutations against the selected existing tests. `audit` compares
existing coverage with generated/migrated checks and mutation-validates mined
properties. Its protection verdict describes those resulting checks.

## Is line coverage the same as branch coverage?

No. A line can execute without every decision outcome being explored. Mutation
operators often reveal this by changing comparisons or Boolean logic, but the
reported `coverage_gaps` field contains executable line gaps.

## What score should CI require?

There is no universal number. First eliminate survivors in critical behavior and
prevent regression against a stable, like-for-like baseline. Then choose a score
floor appropriate to the module and preset.

## Should I delete every weak property?

No. Some properties document contracts or provide defense in depth. Delete only
noise. Strengthen important weak properties with more discriminating assertions.

## Where are the exact fields documented?

See the [Test Protection Evidence Schema](../reference/test-protection-schema.md).
