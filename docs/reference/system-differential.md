---
title: System Differential Reference
description: Exact Python API, report fields, and measurement boundaries.
---

# System differential reference

## Entry point

```python
diff(
    old_factory,
    new_factory,
    *,
    sequence: Sequence[Operation | FaultEvent],
    state: Callable[[Any], Any] | None = None,
    side_effects: Callable[[Any], Any] | None = None,
    apply_fault: Callable[[Any, FaultEvent], None] | None = None,
    performance: PerformanceBudget | None = None,
    minimize: bool = True,
    replay_attempts: int = 2,
    compare: Callable[[Any, Any], bool] | None = None,
    normalize: Callable[[Any], Any] | None = None,
    rtol: float | None = None,
    atol: float | None = None,
) -> SystemDiffResult
```

Both factories must be callable without arguments and return fresh systems.
Providing `sequence=` selects system mode on the existing `diff()` function.

## Events

```python
Operation(name, args=(), kwargs={})
FaultEvent(name, action="activate", parameters={})
```

All event data is deep-copied for each version. Without `apply_fault=`, each
system must expose `apply_fault(event)`.

## Semantic checks

| Contract | Default observation |
|---|---|
| Interface | Public static and dynamic names plus callable signatures |
| Outcome | Return comparison or exact exception type and message |
| State | Deep copy of public instance attributes |
| Side effects | Not checked until `side_effects=` is supplied |
| Recovery | Operations after `deactivate`, `recover`, `restart`, or `clear` |

`compare`, `normalize`, `rtol`, and `atol` apply to returned values. State and
side-effect observations use Ordeal's canonical exact evidence comparison.

## `SystemDiffResult`

| Field | Meaning |
|---|---|
| `status` | `divergent`, `no_divergence_observed`, or `inconclusive` |
| `interface` | Export and signature parity report |
| `sequence` | Minimized shared event stream |
| `steps` | Per-event outcomes, state, effects, and recovery phase |
| `mismatches` | Direct differences with kind, event, and both observations |
| `original_length` | Event count before minimization |
| `minimized_length` | Event count after minimization |
| `fault_schedule` | Fault-only view of the minimized sequence |
| `fault_schedule_replayed` | Shared schedule was applied to both versions |
| `recovery_parity` | `True`, `False`, or `None` when not measured |
| `replay_attempts` | Exact final-sequence reruns requested |
| `replay_matches` | Reruns reproducing the exact first mismatch |
| `replay_verified` | Every requested replay matched, or `None` without mismatch |
| `performance` | Separate `PerformanceResult`, or `None` |

`result.equivalent` is `False` after a divergence and otherwise `None`; sampled
agreement is not a universal equivalence proof.

## Step and mismatch records

Each `StepComparison` exposes `event`, both outcomes, `outcome_match`, optional
state/effect observations and matches, `recovery_phase`, and combined `matches`.

Each `SystemMismatch` has `kind` (`outcome`, `state`, or `side_effects`), step
index, event, and `observed_a`/`observed_b`.

## Performance

```python
PerformanceBudget(
    max_slowdown=None,
    max_candidate_seconds=None,
    samples=5,
    warmup=1,
)
```

At least one limit is required. `PerformanceResult` contains every baseline and
candidate sample, both medians, slowdown, `within_budget`, and the budget.

The original sequence is measured. Factory construction, event copying, state
probes, side-effect probes, comparison, minimization, and replay are outside the
timed region. A budget failure never changes semantic `status`.

Start with the [mental model](../concepts/system-differential.md), follow the
[first run](../guides/system-differential.md), or use the
[troubleshooting guide](../guides/system-differential-troubleshooting.md).
