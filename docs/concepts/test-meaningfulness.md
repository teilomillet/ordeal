# Are your tests meaningful?

A passing test proves one thing: the tested run did not fail. It does not prove
that the test would notice a wrong answer.

Imagine testing a smoke alarm by walking through every room. You covered the
whole house, but you never made smoke. Line coverage works the same way: it says
which code ran, not whether the alarm in your test suite works.

Ordeal answers the harder question by deliberately changing the software and
checking whether the tests object.

## The six pieces of evidence

- **Line coverage** asks which executable lines ran. A gap means some code was
  never exercised.
- **Mutation score** asks whether tests detected small wrong changes. A miss
  means a realistic change escaped.
- **Survivors** name the exact changes that escaped. Each one identifies a
  behavior without measured protection.
- **Kill attribution** names the test or property that noticed a change. It
  reveals where the useful oracle lives.
- **Property strength** asks whether a property rejected broken variants. A
  zero-kill property may be too broad or tautological for this mutant set.
- **Property exercise** asks whether a declared property was ever observed. A
  zero-hit property represents a requirement the run never tested.

No one number replaces the others. Mutation survival is decisive: a surviving
non-equivalent mutant is direct evidence that the measured tests are weak for
that behavior, even if line coverage is 100%.

## A minimal example

```python
def classify(value: int) -> str:
    if value > 0:
        return "positive"
    return "nonpositive"


def test_classify_runs():
    classify(1)
    classify(-1)
```

This test can execute every line. But it makes no assertion. Changing `>` to
`<=` still leaves the test green. Coverage says **100%**; mutation score says
**0%**. The second measurement reveals what the first cannot.

Ordeal keeps this exact counterexample as an executable regression test.

## The verdicts

- `WEAK`: a mutant survived, a line is uncovered, or a declared property was
  never exercised.
- `PROTECTIVE_WITHIN_MEASURED_SCOPE`: every tested mutant was killed and every
  executable line was covered.
- `INCONCLUSIVE`: mutation or coverage evidence was unavailable.

The wording “within measured scope” matters. Ordeal tests selected mutation
operators, inputs, properties, and code paths. It does not prove the absence of
all possible bugs.

## What property strength means

A property such as “the result is not `None`” may hold on both the real code and
every broken variant. Ordeal labels it `tautological_or_weak` for the measured
mutants. That is evidence of poor discrimination, not a formal proof that the
statement is logically tautological.

A `discriminating` property killed at least one mutant. An `unexercised`
property was declared but received zero observations. `not_measured` means no
non-equivalent mutant was available for the comparison.

## Choose your next step

- Run the workflow: [Test Protection Guide](../guides/test-protection.md)
- Add a CI policy: [Test Protection in CI](../guides/test-protection-ci.md)
- Interpret edge cases: [Test Protection FAQ](../guides/test-protection-faq.md)
- Consume the payload: [Evidence Schema](../reference/test-protection-schema.md)
