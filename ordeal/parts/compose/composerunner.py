from __future__ import annotations
# ruff: noqa
class ComposeRunner:
    """Explore one long-lived Compose topology and record every action."""

    def __init__(
        self,
        config: ComposeConfig,
        *,
        controller: ComposeController | None = None,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.controller = controller or ComposeController(config)
        self.transport = transport or HttpTransport()
        self._monotonic = monotonic
        self._sleep = sleep
        self._rng = random.Random(config.seed)
        self._started_at = 0.0
        self._owned = False
        self._killed: set[str] = set()
        self.state = copy.deepcopy(config.initial_state)

    def _new_action(
        self,
        trace: ComposeTrace,
        kind: str,
        name: str,
        params: Mapping[str, object] | None = None,
    ) -> ComposeTraceAction:
        return ComposeTraceAction(
            index=len(trace.actions),
            kind=kind,
            name=name,
            params=dict(params or {}),
            timestamp_offset=max(0.0, self._monotonic() - self._started_at),
        )

    @staticmethod
    def _failure(action: ComposeTraceAction, kind: str, message: str) -> ComposeFailure:
        action.result.update({"error": message, "failure_kind": kind})
        return ComposeFailure(
            kind=kind,
            message=message,
            action_index=action.index,
            action_name=action.name,
        )

    def _execute(self, action: ComposeTraceAction) -> ComposeFailure | None:
        try:
            if action.kind == "lifecycle":
                return self._execute_lifecycle(action)
            if action.kind == "fault":
                return self._execute_fault(action)
            if action.kind == "request":
                return self._execute_request(action)
            return self._failure(action, "trace_format", f"unknown action kind {action.kind!r}")
        except ComposeCommandError as exc:
            return self._failure(action, "compose_command", str(exc))

    def _execute_lifecycle(self, action: ComposeTraceAction) -> ComposeFailure | None:
        if action.name == "up":
            self._owned = self.controller.start()
            action.result = {"owned_cleanup": self._owned}
        elif action.name == "down":
            self.controller.stop()
            action.result = {"stopped": True}
        elif action.name == "start_service":
            service = str(action.params["service"])
            self.controller.start_service(service)
            self._killed.discard(service)
            action.result = {"service": service, "started": True}
        elif action.name == "wait_ready":
            return self._wait_ready(action)
        elif action.name == "leave_running":
            action.result = {"kept_running": True}
        else:
            return self._failure(action, "trace_format", f"unknown lifecycle {action.name!r}")
        return None

    def _execute_fault(self, action: ComposeTraceAction) -> ComposeFailure | None:
        if action.name == "kill":
            service = str(action.params["service"])
            self.controller.kill(service)
            self._killed.add(service)
            action.result = {"service": service, "signal": "SIGKILL"}
        elif action.name == "restart":
            service = str(action.params["service"])
            self.controller.restart(service)
            action.result = {"service": service, "restarted": True}
        elif action.name in {"delay_response", "corrupt_response"}:
            action.result = {"armed_for_next_request": True}
        else:
            return self._failure(action, "trace_format", f"unknown fault {action.name!r}")
        return None

    def _wait_ready(self, action: ComposeTraceAction) -> ComposeFailure | None:
        url = str(action.params["url"])
        timeout = float(action.params["timeout"])
        deadline = self._monotonic() + timeout
        attempts = 0
        last_error = "no response"
        while self._monotonic() < deadline:
            attempts += 1
            try:
                response = self.transport.request(
                    "GET",
                    url,
                    headers={},
                    json_body=None,
                    timeout=min(self.config.request_timeout, timeout),
                )
                if response.status < 500:
                    action.result = {"attempts": attempts, "status": response.status}
                    if action.params.get("operation"):
                        _record_request_property(action, "service_ready", passed=True)
                    return None
                last_error = f"HTTP {response.status}"
            except ServiceRequestError as exc:
                last_error = str(exc)
            self._sleep(min(0.1, max(0.0, deadline - self._monotonic())))
        if action.params.get("operation"):
            _record_request_property(action, "service_ready", passed=False)
        return self._failure(
            action,
            "readiness_timeout",
            f"service did not become ready at {url} after {attempts} attempts: {last_error}",
        )

    def _execute_request(self, action: ComposeTraceAction) -> ComposeFailure | None:
        if "template_error" in action.params:
            return self._failure(action, "template_error", str(action.params["template_error"]))
        method = str(action.params["method"])
        url = str(action.params["url"])
        validate = bool(action.params.get("validate", True))
        response_fault = action.params.get("response_fault")
        try:
            headers = cast(
                Mapping[str, str],
                _resolve_environment_value(action.params.get("headers", {})),
            )
            json_body = _resolve_environment_value(action.params.get("json_body"))
            response = self.transport.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                timeout=self.config.request_timeout,
            )
        except ServiceRequestError as exc:
            if not validate:
                action.result = {"expected_fault_window": True, "request_error": str(exc)}
                return None
            return self._failure(action, "request_error", f"{method} {url}: {exc}")

        original_hash = hashlib.sha256(response.body).hexdigest()
        if response_fault == "delay_response":
            delay = float(action.params.get("delay_seconds", self.config.delay_seconds))
            self._sleep(delay)
            response = replace(response, elapsed=response.elapsed + delay)
        elif response_fault == "corrupt_response":
            corrupted = (
                b"\x00"
                if not response.body
                else bytes([response.body[0] ^ 0xFF]) + response.body[1:]
            )
            response = replace(response, body=corrupted)

        action.result = {
            "status": response.status,
            "elapsed_seconds": response.elapsed,
            "headers": _redact_trace_value(response.headers),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
            "original_body_sha256": original_hash,
            "response_fault": response_fault,
            "expected_fault_window": not validate,
        }
        if not validate:
            return None

        expected_statuses = [
            int(item) for item in cast(list[object], action.params.get("expect_status", []))
        ]
        if expected_statuses:
            status_ok = response.status in expected_statuses
            status_description = repr(expected_statuses)
        else:
            status_ok = 200 <= response.status < 300
            status_description = "2xx"
        if not status_ok:
            _record_request_property(
                action,
                _status_property(action.params.get("expect_status", [])),
                passed=False,
            )
            return self._failure(
                action,
                "unexpected_status",
                f"{method} {url} expected {status_description}, got {response.status}",
            )
        _record_request_property(
            action,
            _status_property(action.params.get("expect_status", [])),
            passed=True,
        )

        expectations = cast(Mapping[str, object], action.params.get("expect_json", {}))
        captures = cast(Mapping[str, str], action.params.get("capture", {}))
        if not expectations and not captures:
            return None
        try:
            parsed = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _record_request_property(action, "valid_json", passed=False)
            return self._failure(
                action,
                "invalid_json",
                f"{method} {url} did not return valid JSON: {exc}",
            )
        _record_request_property(action, "valid_json", passed=True)
        for path, expected in expectations.items():
            try:
                observed = _extract_json_path(parsed, path)
            except KeyError:
                _record_request_property(action, f"json:{path}", passed=False)
                return self._failure(
                    action,
                    "unexpected_json",
                    f"{method} {url} response is missing {path!r}",
                )
            if observed != expected:
                _record_request_property(action, f"json:{path}", passed=False)
                return self._failure(
                    action,
                    "unexpected_json",
                    f"{method} {url} response differed at {path!r}",
                )
            _record_request_property(action, f"json:{path}", passed=True)
        captured: dict[str, object] = {}
        for state_name, path in captures.items():
            try:
                captured[state_name] = _extract_json_path(parsed, path)
            except KeyError:
                _record_request_property(action, f"capture:{state_name}", passed=False)
                return self._failure(
                    action,
                    "capture_error",
                    f"{method} {url} response is missing capture path {path!r}",
                )
            _record_request_property(action, f"capture:{state_name}", passed=True)
        self.state.update(captured)
        action.result["captured_state"] = captured
        return None

    def _record(self, trace: ComposeTrace, action: ComposeTraceAction) -> bool:
        trace.actions.append(action)
        failure = self._execute(action)
        if failure is not None:
            trace.failure = failure
            return False
        return True

    def _request_action(
        self,
        trace: ComposeTrace,
        request: ComposeRequestConfig,
        *,
        validate: bool,
        response_fault: str | None = None,
        fault: str = "none",
    ) -> ComposeTraceAction:
        try:
            path = str(_render_value(request.path, self.state))
            headers = cast(dict[str, str], _render_value(request.headers, self.state))
            body = _render_value(request.json_body, self.state)
            expectations = cast(dict[str, object], _render_value(request.expect_json, self.state))
        except KeyError as exc:
            return self._new_action(
                trace,
                "request",
                request.name,
                {"template_error": str(exc), "validate": validate},
            )
        return self._new_action(
            trace,
            "request",
            request.name,
            {
                "method": request.method,
                "url": _join_url(self.config.base_url, path),
                "headers": headers,
                "json_body": body,
                "expect_status": list(request.expect_status),
                "expect_json": expectations,
                "capture": dict(request.capture),
                "validate": validate,
                "fault": fault,
                "response_fault": response_fault,
                "delay_seconds": self.config.delay_seconds,
            },
        )

    def _ready_action(
        self,
        trace: ComposeTrace,
        *,
        operation: str | None = None,
        fault: str = "none",
    ) -> ComposeTraceAction:
        params: dict[str, object] = {
            "url": _join_url(self.config.base_url, self.config.health_path),
            "timeout": self.config.startup_timeout,
        }
        if operation is not None:
            params.update({"operation": operation, "fault": fault})
        return self._new_action(
            trace,
            "lifecycle",
            "wait_ready",
            params,
        )

    def _run_fault_cycle(
        self,
        trace: ComposeTrace,
        request: ComposeRequestConfig,
        fault: str,
    ) -> bool:
        if fault in {"kill", "restart"}:
            service = self._rng.choice(self.config.services)
            if not self._record(
                trace,
                self._new_action(trace, "fault", fault, {"service": service}),
            ):
                return False
            if fault == "kill":
                if not self._record(
                    trace,
                    self._request_action(trace, request, validate=False, fault=fault),
                ):
                    return False
                if not self._record(
                    trace,
                    self._new_action(
                        trace,
                        "lifecycle",
                        "start_service",
                        {"service": service},
                    ),
                ):
                    return False
            if not self._record(
                trace,
                self._ready_action(trace, operation=request.name, fault=fault),
            ):
                return False
        else:
            params: dict[str, object] = {}
            if fault == "delay_response":
                params["seconds"] = self.config.delay_seconds
            if not self._record(trace, self._new_action(trace, "fault", fault, params)):
                return False
            if not self._record(
                trace,
                self._request_action(
                    trace,
                    request,
                    validate=False,
                    response_fault=fault,
                    fault=fault,
                ),
            ):
                return False
        return self._record(
            trace,
            self._request_action(trace, request, validate=True, fault=fault),
        )

    def _cleanup_safely(self) -> None:
        for service in sorted(self._killed):
            try:
                self.controller.start_service(service)
            except ComposeCommandError:
                pass
        if self._owned and not self.config.keep_running:
            try:
                self.controller.stop()
            except ComposeCommandError:
                pass

    def run(self) -> ComposeTrace:
        """Run a seeded service exploration and return its exact trace."""
        self._started_at = self._monotonic()
        trace = ComposeTrace(seed=self.config.seed, compose=_config_payload(self.config))
        if not self._record(trace, self._new_action(trace, "lifecycle", "up")):
            trace.final_state = copy.deepcopy(self.state)
            trace.duration = self._monotonic() - self._started_at
            return trace
        if not self._record(trace, self._ready_action(trace)):
            self._cleanup_safely()
            trace.final_state = copy.deepcopy(self.state)
            trace.duration = self._monotonic() - self._started_at
            return trace

        for _ in range(self.config.steps):
            if self._monotonic() - self._started_at >= self.config.max_time:
                break
            eligible = [
                request
                for request in self.config.requests
                if all(name in self.state for name in request.requires)
            ]
            if not eligible:
                action = self._new_action(trace, "request", "select")
                trace.actions.append(action)
                trace.failure = self._failure(
                    action,
                    "scenario_state",
                    "no request is eligible for the current captured state",
                )
                break
            request = self._rng.choice(eligible)
            inject = (
                request.faultable
                and bool(self.config.faults)
                and self._rng.random() < self.config.fault_probability
            )
            if inject:
                if not self._run_fault_cycle(trace, request, self._rng.choice(self.config.faults)):
                    break
            elif not self._record(trace, self._request_action(trace, request, validate=True)):
                break

        trace.final_state = copy.deepcopy(self.state)
        if trace.failure is None:
            cleanup_name = (
                "leave_running" if self.config.keep_running or not self._owned else "down"
            )
            self._record(trace, self._new_action(trace, "lifecycle", cleanup_name))
        else:
            self._cleanup_safely()
        trace.duration = self._monotonic() - self._started_at
        return trace

    def replay(self, source: ComposeTrace) -> ComposeFailure | None:
        """Execute the recorded actions once and return the observed failure."""
        self._started_at = self._monotonic()
        self.state = copy.deepcopy(self.config.initial_state)
        observed: ComposeFailure | None = None
        for source_action in source.actions:
            action = ComposeTraceAction(
                index=source_action.index,
                kind=source_action.kind,
                name=source_action.name,
                params=copy.deepcopy(source_action.params),
                timestamp_offset=source_action.timestamp_offset,
            )
            observed = self._execute(action)
            if observed is not None:
                break
        self._cleanup_safely()
        return observed
def replay_compose_trace(
    trace: ComposeTrace,
    *,
    attempts: int | None = None,
    runner_factory: Callable[[ComposeConfig], ComposeRunner] | None = None,
) -> ComposeReplayReport:
    """Replay an exact service trace repeatedly and count exact failure matches."""
    count = attempts if attempts is not None else int(trace.compose.get("replay_attempts", 3))
    if count < 1:
        raise ValueError("replay attempts must be >= 1")
    expected = trace.failure_signature
    if expected is None:
        return ComposeReplayReport(
            attempted=0,
            reproduced=0,
            expected_signature=None,
            observed_signatures=[],
        )
    config = _config_from_payload(trace.compose)
    factory = runner_factory or ComposeRunner
    observed_signatures: list[str | None] = []
    for _ in range(count):
        failure = factory(config).replay(trace)
        observed_signatures.append(failure.signature if failure is not None else None)
    return ComposeReplayReport(
        attempted=count,
        reproduced=sum(signature == expected for signature in observed_signatures),
        expected_signature=expected,
        observed_signatures=observed_signatures,
    )
