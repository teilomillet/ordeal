"""Tests for ordeal.integrations.openapi — built-in OpenAPI chaos testing engine.

Exercises the complete engine: $ref resolution, JSON Schema -> strategy
generation, OpenAPI parsing, ASGI/WSGI test clients, and the chaos_api_test
entry point.  No external dependencies beyond hypothesis + stdlib.
"""

from __future__ import annotations

import json

import pytest

from ordeal.assertions import tracker
from ordeal.faults import LambdaFault
from ordeal.integrations.openapi import (
    ChaosAPIResult,
    _APICase,
    _ASGIClient,
    _Endpoint,
    _endpoint_strategy,
    _FaultScheduler,
    _parse_endpoints,
    _resolve_refs,
    _Response,
    _schema_to_strategy,
    _validate_response,
    _WSGIClient,
    chaos_api_test,
    with_chaos,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_faults(n: int = 3) -> list[LambdaFault]:
    """Create n trackable faults."""
    log: list[str] = []
    faults = []
    for i in range(n):
        f = LambdaFault(
            f"fault_{i}",
            on_activate=lambda i=i: log.append(f"on_{i}"),
            on_deactivate=lambda i=i: log.append(f"off_{i}"),
        )
        f._test_log = log
        faults.append(f)
    return faults


# ============================================================================
# $ref resolution
# ============================================================================


class TestResolveRefs:
    def test_simple_ref(self):
        root = {
            "components": {"schemas": {"Item": {"type": "object"}}},
            "paths": {"$ref": "#/components/schemas/Item"},
        }
        result = _resolve_refs(root["paths"], root)
        assert result == {"type": "object"}

    def test_nested_ref(self):
        root = {
            "components": {
                "schemas": {
                    "Name": {"type": "string"},
                    "Item": {
                        "type": "object",
                        "properties": {"name": {"$ref": "#/components/schemas/Name"}},
                    },
                }
            }
        }
        result = _resolve_refs({"$ref": "#/components/schemas/Item"}, root)
        assert result == {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }

    def test_ref_in_list(self):
        root = {"components": {"schemas": {"T": {"type": "integer"}}}}
        node = [{"$ref": "#/components/schemas/T"}, {"type": "string"}]
        result = _resolve_refs(node, root)
        assert result == [{"type": "integer"}, {"type": "string"}]

    def test_no_ref_passthrough(self):
        node = {"type": "string", "minLength": 1}
        assert _resolve_refs(node, {}) == node

    def test_scalar_passthrough(self):
        assert _resolve_refs(42, {}) == 42
        assert _resolve_refs("hello", {}) == "hello"
        assert _resolve_refs(True, {}) is True

    def test_tilde_escape(self):
        root = {"components": {"a/b": {"type": "boolean"}}}
        node = {"$ref": "#/components/a~1b"}
        result = _resolve_refs(node, root)
        assert result == {"type": "boolean"}


# ============================================================================
# JSON Schema -> strategy
# ============================================================================


class TestSchemaToStrategy:
    def test_integer(self):
        strat = _schema_to_strategy({"type": "integer"}, {})
        value = strat.example()
        assert isinstance(value, int)

    def test_integer_with_bounds(self):
        strat = _schema_to_strategy({"type": "integer", "minimum": 1, "maximum": 10}, {})
        for _ in range(20):
            v = strat.example()
            assert 1 <= v <= 10

    def test_number(self):
        strat = _schema_to_strategy({"type": "number"}, {})
        value = strat.example()
        assert isinstance(value, (int, float))

    def test_string(self):
        strat = _schema_to_strategy({"type": "string"}, {})
        value = strat.example()
        assert isinstance(value, str)

    def test_string_with_length(self):
        strat = _schema_to_strategy({"type": "string", "minLength": 2, "maxLength": 5}, {})
        for _ in range(20):
            v = strat.example()
            assert isinstance(v, str)

    def test_boolean(self):
        strat = _schema_to_strategy({"type": "boolean"}, {})
        value = strat.example()
        assert isinstance(value, bool)

    def test_null(self):
        strat = _schema_to_strategy({"type": "null"}, {})
        assert strat.example() is None

    def test_enum(self):
        strat = _schema_to_strategy({"enum": ["a", "b", "c"]}, {})
        for _ in range(20):
            assert strat.example() in {"a", "b", "c"}

    def test_const(self):
        strat = _schema_to_strategy({"const": 42}, {})
        assert strat.example() == 42

    def test_array(self):
        strat = _schema_to_strategy(
            {"type": "array", "items": {"type": "integer"}, "maxItems": 5}, {}
        )
        value = strat.example()
        assert isinstance(value, list)
        assert all(isinstance(x, int) for x in value)

    def test_object(self):
        strat = _schema_to_strategy(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
                "required": ["name"],
            },
            {},
        )
        value = strat.example()
        assert isinstance(value, dict)
        assert "name" in value
        assert isinstance(value["name"], str)

    def test_object_optional_fields(self):
        strat = _schema_to_strategy(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["name"],
            },
            {},
        )
        # Over many draws, some should have tag and some shouldn't
        values = [strat.example() for _ in range(30)]
        assert all("name" in v for v in values)

    def test_nullable(self):
        strat = _schema_to_strategy({"type": "string", "nullable": True}, {})
        values = [strat.example() for _ in range(50)]
        types = {type(v) for v in values}
        # Should produce both str and None
        assert str in types or type(None) in types

    def test_one_of(self):
        strat = _schema_to_strategy({"oneOf": [{"type": "string"}, {"type": "integer"}]}, {})
        values = [strat.example() for _ in range(30)]
        types = {type(v) for v in values}
        assert types & {str, int}

    def test_any_of(self):
        strat = _schema_to_strategy({"anyOf": [{"type": "boolean"}, {"type": "null"}]}, {})
        value = strat.example()
        assert isinstance(value, bool) or value is None

    def test_all_of_merges(self):
        strat = _schema_to_strategy(
            {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}},
                        "required": ["a"],
                    },
                    {
                        "type": "object",
                        "properties": {"b": {"type": "string"}},
                        "required": ["b"],
                    },
                ]
            },
            {},
        )
        value = strat.example()
        assert "a" in value and "b" in value

    def test_ref_resolution(self):
        root = {"components": {"schemas": {"Name": {"type": "string"}}}}
        strat = _schema_to_strategy({"$ref": "#/components/schemas/Name"}, root)
        assert isinstance(strat.example(), str)

    def test_depth_guard(self):
        # Deeply nested schema should not recurse forever
        strat = _schema_to_strategy({"type": "string"}, {}, _depth=11)
        assert strat.example() is None

    def test_none_schema(self):
        strat = _schema_to_strategy(None, {})
        assert strat.example() is None

    def test_empty_schema(self):
        strat = _schema_to_strategy({}, {})
        assert strat.example() is None

    def test_format_datetime(self):
        strat = _schema_to_strategy({"type": "string", "format": "date-time"}, {})
        value = strat.example()
        assert isinstance(value, str)
        assert "T" in value  # ISO format

    def test_format_date(self):
        strat = _schema_to_strategy({"type": "string", "format": "date"}, {})
        value = strat.example()
        assert isinstance(value, str)
        assert "-" in value

    def test_format_uuid(self):
        strat = _schema_to_strategy({"type": "string", "format": "uuid"}, {})
        value = strat.example()
        assert isinstance(value, str)
        assert len(value) == 36  # UUID format

    def test_format_email(self):
        strat = _schema_to_strategy({"type": "string", "format": "email"}, {})
        value = strat.example()
        assert "@" in value


# ============================================================================
# OpenAPI parser
# ============================================================================


SAMPLE_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Test", "version": "1.0"},
    "paths": {
        "/items": {
            "get": {
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            }
                        }
                    }
                },
                "responses": {
                    "201": {"description": "created"},
                    "400": {"description": "bad"},
                },
            },
        },
        "/items/{id}": {
            "get": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {"description": "ok"},
                    "404": {"description": "not found"},
                },
            },
        },
    },
}


class TestParseEndpoints:
    def test_endpoint_count(self):
        eps = _parse_endpoints(SAMPLE_SPEC)
        assert len(eps) == 3

    def test_methods(self):
        eps = _parse_endpoints(SAMPLE_SPEC)
        methods = {ep.method for ep in eps}
        assert methods == {"GET", "POST"}

    def test_path_params(self):
        eps = _parse_endpoints(SAMPLE_SPEC)
        get_by_id = [ep for ep in eps if ep.path == "/items/{id}"][0]
        assert len(get_by_id.path_params) == 1
        assert get_by_id.path_params[0]["name"] == "id"

    def test_request_body(self):
        eps = _parse_endpoints(SAMPLE_SPEC)
        post = [ep for ep in eps if ep.method == "POST"][0]
        assert post.request_body is not None
        assert post.request_body["type"] == "object"

    def test_response_codes(self):
        eps = _parse_endpoints(SAMPLE_SPEC)
        post = [ep for ep in eps if ep.method == "POST"][0]
        assert 201 in post.response_codes
        assert 400 in post.response_codes

    def test_empty_paths(self):
        assert _parse_endpoints({"paths": {}}) == []

    def test_no_paths(self):
        assert _parse_endpoints({}) == []

    def test_with_refs(self):
        spec = {
            "components": {
                "schemas": {"Name": {"type": "string"}},
                "parameters": {
                    "IdParam": {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                },
            },
            "paths": {
                "/things/{id}": {
                    "get": {
                        "parameters": [{"$ref": "#/components/parameters/IdParam"}],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        eps = _parse_endpoints(spec)
        assert len(eps) == 1
        assert eps[0].path_params[0]["name"] == "id"


# ============================================================================
# Endpoint strategy
# ============================================================================


class TestEndpointStrategy:
    def test_generates_api_case(self):
        ep = _Endpoint(
            method="GET",
            path="/items",
            response_codes={200},
        )
        strat = _endpoint_strategy(ep, {})
        case = strat.example()
        assert isinstance(case, _APICase)
        assert case.method == "GET"
        assert case.path == "/items"

    def test_path_param_substitution(self):
        ep = _Endpoint(
            method="GET",
            path="/items/{id}",
            path_params=[{"name": "id", "schema": {"type": "integer"}}],
            response_codes={200},
        )
        strat = _endpoint_strategy(ep, {})
        case = strat.example()
        assert "{id}" not in case.path

    def test_body_generation(self):
        ep = _Endpoint(
            method="POST",
            path="/items",
            request_body={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            response_codes={201},
        )
        strat = _endpoint_strategy(ep, {})
        case = strat.example()
        assert case.body is not None
        assert "name" in case.body


# ============================================================================
# Test clients
# ============================================================================


# -- ASGI app for testing --


async def _asgi_app(scope, receive, send):
    """Minimal ASGI app for tests."""
    if scope["type"] != "http":
        return

    path = scope["path"]
    method = scope["method"]

    # Read body
    body = b""
    msg = await receive()
    body = msg.get("body", b"")

    if path == "/openapi.json":
        spec = json.dumps(SAMPLE_SPEC).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": spec})
        return

    if path == "/items" and method == "GET":
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"[]"})
        return

    if path == "/items" and method == "POST":
        try:
            data = json.loads(body)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                resp = json.dumps({"id": 1, "name": data["name"]}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 201,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": resp})
                return
        except Exception:
            pass
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"bad"}'})
        return

    # Default: 404
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"error":"not found"}'})


# -- WSGI app for testing --


def _wsgi_app(environ, start_response):
    """Minimal WSGI app for tests."""
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    if path == "/openapi.json":
        body = json.dumps(SAMPLE_SPEC).encode()
        start_response("200 OK", [("Content-Type", "application/json")])
        return [body]

    if path == "/items" and method == "GET":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b"[]"]

    if path == "/items" and method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH", 0))
            raw = environ["wsgi.input"].read(length)
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                resp = json.dumps({"id": 1, "name": data["name"]}).encode()
                start_response("201 Created", [("Content-Type", "application/json")])
                return [resp]
        except Exception:
            pass
        start_response("400 Bad Request", [("Content-Type", "application/json")])
        return [b'{"error":"bad"}']

    start_response("404 Not Found", [("Content-Type", "application/json")])
    return [b'{"error":"not found"}']


class TestASGIClient:
    def test_get(self):
        client = _ASGIClient(_asgi_app)
        resp = client.request("GET", "/items")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_post(self):
        client = _ASGIClient(_asgi_app)
        body = json.dumps({"name": "test"}).encode()
        resp = client.request("POST", "/items", {"content-type": "application/json"}, body)
        assert resp.status_code == 201
        assert resp.json()["name"] == "test"

    def test_404(self):
        client = _ASGIClient(_asgi_app)
        resp = client.request("GET", "/nope")
        assert resp.status_code == 404

    def test_get_schema(self):
        client = _ASGIClient(_asgi_app)
        spec = client.get_schema("/openapi.json")
        assert spec["openapi"] == "3.0.3"
        assert "/items" in spec["paths"]

    def test_query_string(self):
        client = _ASGIClient(_asgi_app)
        resp = client.request("GET", "/items?limit=10")
        assert resp.status_code == 200


class TestWSGIClient:
    def test_get(self):
        client = _WSGIClient(_wsgi_app)
        resp = client.request("GET", "/items")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_post(self):
        client = _WSGIClient(_wsgi_app)
        body = json.dumps({"name": "test"}).encode()
        resp = client.request("POST", "/items", {"content-type": "application/json"}, body)
        assert resp.status_code == 201
        assert resp.json()["name"] == "test"

    def test_404(self):
        client = _WSGIClient(_wsgi_app)
        resp = client.request("GET", "/nope")
        assert resp.status_code == 404

    def test_get_schema(self):
        client = _WSGIClient(_wsgi_app)
        spec = client.get_schema("/openapi.json")
        assert spec["openapi"] == "3.0.3"

    def test_headers_forwarded(self):
        calls: list[dict] = []

        def capturing_app(environ, start_response):
            calls.append(dict(environ))
            start_response("200 OK", [])
            return [b"ok"]

        client = _WSGIClient(capturing_app)
        client.request("GET", "/test", headers={"X-Custom": "val"})
        assert calls[0]["HTTP_X_CUSTOM"] == "val"


# ============================================================================
# Response validation
# ============================================================================


class TestValidateResponse:
    def test_5xx_is_failure(self):
        resp = _Response(status_code=500, headers={}, body=b"")
        ep = _Endpoint(method="GET", path="/items", response_codes={200})
        fail = _validate_response(resp, ep, ["fault_0"])
        assert fail is not None
        assert fail["type"] == "server_error"
        assert fail["status_code"] == 500

    def test_200_is_ok(self):
        resp = _Response(status_code=200, headers={}, body=b"[]")
        ep = _Endpoint(method="GET", path="/items", response_codes={200})
        assert _validate_response(resp, ep, []) is None

    def test_404_is_ok(self):
        resp = _Response(status_code=404, headers={}, body=b"")
        ep = _Endpoint(method="GET", path="/items/{id}", response_codes={200, 404})
        assert _validate_response(resp, ep, []) is None


# ============================================================================
# ChaosAPIResult
# ============================================================================


class TestChaosAPIResult:
    def test_passed_no_failures(self):
        result = ChaosAPIResult(
            total_requests=10,
            failures=[],
            fault_activations={},
            duration_seconds=1.0,
            deferred_ok=True,
        )
        assert result.passed is True

    def test_not_passed_with_failures(self):
        result = ChaosAPIResult(
            total_requests=10,
            failures=[{"type": "error", "error": "boom"}],
            fault_activations={},
            duration_seconds=1.0,
            deferred_ok=True,
        )
        assert result.passed is False

    def test_not_passed_deferred_fail(self):
        result = ChaosAPIResult(
            total_requests=10,
            failures=[],
            fault_activations={},
            duration_seconds=1.0,
            deferred_ok=False,
        )
        assert result.passed is False

    def test_frozen(self):
        result = ChaosAPIResult(
            total_requests=0,
            failures=[],
            fault_activations={},
            duration_seconds=0.0,
            deferred_ok=True,
        )
        with pytest.raises(AttributeError):
            result.total_requests = 5


# ============================================================================
# _FaultScheduler
# ============================================================================


class TestFaultScheduler:
    def test_request_counting(self):
        faults = _make_faults(2)
        sched = _FaultScheduler(faults, fault_probability=1.0, seed=42)
        sched.before_request()
        sched.after_request()
        sched.before_request()
        assert sched.request_count == 2

    def test_activation_tracking(self):
        faults = _make_faults(2)
        sched = _FaultScheduler(faults, fault_probability=1.0, seed=42)
        sched.before_request()
        assert all(v >= 1 for v in sched.activations.values())

    def test_swarm_subset(self):
        faults = _make_faults(5)
        sched = _FaultScheduler(faults, fault_probability=1.0, seed=42, swarm=True)
        assert len(sched.eligible) <= len(faults)
        assert len(sched.eligible) >= 1

    def test_seed_reproducibility(self):
        faults = _make_faults(3)
        patterns = []
        for _ in range(2):
            sched = _FaultScheduler(faults, fault_probability=0.5, seed=99)
            run = []
            for _ in range(10):
                active = sched.before_request()
                run.append(tuple(active))
                sched.after_request()
            patterns.append(run)
        assert patterns[0] == patterns[1]


# ============================================================================
# with_chaos decorator
# ============================================================================


class TestWithChaos:
    def test_decorator_toggles_faults(self):
        faults = _make_faults(2)
        decorated = with_chaos(faults, fault_probability=1.0, seed=42)(lambda: None)
        decorated()
        # After call, faults should be reset
        assert not any(f.active for f in faults)

    def test_tracker_activated(self):
        prev = tracker.active
        faults = _make_faults(1)

        @with_chaos(faults, fault_probability=0.0)
        def fn():
            return tracker.active

        result = fn()
        assert result is True
        tracker.active = prev


# ============================================================================
# chaos_api_test integration
# ============================================================================


class TestChaosApiTestASGI:
    def test_basic_run(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=_asgi_app,
            faults=faults,
            fault_probability=0.5,
            seed=42,
            max_examples=10,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.total_requests > 0

    def test_zero_probability(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=_asgi_app,
            faults=faults,
            fault_probability=0.0,
            seed=42,
            max_examples=5,
        )
        assert all(v == 0 for v in result.fault_activations.values())

    def test_full_probability(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=_asgi_app,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            max_examples=5,
        )
        assert all(v > 0 for v in result.fault_activations.values())

    def test_swarm_mode(self):
        faults = _make_faults(5)
        result = chaos_api_test(
            app=_asgi_app,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            swarm=True,
            max_examples=10,
        )
        # Swarm: not all faults will be activated
        activated = [k for k, v in result.fault_activations.items() if v > 0]
        assert len(activated) <= len(faults)

    def test_custom_schema_path(self):
        result = chaos_api_test(
            app=_asgi_app,
            faults=[],
            schema_path="/openapi.json",
            max_examples=3,
        )
        assert result.total_requests > 0

    def test_headers_forwarded(self):
        """Headers should be forwarded to each request."""
        result = chaos_api_test(
            app=_asgi_app,
            faults=[],
            headers={"X-Test": "yes"},
            max_examples=3,
        )
        assert result.total_requests > 0

    def test_record_traces(self):
        result = chaos_api_test(
            app=_asgi_app,
            faults=_make_faults(1),
            fault_probability=0.5,
            seed=42,
            max_examples=5,
            record_traces=True,
        )
        assert len(result.traces) == 1
        trace = result.traces[0]
        assert len(trace.steps) > 0

    def test_deferred_ok(self):
        result = chaos_api_test(
            app=_asgi_app,
            faults=[],
            max_examples=3,
        )
        assert result.deferred_ok is True

    def test_no_app_no_url_raises(self):
        with pytest.raises(ValueError, match="Provide either"):
            chaos_api_test(faults=[])


class TestChaosApiTestWSGI:
    def test_basic_wsgi(self):
        faults = _make_faults(1)
        result = chaos_api_test(
            app=_wsgi_app,
            wsgi=True,
            faults=faults,
            fault_probability=0.5,
            seed=42,
            max_examples=5,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.total_requests > 0

    def test_wsgi_with_faults(self):
        faults = _make_faults(2)
        result = chaos_api_test(
            app=_wsgi_app,
            wsgi=True,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            max_examples=5,
        )
        assert all(v > 0 for v in result.fault_activations.values())


# ============================================================================
# Backward compatibility: imports from schemathesis_ext still work
# ============================================================================


class TestBackwardCompat:
    def test_import_chaos_api_result(self):
        from ordeal.integrations.schemathesis_ext import ChaosAPIResult as R

        assert R is ChaosAPIResult

    def test_import_with_chaos(self):
        from ordeal.integrations.schemathesis_ext import with_chaos as wc

        assert wc is with_chaos

    def test_import_fault_scheduler(self):
        from ordeal.integrations.schemathesis_ext import _FaultScheduler as FS

        assert FS is _FaultScheduler

    def test_schemathesis_ext_chaos_api_test_works(self):
        from ordeal.integrations.schemathesis_ext import (
            chaos_api_test as schemathesis_chaos_api_test,
        )

        result = schemathesis_chaos_api_test(
            app=_asgi_app,
            faults=[],
            max_examples=3,
        )
        assert isinstance(result, ChaosAPIResult)
        assert result.total_requests > 0


# ============================================================================
# Tracker cleanup
# ============================================================================


class TestTrackerCleanup:
    def test_tracker_restored(self):
        prev = tracker.active
        chaos_api_test(
            app=_asgi_app,
            faults=[],
            max_examples=3,
        )
        assert tracker.active == prev

    def test_faults_deactivated(self):
        faults = _make_faults(3)
        chaos_api_test(
            app=_asgi_app,
            faults=faults,
            fault_probability=1.0,
            seed=42,
            max_examples=5,
        )
        assert not any(f.active for f in faults)
