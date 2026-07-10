from __future__ import annotations
# ruff: noqa
# ---------------------------------------------------------------------------
# Batteries-included entry point
# ---------------------------------------------------------------------------


def chaos_api_test(
    schema_url: str | None = None,
    *,
    app: Any = None,
    wsgi: bool = False,
    schema_path: str = "/openapi.json",
    faults: list[Fault] | None = None,
    fault_probability: float = 0.3,
    seed: int | None = None,
    swarm: bool = False,
    base_url: str | None = None,
    auth: Any = None,
    headers: dict[str, str] | None = None,
    stateful: bool = True,
    max_examples: int = 100,
    record_traces: bool = False,
    mutation_targets: list[str] | None = None,
    auto_discover: bool = False,
) -> ChaosAPIResult:
    """Run OpenAPI chaos testing against an API with fault injection.

    Loads the OpenAPI schema, generates test cases via Hypothesis, and
    randomly injects faults while exercising every API endpoint.

    **What it validates automatically (no configuration needed):**

    Every response is checked for HTTP protocol violations that cause
    real production failures but are invisible at the application layer:

    - **5xx status codes** — application errors under fault injection
    - **Content-Length mismatch** — middleware modifies body after headers
      (causes uvicorn RuntimeError)
    - **Content-Length + Transfer-Encoding** — RFC 7230 §3.3.3 violation
      (proxies drop the connection)
    - **Body on 204/304** — RFC violation, proxy misframing
    - **JSON Content-Type with non-JSON body** — clients crash on .json()
    - **Missing Content-Type** — MIME sniffing, XSS risk
    - **CORS headers lost under faults** — error handlers bypass middleware

    - **Schema violation** — response body doesn't match declared OpenAPI schema
      (missing required fields, wrong types)

    After all requests, cross-request analysis detects:

    - **Inconsistent error format** — same endpoint returns JSON and HTML
    - **CORS present normally, missing under faults** — middleware bypass
    - **Latency spike under faults** — 5x+ slower under faults, missing timeout

    Supports three schema sources (exactly one of *schema_url* or *app*
    must be provided):

    - **URL**: pass *schema_url* (requires a running server).
    - **ASGI**: pass *app* (in-process, no server needed).
    - **WSGI**: pass *app* and ``wsgi=True`` (in-process, no server needed).

    Args:
        schema_url: URL to an OpenAPI schema (e.g.
            ``"http://localhost:8080/openapi.json"``).
        app: An ASGI or WSGI application instance for in-process testing.
        wsgi: Set ``True`` when *app* is a WSGI application (default assumes
            ASGI).
        schema_path: Path to the schema endpoint within *app* (default
            ``"/openapi.json"``).  Only used with *app*.
        faults: Fault instances to inject server-side.
        fault_probability: Probability of each fault being active per request.
        seed: Random seed for reproducibility.
        swarm: Use swarm mode -- random fault subset per run for better
            aggregate coverage.
        base_url: Override base URL for API calls (URL mode only).
        auth: String auth header value (e.g. ``"Bearer ..."``) or use
            *headers* for full control.
        headers: Extra headers to include in every request.
        stateful: If ``True``, follow supported OpenAPI response links
            (``operationId`` and local ``operationRef``) to turn a single
            request into a short stateful sequence. Falls back to independent
            endpoint sampling when the schema has no supported links.
        max_examples: Maximum test cases to generate.
        record_traces: If ``True``, record API calls as ordeal traces.
        mutation_targets: Dotted paths to functions for auto-fault generation
            via AST mutations, semantic faults, and dependency faults.
        auto_discover: If ``True`` and *app* is provided, BFS app routes to
            auto-discover fault targets.

    Returns:
        :class:`ChaosAPIResult` with request counts, failures, fault
        activation stats, and deferred assertion results.
    """
    if app is None and schema_url is None:
        raise ValueError("Provide either 'schema_url' or 'app'")

    # Build fault list: explicit faults + auto-generated
    all_faults = list(faults or [])
    use_auto = False
    if mutation_targets:
        all_faults.extend(auto_faults(mutation_targets))
        use_auto = True
    elif auto_discover and app is not None:
        discovered = _discover_handlers(app)
        if discovered:
            all_faults.extend(auto_faults(discovered))
            use_auto = True
    faults = all_faults

    if auth is not None:
        if isinstance(auth, str):
            headers = {**(headers or {}), "Authorization": auth}
        else:
            _log.warning(
                "auth must be a string (e.g. 'Bearer ...'). "
                "Use headers={'Authorization': '...'} for full control."
            )

    # Select client and fetch spec
    if app is not None:
        client: _ASGIClient | _WSGIClient | _URLClient = (
            _WSGIClient(app) if wsgi else _ASGIClient(app)
        )
        spec = client.get_schema(schema_path)
    else:
        assert schema_url is not None
        # Derive base_url from schema_url if not provided
        parsed = urllib.parse.urlsplit(schema_url)
        effective_base = base_url or f"{parsed.scheme}://{parsed.netloc}"
        client = _URLClient(effective_base)
        spec = client.get_schema(schema_url)

    # Parse endpoints
    endpoints = _parse_endpoints(spec)
    if not endpoints:
        _log.warning("No endpoints found in OpenAPI spec")
        return ChaosAPIResult(
            total_requests=0,
            failures=[],
            fault_activations={f.name: 0 for f in faults},
            duration_seconds=0.0,
            deferred_ok=True,
            _had_app=app is not None,
            _requested_stateful=stateful,
        )

    # Build composite strategy across all endpoints
    endpoint_strategies = [_endpoint_strategy(ep, spec) for ep in endpoints]
    composite = st.one_of(*endpoint_strategies)
    operation_ids = {
        ep.operation_id: ep
        for ep in endpoints
        if isinstance(ep.operation_id, str) and ep.operation_id
    }
    operation_refs = {(ep.path, ep.method): ep for ep in endpoints}
    stateful_sources = [
        ep
        for ep in endpoints
        if any(
            _resolve_link_target(
                link,
                operation_ids=operation_ids,
                operation_refs=operation_refs,
            )
            is not None
            for links in ep.response_links.values()
            for link in links
        )
    ]
    stateful_links_available = bool(stateful_sources)

    # Set up scheduler and tracking (auto-faults always use swarm)
    scheduler = _FaultScheduler(
        faults,
        fault_probability=fault_probability,
        seed=seed,
        swarm=swarm if not use_auto else True,
    )

    existing_props = {p.name for p in tracker.results}
    prev_active = tracker.active
    tracker.active = True

    collector = _TraceCollector() if record_traces else None
    failures: list[dict[str, Any]] = []
    all_responses: list[tuple[_Endpoint, _Response, list[str]]] = []
    extra_headers = headers or {}
    t0 = time.monotonic()
    first_exc: Exception | None = None
    used_stateful = False

    # Map endpoint paths to parsed endpoints for validation
    ep_map: dict[str, _Endpoint] = {ep.path: ep for ep in endpoints}

    try:
        if stateful and stateful_links_available:
            for endpoint in stateful_sources:
                warmup_case = _materialize_case(endpoint, spec)
                warmup_response, resolved_endpoint = _execute_case(
                    warmup_case,
                    client=client,
                    scheduler=scheduler,
                    collector=collector,
                    extra_headers=extra_headers,
                    ep_map=ep_map,
                    all_responses=all_responses,
                    failures=failures,
                )
                if resolved_endpoint is None:
                    continue
                used_stateful = (
                    _follow_linked_sequence(
                        endpoint=resolved_endpoint,
                        case=warmup_case,
                        response=warmup_response,
                        root=spec,
                        operation_ids=operation_ids,
                        operation_refs=operation_refs,
                        client=client,
                        scheduler=scheduler,
                        collector=collector,
                        extra_headers=extra_headers,
                        ep_map=ep_map,
                        all_responses=all_responses,
                        failures=failures,
                    )
                    or used_stateful
                )
        elif stateful:
            _log.debug("No supported OpenAPI links found; falling back to parametrized requests.")

        @given(case=composite)
        @h_settings(
            max_examples=max_examples,
            database=None,
            suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
        )
        def _test(case: _APICase) -> None:
            nonlocal used_stateful

            response, endpoint = _execute_case(
                case,
                client=client,
                scheduler=scheduler,
                collector=collector,
                extra_headers=extra_headers,
                ep_map=ep_map,
                all_responses=all_responses,
                failures=failures,
            )
            if stateful and endpoint is not None and stateful_links_available:
                used_stateful = (
                    _follow_linked_sequence(
                        endpoint=endpoint,
                        case=case,
                        response=response,
                        root=spec,
                        operation_ids=operation_ids,
                        operation_refs=operation_refs,
                        client=client,
                        scheduler=scheduler,
                        collector=collector,
                        extra_headers=extra_headers,
                        ep_map=ep_map,
                        all_responses=all_responses,
                        failures=failures,
                    )
                    or used_stateful
                )

        _test()

    except Exception as exc:
        first_exc = exc
        failures.append({"type": "unexpected", "error": str(exc)})
    finally:
        scheduler.after_request()
        tracker.active = prev_active

    duration = time.monotonic() - t0

    # Check deferred assertions registered during this run
    new_failures = [p for p in tracker.failures if p.name not in existing_props]
    deferred_ok = len(new_failures) == 0
    for prop in new_failures:
        failures.append({"type": "deferred_assertion", "error": prop.summary})

    # Cross-request analysis — patterns only visible across multiple responses
    if all_responses:
        failures.extend(_analyze_cross_request(all_responses))

    # Build trace if requested
    traces: tuple = ()
    if collector is not None:
        label = schema_url or (f"{'wsgi' if wsgi else 'asgi'}:{schema_path}")
        traces = (collector.to_trace(seed=seed or 0, label=label, failure=first_exc),)

    return ChaosAPIResult(
        total_requests=scheduler.request_count,
        failures=failures,
        fault_activations=dict(scheduler.activations),
        duration_seconds=duration,
        deferred_ok=deferred_ok,
        traces=traces,
        _used_swarm=swarm or use_auto,
        _used_auto_discover=auto_discover,
        _used_mutation_targets=bool(mutation_targets),
        _had_app=app is not None,
        _requested_stateful=stateful,
        _stateful_links_available=stateful_links_available,
        _used_stateful=used_stateful,
    )
def catalog() -> list[dict[str, str]]:
    """Discover public entry points in this integration module.

    Fully automatic — scans all public functions defined in this module.
    """
    import inspect as _inspect
    import sys

    mod = sys.modules[__name__]
    entries: list[dict[str, str]] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_") or attr_name == "catalog":
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or _inspect.isclass(obj):
            continue
        if getattr(obj, "__module__", None) != __name__:
            continue
        try:
            sig = str(_inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(...)"
        entries.append(
            {
                "name": attr_name,
                "qualname": f"ordeal.integrations.openapi.{attr_name}",
                "signature": sig,
                "doc": (_inspect.getdoc(obj) or "").split("\n")[0],
            }
        )
    return entries
