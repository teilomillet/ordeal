"""API chaos testing via Schemathesis.

Combines Schemathesis's OpenAPI/GraphQL-driven testing with ordeal's fault
injection.  Faults fire on the server side while Schemathesis exercises
every API endpoint.

Requires: ``pip install ordeal[api]``

Usage::

    from ordeal.integrations.schemathesis_ext import chaos_api_test

    # Run chaos testing against an API with server-side fault injection
    chaos_api_test(
        schema_url="http://localhost:8080/openapi.json",
        faults=[
            timing.slow("myapp.db.query", delay=2.0),
            numerical.nan_injection("myapp.scoring.predict"),
            io.error_on_call("myapp.storage.save"),
        ],
    )

Or use the decorator for more control::

    import schemathesis
    from ordeal.integrations.schemathesis_ext import with_chaos

    schema = schemathesis.from_uri("http://localhost:8080/openapi.json")

    @schema.parametrize()
    @with_chaos(faults=[...])
    def test_api(case):
        response = case.call()
        case.validate_response(response)
"""

from __future__ import annotations

import functools
import random
from typing import Any, Callable

from ordeal.assertions import tracker
from ordeal.faults import Fault


def with_chaos(
    faults: list[Fault],
    *,
    fault_probability: float = 0.3,
    seed: int | None = None,
    swarm: bool = False,
) -> Callable:
    """Decorator that wraps a Schemathesis test with fault injection.

    Before each API call, randomly activates/deactivates faults.
    After the call, deactivates all faults to avoid interference.

    Args:
        faults: Fault instances to inject.
        fault_probability: Probability of each fault being active per request.
        seed: Random seed for reproducibility.
        swarm: Use swarm mode — pick a random subset of faults once, then
            toggle only those for the lifetime of the wrapper. Better
            aggregate coverage than all-faults-always-eligible.
    """
    rng = random.Random(seed)
    if swarm and faults:
        k = max(1, rng.randint(1, len(faults)))
        eligible = set(rng.sample(faults, k))
    else:
        eligible = set(faults)

    def decorator(test_fn: Callable) -> Callable:
        @functools.wraps(test_fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracker.active = True
            # Randomly activate faults
            for fault in faults:
                if fault in eligible and rng.random() < fault_probability:
                    fault.activate()
                else:
                    fault.deactivate()
            try:
                return test_fn(*args, **kwargs)
            finally:
                for fault in faults:
                    fault.reset()

        return wrapper

    return decorator


def chaos_api_test(
    schema_url: str,
    faults: list[Fault],
    *,
    fault_probability: float = 0.3,
    seed: int | None = None,
    base_url: str | None = None,
    stateful: bool = True,
    max_examples: int = 100,
    checks: tuple | None = None,
) -> None:
    """Run Schemathesis against *schema_url* with ordeal fault injection.

    This is the batteries-included entry point: loads the schema, generates
    test cases for every endpoint, and randomly injects faults while
    Schemathesis exercises the API.

    Args:
        schema_url: URL to OpenAPI/GraphQL schema.
        faults: Fault instances to inject server-side.
        fault_probability: Probability of each fault being active per request.
        seed: Random seed for reproducibility.
        base_url: Override base URL for API calls.
        stateful: Enable Schemathesis stateful testing (link-based).
        max_examples: Max test cases per endpoint.
        checks: Schemathesis checks to run (defaults to all).
    """
    try:
        import schemathesis
    except ImportError:
        raise ImportError(
            "schemathesis is required for API chaos testing. Install with: pip install ordeal[api]"
        ) from None

    schema = schemathesis.from_uri(schema_url, base_url=base_url)
    rng = random.Random(seed)
    tracker.active = True

    all_checks = checks or schemathesis.checks.ALL_CHECKS

    @schema.parametrize()
    def _test(case: Any) -> None:
        # Toggle faults
        for fault in faults:
            if rng.random() < fault_probability:
                fault.activate()
            else:
                fault.deactivate()

        try:
            response = case.call()
            case.validate_response(response, checks=all_checks)
        finally:
            for fault in faults:
                fault.reset()

    # Run via Schemathesis engine
    if stateful:
        schema.as_state_machine().run(settings={"max_examples": max_examples})
    else:
        # Parametrized mode — run each endpoint
        _test()


class ChaosAPIHook:
    """Schemathesis hook that injects faults around API calls.

    Register with schemathesis::

        import schemathesis
        from ordeal.integrations.schemathesis_ext import ChaosAPIHook

        hook = ChaosAPIHook(faults=[...])
        schemathesis.hooks.register(hook.before_call, "before_call")
        schemathesis.hooks.register(hook.after_call, "after_call")
    """

    def __init__(
        self,
        faults: list[Fault],
        fault_probability: float = 0.3,
        seed: int | None = None,
        swarm: bool = False,
    ):
        self.faults = faults
        self.probability = fault_probability
        self.rng = random.Random(seed)
        if swarm and faults:
            k = max(1, self.rng.randint(1, len(faults)))
            self.eligible: set[Fault] = set(self.rng.sample(faults, k))
        else:
            self.eligible = set(faults)

    def before_call(self, context: Any, case: Any) -> None:
        """Randomly activate/deactivate faults before each API call."""
        for fault in self.faults:
            if fault in self.eligible and self.rng.random() < self.probability:
                fault.activate()
            else:
                fault.deactivate()

    def after_call(self, context: Any, case: Any, response: Any) -> None:
        """Reset all faults after each API call to avoid cross-request interference."""
        for fault in self.faults:
            fault.reset()
