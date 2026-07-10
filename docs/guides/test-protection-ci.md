# Test protection in CI

CI should fail on evidence you have chosen to treat as release-blocking. Keep
the policy explicit: a mutation threshold alone does not include coverage gaps
or unexercised properties.

## Simple mutation gate

```bash
ordeal mutate myapp.scoring \
  --preset standard \
  --workers 4 \
  --threshold 0.85
```

The command exits non-zero when the mutation score is below `0.85`. It always
prints a machine-parseable final line:

```text
Score: 17/20 (85%)
```

Use this for a fast score floor. Review survivors even when the aggregate score
passes; one missed authorization check matters more than several harmless
constant mutations.

## Combined audit evidence

```bash
ordeal audit myapp.scoring --json > ordeal-audit.json
```

The agent-facing envelope contains one entry per module at:

```text
raw_details.protection_views[]
```

A small policy script can fail on the combined verdict:

```python
import json
import sys

payload = json.load(open("ordeal-audit.json", encoding="utf-8"))
views = payload["raw_details"]["protection_views"]
bad = [view for view in views if view["status"] != "protective_within_measured_scope"]

for view in bad:
    print(f"{view['module']}: {view['status']}: {view['summary']}")
raise SystemExit(1 if bad else 0)
```

Remember: the audit protection view describes generated/migrated checks. Use
`ordeal mutate` when your gate specifically concerns the existing pytest suite.

## Recommended rollout

1. Record the current score and survivors without failing CI.
2. Remove or justify equivalent mutants.
3. Block new survivors in changed critical modules.
4. Add a score floor once the baseline is stable.
5. Periodically run `thorough`; keep `standard` for ordinary pull requests.

This avoids a rushed “hit 90% at any cost” campaign that adds brittle or empty
assertions.

## Policy examples

| Context | Reasonable first policy |
|---|---|
| New library | No survivors in public API behavior |
| Payments/auth | No survivors in critical functions; thorough preset |
| Legacy service | Do not regress score; close touched-line survivors |
| Generated checks | Combined protection verdict must not be `weak` |
| Experimental code | Report only, with an expiry date for the exception |

## Store useful artifacts

Keep the JSON audit output and mutation summary as CI artifacts. They explain a
failure without rerunning the full job and make score changes reviewable.

Do not compare scores produced with different presets, filters, targets, or
equivalence settings as if they were the same metric. Pin those inputs in the
workflow.

## Flaky or isolated tests

Mutation testing repeatedly runs tests. Eliminate ordinary flakiness first. If
tests execute the target in another process, in-memory patching may be invisible;
use disk mutation where supported or inspect Ordeal's mine-oracle fallback note.

## See also

- [Practical workflow](test-protection.md)
- [FAQ](test-protection-faq.md)
- [Evidence schema](../reference/test-protection-schema.md)
