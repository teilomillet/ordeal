---
description: Build multi-request Compose scenarios with captures, templates, prerequisites, and safe repeat behavior.
---

# Stateful request workflows

"Stateful" means a value learned from one response can shape later requests.
Ordeal keeps this scenario state in memory for the entire run.

## The three pieces

- `capture` extracts a JSON value into named state.
- `{name}` templates insert state into later strings.
- `requires` prevents an operation from being selected before state exists.

```toml
[[compose.requests]]
name = "create-item"
method = "POST"
path = "/items"
json = {name = "sample"}
expect_status = 201
capture = {item_id = "json.id"}

[[compose.requests]]
name = "read-item"
path = "/items/{item_id}"
requires = ["item_id"]
expect_status = 200
expect_json = {"json.name" = "sample"}
```

At first, only `create-item` is eligible. After a successful validated response
such as `{"id": "abc"}`, `read-item` can resolve to `/items/abc`.

## JSON paths

Paths may start with `json.` and traverse object keys or numeric list indexes:

```toml
capture = {job_id = "json.job.id", first_id = "json.items.0.id", whole_response = "json"}
```

A missing capture path is a `capture_error`. A missing expectation path is an
`unexpected_json` failure. Equality checks are exact; there is no schema or
pattern language in this runner. Do not capture authentication values; sensitive
source paths are redacted from saved action results and final state.

## Where templates work

String templates are resolved in:

- `path`
- header values
- strings nested inside `json`
- strings nested inside `expect_json`

```toml
[compose]
initial_state = {tenant = "acme", token = "test-token"}

[[compose.requests]]
name = "tenant-items"
path = "/v1/{tenant}/items"
headers = {Authorization = "Bearer {token}"}
json = {owner = "{tenant}"}
```

If a referenced value is absent, the trace records `template_error`. Use
`requires` for values that are expected to appear later.

## Initial state versus captured state

Use `initial_state` for constants, seeded identifiers, or setup performed outside
ordeal. A later capture with the same name replaces the earlier value.

Captured values are JSON values, not only strings. Formatting a list or object
into a string uses its Python string representation, so capture scalar IDs for
URLs and headers.

## Selection is intentionally simple

Each step chooses uniformly from currently eligible requests. `requires` is a
prerequisite, not an ordering language. Once `create-item` becomes eligible, it
remains eligible and may run again.

The current runner has no "run once," dependency graph, state deletion, or custom
selection weight. Use an idempotent setup endpoint, seed `initial_state`, or make
repeated operations safe when duplicates would be misleading.

## Faultable requests and side effects

A fault cycle can send a fault-window request and then repeat a clean recovery
request. GET, HEAD, and OPTIONS opt in by default. POST, PUT, PATCH, DELETE, and
other methods opt out by default.

```toml
[[compose.requests]]
name = "idempotent-upsert"
method = "PUT"
path = "/items/{item_id}"
requires = ["item_id"]
faultable = true
```

Only set `faultable = true` when repeating the operation is valid for your API.
For example, prefer an idempotency key or stable resource ID over a blind POST.

Fault-window responses are recorded but not validated or captured. State changes
only from the clean validated request. The service may still have processed the
fault-window request, which is why idempotency matters.

Related: [Configuration](compose-configuration.md),
[fault semantics](compose-fault-model.md), and
[trace request fields](compose-traces.md#what-is-recorded).
