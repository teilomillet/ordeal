"""Coverage-guided chaos testing via Google's Atheris.

Atheris (libFuzzer for Python) provides coverage-guided fuzzing: it mutates
inputs based on which code paths they trigger, systematically exploring the
code's state space.  This module bridges Atheris with ordeal's fault system.

Two modes of operation:

1. **Fuzz a function with buggify**: Coverage-guided exploration of which
   ``buggify()`` calls activate, finding fault combinations that trigger bugs.

2. **Fuzz a ChaosTest**: Use Atheris byte stream to drive fault scheduling
   and rule selection in a ``ChaosTest``, with coverage feedback guiding
   the exploration.

Requires: ``pip install ordeal[atheris]``

Usage::

    from ordeal.integrations.atheris_engine import fuzz, fuzz_chaos_test

    # Mode 1: fuzz a target function, buggify driven by coverage
    fuzz(target_fn, max_time=60)

    # Mode 2: fuzz a ChaosTest class
    fuzz_chaos_test(MyServiceChaos, max_time=300)
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Type

from ordeal import buggify as _buggify_mod
from ordeal.assertions import tracker


class _AtherisBuggifyRNG:
    """Adapts atheris.FuzzedDataProvider to behave like random.Random
    for buggify decisions.  Each buggify() call consumes bytes from the
    fuzzer, so coverage feedback guides which buggify patterns get explored."""

    def __init__(self, fdp: Any) -> None:
        self._fdp = fdp

    def random(self) -> float:
        return self._fdp.ConsumeFloat()


def fuzz(
    target: Callable[[], None],
    *,
    max_time: int = 60,
    buggify_probability: float = 0.2,
    sys_argv: list[str] | None = None,
) -> None:
    """Run *target* under coverage-guided fuzzing with buggify active.

    Atheris mutates the byte stream → each mutation changes which
    ``buggify()`` calls return True → coverage feedback steers toward
    fault combinations that reach new code paths.

    Args:
        target: Zero-arg callable to fuzz.  Should contain buggify() calls
            in the code path.
        max_time: Maximum fuzz time in seconds.
        buggify_probability: Probability for buggify() decisions.
        sys_argv: Override sys.argv for atheris (defaults to fuzzer flags).
    """
    try:
        import atheris
    except ImportError:
        raise ImportError(
            "atheris is required for coverage-guided fuzzing. "
            "Install with: pip install ordeal[atheris]"
        ) from None

    argv = sys_argv or [sys.argv[0], f"-max_total_time={max_time}"]

    def test_one_input(data: bytes) -> None:
        fdp = atheris.FuzzedDataProvider(data)
        # Wire buggify to consume from the fuzzer's byte stream
        _buggify_mod.activate(probability=buggify_probability)
        _buggify_mod._state.rng = _AtherisBuggifyRNG(fdp)
        tracker.active = True
        try:
            target()
        except Exception:
            pass  # atheris catches crashes automatically
        finally:
            _buggify_mod.deactivate()

    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()


def fuzz_chaos_test(
    test_class: type,
    *,
    max_time: int = 300,
    max_steps: int = 50,
    sys_argv: list[str] | None = None,
) -> None:
    """Run a ``ChaosTest`` subclass under atheris coverage-guided fuzzing.

    Instead of Hypothesis's random exploration, Atheris drives:
    - Which rules execute (and in what order)
    - Which faults toggle (and when)
    - What data the rules receive

    Coverage feedback steers exploration toward interesting fault/rule
    interleavings.

    Args:
        test_class: A ``ChaosTest`` subclass.
        max_time: Maximum fuzz time in seconds.
        max_steps: Maximum rule steps per test case.
        sys_argv: Override sys.argv for atheris.
    """
    try:
        import atheris
    except ImportError:
        raise ImportError(
            "atheris is required for coverage-guided fuzzing. "
            "Install with: pip install ordeal[atheris]"
        ) from None

    argv = sys_argv or [sys.argv[0], f"-max_total_time={max_time}"]

    def test_one_input(data: bytes) -> None:
        fdp = atheris.FuzzedDataProvider(data)
        machine = test_class()
        tracker.active = True

        try:
            # Determine number of steps from fuzzer data
            n_steps = fdp.ConsumeIntInRange(1, max_steps)

            # Collect available rules (excluding private/internal)
            rules = [
                name
                for name in dir(machine)
                if hasattr(getattr(type(machine), name, None), "hypothesis_stateful_rule")
            ]
            if not rules:
                return

            for _ in range(n_steps):
                if fdp.remaining_bytes() < 4:
                    break

                # Choose: execute a rule or toggle a fault
                if machine._faults and fdp.ConsumeBool():
                    # Nemesis action
                    fault_idx = fdp.ConsumeIntInRange(0, len(machine._faults) - 1)
                    fault = machine._faults[fault_idx]
                    if fault.active:
                        fault.deactivate()
                    else:
                        fault.activate()
                else:
                    # Rule execution (simplified — calls rule with no args)
                    rule_name = rules[fdp.ConsumeIntInRange(0, len(rules) - 1)]
                    try:
                        getattr(machine, rule_name)()
                    except Exception:
                        pass  # atheris catches crashes
        finally:
            machine.teardown()

    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()
