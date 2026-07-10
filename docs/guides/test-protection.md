# Test protection guide

Use this guide when “the tests pass” is not enough and you need evidence that
the tests would catch wrong behavior.

New to the idea? Read [Are your tests meaningful?](../concepts/test-meaningfulness.md)
first. It explains coverage, mutations, and properties without assuming testing
terminology.

## Five-minute workflow

Start with the module audit:

```bash
ordeal audit myapp.scoring
```

`audit` measures the existing suite, generates incremental checks, measures
their coverage, validates mined properties against mutations, and prints a
protection verdict for those generated/migrated checks.

Use direct mutation testing when you want to judge the selected existing pytest
tests themselves:

```bash
ordeal mutate myapp.scoring --preset standard
```

## Read the audit result

```text
myapp.scoring
  generated incremental: 12 tests | 130 lines | 100% coverage [verified]
  mutation: 3/4 (75%)
  weakest killers:
    - property:bounded output: 1 kill(s)
  protection: WEAK: 100% line coverage but 1/4 mutation(s) survived
```

Read it in this order:

1. **Protection** gives the scoped decision.
2. **Surviving mutants** are concrete behaviors the checks did not protect.
3. **Coverage gaps** show executable lines the checks never reached.
4. **Property strength** separates useful oracles from weak observations.
5. **Kill attribution** shows which test or property supplied the signal.

## Fix a weak verdict

For every survivor:

1. Read its source line and mutation description.
2. Decide whether the mutant changes intended behavior.
3. If it does, add the smallest assertion that distinguishes original and
   mutant behavior.
4. If it does not, document or filter the equivalent mutant.
5. Run the same command again and confirm that the survivor is killed.

Common examples:

| Survivor | Likely missing assertion |
|---|---|
| `+ -> -` | Exact numeric result |
| `< -> <=` | Boundary input |
| `return value -> None` | Return value or type |
| deleted statement | Side effect or state transition |
| `and -> or` | Both sides of the condition |

Generate draft review stubs when useful:

```bash
ordeal mutate myapp.scoring --generate-stubs tests/test_scoring_gaps.py
```

Stubs are prompts for a human-reviewed assertion, not proof by themselves.

## Read property strength

Programmatically:

```python
from ordeal import validate_mined_properties

result = validate_mined_properties("myapp.scoring.compute")
for property_ in result.property_strength():
    print(property_["name"], property_["status"], property_["mutants_killed"])
```

- `discriminating`: killed at least one tested mutant;
- `tautological_or_weak`: ran, but killed none of this mutant set;
- `unexercised`: zero observations;
- `not_measured`: no non-equivalent mutants were tested.

For hand-written `always`, `sometimes`, and `reachable` assertions, `report()`
adds `evidence_status`. Declared properties with zero hits remain visible as
`unexercised`.

## Decide what “good enough” means

Do not copy a universal score threshold. Start by requiring:

- no survivors on money, permissions, persistence, or destructive operations;
- no uncovered executable lines in the measured target;
- no unexercised declared safety properties;
- no unexplained score regression from the accepted baseline.

Then widen operators or use `--preset thorough` before higher-risk releases.

## Next

- Automate the decision: [Test Protection in CI](test-protection-ci.md)
- Resolve confusing results: [FAQ](test-protection-faq.md)
- Use Python or JSON: [Evidence Schema](../reference/test-protection-schema.md)
