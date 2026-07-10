# Replay Notes

Target: `ordeal.demo`

## decode: strong candidate issue on contract-valid inputs

- Finding ID: `fnd_8dbee6c2f122`
- Impact: the function crashes on an input that matches the inferred contract.
- Command: `uv run ordeal scan ordeal.demo --mode candidate --targets ordeal.demo:decode -n 1`

```python
from importlib import import_module
mod = import_module('ordeal.demo')
args = {'s': 'utf-8', 'errors': 'backslashreplace'}
mod.decode(**args)
```

```json
{
  "s": "utf-8",
  "errors": "backslashreplace"
}
```

## idempotent (97%)

- Finding ID: `fnd_dcb0fc0808d3`
