---
description: Read, share, secure, and repeatedly replay exact Compose action traces.
---

# Compose traces and replay

Every run writes a JSON trace, whether it passes or fails:

```text
.ordeal/traces/compose-<seed>-<trace-hash>.json
```

The trace is the durable record. Console output is only a summary.
For failures, the filename hash is computed from the discovery trace before the
immediate replay summary is attached to the saved payload.

## What is recorded

Top-level fields include:

- `runner: "compose"` and `schema_version: 1`
- the effective Compose configuration used by replay
- the seed and total duration
- ordered `actions`
- `final_state`
- a failure and failure signature, when present
- immediate replay counts, when a failure was replayed

Each action records its index, kind, name, state-resolved parameters, observed
result, and timestamp offset. Credential environment placeholders remain
unresolved until the HTTP transport boundary. Kinds are `lifecycle`, `fault`,
and `request`.

Request parameters contain the URL, redacted headers and JSON body,
expectations, capture map, validation mode, and response fault. Results contain
status, elapsed time, redacted response headers, full-body SHA-256, and
original-body SHA-256. Validated actions also contain redaction-safe
`property_results` used for operation × fault × property coverage. Response
body bytes and previews are not stored.

## Failure kinds

| Kind | Meaning |
|---|---|
| `compose_command` | Docker/Compose command missing, timed out, or failed |
| `readiness_timeout` | Health URL did not return a status below 500 in time |
| `request_error` | A validated HTTP request produced no response |
| `unexpected_status` | Status did not match configured codes or default 2xx |
| `invalid_json` | JSON was required but response parsing failed |
| `unexpected_json` | Expected JSON path was absent or unequal |
| `capture_error` | Capture JSON path was absent |
| `template_error` | Request referenced unavailable state |
| `scenario_state` | No configured request was eligible |
| `trace_format` | Replay encountered an unknown recorded action |

## Exact failure signature

The signature is SHA-256 over four exact values:

- failure kind
- message
- action index
- action name

A replay counts as reproduced only when its observed signature equals the saved
signature. A similar 500 on a different action is not an exact reproduction.

## Repeated replay

Failures are replayed immediately using `replay_attempts`. You can later choose a
larger sample:

```bash
ordeal replay .ordeal/traces/compose-42-abcd.json --attempts 20
ordeal replay .ordeal/traces/compose-42-abcd.json --attempts 20 --json
```

`attempted 20 / reproduced 7` means the exact sequence was run twenty times and
the exact failure signature appeared seven times. It does not mean the other
thirteen runs were equivalent or that the root cause is known.

## Exit codes

- Compose exploration returns `0` with no recorded failure and `1` with one.
- Compose replay returns `1` if at least one attempt reproduces, otherwise `0`.
- Invalid options, unreadable traces, and malformed trace data return `2`.

Compose traces do not support `--shrink`, `--ablate`, or `--output`. Those options
belong to Python state-machine traces; real service actions can have persistent
side effects that make automatic deletion of steps misleading.

## Durable post-fix control

`explore --runner compose --save-artifacts` promotes only a failure with at
least one exact replay match. It writes a portable bound copy under `tests/`.
After a fix, `ordeal verify <finding-id> --allow-unsafe-artifacts` accepts the
trace only when every replay attempt completes without any failure. See the
[Compose evidence loop](compose-evidence-loop.md).

## Confidentiality

Use `${ENV_NAME}` values for credentials in headers and JSON bodies. Ordeal
preserves those templates in traces, resolves them only for transport, and
redacts literal credential-shaped fields. Mixed literal-plus-placeholder secret
values are not replay-safe after redaction, so put the complete secret in the
environment variable. Response bodies are represented only by hashes.

Redaction is a safeguard, not a data-loss-prevention proof. Do not put secrets
in URLs, arbitrary non-credential fields, or captured state, and review traces
before sharing them externally.

## Honest boundary

The action order and non-secret inputs are exact. Credential env templates stay
replayable, while redacted literals and sensitive captures intentionally do not.
Compose volumes are preserved, and Docker, network, database, queue, and
application timing remain real. Replay frequency is evidence about
reproducibility under those conditions, not deterministic proof.

Related: [Fault Model](compose-fault-model.md),
[CI artifact handling](compose-operations.md), and
[Troubleshooting](compose-troubleshooting.md).
