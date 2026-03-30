# API Reference

Complete module listing. Each links to in-code docstrings.

## Core

| Import | What |
|---|---|
| `ordeal.ChaosTest` | Stateful chaos test base class |
| `ordeal.rule` | Declare a test rule (re-export from Hypothesis) |
| `ordeal.invariant` | Declare an invariant check (re-export) |
| `ordeal.initialize` | Declare initialization rule (re-export) |
| `ordeal.precondition` | Gate a rule on current state (re-export) |
| `ordeal.Bundle` | Named collection for data flow between rules (re-export) |
| `ordeal.auto_configure()` | Enable chaos mode programmatically |

## Auto

| Import | What |
|---|---|
| `auto.scan_module(module)` | Smoke-test every public function |
| `auto.fuzz(fn, **fixtures)` | Deep-fuzz a single function |
| `auto.chaos_for(module, ...)` | Auto-generate ChaosTest from API |
| `auto.ScanResult` | Result of `scan_module` |
| `auto.FuzzResult` | Result of `fuzz` |

## Assertions

| Import | What |
|---|---|
| `ordeal.always(cond, name)` | Must be true every call |
| `ordeal.sometimes(cond, name)` | Must be true at least once |
| `ordeal.reachable(name)` | Code path must execute |
| `ordeal.unreachable(name)` | Code path must never execute |

## Buggify

| Import | What |
|---|---|
| `ordeal.buggify()` | Returns True during testing (probabilistic) |
| `ordeal.buggify_value(normal, faulty)` | Returns faulty during testing |
| `ordeal.buggify.activate(prob)` | Enable for current thread |
| `ordeal.buggify.deactivate()` | Disable for current thread |
| `ordeal.buggify.set_seed(seed)` | Seed the RNG |

## Faults

| Import | What |
|---|---|
| `faults.Fault` | Base class (subclass for custom faults) |
| `faults.PatchFault(target, wrapper_fn)` | Monkey-patch a function |
| `faults.LambdaFault(name, on_activate, on_deactivate)` | Quick custom fault |
| `faults.io.error_on_call(target, error, msg)` | Raise on call |
| `faults.io.return_empty(target)` | Return None |
| `faults.io.truncate_output(target, fraction)` | Truncate output |
| `faults.io.corrupt_output(target)` | Random bytes output |
| `faults.io.disk_full()` | Writes fail (global) |
| `faults.io.permission_denied()` | Writes fail (global) |
| `faults.numerical.nan_injection(target)` | Output becomes NaN |
| `faults.numerical.inf_injection(target)` | Output becomes Inf |
| `faults.numerical.wrong_shape(target, expected, actual)` | Wrong array shape |
| `faults.numerical.corrupted_floats(type)` | Standalone corrupt float source |
| `faults.timing.timeout(target, delay)` | Raise TimeoutError |
| `faults.timing.slow(target, delay)` | Add real delay |
| `faults.timing.intermittent_crash(target, every_n)` | Crash periodically |
| `faults.timing.jitter(target, magnitude)` | Perturb numeric output |

## Explorer

| Import | What |
|---|---|
| `explore.Explorer(test_class, ...)` | Coverage-guided engine |
| `explore.Explorer.run(...)` | Run exploration loop |
| `explore.CoverageCollector(paths)` | AFL-style edge coverage |
| `explore.ExplorationResult` | Results dataclass |
| `explore.Failure` | Failure with trace |
| `explore.ProgressSnapshot` | Live stats |

## Trace

| Import | What |
|---|---|
| `trace.Trace` | Full run recording |
| `trace.TraceStep` | Single step in a trace |
| `trace.replay(trace)` | Reproduce a failure |
| `trace.shrink(trace, test_class)` | Minimize a failure |

## QuickCheck

| Import | What |
|---|---|
| `quickcheck.quickcheck` | Decorator: infer strategies from types |
| `quickcheck.strategy_for_type(tp)` | Derive strategy from type hint |
| `quickcheck.biased.integers(min, max)` | Boundary-biased integers |
| `quickcheck.biased.floats(min, max)` | Boundary-biased floats |
| `quickcheck.biased.strings(min, max)` | Boundary-biased strings |
| `quickcheck.biased.lists(elements, ...)` | Boundary-biased lists |

## Invariants

| Import | What |
|---|---|
| `invariants.no_nan` | Reject NaN |
| `invariants.no_inf` | Reject Inf |
| `invariants.finite` | `no_nan & no_inf` |
| `invariants.bounded(lo, hi)` | Value in range |
| `invariants.monotonic(strict=False)` | Non-decreasing sequence |
| `invariants.unique(key=None)` | No duplicates |
| `invariants.non_empty()` | Not empty/falsy |
| `inv_a & inv_b` | Compose with `&` |

## Simulate

| Import | What |
|---|---|
| `simulate.Clock(start=0)` | Controllable clock |
| `simulate.Clock.advance(seconds)` | Advance time (instant) |
| `simulate.Clock.set_timer(delay, cb)` | Schedule callback |
| `simulate.Clock.patch()` | Context manager: patch `time.time`/`time.sleep` |
| `simulate.FileSystem()` | In-memory filesystem |
| `simulate.FileSystem.inject_fault(path, type)` | Inject fault |

## Mutations

| Import | What |
|---|---|
| `mutations.mutate_function_and_test(target, test_fn)` | Mutate a function |
| `mutations.mutate_and_test(module, test_fn)` | Mutate a module |
| `mutations.generate_mutants(source)` | Generate AST mutants |
| `mutations.MutationResult` | Results with `.score`, `.summary()` |

## Config

| Import | What |
|---|---|
| `config.load_config(path)` | Load `ordeal.toml` |
| `config.OrdealConfig` | Top-level config |
| `config.ExplorerConfig` | Explorer settings |
| `config.TestConfig` | `[[tests]]` entry |
| `config.ReportConfig` | `[report]` settings |
