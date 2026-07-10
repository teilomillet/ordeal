from __future__ import annotations
# ruff: noqa
class HttpTransport:
    """Standard-library HTTP transport used by the Compose runner."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: object | None,
        timeout: float,
    ) -> HttpResponse:
        """Send one HTTP request and return HTTP errors as normal responses."""
        request_headers = dict(headers)
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers={str(k): str(v) for k, v in response.headers.items()},
                    body=response.read(),
                    elapsed=time.monotonic() - started,
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                headers={str(k): str(v) for k, v in (exc.headers.items() if exc.headers else [])},
                body=exc.read(),
                elapsed=time.monotonic() - started,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ServiceRequestError(str(exc)) from exc
class _StrictState(dict[str, object]):
    def __missing__(self, key: str) -> object:
        raise KeyError(f"missing scenario state {key!r}")
def _render_value(value: object, state: Mapping[str, object]) -> object:
    if isinstance(value, str):
        placeholders: list[str] = []

        def protect(match: re.Match[str]) -> str:
            placeholders.append(match.group(0))
            return f"__ORDEAL_ENV_{len(placeholders) - 1}__"

        protected = _ENV_REFERENCE.sub(protect, value)
        rendered = protected.format_map(_StrictState(state))
        for index, placeholder in enumerate(placeholders):
            rendered = rendered.replace(f"__ORDEAL_ENV_{index}__", placeholder)
        return rendered
    if isinstance(value, list):
        return [_render_value(item, state) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_value(item, state) for key, item in value.items()}
    return value
def _join_url(base_url: str, path: str) -> str:
    if urlsplit(path).scheme in {"http", "https"}:
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
def _extract_json_path(value: object, path: str) -> object:
    normalized = path.removeprefix("json.")
    if normalized == "json":
        normalized = ""
    current = value
    for part in filter(None, normalized.split(".")):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current
def _config_payload(config: ComposeConfig) -> dict[str, object]:
    return cast(dict[str, object], asdict(config))
def _config_from_payload(payload: Mapping[str, object]) -> ComposeConfig:
    requests_raw = payload.get("requests", [])
    if not isinstance(requests_raw, list):
        raise ValueError("Compose trace requests config must be a list")
    requests = [
        ComposeRequestConfig(**cast(dict[str, object], request)) for request in requests_raw
    ]
    return ComposeConfig(
        base_url=str(payload["base_url"]),
        file=str(payload.get("file", "compose.yaml")),
        project_name=(str(payload["project_name"]) if payload.get("project_name") else None),
        health_path=str(payload.get("health_path", "/")),
        services=[str(item) for item in cast(list[object], payload.get("services", []))],
        requests=requests,
        initial_state=dict(cast(Mapping[str, object], payload.get("initial_state", {}))),
        max_time=float(payload.get("max_time", 60.0)),
        steps=int(payload.get("steps", 50)),
        seed=int(payload.get("seed", 42)),
        fault_probability=float(payload.get("fault_probability", 0.3)),
        faults=[str(item) for item in cast(list[object], payload.get("faults", []))],
        delay_seconds=float(payload.get("delay_seconds", 0.5)),
        request_timeout=float(payload.get("request_timeout", 5.0)),
        startup_timeout=float(payload.get("startup_timeout", 30.0)),
        replay_attempts=int(payload.get("replay_attempts", 3)),
        workload_mutations=int(payload.get("workload_mutations", 0)),
        trace_dir=str(payload.get("trace_dir", ".ordeal/traces")),
        keep_running=bool(payload.get("keep_running", False)),
    )
