---
description: Exact semantics and limitations of worker and response faults in the Compose runner.
---

# Compose fault model

A fault is not just a label. Each fault expands into a fixed action cycle. This
page states exactly what ordeal does so results are not over-interpreted.

## How a fault is chosen

For each step, ordeal chooses one eligible request. If that request is faultable,
the configured fault list is non-empty, and the seeded probability check passes,
ordeal chooses one enabled fault. For process faults it also chooses one service
from `services`.

The seed controls these choices. It does not control Docker scheduling, network
timing, application randomness, databases, queues, or external dependencies.

## `kill`

Exact cycle:

1. Run `docker compose kill -s SIGKILL <service>`.
2. Send the chosen request with validation disabled.
3. Run `docker compose up -d <service>`.
4. Poll the configured HTTP health URL.
5. Repeat the request with normal validation and state capture.

The first request answers "what happened during the outage?" The second answers
"did the application recover?" Only the recovery request can fail validation.
SIGKILL means a forced stop without graceful application shutdown handlers.

## `restart`

Exact cycle:

1. Run `docker compose restart <service>`.
2. Poll the configured HTTP health URL.
3. Send and validate the selected request.

There is no request while `restart` is in progress because the Compose command is
synchronous from the harness's point of view.

## `delay_response`

Exact cycle:

1. Arm a response-boundary delay.
2. Send the request and receive its response from the network.
3. Sleep `delay_seconds` before returning that response to the harness logic.
4. Record the fault-window response without validation or capture.
5. Send and validate a clean recovery request.

This models a slow response as observed by ordeal. It does not delay packets
inside Docker, slow your server, or force `urlopen` itself to time out.

## `corrupt_response`

Exact cycle:

1. Arm response corruption.
2. Send the request and receive its response.
3. Flip every bit in the first response byte; an empty body becomes one null byte.
4. Record response hashes without validation or capture.
5. Send and validate a clean recovery request.

This corrupts the bytes seen by ordeal's client boundary. It does not alter a
response inside the Docker network or feed corrupt bytes to another service.

## Expected symptom versus finding

An error during the intentional fault window is recorded as expected evidence.
It is not automatically a bug. A clean request that fails after recovery is a
finding because it violates a configured status, JSON expectation, or capture.

This distinction prevents ordeal from reporting "SIGKILL caused connection
failure" as a discovery; that is the injected condition, not the recovery defect.

## Readiness semantics

Readiness polls `GET base_url + health_path` until it gets any status below 500 or
the startup timeout expires. HTTP 404 proves the process is reachable, but it may
not prove a background worker is ready. Point `health_path` at an endpoint that
reflects the dependencies you care about.

## Lifecycle and cleanup

Ordeal checks `docker compose ps -q` before `up -d`. If no containers were active,
it owns cleanup and runs `down --remove-orphans` unless `keep_running = true`.
If containers were already active, it leaves the topology running. It attempts to
start any service left killed by an interrupted run. It never deletes volumes.

## Outside the current model

There is no network partition, packet loss, DNS fault, clock skew, CPU/memory
pressure, disk corruption, multi-client concurrency, or hypervisor-level replay.
Use the Python fault APIs or external infrastructure for those boundaries.

Related: [Stateful Workflows](compose-stateful-workflows.md),
[Traces and Replay](compose-traces.md), and
[CI and Operations](compose-operations.md).
