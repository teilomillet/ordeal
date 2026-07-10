# Compose service evidence-loop fixture

This tiny application is Ordeal's real Docker acceptance example. It exists to
prove the whole path from a recovery defect to a portable CI guard, rather than
only unit-test the Python implementation.

## The story

The service exposes `/health` and `/probe`. Ordeal sends a successful probe,
kills the service container, starts it again, waits for health, and probes once
more.

- `ORDEAL_SERVICE_VARIANT=buggy` comes back healthy but returns a degraded
  business status after restart.
- `ORDEAL_SERVICE_VARIANT=fixed` comes back healthy and returns the promised
  `status = ok` response.

That is a real recovery defect: process recovery succeeds while the business
property fails.

## What each file does

| File | Purpose |
|---|---|
| `compose.yaml` | Runs the same service as either the buggy or fixed variant |
| `service.py` | Implements health, probe, and restart-sensitive behavior |
| `ordeal.toml` | Defines requests, the kill fault, properties, and three replays |
| `tests/ordeal-regressions.json` | Portable regression manifest consumed by `verify --ci` |
| `tests/ordeal-compose-regressions/*.json` | Exact, hash-bound Compose trace |

## Run the complete proof

From the repository root, with Docker Compose available:

```bash
uv run python scripts/verify_compose_evidence_loop.py \
  --output .artifacts/compose-evidence-loop.json
uv run python scripts/verify_compose_service_matrix.py \
  --recovery-report .artifacts/compose-evidence-loop.json \
  --output .artifacts/compose-service-matrix.json
```

The script fails closed unless all of these claims hold:

1. exploration finds the degraded post-restart response;
2. the exact failure signature reproduces `3/3`;
3. the operation × fault × property report identifies the broken promise;
4. the checked-in regression makes buggy `verify --ci` exit `1`;
5. the same regression makes fixed `verify --ci` exit `0` with clean replay `3/3`;
6. all nine configured coverage cells pass on the fixed control;
7. the workload catches four deliberately wrong response expectations.

The command writes a machine-readable `ordeal.service-evidence-loop/v1` report
to the requested output path. The CI job uploads that file and Docker logs even
when the gate fails.

The matrix report adds the checked `compose_persistence` and
`compose_concurrency` systems: two-service state survives an API restart, and
eight concurrent worker calls remain valid under delay and corruption cycles.

Read [the layman mental model](../../../docs/concepts/service-evidence-loop.md)
or follow [the complete guide](../../../docs/guides/compose-evidence-loop.md).
