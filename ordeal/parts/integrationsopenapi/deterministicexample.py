from __future__ import annotations
# ruff: noqa
def _deterministic_example(schema: dict | None, root: dict, *, _depth: int = 0) -> Any:
    """Generate a cheap deterministic example for linked follow-up requests."""
    if schema is None or not isinstance(schema, dict) or _depth > _MAX_REF_DEPTH:
        return None

    if "$ref" in schema:
        resolved = _resolve_refs(schema, root)
        return _deterministic_example(resolved, root, _depth=_depth + 1)

    if "const" in schema:
        return schema["const"]

    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    for key in ("oneOf", "anyOf"):
        if key in schema and schema[key]:
            return _deterministic_example(schema[key][0], root, _depth=_depth + 1)

    if "allOf" in schema and schema["allOf"]:
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            resolved = _resolve_refs(sub, root) if isinstance(sub, dict) and "$ref" in sub else sub
            if not isinstance(resolved, dict):
                continue
            for name, value in resolved.items():
                if name == "properties" and name in merged:
                    merged[name] = {**merged[name], **value}
                elif name == "required" and name in merged:
                    merged[name] = list(dict.fromkeys([*merged[name], *value]))
                else:
                    merged[name] = value
        return _deterministic_example(merged, root, _depth=_depth + 1)

    match schema.get("type"):
        case "integer":
            return schema.get("minimum", 0)
        case "number":
            return float(schema.get("minimum", 0.0))
        case "string":
            match schema.get("format"):
                case "date-time":
                    return "2024-01-01T00:00:00+00:00"
                case "date":
                    return "2024-01-01"
                case "uuid":
                    return "00000000-0000-0000-0000-000000000000"
                case "uri" | "url":
                    return "https://example.com/path"
                case "email":
                    return "ordeal@example.com"
            min_length = schema.get("minLength", 1)
            return "x" * max(1, min_length)
        case "boolean":
            return False
        case "null":
            return None
        case "array":
            item = _deterministic_example(schema.get("items", {}), root, _depth=_depth + 1)
            min_items = schema.get("minItems", 0)
            return [item for _ in range(min_items)]
        case "object" | None:
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if properties or required:
                return {
                    name: _deterministic_example(prop_schema, root, _depth=_depth + 1)
                    for name, prop_schema in properties.items()
                    if name in required
                }
    return None
def _json_pointer_get(data: Any, pointer: str) -> Any:
    """Resolve a JSON Pointer against dict/list data."""
    if pointer in ("", "#"):
        return data
    if pointer.startswith("#"):
        pointer = pointer[1:]
    if not pointer:
        return data
    if not pointer.startswith("/"):
        return None

    current = data
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return None
        else:
            return None
    return current
def _request_header(case: _APICase, name: str) -> Any:
    """Look up a request header case-insensitively."""
    lowered = name.lower()
    for key, value in case.headers.items():
        if key.lower() == lowered:
            return value
    return None
def _response_body_json(response: _Response) -> Any:
    """Parse a JSON response body when possible."""
    if not response.body:
        return None
    try:
        return json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
def _resolve_runtime_expression(expression: str, response: _Response, case: _APICase) -> Any:
    """Resolve an OpenAPI runtime expression against the current request/response."""
    if not expression.startswith("$"):
        return expression

    if expression == "$statusCode":
        return response.status_code

    if expression.startswith("$response.body"):
        body = _response_body_json(response)
        suffix = expression[len("$response.body") :]
        return _json_pointer_get(body, suffix)

    if expression.startswith("$response.header."):
        name = expression.removeprefix("$response.header.")
        return response.headers.get(name.lower())

    if expression.startswith("$request.path."):
        name = expression.removeprefix("$request.path.")
        return case.path_params.get(name)

    if expression.startswith("$request.query."):
        name = expression.removeprefix("$request.query.")
        return case.query_params.get(name)

    if expression.startswith("$request.header."):
        name = expression.removeprefix("$request.header.")
        return _request_header(case, name)

    if expression.startswith("$request.body"):
        suffix = expression[len("$request.body") :]
        return _json_pointer_get(case.body, suffix)

    return expression
def _resolve_link_value(value: Any, response: _Response, case: _APICase) -> Any:
    """Resolve runtime expressions recursively inside a Link Object value."""
    if isinstance(value, str):
        return _resolve_runtime_expression(value, response, case)
    if isinstance(value, dict):
        return {k: _resolve_link_value(v, response, case) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_link_value(v, response, case) for v in value]
    return value
def _materialize_case(
    endpoint: _Endpoint,
    root: dict,
    *,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    body: Any = _MISSING,
) -> _APICase:
    """Build one concrete API case from an endpoint and optional overrides."""
    resolved_path_params: dict[str, Any] = {}
    for param in endpoint.path_params:
        name = param["name"]
        schema = param.get("schema", {"type": "string"})
        value = (path_params or {}).get(name, _MISSING)
        if value is _MISSING:
            value = _deterministic_example(schema, root)
        resolved_path_params[name] = value

    resolved_query_params: dict[str, str] = {}
    for param in endpoint.query_params:
        name = param["name"]
        value = (query_params or {}).get(name, _MISSING)
        if value is _MISSING:
            if not param.get("required"):
                continue
            value = _deterministic_example(param.get("schema", {"type": "string"}), root)
        resolved_query_params[name] = "" if value is None else str(value)

    resolved_headers: dict[str, str] = {}
    for param in endpoint.header_params:
        name = param["name"]
        value = (headers or {}).get(name, _MISSING)
        if value is _MISSING:
            if not param.get("required"):
                continue
            value = _deterministic_example(param.get("schema", {"type": "string"}), root)
        resolved_headers[name] = "" if value is None else str(value)

    body_value = (
        _deterministic_example(endpoint.request_body, root)
        if body is _MISSING and endpoint.request_body is not None
        else (None if body is _MISSING else body)
    )

    resolved_path = endpoint.path
    for name, value in resolved_path_params.items():
        path_value = "0" if value is None else str(value)
        resolved_path = resolved_path.replace(
            f"{{{name}}}", urllib.parse.quote(path_value, safe="")
        )

    return _APICase(
        method=endpoint.method,
        path=resolved_path,
        headers=resolved_headers,
        query_params=resolved_query_params,
        body=body_value,
        endpoint_path=endpoint.path,
        path_params=resolved_path_params,
    )
def _resolve_link_target(
    link: _Link,
    *,
    operation_ids: dict[str, _Endpoint],
    operation_refs: dict[tuple[str, str], _Endpoint],
) -> _Endpoint | None:
    """Resolve a Link Object target to a parsed endpoint."""
    if link.operation_id:
        return operation_ids.get(link.operation_id)

    if not link.operation_ref or not link.operation_ref.startswith("#/paths/"):
        return None

    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in link.operation_ref[2:].split("/")
    ]
    if len(parts) != 3 or parts[0] != "paths":
        return None
    return operation_refs.get((parts[1], parts[2].upper()))
def _linked_case(
    link: _Link,
    target: _Endpoint,
    *,
    root: dict,
    response: _Response,
    case: _APICase,
) -> _APICase:
    """Build the follow-up request for one resolved OpenAPI link."""
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    headers: dict[str, Any] = {}

    path_names = {param["name"] for param in target.path_params}
    query_names = {param["name"] for param in target.query_params}
    header_names = {param["name"] for param in target.header_params}

    for name, raw_value in link.parameters.items():
        value = _resolve_link_value(raw_value, response, case)
        if name in path_names:
            path_params[name] = value
        elif name in query_names:
            query_params[name] = value
        elif name in header_names:
            headers[name] = value

    body = _MISSING
    if link.request_body is not None:
        body = _resolve_link_value(link.request_body, response, case)

    return _materialize_case(
        target,
        root,
        path_params=path_params,
        query_params=query_params,
        headers=headers,
        body=body,
    )
# ---------------------------------------------------------------------------
# Test clients
# ---------------------------------------------------------------------------


class _ASGIClient:
    """Minimal ASGI test client (no framework dependency)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _Response:
        parsed = urllib.parse.urlsplit(path)
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "path": parsed.path,
            "query_string": (parsed.query or "").encode(),
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }

        status_code = 0
        resp_headers: dict[str, str] = {}
        resp_body = bytearray()
        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": body or b"",
                    "more_body": False,
                }
            # After body, wait for disconnect (shouldn't normally reach here)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status_code, resp_headers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers", []):
                    resp_headers[k.decode().lower()] = v.decode()
            elif message["type"] == "http.response.body":
                resp_body.extend(message.get("body", b""))

        async def run() -> None:
            await self.app(scope, receive, send)

        # Run the ASGI app synchronously
        try:
            asyncio.get_running_loop()
            # Already in an event loop — run in a separate thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, run()).result()
        except RuntimeError:
            asyncio.run(run())

        return _Response(status_code=status_code, headers=resp_headers, body=bytes(resp_body))

    def get_schema(self, schema_path: str = "/openapi.json") -> dict:
        """Fetch and parse the OpenAPI schema from the app."""
        resp = self.request("GET", schema_path)
        return json.loads(resp.body)
class _WSGIClient:
    """Minimal WSGI test client (PEP 3333)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _Response:
        parsed = urllib.parse.urlsplit(path)
        body_bytes = body or b""

        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query or "",
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": "testserver",
            "wsgi.input": io.BytesIO(body_bytes),
            "wsgi.errors": io.StringIO(),
            "wsgi.url_scheme": "http",
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(body_bytes)),
        }

        if body_bytes:
            environ["CONTENT_TYPE"] = "application/json"

        for k, v in (headers or {}).items():
            key = f"HTTP_{k.upper().replace('-', '_')}"
            environ[key] = v

        status_code = 0
        resp_headers: dict[str, str] = {}

        def start_response(
            status: str,
            response_headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Any:
            nonlocal status_code, resp_headers
            status_code = int(status.split(" ", 1)[0])
            for k, v in response_headers:
                resp_headers[k.lower()] = v

        result = self.app(environ, start_response)
        try:
            resp_body = b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()

        return _Response(status_code=status_code, headers=resp_headers, body=resp_body)

    def get_schema(self, schema_path: str = "/openapi.json") -> dict:
        """Fetch and parse the OpenAPI schema from the app."""
        resp = self.request("GET", schema_path)
        return json.loads(resp.body)
class _URLClient:
    """HTTP client for remote servers via urllib (stdlib)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _Response:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                return _Response(
                    status_code=resp.status,
                    headers=resp_headers,
                    body=resp_body,
                )
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            return _Response(
                status_code=e.code,
                headers=resp_headers,
                body=resp_body,
            )

    def get_schema(self, schema_url: str = "/openapi.json") -> dict:
        """Fetch and parse the OpenAPI schema from a URL."""
        # If schema_url is a full URL, use it directly
        if schema_url.startswith("http"):
            url = schema_url
        else:
            url = f"{self.base_url}{schema_url}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
