---
title: Object Harnesses and Stateful Replay
description: How scan discovers, validates, runs, and exactly replays bound instance methods.
---

# Object Harnesses and Stateful Replay

## Why a harness exists

A free function can be called directly. A bound method may need a constructed
object, credentials replaced by fakes, prepared state, collaborators, and
cleanup. The harness describes that missing lifecycle.

```text
factory → setup → scenarios → state injection → method → teardown
```

For each `scan_module` invocation, ordeal builds a fresh object for each input
attempt. `harness = "stateful"` additionally tells `chaos_for` to keep one object
across state-machine rule calls. A saved scan regression reconstructs one exact
failing invocation; it is not a multi-step trace.

## Inspect automatic discovery

```bash
ordeal scan myapp.envs --list-targets
ordeal scan myapp.envs --list-targets --json
```

Ordeal mines nearby pytest files, fixtures, support/factory modules, target
source collaborator attributes, and docs. Hints carry evidence, confidence,
structural signals, and a suggested `[[objects]]` key. Constructor evidence
outranks a merely similar name.

Automatic hooks are dry-run before a method becomes a promoted finding. If the
factory fails or returns the wrong class, ordeal reports the target as blocked
or demotes its crash instead of blaming product code.

## Configure the lifecycle

```toml
[[objects]]
target = "myapp.envs:ComposableEnv"
factory = "tests.support:make_env"
setup = "tests.support:prepare_env"
state_factory = "tests.support:make_state"
scenarios = ["sandbox", "subprocess"]
teardown = "tests.support:close_env"
harness = "stateful"
methods = ["rollout"]
```

| Hook | Responsibility |
|---|---|
| `factory` | Return an instance of the configured class |
| `setup` | Prepare or replace that instance before scenarios |
| `scenarios` | Install collaborator behavior; entries run in order |
| `state_factory` | Supply an omitted runtime parameter such as `state` |
| `teardown` | Run after the method, including failure paths |
| `harness` | `fresh` for ordinary calls; `stateful` for shared `chaos_for` state |

Hooks may be sync or async. A setup/scenario hook may mutate in place or return
a replacement instance. Built-in scenario packs include `sandbox`,
`subprocess`, `http`, `state_store`, and `upload_download`.

## What exact harness replay requires

For a bound-method regression, ordeal records:

```text
owner, method, factory, setup, scenarios, state_factory, state_param,
teardown, harness mode, failing keyword arguments
```

Every required callable must have a stable symbol reference. Supported forms
are importable `module:qualname` symbols and source-file `path.py:qualname`
symbols. Lambdas and functions nested inside another function contain
`<lambda>` or `<locals>` and cannot be imported later.

When all references resolve, `minimal_reproduction.harness_replay_supported` is
`true`. The generated pytest test rebuilds the same wrapper and invokes it with
the exact witness. When any reference is unstable, ordeal leaves that flag
`false` and skips the regression instead of writing invalid Python.

## Read lifecycle failures

Proof bundles may include:

- `lifecycle.failure_stage`: factory, probe, setup, scenario, prepare, invoke,
  or teardown.
- `lifecycle.teardown_called` and `teardown_error`.
- `minimal_reproduction.harness`: the resolvable hook references.
- `verdict.demotion_reason`: why a harness failure stayed exploratory.

Use `.ordeal/findings/<module>.replay.md` for the runnable snippet and
`.proofs.json` for the structured form.

## Keep discovery fast and relevant

Ordeal prunes generated/tool trees before searching: `.ordeal`, virtualenvs,
package caches, `node_modules`, and root-level generated `site`. It still reads
relevant project tests, support modules, docs, and source.

For the shortest path, select one method and keep its support helpers near the
target tests:

```bash
ordeal scan myapp.envs --target ComposableEnv.rollout
```

See [Configuration](configuration.md) for all TOML keys and
[Scan Troubleshooting](scan-troubleshooting.md) for discovery failures.
