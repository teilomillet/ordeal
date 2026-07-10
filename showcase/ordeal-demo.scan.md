# Ordeal Finding Report

Target: `ordeal.demo`
Tool: `ordeal scan`
Status: findings found
Confidence: `86%`
Seed: `42`

## Summary

- Checked: 7 functions, 2 transitions, 2 checkpoints
- Promoted findings: 1
- Lifecycle contracts: 0
- Semantic contracts: 0
- Coverage gaps: 0
- Invalid-input crashes: 0
- Beyond-contract robustness: 0
- Exploratory crashes: 0
- Exploratory properties: 1
- Expected precondition failures: 0
- Gaps: 1
- Evidence: search depth=7 functions/2 transitions/2 checkpoints, replayability=2/2, mutation strength=n/a, fixture completeness=100%
- Config suggestions: 1 ready-to-paste ordeal.toml block

## Findings

### 1. `ordeal.demo.decode`

- Type: crash
- Finding: decode: strong candidate issue on contract-valid inputs
- Evidence class: candidate_issue
- Internal category: likely_bug
- Evidence: `decode() got an unexpected keyword argument 'errors'`
- Ranking: contract fit=60%, reachability=85%, realism=55%
- Replay: `2/2` matching replays
- Why this matters: the function crashes on an input that matches the inferred contract.

Failing input:
```json
{
  "s": "utf-8",
  "errors": "backslashreplace"
}
```

Proof bundle:
```json
{
  "input": {
    "s": "utf-8",
    "errors": "backslashreplace"
  },
  "source": "call_site",
  "seed_sources": [
    {
      "source": "'call_site'",
      "evidence": "'agent_schema.py:33'"
    }
  ],
  "supporting_evidence": [
    {
      "parameter": "'s'",
      "value": "'utf-8'",
      "value_type": "'str'",
      "checks": "[{'kind': 'type_hint', 'detail': 'builtins.str'}, {'kind': 'observed_types', 'detail': ['str'], 'matched': True}, {'k..."
    },
    {
      "parameter": "'errors'",
      "value": "'backslashreplace'",
      "value_type": "'str'",
      "checks": "[]"
    }
  ]
}
```

Contract basis:
```json
{
  "category": "likely_bug",
  "evidence_class": "candidate_issue",
  "fit": 0.595,
  "reachability": 0.85,
  "realism": 0.55,
  "fixture_completeness": 1.0,
  "...": "+9 more field(s)"
}
```

Confidence breakdown:
```json
{
  "replayability": 1.0,
  "contract_fit": 0.595,
  "reachability": 0.85,
  "realism": 0.55,
  "fixture_completeness": 1.0,
  "sink_signal": 0.0,
  "...": "+2 more field(s)"
}
```

Minimal reproduction:
```json
{
  "target": "ordeal.demo:decode",
  "command": "uv run ordeal scan ordeal.demo --mode candidate --targets ordeal.demo:decode -n 1",
  "direct_call_supported": true,
  "note": null
}
```

Python snippet:
```python
from importlib import import_module
mod = import_module('ordeal.demo')
args = {'s': 'utf-8', 'errors': 'backslashreplace'}
mod.decode(**args)
```

Failure path:
```json
{
  "target": "ordeal.demo.decode",
  "qualname": "ordeal.demo.decode",
  "error_type": "TypeError",
  "error": "decode() got an unexpected keyword argument 'errors'",
  "traceback": [
    "auto.py:9308:_test_one_function",
    "auto.py:9284:_run_one",
    "auto.py:2237:_call_sync"
  ]
}
```
- Likely impact: the function crashes on an input that matches the inferred contract.

Impact details:
```json
{
  "class": "likely_bug",
  "evidence_class": "candidate_issue",
  "sink_categories": [],
  "callable_sink_categories": [],
  "critical_sinks": [],
  "trust_boundary_signal": 0.0,
  "...": "+1 more field(s)"
}
```

Regression test stub:
```python
from ordeal.demo import decode


def test_decode_crash_regression() -> None:
    args = {'s': 'utf-8', 'errors': 'backslashreplace'}
    decode(**args)
```

Next steps:
- `ordeal mine ordeal.demo.decode -n 200`
- Reproduce the crash directly in a regression test for `ordeal.demo.decode`

### 2. `ordeal.demo.normalize`

- Type: property
- Finding: idempotent
- Evidence class: speculative_property
- Internal category: speculative_property
- Evidence: `29/30` examples (`97%` confidence)
- Why this matters: calling the function again changed a value that should have stabilized.

Counterexample:
```json
{
  "index": 22,
  "input": {
    "xs": [
      "-0.5",
      "-1e-10",
      "1e-10",
      "-1e-10",
      "4.144458155102842e+156",
      "-6.6963419951248536e+16",
      "... +42 more item(s)"
    ]
  },
  "output": [
    -0.0,
    -0.0,
    0.0,
    -0.0,
    0.0,
    -0.0,
    "... +42 more item(s)"
  ],
  "replayed": [
    0.020833333333333332,
    0.020833333333333332,
    0.020833333333333332,
    0.020833333333333332,
    0.020833333333333332,
    0.020833333333333332,
    "... +42 more item(s)"
  ]
}
```

Regression test stub:
```python
from ordeal.demo import normalize


def test_normalize_idempotent_regression() -> None:
    args = {'xs': [-0.5,
        -1e-10,
        1e-10,
        -1e-10,
        4.144458155102842e+156,
        -6.6963419951248536e+16,
        '... +42 more item(s)']}
    first = normalize(**args)
    replay_args = dict(args)
    replay_args['xs'] = first
    second = normalize(**replay_args)
    assert second == first
```

Next steps:
- `ordeal check ordeal.demo.normalize -p "idempotent" -n 200`
- `ordeal mutate ordeal.demo.normalize`

## Gaps To Close

- `ordeal.demo.normalize`: property: idempotent (97%)

## Evidence Dimensions

- search depth: 7 functions, 2 transitions, 2 checkpoints
- replayability: 2/2 findings have concrete inputs
- mutation strength: not measured yet
- fixture completeness: 100%

## Suggested ordeal.toml

### Persist scan defaults for ordeal.demo

Keep this scan target selection and runtime policy in ordeal.toml.

Target: `ordeal.demo`

```toml
[[scan]]
module = "ordeal.demo"
```

## Suggested Commands

- `ordeal scan ordeal.demo`
- `ordeal mine ordeal.demo -n 200`
- `ordeal mutate ordeal.demo`
