"""Built-in OpenAPI chaos testing engine (zero external dependencies).

Quick start — pick one::

    # ASGI (FastAPI, Starlette) — most common
    from ordeal.integrations.openapi import chaos_api_test
    result = chaos_api_test(app=my_fastapi_app, faults=[...])

    # WSGI (Flask, Django)
    result = chaos_api_test(app=my_flask_app, wsgi=True, faults=[...])

    # Remote server
    result = chaos_api_test(schema_url="http://localhost:8080/openapi.json", faults=[...])

Go deeper — each parameter unlocks more power:

    # Auto-generate faults from your app's source code (AST mutations + semantic)
    result = chaos_api_test(app=my_app, auto_discover=True)

    # Target specific functions for mutation-based fault generation
    result = chaos_api_test(app=my_app, mutation_targets=["myapp.db.save"])

    # Swarm mode — random fault subsets per run, better aggregate coverage
    result = chaos_api_test(app=my_app, faults=[...], swarm=True)

    # Record replayable traces of every API call and fault activation
    result = chaos_api_test(app=my_app, faults=[...], record_traces=True)

    # Print results with contextual hints for next steps
    print(result.summary())

The ``@with_chaos`` decorator wraps any function with fault injection::

    from ordeal.integrations.openapi import with_chaos

    @with_chaos(faults=[timing.slow("myapp.db.query")], seed=42)
    def test_my_endpoint():
        response = call_api()
        assert response.status_code != 500
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "response.py",
    "discoverhandlers.py",
    "deterministicexample.py",
    "withchaos.py",
    "chaosapitest.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "integrationsopenapi"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
