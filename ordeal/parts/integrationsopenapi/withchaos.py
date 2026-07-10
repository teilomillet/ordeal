from __future__ import annotations
# ruff: noqa
# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def with_chaos(
    faults: list[Fault],
    *,
    fault_probability: float = 0.3,
    seed: int | None = None,
    swarm: bool = False,
) -> Callable:
    """Decorator that wraps a test function with fault injection.

    Before each call, randomly activates/deactivates faults.
    After the call, resets all faults to avoid cross-request interference.

    Args:
        faults: Fault instances to inject.
        fault_probability: Probability of each fault being active per request.
        seed: Random seed for reproducibility.
        swarm: Use swarm mode -- pick a random subset of faults once, then
            toggle only those for the lifetime of the wrapper.
    """
    scheduler = _FaultScheduler(
        faults,
        fault_probability=fault_probability,
        seed=seed,
        swarm=swarm,
    )

    def decorator(test_fn: Callable) -> Callable:
        @functools.wraps(test_fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracker.active = True
            scheduler.before_request()
            try:
                return test_fn(*args, **kwargs)
            finally:
                scheduler.after_request()

        return wrapper

    return decorator
# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def _validate_response(
    response: _Response,
    endpoint: _Endpoint,
    active_faults: list[str],
) -> dict[str, Any] | None:
    """Return a failure dict if the response violates HTTP semantics.

    Validates at three layers:

    1. **Application**: 5xx status codes.
    2. **Protocol**: Content-Length mismatch, Transfer-Encoding conflict,
       body on 204/304, JSON Content-Type with non-JSON body.
    3. **Security**: missing security headers on non-fault responses.

    Each check catches a class of bug that is invisible at the application
    layer but crashes or misbehaves at the transport/browser layer.
    """
    prefix = f"{endpoint.method} {endpoint.path}"
    headers = {k.lower(): v for k, v in response.headers.items()}
    status = response.status_code

    def _fail(fail_type: str, msg: str, **extra: Any) -> dict[str, Any]:
        return {
            "type": fail_type,
            "error": msg,
            "endpoint": endpoint.path,
            "method": endpoint.method,
            "status_code": status,
            "active_faults": active_faults,
            **extra,
        }

    # -- Application layer --

    if status >= 500:
        return _fail("server_error", f"{prefix} returned {status}")

    # -- Protocol layer --

    # Content-Length must match actual body length.
    # Middleware (CORS, compression) can modify the body after headers are
    # set, causing "Response content longer than Content-Length" at the
    # transport layer (uvicorn/hypercorn).
    cl = headers.get("content-length")
    if cl is not None:
        try:
            declared = int(cl)
            actual = len(response.body)
            if declared != actual:
                return _fail(
                    "content_length_mismatch",
                    f"{prefix}: Content-Length={declared} but body is {actual} bytes. "
                    "Causes RuntimeError at the transport layer. "
                    "Likely middleware modifying the response after headers were set.",
                    declared_length=declared,
                    actual_length=actual,
                )
        except (ValueError, TypeError):
            pass

    # Content-Length + Transfer-Encoding: chunked is a protocol violation.
    # RFC 7230 §3.3.3: "A sender MUST NOT send a Content-Length header
    # field in any message that contains a Transfer-Encoding header field."
    te = headers.get("transfer-encoding", "")
    if cl is not None and "chunked" in te.lower():
        return _fail(
            "conflicting_transfer_headers",
            f"{prefix}: has both Content-Length and Transfer-Encoding: chunked. "
            "RFC 7230 §3.3.3 forbids this. Proxies may drop the connection.",
        )

    # 204 No Content and 304 Not Modified MUST NOT have a body.
    # Frameworks sometimes return a body anyway (e.g. error middleware).
    # Proxies and browsers may reject or misinterpret the response.
    if status in (204, 304) and len(response.body) > 0:
        return _fail(
            "body_on_no_content",
            f"{prefix}: status {status} with {len(response.body)}-byte body. "
            f"HTTP {status} MUST NOT contain a message body (RFC 7230). "
            "Proxies may close the connection or misframe subsequent requests.",
        )

    # JSON Content-Type with non-JSON body.
    # Common when error middleware replaces a JSON response with HTML/plain
    # text but preserves the original Content-Type header.
    ct = headers.get("content-type", "")
    if "application/json" in ct and response.body:
        try:
            json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _fail(
                "invalid_json_body",
                f"{prefix}: Content-Type is application/json but body is not valid JSON. "
                "Clients calling .json() will crash. "
                "Likely an error handler returning plain text with the wrong Content-Type.",
            )

    # Non-empty body with no Content-Type.
    # Browsers guess the type (MIME sniffing), which is a security risk
    # (XSS via type confusion). Servers should always declare Content-Type.
    if response.body and not ct and status not in (204, 304):
        return _fail(
            "missing_content_type",
            f"{prefix}: {len(response.body)}-byte body with no Content-Type header. "
            "Browsers will MIME-sniff the content, which can lead to XSS. "
            "Add a Content-Type header to every response with a body.",
        )

    # CORS headers disappear under faults.
    # When faults are active and the response has no Access-Control-Allow-Origin,
    # but the request is likely cross-origin, the browser blocks the response.
    # This is the #1 fault-induced regression: error handlers skip CORS middleware.
    if active_faults and "access-control-allow-origin" not in headers and status < 500:
        acao_expected = any(
            h in headers for h in ("access-control-allow-methods", "access-control-max-age")
        )
        if acao_expected:
            return _fail(
                "cors_header_lost",
                f"{prefix}: CORS response headers partially present but "
                "Access-Control-Allow-Origin is missing (faults active: "
                f"{', '.join(active_faults)}). "
                "Error/fault handlers often bypass CORS middleware, "
                "causing browsers to block the response entirely.",
            )

    # Response body vs OpenAPI schema contract.
    # If the spec declares a schema for this status code, validate the body
    # against it. Catches drift between spec and implementation.
    schema = endpoint.response_schemas.get(status)
    if schema and "application/json" in ct and response.body:
        try:
            body_data = json.loads(response.body)
            errors = _validate_json_schema(body_data, schema)
            if errors:
                return _fail(
                    "schema_violation",
                    f"{prefix}: response body violates OpenAPI schema. {errors[0]}",
                    schema_errors=errors,
                )
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # already caught by invalid_json_body above

    return None
def _validate_json_schema(data: Any, schema: dict) -> list[str]:
    """Minimal JSON Schema validator — no external dependencies.

    Validates type, required properties, and basic structure.
    Returns a list of human-readable error strings (empty = valid).
    """
    errors: list[str] = []
    schema_type = schema.get("type")

    if schema_type == "object":
        if not isinstance(data, dict):
            errors.append(f"Expected object, got {type(data).__name__}")
            return errors
        # Check required properties
        for prop in schema.get("required", []):
            if prop not in data:
                errors.append(f"Missing required property: {prop!r}")
        # Validate property types
        properties = schema.get("properties", {})
        for prop, prop_schema in properties.items():
            if prop in data:
                errors.extend(
                    f"{prop}: {e}" for e in _validate_json_schema(data[prop], prop_schema)
                )
    elif schema_type == "array":
        if not isinstance(data, list):
            errors.append(f"Expected array, got {type(data).__name__}")
            return errors
        items_schema = schema.get("items", {})
        if items_schema:
            for i, item in enumerate(data[:5]):  # check first 5 items
                errors.extend(f"[{i}]: {e}" for e in _validate_json_schema(item, items_schema))
    elif schema_type == "string":
        if not isinstance(data, str):
            errors.append(f"Expected string, got {type(data).__name__}")
    elif schema_type == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            errors.append(f"Expected integer, got {type(data).__name__}")
    elif schema_type == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            errors.append(f"Expected number, got {type(data).__name__}")
    elif schema_type == "boolean":
        if not isinstance(data, bool):
            errors.append(f"Expected boolean, got {type(data).__name__}")

    return errors
def _analyze_cross_request(
    responses: list[tuple[_Endpoint, _Response, list[str]]]
    | list[tuple[_Endpoint, _Response, list[str], float]],
) -> list[dict[str, Any]]:
    """Post-run analysis of cross-request patterns.

    Detects bugs that only appear when comparing multiple responses:
    - Error format inconsistency (some endpoints return JSON, others HTML)
    - CORS disappearing on fault responses vs normal responses
    - Latency spikes under faults (missing timeout handling)
    """
    findings: list[dict[str, Any]] = []

    # Track error response formats per endpoint
    error_formats: dict[str, set[str]] = {}
    normal_cors: dict[str, bool] = {}
    fault_cors: dict[str, bool] = {}
    normal_latencies: dict[str, list[float]] = {}
    fault_latencies: dict[str, list[float]] = {}

    for entry in responses:
        if len(entry) == 4:
            ep, resp, faults, duration = entry
        else:
            ep, resp, faults = entry[:3]
            duration = 0.0
        key = f"{ep.method} {ep.path}"
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        has_cors = "access-control-allow-origin" in hdrs

        if 400 <= resp.status_code < 600:
            ct = hdrs.get("content-type", "none")
            base_ct = ct.split(";")[0].strip().lower()
            error_formats.setdefault(key, set()).add(base_ct)

        if faults:
            fault_cors[key] = has_cors
            fault_latencies.setdefault(key, []).append(duration)
        else:
            normal_cors[key] = has_cors
            normal_latencies.setdefault(key, []).append(duration)

    # Error format inconsistency: same endpoint returns different Content-Types
    # for errors. Clients parsing JSON errors will crash on HTML errors.
    for ep_key, formats in error_formats.items():
        if len(formats) > 1:
            findings.append(
                {
                    "type": "inconsistent_error_format",
                    "error": (
                        f"{ep_key}: error responses use mixed Content-Types: "
                        f"{', '.join(sorted(formats))}. "
                        "Clients expecting JSON errors will crash on non-JSON responses. "
                        "Ensure all error handlers return the same format."
                    ),
                    "endpoint": ep_key,
                    "formats": sorted(formats),
                }
            )

    # CORS present on normal responses but missing on fault responses.
    # This means faults bypass the CORS middleware chain.
    for ep_key, has_normal in normal_cors.items():
        has_fault = fault_cors.get(ep_key, True)
        if has_normal and not has_fault:
            findings.append(
                {
                    "type": "cors_lost_under_faults",
                    "error": (
                        f"{ep_key}: CORS headers present on normal responses "
                        "but missing when faults are active. "
                        "Error/fault code paths bypass the CORS middleware, "
                        "causing browsers to block fault responses entirely."
                    ),
                    "endpoint": ep_key,
                }
            )

    # Latency spikes under faults — indicates missing timeout handling.
    # If fault responses are 5x+ slower than normal, the fault is probably
    # hitting a code path without a timeout (e.g. a retry loop on a dead DB).
    for ep_key, normal_times in normal_latencies.items():
        fault_times = fault_latencies.get(ep_key)
        if not fault_times or not normal_times:
            continue
        normal_avg = sum(normal_times) / len(normal_times)
        fault_avg = sum(fault_times) / len(fault_times)
        if normal_avg > 0 and fault_avg > normal_avg * 5 and fault_avg > 0.5:
            findings.append(
                {
                    "type": "latency_spike_under_faults",
                    "error": (
                        f"{ep_key}: avg response time {fault_avg:.2f}s under faults "
                        f"vs {normal_avg:.3f}s normally ({fault_avg / normal_avg:.0f}x slower). "
                        "This suggests a missing timeout — the fault is probably "
                        "hitting a retry loop or blocking call without a deadline."
                    ),
                    "endpoint": ep_key,
                    "normal_avg_seconds": round(normal_avg, 4),
                    "fault_avg_seconds": round(fault_avg, 4),
                }
            )

    return findings
def _execute_case(
    case: _APICase,
    *,
    client: _ASGIClient | _WSGIClient | _URLClient,
    scheduler: _FaultScheduler,
    collector: _TraceCollector | None,
    extra_headers: dict[str, str],
    ep_map: dict[str, _Endpoint],
    all_responses: list[tuple[_Endpoint, _Response, list[str], float]],
    failures: list[dict[str, Any]],
) -> tuple[_Response, _Endpoint | None]:
    """Execute one API case, record telemetry, and validate the response."""
    call_headers = {**extra_headers, **case.headers}
    if case.body is not None:
        call_headers.setdefault("content-type", "application/json")

    active = scheduler.before_request()
    if collector is not None:
        collector.before(active)

    try:
        body_bytes = json.dumps(case.body).encode() if case.body is not None else None
        path = case.path
        if case.query_params:
            qs = urllib.parse.urlencode(case.query_params)
            path = f"{path}?{qs}"

        req_t0 = time.monotonic()
        response = client.request(case.method, path, call_headers, body_bytes)
        req_duration = time.monotonic() - req_t0
        if collector is not None:
            collector.after(case.method, case.endpoint_path, response.status_code)

        endpoint = ep_map.get(case.endpoint_path)
        if endpoint is not None:
            all_responses.append((endpoint, response, list(active), req_duration))
            failure = _validate_response(response, endpoint, active)
            if failure is not None:
                failures.append(failure)
        return response, endpoint
    except Exception:
        if collector is not None:
            collector.after(case.method, case.endpoint_path, None)
        raise
    finally:
        scheduler.after_request()
def _follow_linked_sequence(
    *,
    endpoint: _Endpoint,
    case: _APICase,
    response: _Response,
    root: dict,
    operation_ids: dict[str, _Endpoint],
    operation_refs: dict[tuple[str, str], _Endpoint],
    client: _ASGIClient | _WSGIClient | _URLClient,
    scheduler: _FaultScheduler,
    collector: _TraceCollector | None,
    extra_headers: dict[str, str],
    ep_map: dict[str, _Endpoint],
    all_responses: list[tuple[_Endpoint, _Response, list[str], float]],
    failures: list[dict[str, Any]],
    max_depth: int = 3,
) -> bool:
    """Follow one supported OpenAPI link chain from a response."""
    used = False
    current_endpoint = endpoint
    current_case = case
    current_response = response

    for _ in range(max_depth):
        links = current_endpoint.response_links.get(current_response.status_code, [])
        next_endpoint: _Endpoint | None = None
        next_case: _APICase | None = None
        for link in links:
            target = _resolve_link_target(
                link,
                operation_ids=operation_ids,
                operation_refs=operation_refs,
            )
            if target is None:
                continue
            next_endpoint = target
            next_case = _linked_case(
                link,
                target,
                root=root,
                response=current_response,
                case=current_case,
            )
            break

        if next_endpoint is None or next_case is None:
            break

        used = True
        current_response, resolved_endpoint = _execute_case(
            next_case,
            client=client,
            scheduler=scheduler,
            collector=collector,
            extra_headers=extra_headers,
            ep_map=ep_map,
            all_responses=all_responses,
            failures=failures,
        )
        if resolved_endpoint is None:
            break
        current_endpoint = resolved_endpoint
        current_case = next_case

    return used
