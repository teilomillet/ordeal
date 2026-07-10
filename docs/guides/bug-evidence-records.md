---
title: Bug Evidence Records
description: Verify one bug and fix pair against pinned sources and exact replay values.
---

# Bug Evidence Records

These are maintainer records for Ordeal's external benchmark. A project user
gets finding-level witnesses and replay checks directly from
[`ordeal scan`](finding-evidence.md).

An evidence record supports one narrow claim. It does not certify Ordeal in
general. Verification passes only when every declared source, local artifact,
and executable outcome agrees.

## Verify HTTPie Bug 3

```bash
ordeal benchmark \
  --verify-evidence benchmarks/evidence/httpie-3.toml \
  --online-sources \
  --output-json httpie-3-evidence.json
```

PySnooper bug 3 uses the same gate:

```bash
ordeal benchmark \
  --verify-evidence benchmarks/evidence/pysnooper-3.toml \
  --online-sources \
  --output-json pysnooper-3-evidence.json
```

Tornado bug 14 is verified independently:

```bash
ordeal benchmark \
  --verify-evidence benchmarks/evidence/tornado-14.toml \
  --online-sources \
  --output-json tornado-14-evidence.json
```

The checked-in record verifies 30 values:

- 6 pinned BugsInPy or HTTPie files: HTTPS URL, byte size, and SHA-256
- 8 content checks connecting metadata, patch, source, and regression test
- 4 local executable files: package and module byte sizes and SHA-256 values
- 1 canonical-input SHA-256
- 1 replay-interpreter requirement with observed runtime details
- 5 fresh-process buggy replays and 5 fresh-process fixed replays

HTTPie's exact input is
`{"request_headers":{"X-Ordeal":null}}`. Its canonical SHA-256 is
`bdd0c82f4c058188fce09292f1530bfa3665fb9b544189251ec6c3942a65d296`.
The buggy side must raise `AttributeError` with the exact declared message. The
fixed side must return `{}`. A different crash on the same callable does not
count.

PySnooper's exact input is `{"output":"trace.log"}`, whose SHA-256 is
`aa8a92b273e3465d2712da546a934467fc4de7a635d79465989775853efc993d`.
Its buggy closure must raise `NameError`; the fixed control must return the
declared path and captured text without touching the real filesystem.

Tornado's exact input is `{"make_current":true}`, with SHA-256
`c49dbffe64d4cfcab1f753a0c6fade4928fef28f66bda199d3845e900dfa6972`.
The buggy first construction must raise `RuntimeError`; the fixed sequence must
allow the first loop, reject the second, and report that current state survived.

## Record Structure

```toml
schema_version = 1
evidence_id = "dataset-project-bug"
claim = "One falsifiable statement."
scope = "The exact domain covered by the statement."
online_sources_required = true

[[sources]]
name = "upstream_patch"
url = "https://.../immutable-commit/..."
sha256 = "..."
bytes = 123

[[content_checks]]
name = "patch_contains_guard"
source = "upstream_patch"
contains = "if value is None:"
```

The record also requires `[upstream]`, `[reproduction]`,
`[expected.buggy]`, `[expected.fixed]`, and one or more `[[artifacts]]` tables.
Replays declare `requires_python`, have a positive timeout, and run in separate
Python processes inside a temporary workspace containing only declared artifact
bytes.

## Manifest Binding

Set `evidence_path` on both the bug and fixed cases. Before scanning, Ordeal
checks the record ID, project, bug ID, fixed commit, module, callable, target,
and positive oracle values against the manifest. Local verification or binding
failure produces `blocked`, never a hit or correct rejection. Pass
`--online-sources` to require the authoritative source checks in that run.

## What Verified Means

`VERIFIED` means all checks in this record passed now. SHA-256 detects changed
bytes; it does not establish signer identity. Fresh replay demonstrates the
declared modern reproduction; it does not recreate HTTPie's historical Python
3.7.3 environment or justify an accuracy claim beyond this regression pair.
