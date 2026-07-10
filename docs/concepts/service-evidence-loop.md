---
description: Understand how Ordeal turns one real service failure into a portable CI guard.
---

# From “it broke once” to “it cannot come back”

A service can look healthy after a restart while still being wrong. The process
is running and `/health` is green, but an order is missing, a job is stuck, or a
read returns stale data. A health check alone cannot catch that recovery defect.

Ordeal turns one such failure into evidence that a person can review, a machine
can replay, and any CI provider can guard.

## The idea in one minute

```text
explore → see what was covered → replay the exact actions → save the finding
        → commit the regression → verify the fix in CI → test the test itself
```

1. **Explore:** use the real Compose application and introduce allowed faults.
2. **See coverage:** report which request, fault, and promise combinations ran.
3. **Replay:** repeat the recorded actions and count matching failures.
4. **Save:** keep only a replay-backed, narrowly described finding.
5. **Commit:** store a portable trace and manifest with the code.
6. **Verify:** require the old version to fail and the fixed version to pass.
7. **Test the test:** deliberately alter expected responses and prove the
   workload notices.

That last step matters. A green test is weak evidence if it would also stay
green when the answer was wrong.

## A concrete recovery defect

The checked-in example at `tests/fixtures/compose_e2e/` has two variants:

- `buggy` returns a degraded business status after its worker is killed and
  started again, even though HTTP and health checks recover;
- `fixed` returns the promised status after the same sequence.

The failure is not summarized as “restart was flaky.” Its record says which
request failed, after which fault, which JSON promise was broken, and where in
the action sequence that happened. Three matching replays are reported as
`attempted 3 / reproduced 3`; the count stays explicit because real service
timing is never silently called deterministic.

## The small vocabulary

| Term | Plain meaning |
|---|---|
| **Operation** | Something a user or service does, such as reading an order |
| **Fault** | The breakage introduced, such as killing a worker |
| **Property** | The promise that must remain true, such as `status = committed` |
| **Trace** | The exact ordered receipt of requests, faults, and lifecycle actions |
| **Bounded finding** | A claim limited to the failure that replay actually matched |
| **Portable regression** | A repository-relative trace plus a provider-neutral manifest |
| **Workload-strength control** | A check that the workload rejects deliberately wrong answers |

## How to read the result

- `PASS` means that exact operation × fault × property cell ran and held.
- `FAIL` means Ordeal observed that exact promise break.
- `NOT EXERCISED` means the combination never ran. It is a gap, not a pass.
- `reproduced 3/3` means all three attempts matched the recorded failure
  signature. It does not promise identical wall-clock timing.
- `protective_within_measured_scope` means every measured wrong-answer change
  was caught and no configured coverage cell was missing or failing.

## What the evidence does not claim

This loop proves a real observation, exact recorded actions, bounded replay,
and a post-fix control. It does not prove the root cause, deterministic
scheduling, every possible failure, or universal service correctness.

Next, follow the copyable [service evidence loop](../guides/compose-evidence-loop.md).
Use [Compose configuration](../guides/compose-configuration.md) for every setting,
[traces and replay](../guides/compose-traces.md) for the machine record, and
[CI and operations](../guides/compose-operations.md) for safe automation.
