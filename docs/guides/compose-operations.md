---
description: Run the Compose service harness safely in CI and shared development environments.
---

# Compose CI and operations

The runner is intentionally disruptive. Treat it like a fault-injection job, not
an ordinary production smoke test.

## Recommended CI config

Keep a focused file such as `ordeal.compose.toml`:

```toml
[compose]
file = "compose.test.yaml"
project_name = "ordeal-ci"
base_url = "http://127.0.0.1:8080"
health_path = "/ready"
services = ["api", "worker"]
steps = 100
max_time = 180
seed = 42
fault_probability = 0.25
faults = ["kill", "restart", "delay_response", "corrupt_response"]
replay_attempts = 10
trace_dir = ".ordeal/compose-traces"
keep_running = false

[[compose.requests]]
name = "health"
path = "/health"
expect_status = 200
```

Run it with:

```bash
ordeal explore --runner compose -c ordeal.compose.toml
```

## Example GitHub Actions step

Use a runner where Docker Compose is available and ordeal is already installed:

```yaml
- name: Explore service recovery
  run: uv run ordeal explore --runner compose -c ordeal.compose.toml

- name: Upload exact service traces
  if: always()
  uses: actions/upload-artifact@v7
  with:
    name: ordeal-compose-traces
    path: .ordeal/compose-traces/
```

Upload traces with `if: always()` because passing runs also provide useful action
evidence, and a failed command would otherwise skip the artifact step.

## Budgets and repeatability

- `steps` bounds request-selection iterations.
- `max_time` bounds exploration wall time, though an in-flight external command
  or HTTP timeout can make shutdown occur slightly later.
- `request_timeout` limits each HTTP request.
- `startup_timeout` limits each readiness phase.
- `replay_attempts` multiplies the cost of a failure.

Start with five to ten steps and one fault. Increase budgets only after startup,
cleanup, and trace upload are proven.

Keep the seed fixed in CI to make selected actions comparable. Rotate or matrix
seeds in a scheduled job when you want broader selection coverage.

## Project isolation

Use a dedicated Compose file and `project_name` per concurrent job. Two jobs with
the same project can restart or kill each other's services. Avoid pointing
`base_url` at a shared staging environment unless disruption is explicitly owned.

Ordeal does not delete volumes. This protects data by default but means database
state can survive between a discovery and its replay attempts. If your test
requires a clean database, create it through your CI setup or application API.
Do not add automatic volume deletion without reviewing the data-loss risk.

## Cleanup behavior

- A topology already active before the run is left active afterward.
- A topology started by ordeal is taken down unless `keep_running = true`.
- Killed services are restarted during normal recovery and best-effort cleanup.
- Compose command failures can prevent cleanup; keep an outer CI cleanup step for
  disposable projects if that matters to resource usage.

## Interpreting a red job

Exit `1` means a failure was recorded, not necessarily that it reproduced every
time. Read the failure kind, `attempted/reproduced` counts, and trace before
classifying it. Exit `2` means configuration, trace, or option handling failed.

For flaky exact reproduction, preserve service logs, queue/database diagnostics,
and the trace from the same job. The trace alone records actions, not internal
service logs or root cause.

Related: [Trace interpretation](compose-traces.md),
[Fault Model](compose-fault-model.md), and
[Troubleshooting](compose-troubleshooting.md).
