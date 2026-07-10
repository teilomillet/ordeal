---
description: >-
  Use Ordeal reliability coverage in CI and external execution platforms with
  pytest-xdist aggregation, JSON output, and explicit gating policies.
---

# Reliability Coverage in CI and External Platforms

The matrix is provider-neutral. Humans can read the pytest summary; automation
can consume the same counters from `report()`.

## Run it in CI

```bash
pytest --chaos --chaos-seed 42
```

Pinning the seed makes a failing schedule easier to reproduce. The matrix also
works with pytest-xdist:

```bash
pytest --chaos --chaos-seed 42 -n auto
```

Each worker sends raw `hits`, `passes`, and `failures` to the controller. The
controller sums them and derives the final status. It does not trust a
worker-supplied status string.

## Decide what should block delivery

Reporting the matrix does not add a separate pytest exit-code policy. Unmuted
`always` and `unreachable` violations still fail immediately, but deferred or
muted findings need an explicit summary gate. Teams choose a policy:

| Policy | Exit rule | Typical use |
|---|---|---|
| Report only | Keep pytest's existing result | Adoption and baselining |
| Fail violations | Block when `summary.fail > 0` | Observe gaps without blocking |
| Require all cells | Block on fail or not-exercised | Critical payment or data paths |
| Ratchet | Block only new fail/gap rows | Large existing systems |

Do not silently count `NOT EXERCISED` as success when calculating percentages.

## Read the structured payload

Call `report()` after the execution lifecycle has finished:

```python
from ordeal import report

coverage = report().get(
    "reliability_coverage",
    {"dimensions": [], "rows": [], "summary": {}},
)
```

```json
{
  "dimensions": ["operation", "fault", "property"],
  "rows": [
    {
      "operation": "create_order",
      "fault": "timeout",
      "property": "no_duplicate_charge",
      "type": "always",
      "status": "PASS",
      "hits": 8,
      "passes": 8,
      "failures": 0
    }
  ],
  "summary": {"pass": 1, "not_exercised": 0, "fail": 0, "total": 1}
}
```

Rows are ordered by operation, fault, then property and contain JSON-safe values.

## Add a strict pytest gate

In `conftest.py`, inspect the controller after workers have completed:

```python
import pytest

from ordeal import report

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        return
    coverage = report().get("reliability_coverage")
    if not coverage:
        return
    summary = coverage["summary"]
    blocked = summary["fail"] or summary["not_exercised"]
    if blocked and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
```

Use `trylast=True` so Ordeal's worker evidence has already been merged. Remove
the `not_exercised` condition for an observe-only rollout.

## Publish from another execution platform

When you control the process lifecycle, serialize the report at shutdown:

```python
import json
from pathlib import Path

from ordeal import report

def publish_reliability(path="reliability-coverage.json"):
    payload = report().get("reliability_coverage", {})
    Path(path).write_text(json.dumps(payload, indent=2) + "\n")
```

Upload that file as an artifact, send it to a dashboard, or compare it with a
checked-in expectation. Keep the three statuses separate in every downstream
system.

## Aggregation rules

- Matching cells are keyed by the exact operation, fault, and property names.
- Worker counters are summed; declarations with zero hits remain visible.
- Any `always` failure makes that cell `FAIL`.
- Any successful `sometimes` observation makes that cell `PASS`.
- Conflicting assertion types for the same cell are rejected.
