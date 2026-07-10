---
description: Complete ordeal Compose configuration schema, defaults, validation, and path rules.
---

# Compose configuration

The runner reads `[compose]` and zero or more `[[compose.requests]]` tables from
the same `ordeal.toml` used by other commands.

## `[compose]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `base_url` | string | required | Absolute host-reachable `http://` or `https://` URL |
| `file` | string | `compose.yaml` | Compose file, relative to `ordeal.toml` |
| `project_name` | string/null | null | Passed to `docker compose --project-name` |
| `health_path` | string | `/` | Relative path or absolute URL polled after startup/recovery |
| `services` | list[string] | `[]` | Services eligible for `kill` or `restart` |
| `requests` | array of tables | one `GET /` | Loaded from `[[compose.requests]]` entries |
| `initial_state` | table | `{}` | Values available to request templates at step zero |
| `max_time` | number | `60` | Maximum exploration wall time in seconds |
| `steps` | integer | `50` | Maximum request-selection iterations |
| `seed` | integer | `42` | Seed for request, service, and fault selection |
| `fault_probability` | number | `0.3` | Chance of a fault cycle for a faultable request |
| `faults` | list[string] | conditional | Enabled fault names; see below |
| `delay_seconds` | number | `0.5` | Harness-boundary response delay |
| `request_timeout` | number | `5` | Timeout passed to each HTTP request |
| `startup_timeout` | number | `30` | Readiness polling budget |
| `replay_attempts` | integer | `3` | Immediate attempts after a failure |
| `workload_mutations` | integer | `0` | Replay clean prefixes with up to N mutated response oracles |
| `trace_dir` | string | `.ordeal/traces` | Output directory, relative to `ordeal.toml` |
| `keep_running` | boolean | `false` | Leave a topology started by ordeal running |

`base_url`, timeouts, probability, step count, and replay count are validated.
Unknown keys fail closed with a list of valid keys.

`workload_mutations = 0` disables the extra replays. A positive budget mutates
only response expectations whose unmodified trace prefix first replays cleanly;
it does not mutate application source inside containers.

If `services` is empty, `faults` defaults to `delay_response` and
`corrupt_response`. If services are present, it also defaults to `kill` and
`restart`. Set `faults = []` for an intentionally fault-free run.
Those four names are the complete currently supported Compose fault set.

## `[[compose.requests]]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | `request-N` | Unique trace label |
| `method` | string | `GET` | Alphabetic HTTP method, normalized uppercase |
| `path` | string | `/` | Relative path or absolute HTTP(S) URL |
| `headers` | table | `{}` | Request headers; keys and values are coerced to strings |
| `json` | TOML value | none | JSON-encoded request body |
| `expect_status` | int/list[int] | any 2xx | Accepted status code or codes |
| `expect_json` | table | `{}` | Dotted JSON paths and exact expected values |
| `capture` | table | `{}` | State name to dotted JSON path |
| `requires` | list[string] | `[]` | State names required before selection |
| `faultable` | boolean | method-dependent | Permit fault-window plus recovery execution |

GET, HEAD, and OPTIONS default to `faultable = true`; other methods default to
false. This avoids automatically repeating a potentially successful POST.

With no request tables, ordeal creates one `GET /` request named `root`.

## Complete example

```toml
[compose]
file = "deploy/compose.test.yml"
project_name = "ordeal-demo"
base_url = "http://127.0.0.1:8080"
health_path = "/ready"
services = ["api", "worker"]
initial_state = {tenant = "acme"}
steps = 100
max_time = 300
seed = 42
fault_probability = 0.25
faults = ["kill", "restart", "delay_response", "corrupt_response"]
delay_seconds = 0.5
request_timeout = 3
startup_timeout = 45
replay_attempts = 10
workload_mutations = 20
trace_dir = ".ordeal/service-traces"
keep_running = false

[[compose.requests]]
name = "list"
path = "/{tenant}/items"
headers = {Accept = "application/json"}
expect_status = [200, 206]
expect_json = {"json.ready" = true}
capture = {first_id = "json.items.0.id"}
```

CLI `--seed`, `--max-time`, and `--replay-attempts` override the matching
Compose values. `--workers` must be omitted or set to `1` because one runner owns
one long-lived topology.

Related: [Quickstart](compose-quickstart.md),
[stateful workflows](compose-stateful-workflows.md), and
[API reference](../reference/api.md#composeconfig).
