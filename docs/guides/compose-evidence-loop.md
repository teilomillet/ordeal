---
description: Copy a complete path from one real Compose recovery failure to a portable CI guard.
---

# Turn a service failure into a permanent CI guard

This operational companion to the plain-language
[service evidence loop](../concepts/service-evidence-loop.md) takes one recovery failure to a portable CI guard.

```text
explore → coverage → exact replay → bounded finding → portable regression
        → verify --ci → workload-strength control
```

## Before you start: name the promise

Tell Ordeal how to reach the service, which container it may disrupt, and what a correct response looks like:

```toml
[compose]
file = "compose.test.yaml"
base_url = "http://127.0.0.1:8080"
services = ["worker"]
faults = ["kill"]
replay_attempts = 3

[[compose.requests]]
name = "read_order"
path = "/orders/{order_id}"
expect_status = 200
expect_json = {"json.state" = "committed"}
```

`expect_status` and `expect_json` are business promises, not just health checks. Start with safe `GET` requests;
mutating requests are not faultable by default because a recovery cycle may repeat them.

## 1. Explore the real service

```bash
ordeal explore --runner compose -c ordeal.toml
```

Ordeal keeps the topology alive, introduces allowed faults, waits for recovery, and checks the clean response.
The report contains one row per operation × fault × property:

| Status | Meaning |
|---|---|
| `PASS` | This exact combination ran and the promise held |
| `FAIL` | This exact combination ran and the promise broke |
| `NOT EXERCISED` | The combination did not run; it is still a gap |

## 2. Demand repeated, exact replay

Ordeal records the exact action order and replays those values instead of drawing a new scenario. Read the count literally:
`attempted 3 / reproduced 3` means three signatures matched. Actions are exact; external timing is not assumed deterministic.

For an existing trace, choose the count explicitly:

```bash
ordeal replay .ordeal/traces/compose-42-abc.json --attempts 10
```

## 3. Save only a bounded finding

```bash
ordeal explore --runner compose -c ordeal.toml --save-artifacts
```

Promotion requires a replay match on failure kind, message, action index, and action name. Ordeal then writes:

```text
tests/ordeal-compose-regressions/fnd_compose_<id>.json
tests/ordeal-regressions.json
```

The first file is the portable trace; the second is the shared regression manifest. Paths are repository-relative,
so the pair is not tied to GitHub Actions, another CI vendor, or the discovery machine.

Harness and configuration errors remain diagnostics, not service defects.

## 4. Prove red, then prove green

```bash
ordeal verify fnd_compose_<id> --allow-unsafe-artifacts
```

Before the fix, exit `1` proves the bound witness still catches the bug. After the fix, exit `0` requires every
configured attempt to complete cleanly. A different failure is not accepted as a fix.

Commit the portable trace and manifest together, then guard all records:

```bash
ordeal verify --ci
```

`verify --ci` is read-only and provider-neutral. It rejects hashes or paths that escape the workspace.
It needs Docker Compose and any credentials referenced by the trace.

## 5. Test that the workload can notice wrong answers

After the fixed control is clean, add a bounded budget:

```toml
[compose]
workload_mutations = 20
```

Ordeal first replays an unchanged response. If stable, it changes recorded expectations and checks the workload.
`killed` means it caught the wrong answer; `survived` means it accepted it; `inconclusive` means the control was unstable.

This changes recorded expectations, not application code. It measures only observed trace prefixes.

## Run the checked-in acceptance example

With Docker Compose available, run from the repository root:

```bash
uv run python scripts/verify_compose_evidence_loop.py \
  --output .artifacts/compose-evidence-loop.json
```

The example in `tests/fixtures/compose_e2e/` proves a real kill/restart defect. The buggy guard exits `1` with exact
replay `3/3`; the fixed guard exits `0` with clean replay `3/3`; all nine cells pass; and four wrong expectations are caught.

## Evidence boundary and deeper reference

This proves a bounded observation and post-fix control, not root cause, deterministic scheduling, or universal correctness.

Related: [configuration](compose-configuration.md), [traces and replay](compose-traces.md),
[CI and operations](compose-operations.md), and the [durable regression schema](../reference/durable-regression-schema.md).
