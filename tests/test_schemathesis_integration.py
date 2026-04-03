"""Integration tests for ordeal's API chaos testing engine.

These tests exercise the full path: ASGI/WSGI app -> OpenAPI schema loading ->
fault injection -> result reporting.  No external dependencies beyond starlette
(for the test app).
"""

from __future__ import annotations

import pytest

starlette = pytest.importorskip("starlette", reason="starlette not installed")

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from ordeal.assertions import tracker  # noqa: E402
from ordeal.faults import LambdaFault  # noqa: E402
from ordeal.integrations.openapi import (  # noqa: E402
    ChaosAPIResult,
    _discover_handlers,
    auto_faults,
    chaos_api_test,
    with_chaos,
)

# ---------------------------------------------------------------------------
# Test ASGI app
# ---------------------------------------------------------------------------

_ITEMS: dict[int, dict] = {}
_NEXT_ID = 1


def _reset_items() -> None:
    global _NEXT_ID
    _ITEMS.clear()
    _NEXT_ID = 1


async def list_items(request: Request) -> JSONResponse:
    return JSONResponse(list(_ITEMS.values()))


async def create_item(request: Request) -> JSONResponse:
    global _NEXT_ID
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=422)
    name = body.get("name")
    if not isinstance(name, str):
        return JSONResponse({"error": "name must be string"}, status_code=422)
    item = {"id": _NEXT_ID, "name": name}
    _ITEMS[_NEXT_ID] = item
    _NEXT_ID += 1
    return JSONResponse(item, status_code=201)


async def get_item(request: Request) -> JSONResponse:
    item_id = int(request.path_params["item_id"])
    if item_id not in _ITEMS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_ITEMS[item_id])


OPENAPI_SCHEMA = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "0.1.0"},
    "paths": {
        "/items": {
            "get": {
                "operationId": "listItems",
                "responses": {
                    "200": {
                        "description": "Item list",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "name": {"type": "string"},
                                        },
                                    },
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "operationId": "createItem",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "Created",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "name": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Invalid body"},
                    "422": {"description": "Validation error"},
                },
            },
        },
        "/items/{item_id}": {
            "get": {
                "operationId": "getItem",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Single item",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "name": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "404": {"description": "Not found"},
                },
            },
        },
    },
}

OPENAPI_SCHEMA_WITH_LINKS = {
    **OPENAPI_SCHEMA,
    "paths": {
        **OPENAPI_SCHEMA["paths"],
        "/items": {
            **OPENAPI_SCHEMA["paths"]["/items"],
            "post": {
                **OPENAPI_SCHEMA["paths"]["/items"]["post"],
                "responses": {
                    **OPENAPI_SCHEMA["paths"]["/items"]["post"]["responses"],
                    "201": {
                        **OPENAPI_SCHEMA["paths"]["/items"]["post"]["responses"]["201"],
                        "links": {
                            "GetCreatedItem": {
                                "operationId": "getItem",
                                "parameters": {"item_id": "$response.body#/id"},
                            }
                        },
                    },
                },
            },
        },
    },
}


async def openapi_schema(request: Request) -> JSONResponse:
    return JSONResponse(OPENAPI_SCHEMA)


async def openapi_schema_with_links(request: Request) -> JSONResponse:
    return JSONResponse(OPENAPI_SCHEMA_WITH_LINKS)


async def _not_found(request: Request) -> JSONResponse:
    return JSONResponse({"error": "not found"}, status_code=404)


asgi_app = Starlette(
    routes=[
        Route("/items", list_items, methods=["GET"]),
        Route("/items", create_item, methods=["POST"]),
        Route("/items/{item_id:int}", get_item),
        Route("/openapi.json", openapi_schema),
        Route("/openapi-links.json", openapi_schema_with_links),
    ],
    exception_handlers={404: lambda req, exc: JSONResponse({"error": "not found"}, 404)},
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset item store and tracker between tests."""
    _reset_items()
    old_active = tracker.active
    tracker.reset()
    yield
    _reset_items()
    tracker.reset()
    tracker.active = old_active


def _make_faults(n: int = 2) -> list[LambdaFault]:
    faults = []
    for i in range(n):
        faults.append(
            LambdaFault(f"fault_{i}", on_activate=lambda: None, on_deactivate=lambda: None)
        )
    return faults


# ---------------------------------------------------------------------------
# chaos_api_test — ASGI
# ---------------------------------------------------------------------------


class TestChaosApiTestASGI:
    def test_basic_no_faults(self):
        result = chaos_api_test(
            app=asgi_app,
            faults=[],
            max_examples=5,
            seed=42,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.duration_seconds > 0
        assert result.deferred_ok is True

    def test_with_faults(self):
        faults = _make_faults(3)
        result = chaos_api_test(
            app=asgi_app,
            faults=faults,
            fault_probability=0.5,
            seed=42,
            max_examples=5,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.total_requests > 0
        assert result.fault_activations is not None

    def test_all_faults_active(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=asgi_app,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            max_examples=5,
        )
        # Every request should have activated every fault
        for name, count in result.fault_activations.items():
            assert count > 0, f"{name} was never activated"

    def test_no_faults_zero_activations(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=asgi_app,
            faults=faults,
            fault_probability=0.0,
            seed=42,
            max_examples=5,
        )
        assert all(v == 0 for v in result.fault_activations.values())

    def test_swarm_mode(self):
        faults = _make_faults(5)
        result = chaos_api_test(
            app=asgi_app,
            faults=faults,
            fault_probability=1.0,
            swarm=True,
            seed=42,
            max_examples=5,
        )
        # Swarm picks a subset — not all faults should activate
        activated = {k for k, v in result.fault_activations.items() if v > 0}
        assert 0 < len(activated) <= 5

    def test_custom_schema_path(self):
        result = chaos_api_test(
            app=asgi_app,
            schema_path="/openapi.json",
            faults=[],
            max_examples=3,
            seed=42,
        )
        assert isinstance(result, ChaosAPIResult)

    def test_stateful_false_uses_parametrized(self):
        result = chaos_api_test(
            app=asgi_app,
            faults=_make_faults(1),
            stateful=False,
            max_examples=5,
            seed=42,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.total_requests > 0

    def test_stateful_true_falls_back_without_links(self):
        """Schema without links should gracefully fall back to parametrized."""
        result = chaos_api_test(
            app=asgi_app,
            faults=[],
            stateful=True,
            max_examples=5,
            seed=42,
        )
        # Should still produce a result (fallback to parametrized)
        assert isinstance(result, ChaosAPIResult)
        assert "fallback to parametrized" in result.summary()

    def test_stateful_true_follows_openapi_links(self):
        result = chaos_api_test(
            app=asgi_app,
            schema_path="/openapi-links.json",
            faults=[],
            stateful=True,
            max_examples=1,
            seed=42,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.total_requests >= 2
        assert result.passed is True
        assert "followed OpenAPI links" in result.summary()

    def test_result_passed_property(self):
        result = chaos_api_test(
            app=asgi_app,
            faults=[],
            max_examples=3,
            seed=42,
        )
        # A healthy app with no faults should pass
        assert result.passed is True

    def test_headers_forwarded(self):
        """Custom headers should be passed through to the schema loader."""
        result = chaos_api_test(
            app=asgi_app,
            faults=[],
            headers={"X-Test": "value"},
            max_examples=3,
            seed=42,
        )
        assert isinstance(result, ChaosAPIResult)


# ---------------------------------------------------------------------------
# with_chaos + built-in engine
# ---------------------------------------------------------------------------


class TestWithChaosIntegration:
    def test_decorator_activates_faults(self):
        """with_chaos should activate faults around the wrapped function."""
        faults = _make_faults(2)
        calls = []

        @with_chaos(faults, fault_probability=1.0, seed=42)
        def chaos_fn():
            calls.append(1)
            assert any(f.active for f in faults)

        chaos_fn()
        assert len(calls) == 1

    def test_faults_reset_after_each_call(self):
        faults = _make_faults(2)

        @with_chaos(faults, fault_probability=1.0, seed=42)
        def chaos_fn():
            assert any(f.active for f in faults)

        chaos_fn()
        assert all(not f.active for f in faults)


# ---------------------------------------------------------------------------
# WSGI integration
# ---------------------------------------------------------------------------


def _wsgi_app(environ, start_response):
    """Minimal WSGI app that serves OpenAPI schema and one GET endpoint."""
    import json

    path = environ.get("PATH_INFO", "/")
    if path == "/openapi.json":
        body = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "WSGI Test", "version": "0.1.0"},
                "paths": {
                    "/ping": {
                        "get": {
                            "operationId": "ping",
                            "responses": {
                                "200": {
                                    "description": "OK",
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {"ok": {"type": "boolean"}},
                                            }
                                        }
                                    },
                                }
                            },
                        }
                    }
                },
            }
        ).encode()
    elif path == "/ping":
        body = json.dumps({"ok": True}).encode()
    else:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]

    start_response("200 OK", [("Content-Type", "application/json")])
    return [body]


class TestWSGIIntegration:
    def test_chaos_api_test_wsgi(self):
        result = chaos_api_test(
            app=_wsgi_app,
            wsgi=True,
            faults=[],
            max_examples=3,
            seed=42,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.passed is True

    def test_wsgi_with_faults(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=_wsgi_app,
            wsgi=True,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            max_examples=3,
        )
        assert isinstance(result, ChaosAPIResult)
        for name, count in result.fault_activations.items():
            assert count > 0, f"{name} was never activated"


# ---------------------------------------------------------------------------
# Tracker cleanup
# ---------------------------------------------------------------------------


class TestTrackerCleanup:
    def test_tracker_restored_after_chaos_api_test(self):
        """chaos_api_test should restore tracker.active to its prior state."""
        tracker.active = False
        chaos_api_test(app=asgi_app, faults=[], max_examples=3, seed=42)
        assert tracker.active is False

    def test_tracker_restored_when_previously_active(self):
        tracker.active = True
        chaos_api_test(app=asgi_app, faults=[], max_examples=3, seed=42)
        assert tracker.active is True

    def test_faults_deactivated_after_run(self):
        """All faults should be deactivated after chaos_api_test returns."""
        faults = _make_faults(3)
        chaos_api_test(
            app=asgi_app,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            max_examples=5,
        )
        assert all(not f.active for f in faults)


# ---------------------------------------------------------------------------
# Fault that raises during activate/deactivate
# ---------------------------------------------------------------------------


class TestFaultyFault:
    def test_fault_that_raises_on_activate(self):
        """Scheduler should tolerate faults that raise during activate."""

        def bad_activate():
            raise RuntimeError("activate boom")

        bad = LambdaFault("bad", on_activate=bad_activate, on_deactivate=lambda: None)
        good = _make_faults(1)[0]

        result = chaos_api_test(
            app=asgi_app,
            faults=[bad, good],
            fault_probability=1.0,
            seed=42,
            max_examples=3,
        )
        # Should complete without crashing.
        assert isinstance(result, ChaosAPIResult)


# ---------------------------------------------------------------------------
# auto_faults + auto_discover
# ---------------------------------------------------------------------------


class TestAutoFaults:
    def test_generates_mutation_faults(self):
        faults = auto_faults(
            ["tests._demo_shop.get_stock_level"],
            include_semantic=False,
        )
        mutation = [f for f in faults if f.name.startswith("mutant:")]
        assert len(mutation) > 0

    def test_generates_semantic_faults(self):
        faults = auto_faults(
            ["tests._demo_shop.get_stock_level"],
            operators=[],  # no mutations, just semantic
        )
        names = {f.name for f in faults}
        assert "returns_none(get_stock_level)" in names
        assert "raises(get_stock_level)" in names
        assert "stale(get_stock_level)" in names

    def test_type_hint_sentinels(self):
        """get_stock_level returns int — should generate returns_zero, returns_negative."""
        faults = auto_faults(
            ["tests._demo_shop.get_stock_level"],
            operators=[],
        )
        names = {f.name for f in faults}
        assert "returns_zero(get_stock_level)" in names
        assert "returns_negative(get_stock_level)" in names

    def test_generates_dependency_faults(self):
        faults = auto_faults(
            ["tests._demo_shop.create_order"],
            operators=[],
            include_semantic=False,
        )
        dep = [f for f in faults if f.name.startswith("error_on_call")]
        assert len(dep) > 0

    def test_empty_targets(self):
        faults = auto_faults([])
        assert faults == []


class TestDiscoverHandlers:
    def test_finds_handlers(self):
        from tests._demo_shop import app as shop_app

        targets = _discover_handlers(shop_app)
        names = [t.rsplit(".", 1)[1] for t in targets]
        assert "list_stock" in names
        assert "get_stock" in names
        assert "create_order" in names
        assert "get_order" in names

    def test_follows_call_graph(self):
        from tests._demo_shop import app as shop_app

        targets = _discover_handlers(shop_app)
        names = [t.rsplit(".", 1)[1] for t in targets]
        # Should follow handlers into service-layer functions.
        assert "get_stock_level" in names
        assert "place_new_order" in names
        assert "get_order_by_id" in names

    def test_skips_openapi_endpoint(self):
        from tests._demo_shop import app as shop_app

        targets = _discover_handlers(shop_app)
        names = [t.rsplit(".", 1)[1] for t in targets]
        assert "openapi_schema" not in names

    def test_max_depth_zero(self):
        """Depth 0 should only return handlers, not callees."""
        from tests._demo_shop import app as shop_app

        targets = _discover_handlers(shop_app, max_depth=0)
        names = [t.rsplit(".", 1)[1] for t in targets]
        assert "list_stock" in names
        assert "get_stock_level" not in names


class TestAutoDiscover:
    def test_chaos_api_test_auto_discover(self):
        from tests._demo_shop import app as shop_app

        result = chaos_api_test(
            app=shop_app,
            auto_discover=True,
            seed=42,
            max_examples=10,
            stateful=False,
        )
        assert isinstance(result, ChaosAPIResult)
        # Should have auto-generated faults.
        assert len(result.fault_activations) > 0

    def test_mutation_targets(self):
        from tests._demo_shop import app as shop_app

        result = chaos_api_test(
            app=shop_app,
            mutation_targets=["tests._demo_shop.get_stock_level"],
            seed=42,
            max_examples=10,
            stateful=False,
        )
        assert isinstance(result, ChaosAPIResult)
        assert len(result.fault_activations) > 0
