---
description: A beginner-friendly first run of ordeal against an existing Docker Compose application.
---

# Compose quickstart

This walkthrough starts gently: first prove ordeal can reach the application,
then enable one fault. Do this on a development or disposable test environment.

## Before you begin

You need:

- Docker with the Compose plugin.
- A Compose file that already starts successfully.
- An HTTP endpoint reachable from your terminal.
- A non-production environment where restart and SIGKILL are acceptable.

Check the first two boundaries yourself:

```bash
docker compose version
docker compose up -d
curl -i http://127.0.0.1:8080/health
```

Use your real port and health path. A response below HTTP 500 counts as reachable
for ordeal's startup check; individual requests still use their own expectations.

## 1. Add a read-only first request

Create or extend `ordeal.toml`:

```toml
[compose]
file = "compose.yaml"
base_url = "http://127.0.0.1:8080"
health_path = "/health"
faults = []
steps = 5
max_time = 30

[[compose.requests]]
name = "health"
path = "/health"
expect_status = 200
```

`faults = []` is deliberate. The first run checks configuration, startup,
requests, validation, trace writing, and cleanup without breaking anything.

## 2. Run the harness

```bash
ordeal explore --runner compose
```

A successful first run looks roughly like:

```text
Exploring Compose services from .../compose.yaml...
  Actions: 8 exact, requests=5, faults=0
  Trace: .../.ordeal/traces/compose-42-....json
  No failure recorded; replay not attempted.
```

Action counts include startup, readiness, and cleanup, so they are normally
larger than request counts.

## 3. Enable a response fault

Change the Compose section:

```toml
faults = ["delay_response"]
fault_probability = 0.5
delay_seconds = 0.25
```

Run the same command again. Ordeal may delay a response at its client boundary,
record that request as an expected fault window, and then send a clean validated
request. The seed controls which configured choices ordeal makes.

## 4. Add worker restart testing

Use the exact service names from your Compose file:

```toml
services = ["api", "worker"]
faults = ["restart"]
```

Now ordeal may run `docker compose restart worker`, wait for the configured HTTP
health URL, and validate a recovery request. Add `kill` only after restart works:

```toml
faults = ["restart", "kill"]
```

`kill` sends SIGKILL, makes one request during the fault window, starts the
service again, waits, and validates a clean recovery request.

## 5. Increase depth

Once the small run is stable:

```toml
steps = 100
max_time = 300
replay_attempts = 10
```

Add real application operations using the
[stateful workflow guide](compose-stateful-workflows.md). Look up every option in
[Configuration](compose-configuration.md).

## If the first run fails

Do not increase the budget. Read the recorded failure kind and start with
[Troubleshooting](compose-troubleshooting.md). A missing Docker binary, bad health
URL, and unexpected application response require different fixes.

Next: learn every setting in [Configuration](compose-configuration.md), build
multi-request scenarios with [Stateful Workflows](compose-stateful-workflows.md),
or move a stable recovery check into [CI](compose-operations.md).
