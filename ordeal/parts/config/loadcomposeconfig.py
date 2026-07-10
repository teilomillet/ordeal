from __future__ import annotations
# ruff: noqa
def _load_compose_config(raw: dict, *, config_path: Path) -> ComposeConfig:
    """Load and validate the optional long-lived Compose runner config."""
    from urllib.parse import urlsplit

    if not isinstance(raw, dict):
        raise ConfigError("[compose] must be a table")
    _warn_unknown_keys("compose", raw, _KNOWN_COMPOSE_KEYS)
    base_url = str(raw.get("base_url", "")).rstrip("/")
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError("compose.base_url must be an absolute http:// or https:// URL")

    compose_file = Path(str(raw.get("file", "compose.yaml")))
    if not compose_file.is_absolute():
        compose_file = (config_path.parent / compose_file).resolve()

    requests: list[ComposeRequestConfig] = []
    request_names: set[str] = set()
    for i, request_raw in enumerate(raw.get("requests", [])):
        if not isinstance(request_raw, dict):
            raise ConfigError(f"compose.requests.{i} must be a table")
        _warn_unknown_keys(f"compose.requests.{i}", request_raw, _KNOWN_COMPOSE_REQUEST_KEYS)
        name = str(request_raw.get("name", f"request-{i + 1}")).strip()
        if not name:
            raise ConfigError(f"compose.requests.{i}.name cannot be empty")
        if name in request_names:
            raise ConfigError(f"Duplicate compose request name: {name!r}")
        request_names.add(name)
        method = str(request_raw.get("method", "GET")).upper()
        if not method or not method.isalpha():
            raise ConfigError(f"compose.requests.{i}.method must contain only letters")
        for field_name in ("headers", "expect_json", "capture"):
            if not isinstance(request_raw.get(field_name, {}), dict):
                raise ConfigError(f"compose.requests.{i}.{field_name} must be a table")
        if not isinstance(request_raw.get("requires", []), list):
            raise ConfigError(f"compose.requests.{i}.requires must be a list")
        faultable_default = method in {"GET", "HEAD", "OPTIONS"}
        requests.append(
            ComposeRequestConfig(
                name=name,
                method=method,
                path=str(request_raw.get("path", "/")),
                headers={str(k): str(v) for k, v in request_raw.get("headers", {}).items()},
                json_body=request_raw.get("json"),
                expect_status=_compose_statuses(request_raw.get("expect_status"), request_index=i),
                expect_json=dict(request_raw.get("expect_json", {})),
                capture={str(k): str(v) for k, v in request_raw.get("capture", {}).items()},
                requires=[str(item) for item in request_raw.get("requires", [])],
                faultable=_compose_bool(
                    request_raw.get("faultable", faultable_default),
                    field_name=f"compose.requests.{i}.faultable",
                ),
            )
        )
    if not requests:
        requests.append(ComposeRequestConfig())

    if not isinstance(raw.get("services", []), list):
        raise ConfigError("compose.services must be a list")
    if not isinstance(raw.get("faults", []), list):
        raise ConfigError("compose.faults must be a list")
    if not isinstance(raw.get("initial_state", {}), dict):
        raise ConfigError("compose.initial_state must be a table")
    services = [str(item) for item in raw.get("services", [])]
    default_faults = (
        ["kill", "restart", "delay_response", "corrupt_response"]
        if services
        else ["delay_response", "corrupt_response"]
    )
    faults = [str(item) for item in raw.get("faults", default_faults)]
    unknown_faults = set(faults) - _VALID_COMPOSE_FAULTS
    if unknown_faults:
        raise ConfigError(
            f"Unknown compose fault(s): {', '.join(sorted(unknown_faults))}. "
            f"Valid faults: {', '.join(sorted(_VALID_COMPOSE_FAULTS))}"
        )
    if {"kill", "restart"} & set(faults) and not services:
        raise ConfigError("compose.services is required when kill or restart faults are enabled")

    trace_dir = Path(str(raw.get("trace_dir", ".ordeal/traces")))
    if not trace_dir.is_absolute():
        trace_dir = (config_path.parent / trace_dir).resolve()

    cfg = ComposeConfig(
        base_url=base_url,
        file=str(compose_file),
        project_name=(str(raw["project_name"]) if raw.get("project_name") else None),
        health_path=str(raw.get("health_path", "/")),
        services=services,
        requests=requests,
        initial_state=dict(raw.get("initial_state", {})),
        max_time=float(raw.get("max_time", 60.0)),
        steps=int(raw.get("steps", 50)),
        seed=int(raw.get("seed", 42)),
        fault_probability=float(raw.get("fault_probability", 0.3)),
        faults=faults,
        delay_seconds=float(raw.get("delay_seconds", 0.5)),
        request_timeout=float(raw.get("request_timeout", 5.0)),
        startup_timeout=float(raw.get("startup_timeout", 30.0)),
        replay_attempts=int(raw.get("replay_attempts", 3)),
        workload_mutations=int(raw.get("workload_mutations", 0)),
        trace_dir=str(trace_dir),
        keep_running=_compose_bool(
            raw.get("keep_running", False),
            field_name="compose.keep_running",
        ),
    )
    if cfg.steps < 1:
        raise ConfigError("compose.steps must be >= 1")
    if cfg.max_time <= 0:
        raise ConfigError("compose.max_time must be > 0")
    if not (0.0 <= cfg.fault_probability <= 1.0):
        raise ConfigError("compose.fault_probability must be between 0.0 and 1.0")
    if cfg.delay_seconds < 0:
        raise ConfigError("compose.delay_seconds must be >= 0")
    if cfg.request_timeout <= 0 or cfg.startup_timeout <= 0:
        raise ConfigError("compose request and startup timeouts must be > 0")
    if cfg.replay_attempts < 1:
        raise ConfigError("compose.replay_attempts must be >= 1")
    if cfg.workload_mutations < 0:
        raise ConfigError("compose.workload_mutations must be >= 0")
    return cfg
