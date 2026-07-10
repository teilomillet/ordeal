---
description: Diagnose Docker, readiness, request, state, replay, and cleanup problems in the Compose runner.
---

# Compose troubleshooting

Start with the failure kind in the trace or console. It identifies which boundary
failed; increasing the exploration budget rarely fixes configuration problems.

## Docker was not found

Check:

```bash
docker --version
docker compose version
```

Ordeal calls the argv form `docker compose`; a standalone legacy
`docker-compose` executable is not used.

## Compose command failed

Run the reported argv manually from the same workspace. Common causes are a wrong
`file`, invalid `project_name`, unknown service name, daemon access failure, or a
container that cannot start.

Relative Compose paths are resolved from the directory containing the selected
`ordeal.toml`, not necessarily the shell's current directory.

## Readiness timeout

Check the URL from the host:

```bash
curl -i http://127.0.0.1:8080/health
docker compose ps
docker compose logs --tail 200
```

Ordeal accepts any health status below 500. If an API is reachable before its
worker is ready, make `health_path` represent the dependency you need or increase
`startup_timeout` for genuinely slow startup.

## Unexpected status

The default expectation is any 2xx response. Set one integer or a list when the
operation legitimately returns something else:

```toml
expect_status = [200, 202, 204]
```

The fault-window request is not validated; this failure comes from a clean normal
or recovery request.

## Invalid or unexpected JSON

`expect_json` and `capture` require valid JSON. Inspect the response hashes in
the trace, then reproduce the request with curl. Confirm content type,
authentication, path, and whether the API returns an empty body for that status.

JSON paths use object keys and numeric list indexes, for example
`json.items.0.id`. They do not support JSONPath operators or wildcards.

## Template or capture error

Add every late-bound value to `requires`, or seed it in `initial_state`:

```toml
[compose]
initial_state = {tenant = "acme"}

[[compose.requests]]
path = "/items/{item_id}"
requires = ["item_id"]
```

Confirm that the earlier response actually contains the configured capture path.

## No request is eligible

Every request currently requires missing state. Provide at least one bootstrap
request with no requirements, or provide the required values in `initial_state`.

## No fault actions appear

Check all three gates:

- `faults` is not empty.
- The selected request has `faultable = true`.
- `fault_probability` is greater than zero.

With a small step count, probability may simply select no fault. Set
`fault_probability = 1.0` temporarily to verify wiring.

## Duplicate writes or conflicts

A faultable operation can run once in the fault window and again for recovery.
POST and other mutating methods default to non-faultable for this reason. Remove
`faultable = true`, add an idempotency key, or use a stable PUT-style operation.

## Replay reproduces zero times

This is a valid result. The exact signature did not recur. Check persistent
database/queue state, external dependencies, timing, and whether the original
failure depended on one-time startup. Preserve the trace as evidence; do not call
the issue deterministic.

## Containers are still running

If they were active before ordeal, cleanup intentionally leaves them active. A
new topology is also left active when `keep_running = true`. Compose command
failure can interrupt cleanup; inspect the trace and run the appropriate project
cleanup manually. Ordeal never removes volumes.

## Compose replay rejects shrink or ablate

That is intentional. `--shrink`, `--ablate`, and `--output` apply to Python
state-machine traces. Use `--attempts N` for Compose traces.

Return to the [Compose overview](compose-runner.md), or use the
[Configuration reference](compose-configuration.md) and
[Trace reference](compose-traces.md) for exact field semantics.
