# Troubleshooting

Common issues and how to fix them.

## The explorer runs but finds 0 failures

This can mean your code is correct under fault conditions — or that the explorer isn't reaching interesting states.

**Check edge count.** If edges plateau early (say 10-20), the explorer is stuck in shallow territory.

- Add more `target_modules` to expand coverage tracking
- Increase `steps_per_run` to let runs go deeper
- Add more faults — the explorer needs faults to create interesting state combinations
- Check that your `target_modules` paths are correct (they match by path segment, not substring)

**Check your faults.** If all faults target the same function, the explorer can't create many combinations. Spread faults across different dependencies.

**Check your rules.** If you only have one rule, there aren't many interleavings to explore. Add rules for different operations your system supports.

## The explorer runs but finds too many failures

If every run fails, the explorer can't explore — it's drowning in noise.

**Handle expected exceptions in rules.** When a timeout fault is active, `TimeoutError` is expected. Catch it in your rule:

```python
@rule()
def call_service(self):
    try:
        result = self.service.call()
    except TimeoutError:
        return  # expected when timeout fault is active
    always(result is not None, "result exists")
```

**Enable swarm mode.** With `swarm = True`, each run uses a random subset of faults instead of all of them. This reduces noise and lets the explorer find specific fault combinations that cause real bugs.

**Reduce faults.** Start with 2-3 faults and add more once your base test is stable.

## "Cannot import" error with ordeal explore

```
Cannot import tests.test_chaos:MyServiceChaos: ...
```

The `class` path in `ordeal.toml` must be importable from the working directory. Check:

- Is the module path correct? Format: `"module.path:ClassName"`
- Is your working directory the project root?
- Is the package installed or on `PYTHONPATH`?

## Hypothesis shrinking takes too long

Shrinking is Hypothesis finding the minimal reproducing example. It can be slow with many faults and long rule sequences.

- Use `--no-shrink` during exploration: `ordeal explore --no-shrink`
- Shrink post-hoc: `ordeal replay --shrink trace.json`
- Set a time limit: the Explorer's `max_shrink_time` parameter (default 30s)
- Reduce `steps_per_run` to produce shorter traces

## buggify() always returns False

`buggify()` is a no-op unless explicitly activated. Check:

- Are you running with `--chaos`? (`pytest --chaos`)
- Or did you call `auto_configure()`?
- Or did you call `activate()` directly?

```python
from ordeal.buggify import activate, is_active
activate(probability=0.1)
assert is_active()
```

## Property assertions not tracked in the report

`always()` and `unreachable()` always raise on violation — they are never silent, with or without `--chaos`. But the property *report* (hit counts, pass/fail summary) only appears when the PropertyTracker is active.

`sometimes()` and `reachable()` only track when the tracker is active. Without `--chaos`, they do nothing.

To enable the tracker and the property report:

- Run with `--chaos` flag
- Or call `auto_configure()` at test start
- Check the property report at the end of the pytest output (printed when `--chaos` is active)

## "sometimes" or "reachable" fails at session end

These are deferred assertions — they must be satisfied at least once across the entire session.

- **`sometimes` fails**: the condition was never True. Either the code path isn't being exercised, or the condition is too strict. Check your rules — are they actually reaching the state where this condition holds?
- **`reachable` fails**: the code path was never executed. Your fault injection might not be creating the conditions that trigger this path. Add more faults or rules.

## Coverage collector shows 0 edges

The `CoverageCollector` uses `sys.settrace` to track execution. If it shows 0 edges:

- Check `target_modules` — the collector only tracks files whose path contains a matching segment. `["myapp"]` matches `myapp/foo.py` but NOT `tests/test_myapp.py`.
- Make sure your code actually runs during the test. If all rules raise immediately, no application code is traced.
- Some C extensions bypass `sys.settrace` — coverage only tracks Python code.

## Mutation testing: all mutants survive

If `mutate_function_and_test` returns a 0% kill score, your tests aren't checking the function's behavior:

- Are you testing the right function? The `target` is a dotted path: `"myapp.scoring.compute"`.
- Does your test actually call the function? Mutants are only killed if the test raises an exception.
- Are your assertions specific enough? If you only check `result is not None`, swapping `+` to `-` won't be caught. Add value checks.

## PatchFault doesn't seem to work

`PatchFault` resolves the dotted path lazily (on first activation). If the fault seems inactive:

- Check the target path is correct and the module is importable
- Make sure the fault is activated (check `fault.active`)
- If the target is imported as `from module import func`, the local binding won't be patched — PatchFault patches the module attribute. Import as `import module; module.func()` for PatchFault to work.

## FileSystem.read returns bytes, not str

`FileSystem.read()` returns `bytes`. Use `fs.read_text(path)` for a decoded string:

```python
data = fs.read_text("/config.json")  # str
raw = fs.read("/binary.dat")         # bytes
```

## Clock.advance doesn't fire timers

`Clock.sleep()` advances time but does NOT fire timers. Use `Clock.advance()` instead:

```python
clock = Clock()
clock.set_timer(10.0, callback)

clock.sleep(15.0)    # advances time but callback NOT fired
clock.advance(15.0)  # advances time AND fires callback
```

## ordeal audit shows FAILED instead of coverage

The audit never silently returns 0% — if a measurement fails, it says `FAILED: reason`. Common reasons:

- **"no test files found"**: test files must be named `test_<module_short_name>.py` or `test_<module_short_name>_*.py`. Check the `--test-dir` flag.
- **"pytest not found"** or **"coverage report not generated"**: install `pytest-cov` (`pip install pytest-cov`).
- **"timed out after 120s"**: tests are too slow under coverage. Try with fewer tests or a faster machine.
- **"module not found in coverage report"**: the module path doesn't match what coverage.py tracked. Check the dotted path matches the file location.
- **"coverage data inconsistent"**: coverage.py's reported percentage doesn't match computed. This can happen with dynamic imports or conditional platform code.

Check the `warnings` field for details: `result.warnings` lists every problem encountered during the audit.

## ordeal audit shows 0% migrated coverage (FAILED)

The migrated test is generated to `.ordeal/test_<module>_migrated.py`. Check:

- Can the module be imported? (`python -c "import myapp.scoring"`)
- Does `scan_module("myapp.scoring")` find any functions? Functions without type hints and no fixtures are skipped.
- Use `--show-generated` to see what the generated test looks like.
- Check `result.warnings` — mining failures are logged there.

## Property mining finds no properties

`ordeal.mine` needs the function to be callable with random inputs. If all calls crash, no properties can be observed.

- Provide fixtures for parameters that can't be inferred: `mine(fn, model=mock_model)`
- Check that the function has type hints — mining uses the same strategy inference as `fuzz()`.
- A function that always raises won't have observable output properties.

## Tests pass locally but fail in CI

- **Seed mismatch**: set a fixed seed in `ordeal.toml` for reproducibility
- **Missing dependencies**: make sure `ordeal[all]` or the specific extras are installed
- **Timeout**: CI may be slower — increase `max_time`
- **PYTHONPATH**: ensure the project root is on the path

## Getting help

If you're stuck:

- Check the [full documentation](https://docs.byordeal.com/)
- Open an issue at [github.com/teilomillet/ordeal](https://github.com/teilomillet/ordeal/issues)
