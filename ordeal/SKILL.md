---
name: ordeal
description: Use when working with Python reliability through ordeal: finding missed bugs, assessing existing tests, bootstrapping starter tests, saving regressions or findings, verifying saved findings, or exploring code that imports `ordeal`.
user_invocable: true
---

# ordeal

Use this file as a capability map, not a prescribed workflow.

High-signal entrypoints:
- `ordeal scan <target>`
- `ordeal audit <target>`
- `ordeal mutate <target>`
- `ordeal init [target]`
- `ordeal verify <finding-id>`

Read-first CLI:
- `ordeal scan <target>`: unified bug discovery; `--save-artifacts` writes report, JSON bundle, regressions, and index entries
- `ordeal mine <target>`: discover suspicious properties
- `ordeal audit <target>`: measure gaps in existing tests
- `ordeal verify <finding-id>`: re-run one saved regression from `.ordeal/findings/index.json`
- `ordeal explore -c ordeal.toml`: deeper coverage-guided exploration

Write-producing CLI:
- `ordeal init [target]`: starter tests plus `ordeal.toml`; `--install-skill`, `--close-gaps`, and `--ci` add extra writes
- `ordeal mutate <target> --generate-stubs PATH`: writes suggested test stubs
- `ordeal replay trace.json --output PATH`: writes replay artifacts

Artifacts:
- `.ordeal/findings/`: Markdown reports, JSON bundles, and `index.json`
- `tests/test_ordeal_regressions.py`: default regression path
- `ordeal.toml`: explorer configuration

Discovery:
- `ordeal --help`
- `ordeal <command> --help`
- `from ordeal import catalog; catalog()`
- https://docs.byordeal.com/

Machine surfaces:
- `--json` prints stable agent-facing envelopes
- saved scan bundles include stable `finding_id`
