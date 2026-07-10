---
description: >-
  Understand operation by fault by property reliability coverage, what PASS,
  NOT EXERCISED, and FAIL mean, and why it is stronger than line coverage.
---

# Reliability Coverage

Line coverage answers **“did this line run?”** Reliability coverage answers a
more useful question: **“which promise did we check, during which operation,
while which failure was happening?”**

!!! quote "In plain English"
    A fire drill is not complete because someone walked through the hallway.
    You want to know which evacuation route was tested, which emergency was
    simulated, and whether everyone got out safely. Reliability coverage is
    that drill record for software.

## The three dimensions

Every row describes one concrete claim:

| Dimension | Meaning | Example |
|---|---|---|
| **Operation** | What the system was doing | `create_order` |
| **Fault** | What went wrong during it | `timeout` |
| **Property** | What still had to be true | `no_duplicate_charge` |

Together they form an **operation × fault × property** cell:

```text
create_order × timeout × no_duplicate_charge
```

That is more precise than “the order code has 92% line coverage.” A line can
run without a timeout, without checking duplicate charges, or without checking
anything at all.

## Reading the matrix

```text
operation × fault × property
create_order × timeout × no_duplicate_charge     PASS
create_order × worker_restart × eventual_commit  NOT EXERCISED
refund × stale_response × balance_conserved      FAIL
```

| Status | What Ordeal observed | What you may conclude |
|---|---|---|
| `PASS` | At least one observation, with the property satisfied | This tested cell held in the observed runs |
| `NOT EXERCISED` | An expected cell was declared, but had zero observations | The test suite has a known reliability gap |
| `FAIL` | At least one observation violated the property semantics | Ordeal witnessed a reliability problem |

## What the statuses do not mean

- `PASS` is not a proof that the system can never fail. It is bounded evidence
  for the inputs, schedules, faults, runtime, and assertions actually used.
- `NOT EXERCISED` is not a failure witness and is never silently promoted to a
  pass. It says the intended test did not happen.
- `FAIL` identifies the cell that broke. The underlying exception or shrunk
  trace provides the reproduction evidence.
- No row means no claim. Ordeal cannot report an expected combination unless
  you declare it or record an observation for it.

This distinction is deliberate. Honest “we did not test this” evidence is more
useful than a green dashboard built from missing data.

## How assertion semantics apply

The matrix preserves the normal assertion rules:

| Assertion | A cell passes when... |
|---|---|
| `always` | It was observed and never false |
| `sometimes` | It was observed and true at least once |
| `reachable` | The marked path was reached |
| `unreachable` | Calling it records a failure |

An uncalled `unreachable()` cannot prove that a scenario ran. If you need an
explicit matrix pass for “data loss never happened,” evaluate that condition
with `always(not data_lost, "no_data_loss", ...)`.

## Labels describe evidence; they do not create it

Adding `fault="timeout"` does **not** inject a timeout. Your test or Ordeal fault
harness must create the timeout. The label records what the test actually
arranged. Do not label a randomized run `timeout` when that fault may have been
inactive.

Similarly, `declare()` defines an expected cell; it does not execute the
operation. This is exactly why a declaration can become `NOT EXERCISED`.

## Where to go next

- [Add reliability coverage](../guides/reliability-coverage.md) — practical,
  copyable test patterns
- [CI and external platforms](../guides/reliability-coverage-ci.md) — gating,
  JSON, pytest-xdist, and lifecycle integration
- [Property assertions](property-assertions.md) — full assertion semantics
- [API reference](../reference/api.md#assertions) — signatures and payload fields
