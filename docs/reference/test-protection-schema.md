# Test protection evidence schema

This page documents the protection payloads returned by Python and exposed by
`ordeal audit --json`. Values are JSON-compatible unless noted.

## MutationResult.test_protection_view

```python
result = ordeal.mutate("myapp.scoring.compute", preset="standard")
view = result.test_protection_view()
```

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `weak`, `inconclusive`, or `protective_within_measured_scope` |
| `protects` | `bool | null` | Scoped Boolean decision; null when inconclusive |
| `summary` | `str` | Human-readable reason for the verdict |
| `mutation_score` | `str | null` | Exact `killed/total (percent)` text |
| `surviving_mutants` | `int` | Tested mutants that escaped |
| `kill_attribution` | `list[object]` | Killer, kill count, and operator names |
| `property_strength` | `list[object]` | Observations and kills per property |
| `tautological_or_weak_properties` | `list[str]` | Exercised properties with zero kills |
| `unexercised_properties` | `list[str]` | Declared properties with zero observations |

Each kill-attribution row has `test`, `kills`, and `operators`. Property killers
use the prefix `property:`.

Each property-strength row has `name`, `holds`, `total`, `mutants_killed`,
`mutants_tested`, and `status`.

`MutationResult.epistemic_view()` also records
`validation_sample_matrix_sha256`, the SHA-256 digest of the deterministic
sample matrix replayed against every mutant.

## ModuleAudit.test_protection_view

```python
audit = ordeal.audit("myapp.scoring")
view = audit.test_protection_view()
```

This view combines the migrated coverage measurement with aggregated mutation
validation.

| Field | Type | Meaning |
|---|---|---|
| `label` | `str` | `resulting test protection` |
| `status` | `str` | Combined verdict |
| `protects` | `bool | null` | Scoped decision |
| `summary` | `str` | Decisive evidence in one sentence |
| `mutation_score` | `str | null` | Aggregated score text |
| `killed_mutants` | `int` | Aggregated killed count |
| `tested_mutants` | `int` | Aggregated tested count |
| `surviving_mutants` | `int` | Aggregated survivors |
| `kill_attribution` | `list[object]` | Weakest observed killers |
| `line_coverage_percent` | `float | null` | Verified migrated line coverage |
| `coverage_gaps` | `list[int]` | Exact missing executable lines |
| `coverage_gap_count` | `int` | Missing statement count |
| `property_strength` | `list[object]` | Rows include their mutation target |
| `tautological_or_weak_properties` | `list[object]` | Full weak-property rows |
| `unexercised_properties` | `list[object]` | Full unexercised-property rows |

The Python mutation view returns property names in its two convenience lists;
the module audit view returns full rows because it aggregates multiple targets.

Each row in `ModuleAudit.mutation_targets` also carries `validation_seed`,
`mutant_ids`, and `killed_mutant_ids`. Mutant IDs are stable hashes of the
target and mutation site, so runs with different audit worker counts can be
compared without relying on completion order.

## CLI JSON path

```bash
ordeal audit myapp.scoring --json
```

The envelope includes:

```text
raw_details.protection_views[]
```

Each row adds `module` to the `ModuleAudit.test_protection_view()` fields. The
same module serialization also contains `evidence_views.test_protection`.

## Decision order

For a mutation result:

1. any survivor → `weak`;
2. any unexercised declared property → `weak`;
3. no tested mutants → `inconclusive`;
4. otherwise → `protective_within_measured_scope`.

For a module audit, surviving mutants win first, then unexercised properties,
coverage gaps, missing mutation evidence, and unverified coverage. Only complete
measured evidence reaches `protective_within_measured_scope`.

## Interpretation boundaries

- Mutation score depends on target, preset, filters, and available operators.
- Equivalent-mutant filtering reduces noise but cannot solve equivalence in all cases.
- Coverage gaps are executable line gaps, not a complete branch-coverage model.
- Kill attribution identifies observed killers, not every redundant test.
- A scoped protective verdict is evidence, not a universal correctness proof.
