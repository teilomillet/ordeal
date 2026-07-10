---
description: Start here for long-lived Docker Compose service testing, from plain-English concepts to technical references.
---

# Compose services

`ordeal explore --runner compose` tests a running application instead of calling
one Python function at a time.

In plain English: ordeal starts your services, uses them like a client would,
breaks selected things between requests, and checks whether the application
recovers. If it finds a failure, it saves the exact sequence and tries that
sequence several more times.

## Who this is for

- An application developer with an API and one or more background workers.
- A tester who wants failures described as requests and service events.
- An SRE testing recovery before deployment.
- A library user calling `ComposeRunner` directly from Python.
- A maintainer inspecting exact trace and replay semantics.

You do not need to understand containers deeply to start. You do need a working
Compose file and one HTTP URL that ordeal can reach from the host.

## Four useful terms

- **Topology:** the group of containers described by your Compose file.
- **Fault window:** the short period when ordeal intentionally broke something.
- **Recovery request:** the clean request sent after the service is restored.
- **Trace:** the saved ordered record of lifecycle, fault, and request actions.

## Choose your path

| If you want to... | Read |
|---|---|
| Run a safe first experiment | [Quickstart](compose-quickstart.md) |
| Look up every setting and default | [Configuration](compose-configuration.md) |
| Build create/read/update workflows | [Stateful workflows](compose-stateful-workflows.md) |
| Understand exactly what each fault does | [Fault model](compose-fault-model.md) |
| Read or replay a saved failure | [Traces and replay](compose-traces.md) |
| Put the runner in CI | [CI and operations](compose-operations.md) |
| Fix an error or surprising result | [Troubleshooting](compose-troubleshooting.md) |
| Call the runner from Python | [API reference](../reference/api.md#compose-service-runner) |

## The basic loop

For each configured operation, ordeal:

1. Keeps the same Compose topology and scenario state alive.
2. Selects an eligible HTTP request using the configured seed.
3. Sometimes injects a configured worker or response fault.
4. Sends a request during the fault window when the fault model calls for it.
5. Restores or restarts the affected service.
6. Sends a clean request and validates status, JSON expectations, and captures.
7. Records the resolved request, fault, result, and state change.

This is useful for bugs such as "the API never recovers after its worker dies" or
"the next request fails after a delayed response." It is not a hypervisor and it
does not make Docker, the network, or your services deterministic.

## Smallest useful config

```toml
[compose]
base_url = "http://127.0.0.1:8080"
health_path = "/health"
services = ["api", "worker"]

[[compose.requests]]
name = "list-items"
path = "/items"
```

```bash
ordeal explore --runner compose
```

Every run saves a trace under `.ordeal/traces/`. A failure also gets repeated
replay reporting such as `attempted 5 / reproduced 3`.

Keep credentials replayable without writing them to traces by using environment
placeholders in headers or JSON bodies:

```toml
[compose.requests.headers]
Authorization = "Bearer ${SERVICE_TOKEN}"
```

Ordeal resolves `${SERVICE_TOKEN}` only at the HTTP transport boundary. Trace
serialization preserves the placeholder, redacts literal credential-shaped
fields and response authentication headers, and stores response hashes instead
of body previews. Do not put secrets in request paths or captured state.

## What ordeal promises

- The saved action and fault order is exact.
- Resolved request inputs and failure signatures are recorded.
- Replay counts exact matches; it does not call a flaky replay deterministic.
- Containers that were already active are not taken down by cleanup.
- Named volumes are not deleted.

## What it does not promise

- Deterministic process scheduling or network timing.
- Network partitions, CPU pressure, disk faults, or packet-level corruption.
- Correctness beyond your configured statuses and JSON expectations.
- A clean database between replay attempts.
- Safe use against production. Worker kill and restart are intentionally disruptive.

Start with the [Quickstart](compose-quickstart.md), then use the reference pages
only when you need their depth.
