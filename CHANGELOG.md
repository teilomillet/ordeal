# Changelog

## 0.1.0

Initial release.

- **ChaosTest** — stateful chaos testing with auto-injected nemesis, swarm mode
- **Faults** — io, numerical, timing fault primitives + PatchFault/LambdaFault base
- **Assertions** — always/sometimes/reachable/unreachable (Antithesis model)
- **Invariants** — composable named checks (no_nan & bounded(0,1))
- **Buggify** — FoundationDB-style inline fault injection
- **QuickCheck** — @quickcheck decorator with boundary-biased generation
- **Simulate** — Clock and FileSystem for no-mock testing
- **Mutations** — AST-based mutation testing (arithmetic, comparison, negate, return_none)
- **Explorer** — coverage-guided exploration with checkpointing, energy scheduling, shrinking
- **Traces** — JSON recording, replay, delta-debugging shrinking
- **CLI** — `ordeal explore` and `ordeal replay`
- **Config** — `ordeal.toml` driven configuration
- **Plugin** — pytest integration (--chaos, --chaos-seed, @pytest.mark.chaos)
- **Integrations** — Atheris (coverage-guided fuzzing), Schemathesis (API chaos)
