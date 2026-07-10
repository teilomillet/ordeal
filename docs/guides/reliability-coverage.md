---
description: >-
  Add operation by fault by property reliability coverage to Python tests with
  copyable examples, declaration patterns, naming rules, and common mistakes.
---

# Add Reliability Coverage

This guide assumes a test performs an operation and arranges a real or simulated
fault. Reliability labels record that evidence; they do not inject the fault.

## 1. Enable tracking

```bash
pytest --chaos
```

Or enable tracking in `conftest.py`:

```python
from ordeal import auto_configure

auto_configure(seed=42)
```

Without tracking, immediate assertions still raise on violations, but Ordeal
cannot accumulate the reliability matrix.

## 2. Declare the cells you expect

Declarations make missing tests visible. Put suite-wide expectations in an
autouse session fixture so the contract runs even when an individual path does
not:

```python
import pytest

from ordeal import declare

@pytest.fixture(scope="session", autouse=True)
def reliability_contract():
    declare(
        "no_duplicate_charge",
        "always",
        operation="create_order",
        fault="timeout",
    )
    declare(
        "eventual_commit",
        "sometimes",
        operation="create_order",
        fault="worker_restart",
    )
```

A declared cell starts as `NOT EXERCISED`. An assertion observation changes its
status to `PASS` or `FAIL` according to that assertion type.

## 3. Record what the test actually checked

```python
from ordeal import always, sometimes

def test_timeout_does_not_charge_twice(timeout_payment_gateway):
    result = create_order("order-123")
    always(
        result.charge_count == 1,
        "no_duplicate_charge",
        operation="create_order",
        fault="timeout",
    )

def test_restart_eventually_commits(restarted_worker):
    result = wait_for_order("order-123")
    sometimes(
        result.committed,
        "eventual_commit",
        operation="create_order",
        fault="worker_restart",
    )
```

Here, `timeout_payment_gateway` and `restarted_worker` stand for fixtures that
really create those conditions. The strings only name the evidence.

## 4. Read the result

```text
operation × fault × property
create_order × timeout × no_duplicate_charge     PASS
create_order × worker_restart × eventual_commit  PASS

2 PASS, 0 NOT EXERCISED, 0 FAIL
```

## Choose the right assertion

| Promise | Use |
|---|---|
| Must hold on every observation | `always(condition, name, ...)` |
| Must become true at least once | `sometimes(condition, name, ...)` |
| A recovery path must run | `reachable(name, ...)` |
| Reaching a path is itself a bug | `unreachable(name, ...)` |

For a positive matrix result about something that did *not* happen, prefer an
evaluated `always(not bad_thing, ...)` predicate. An uncalled `unreachable()`
has no observation and therefore cannot produce `PASS`.

## Naming rules that age well

- Use stable business operations: `create_order`, not `test_foo`.
- Name the injected behavior: `timeout`, `disk_full`, `stale_response`.
- Name the promise positively: `balance_conserved`, `eventual_commit`.
- Reuse the exact same three strings in `declare()` and the assertion.
- Split cells when different fault variants deserve separate conclusions.

## Common mistakes

- Supplying only `operation` or only `fault` raises `ValueError`.
- Empty operation or fault names are rejected.
- A misspelled label creates a different cell instead of satisfying the
  declared one.
- A declaration inside a skipped test never runs. Put suite-wide expectations
  in a session fixture.
- Hard-coding a fault label in a run where that fault may be inactive produces
  misleading evidence.

Next: [gate and export the matrix](reliability-coverage-ci.md), or see
[troubleshooting](../troubleshooting.md#the-reliability-coverage-matrix-is-missing).
