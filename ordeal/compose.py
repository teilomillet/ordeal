"""Long-lived Docker Compose service exploration and probabilistic replay.

The runner keeps one service topology alive while it executes repeated HTTP
operations, injects process and response-boundary faults, and retains captured
JSON values between operations.  Its trace is exact; the external scheduler,
network, and service timing are not.  Replay therefore reports attempts and
exact failure-signature matches instead of claiming deterministic reproduction.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, cast
from urllib.parse import urlsplit

from ordeal.config import ComposeConfig, ComposeRequestConfig

REPLAY_BOUNDARY = (
    "The action and fault trace is exact, but container scheduling, network timing, "
    "and external service behavior are not deterministic. Response delay and corruption "
    "are injected at the harness transport boundary."
)

_DURABLE_FAILURE_KINDS = frozenset(
    {
        "readiness_timeout",
        "request_error",
        "unexpected_status",
        "invalid_json",
        "unexpected_json",
        "capture_error",
    }
)

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_CREDENTIAL_TEMPLATE = re.compile(r"(?i)(?:(?:bearer|basic)\s+)?\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "jwt",
        "passwd",
        "password",
        "secret",
        "session",
        "token",
    }
)
_REDACTED = "<redacted>"


def _sensitive_key(value: object) -> bool:
    """Return whether a mapping key conventionally carries authentication data."""
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    parts = {part for part in re.split(r"[^a-z0-9]+", expanded.lower()) if part}
    return bool(parts & _SENSITIVE_KEY_PARTS) or "apikey" in parts or {"api", "key"} <= parts


def _credential_safe_string(value: object) -> object:
    """Preserve safe env templates while removing literal credential material."""
    if not isinstance(value, str):
        return _REDACTED
    if _SAFE_CREDENTIAL_TEMPLATE.fullmatch(value):
        return value
    matches = list(_ENV_REFERENCE.finditer(value))
    if not matches:
        return _REDACTED
    safe_parts: list[str] = []
    cursor = 0
    for match in matches:
        if match.start() > cursor and (not safe_parts or safe_parts[-1] != _REDACTED):
            safe_parts.append(_REDACTED)
        safe_parts.append(match.group(0))
        cursor = match.end()
    if cursor < len(value):
        safe_parts.append(_REDACTED)
    return "".join(safe_parts)


def _redact_trace_value(
    value: object,
    *,
    key: object | None = None,
    sensitive_context: bool = False,
) -> object:
    """Return a JSON-safe value with raw credential-shaped fields removed."""
    sensitive = sensitive_context or (key is not None and _sensitive_key(key))
    if isinstance(value, dict):
        return {
            str(child_key): _redact_trace_value(
                child,
                key=child_key,
                sensitive_context=sensitive,
            )
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_trace_value(child, sensitive_context=sensitive) for child in value]
    if isinstance(value, tuple):
        return [_redact_trace_value(child, sensitive_context=sensitive) for child in value]
    if sensitive:
        return _credential_safe_string(value)
    return value


def _resolve_environment_value(value: object) -> object:
    """Resolve ``${NAME}`` placeholders only at the HTTP transport boundary."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ServiceRequestError(f"missing environment variable {name!r}")
            return os.environ[name]

        return _ENV_REFERENCE.sub(replace, value)
    if isinstance(value, list):
        return [_resolve_environment_value(child) for child in value]
    if isinstance(value, dict):
        return {str(key): _resolve_environment_value(child) for key, child in value.items()}
    return value


class ComposeCommandError(RuntimeError):
    """Raised when a Docker Compose lifecycle command cannot complete."""


class ServiceRequestError(RuntimeError):
    """Raised when an HTTP request cannot produce a response."""


@dataclass
class HttpResponse:
    """HTTP response observed by the service harness."""

    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float


@dataclass
class ComposeTraceAction:
    """One exact lifecycle, request, or fault action in a service trace."""

    index: int
    kind: str
    name: str
    params: dict[str, object] = field(default_factory=dict)
    result: dict[str, object] = field(default_factory=dict)
    timestamp_offset: float = 0.0


@dataclass
class ComposeFailure:
    """Stable failure observation used for exact replay matching."""

    kind: str
    message: str
    action_index: int
    action_name: str

    @property
    def signature(self) -> str:
        """Return a hash of the exact failure kind, location, action, and message."""
        payload = {
            "kind": self.kind,
            "message": self.message,
            "action_index": self.action_index,
            "action_name": self.action_name,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class ComposeReplayReport:
    """Probabilistic reproduction result for one exact Compose trace."""

    attempted: int
    reproduced: int
    expected_signature: str | None
    observed_signatures: list[str | None] = field(default_factory=list)
    boundary: str = REPLAY_BOUNDARY

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe replay report."""
        return cast(dict[str, object], asdict(self))


@dataclass
class ComposeTrace:
    """Self-contained action/fault trace for the Compose runner."""

    seed: int
    compose: dict[str, object]
    actions: list[ComposeTraceAction] = field(default_factory=list)
    failure: ComposeFailure | None = None
    final_state: dict[str, object] = field(default_factory=dict)
    duration: float = 0.0
    replay: ComposeReplayReport | None = None
    runner: str = "compose"
    schema_version: int = 1

    @property
    def failure_signature(self) -> str | None:
        """Return the exact recorded failure signature, when the run failed."""
        return self.failure.signature if self.failure is not None else None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe trace payload."""
        raw_payload = cast(dict[str, object], asdict(self))
        sensitive_state_names: set[str] = set()
        for action in self.actions:
            captures = action.params.get("capture", {})
            if not isinstance(captures, Mapping):
                continue
            sensitive_state_names.update(
                str(state_name)
                for state_name, source_path in captures.items()
                if _sensitive_key(source_path)
            )
        raw_actions = cast(list[dict[str, object]], raw_payload.get("actions", []))
        for action in raw_actions:
            result = action.get("result")
            if not isinstance(result, dict):
                continue
            captured = result.get("captured_state")
            if not isinstance(captured, dict):
                continue
            for state_name in sensitive_state_names & set(captured):
                captured[state_name] = _credential_safe_string(captured[state_name])
        final_state = raw_payload.get("final_state")
        if isinstance(final_state, dict):
            for state_name in sensitive_state_names & set(final_state):
                final_state[state_name] = _credential_safe_string(final_state[state_name])
        payload = cast(dict[str, object], _redact_trace_value(raw_payload))
        payload["failure_signature"] = self.failure_signature
        return payload

    def content_hash(self) -> str:
        """Return a stable short hash of the complete trace payload."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def save(self, path: str | Path) -> None:
        """Save the trace as JSON, with optional gzip compression."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        if target.suffix == ".gz":
            with gzip.open(target, "wt", encoding="utf-8") as stream:
                stream.write(content)
        else:
            target.write_text(content, encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> ComposeTrace:
        """Load and validate a Compose trace from JSON or gzip-compressed JSON."""
        raw = _load_json_file(path)
        if raw.get("runner") != "compose":
            raise ValueError("Trace is not a compose runner trace")
        if int(raw.get("schema_version", 0)) != 1:
            raise ValueError(f"Unsupported compose trace schema: {raw.get('schema_version')!r}")
        actions_raw = raw.get("actions", [])
        if not isinstance(actions_raw, list):
            raise ValueError("Compose trace actions must be a list")
        actions = [ComposeTraceAction(**cast(dict[str, object], item)) for item in actions_raw]
        failure_raw = raw.get("failure")
        replay_raw = raw.get("replay")
        compose_raw = raw.get("compose")
        final_state_raw = raw.get("final_state", {})
        if not isinstance(compose_raw, dict) or not isinstance(final_state_raw, dict):
            raise ValueError("Compose trace config and final state must be objects")
        return cls(
            seed=int(raw["seed"]),
            compose=cast(dict[str, object], compose_raw),
            actions=actions,
            failure=(
                ComposeFailure(**cast(dict[str, object], failure_raw))
                if isinstance(failure_raw, dict)
                else None
            ),
            final_state=cast(dict[str, object], final_state_raw),
            duration=float(raw.get("duration", 0.0)),
            replay=(
                ComposeReplayReport(**cast(dict[str, object], replay_raw))
                if isinstance(replay_raw, dict)
                else None
            ),
        )

    @classmethod
    def is_trace_file(cls, path: str | Path) -> bool:
        """Return whether a JSON trace declares the Compose runner schema."""
        try:
            raw = _load_json_file(path)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return False
        return raw.get("runner") == "compose"


@dataclass
class ComposeExplorationResult:
    """Result and artifacts produced by one Compose exploration."""

    trace: ComposeTrace
    trace_path: Path
    replay: ComposeReplayReport | None
    requests: int
    faults: int
    duration: float
    coverage: dict[str, Any] = field(default_factory=dict)
    protection: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] | None = None


@dataclass
class ComposeRegressionArtifacts:
    """Committed trace and manifest paths for one Compose regression."""

    finding_id: str
    trace_path: Path
    manifest_path: Path
    binding: dict[str, Any]


def _status_property(expect_status: object) -> str:
    """Return the stable property label for one request's status contract."""
    statuses = [int(item) for item in expect_status] if isinstance(expect_status, list) else []
    return "status:" + (",".join(str(item) for item in statuses) if statuses else "2xx")


def _request_property_names(params: Mapping[str, object]) -> list[str]:
    """Return ordered, non-secret property names for one request contract."""
    names = [_status_property(params.get("expect_status", []))]
    expectations = params.get("expect_json", {})
    captures = params.get("capture", {})
    if isinstance(expectations, Mapping) or isinstance(captures, Mapping):
        if expectations or captures:
            names.append("valid_json")
    if isinstance(expectations, Mapping):
        names.extend(f"json:{path}" for path in expectations)
    if isinstance(captures, Mapping):
        names.extend(f"capture:{state_name}" for state_name in captures)
    return names


def _record_request_property(
    action: ComposeTraceAction,
    property_name: str,
    *,
    passed: bool,
) -> None:
    """Append one redaction-safe property observation to a request action."""
    observations = action.result.setdefault("property_results", [])
    if isinstance(observations, list):
        observations.append(
            {
                "property": property_name,
                "type": "always",
                "passed": passed,
            }
        )


def compose_reliability_coverage(trace: ComposeTrace) -> dict[str, Any]:
    """Build operation × fault × property coverage from a Compose trace.

    Configured cells are declared before observations, so a fault/property
    combination that never ran remains ``NOT EXERCISED`` instead of becoming a
    silent pass. Only clean validated requests contribute observations; the
    intentionally unvalidated request inside a fault window does not.
    """
    cells: dict[tuple[str, str, str], dict[str, Any]] = {}

    def ensure(operation: str, fault: str, property_name: str) -> dict[str, Any]:
        key = (operation, fault, property_name)
        return cells.setdefault(
            key,
            {
                "operation": operation,
                "fault": fault,
                "property": property_name,
                "type": "always",
                "hits": 0,
                "passes": 0,
                "failures": 0,
            },
        )

    requests = trace.compose.get("requests", [])
    configured_faults = [str(item) for item in cast(list[object], trace.compose.get("faults", []))]
    if isinstance(requests, list):
        for raw_request in requests:
            if not isinstance(raw_request, Mapping):
                continue
            operation = str(raw_request.get("name") or "root")
            faults = ["none"]
            if bool(raw_request.get("faultable", True)):
                faults.extend(configured_faults)
            for fault in dict.fromkeys(faults):
                for property_name in _request_property_names(raw_request):
                    ensure(operation, fault, property_name)
                if fault in {"kill", "restart"}:
                    ensure(operation, fault, "service_ready")

    for action in trace.actions:
        is_request = action.kind == "request" and bool(action.params.get("validate", True))
        is_recovery = (
            action.kind == "lifecycle"
            and action.name == "wait_ready"
            and bool(action.params.get("operation"))
        )
        if not is_request and not is_recovery:
            continue
        observations = action.result.get("property_results", [])
        if not isinstance(observations, list):
            continue
        operation = str(action.params.get("operation") or action.name)
        fault = str(action.params.get("fault") or "none")
        for observation in observations:
            if not isinstance(observation, Mapping):
                continue
            property_name = str(observation.get("property") or "").strip()
            if not property_name:
                continue
            cell = ensure(operation, fault, property_name)
            cell["hits"] += 1
            if bool(observation.get("passed")):
                cell["passes"] += 1
            else:
                cell["failures"] += 1

    rows = []
    for key in sorted(cells):
        row = cells[key]
        if row["hits"] == 0:
            status = "NOT EXERCISED"
        elif row["failures"]:
            status = "FAIL"
        else:
            status = "PASS"
        rows.append({**row, "status": status})
    return {
        "dimensions": ["operation", "fault", "property"],
        "rows": rows,
        "summary": {
            "pass": sum(row["status"] == "PASS" for row in rows),
            "not_exercised": sum(row["status"] == "NOT EXERCISED" for row in rows),
            "fail": sum(row["status"] == "FAIL" for row in rows),
            "total": len(rows),
        },
    }


def _load_json_file(path: str | Path) -> dict[str, object]:
    target = Path(path)
    if target.suffix == ".gz":
        with gzip.open(target, "rt", encoding="utf-8") as stream:
            raw = json.load(stream)
    else:
        with target.open(encoding="utf-8") as stream:
            raw = json.load(stream)
    if not isinstance(raw, dict):
        raise ValueError("Trace JSON must contain an object")
    return cast(dict[str, object], raw)


class ComposeController:
    """Docker Compose lifecycle adapter using shell-free argv execution."""

    def __init__(
        self,
        config: ComposeConfig,
        *,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.config = config
        self._run_command = run_command

    def _argv(self, *args: str) -> list[str]:
        argv = ["docker", "compose", "-f", self.config.file]
        if self.config.project_name:
            argv.extend(["--project-name", self.config.project_name])
        argv.extend(args)
        return argv

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = self._argv(*args)
        try:
            completed = self._run_command(
                argv,
                capture_output=True,
                text=True,
                timeout=max(30.0, self.config.startup_timeout),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ComposeCommandError(
                "docker was not found; install Docker with the Compose plugin"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ComposeCommandError(f"Compose command timed out: {' '.join(argv)}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "no output").strip()
            raise ComposeCommandError(
                f"Compose command failed ({completed.returncode}): {' '.join(argv)}: {detail}"
            )
        return completed

    def start(self) -> bool:
        """Start the topology and return whether this harness owns its cleanup."""
        was_running = bool(self._run("ps", "-q").stdout.strip())
        self._run("up", "-d")
        return not was_running

    def stop(self) -> None:
        """Stop services started by this harness without deleting volumes."""
        self._run("down", "--remove-orphans")

    def kill(self, service: str) -> None:
        """Send SIGKILL to one configured Compose service."""
        self._run("kill", "-s", "SIGKILL", service)

    def start_service(self, service: str) -> None:
        """Start one stopped service and its required dependencies."""
        self._run("up", "-d", service)

    def restart(self, service: str) -> None:
        """Restart one configured Compose service."""
        self._run("restart", service)


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


def _mutated_property_trace(
    trace: ComposeTrace,
    *,
    action_index: int,
    property_name: str,
) -> ComposeTrace | None:
    """Return a prefix trace whose selected response oracle is deliberately wrong."""
    actions = copy.deepcopy(trace.actions[: action_index + 1])
    if not actions or actions[-1].index != action_index:
        return None
    action = actions[-1]
    if property_name.startswith("status:"):
        observed_status = int(action.result.get("status", 200))
        mutant_status = 599 if observed_status != 599 else 598
        action.params["expect_status"] = [mutant_status]
    elif property_name.startswith("json:"):
        path = property_name.removeprefix("json:")
        expectations = action.params.get("expect_json")
        if not isinstance(expectations, Mapping) or path not in expectations:
            return None
        action.params["expect_json"] = {
            **dict(expectations),
            path: {"__ordeal_mutated_expectation__": True},
        }
    elif property_name.startswith("capture:"):
        state_name = property_name.removeprefix("capture:")
        captures = action.params.get("capture")
        if not isinstance(captures, Mapping) or state_name not in captures:
            return None
        action.params["capture"] = {
            **dict(captures),
            state_name: "json.__ordeal_missing_capture__",
        }
    else:
        return None
    return replace(
        trace,
        actions=actions,
        failure=None,
        final_state={},
        duration=0.0,
        replay=None,
    )


def _control_prefix(trace: ComposeTrace, action_index: int) -> ComposeTrace:
    """Return the exact unmutated prefix ending at one request action."""
    return replace(
        trace,
        actions=copy.deepcopy(trace.actions[: action_index + 1]),
        failure=None,
        final_state={},
        duration=0.0,
        replay=None,
    )


def measure_compose_workload_strength(
    trace: ComposeTrace,
    *,
    budget: int,
    runner_factory: Callable[[ComposeConfig], ComposeRunner] | None = None,
) -> dict[str, Any]:
    """Measure whether observed Compose properties detect oracle mutations.

    Each trial first replays the unmodified trace prefix. Only a clean control
    allows the corresponding status, JSON, or capture expectation to be
    mutated. A mutant is killed only when the altered expectation fails at the
    selected operation. This measures the recorded workload and harness wiring;
    it does not claim that production service source code was mutated.
    """
    if budget < 0:
        raise ValueError("workload mutation budget must be >= 0")
    coverage = compose_reliability_coverage(trace)
    failed_cells = [
        {
            "operation": row["operation"],
            "fault": row["fault"],
            "property": row["property"],
        }
        for row in coverage["rows"]
        if row["status"] == "FAIL"
    ]
    unexercised = [
        {
            "operation": row["operation"],
            "fault": row["fault"],
            "property": row["property"],
        }
        for row in coverage["rows"]
        if row["status"] == "NOT EXERCISED"
    ]
    candidates: list[tuple[ComposeTraceAction, str]] = []
    for action in trace.actions:
        if action.kind != "request" or not bool(action.params.get("validate", True)):
            continue
        observations = action.result.get("property_results", [])
        if not isinstance(observations, list):
            continue
        for observation in observations:
            if not isinstance(observation, Mapping) or not bool(observation.get("passed")):
                continue
            property_name = str(observation.get("property") or "")
            if property_name == "valid_json":
                continue
            if property_name.startswith(("status:", "json:", "capture:")):
                candidates.append((action, property_name))

    mutations: list[dict[str, Any]] = []
    if budget > 0:
        factory = runner_factory or ComposeRunner
        config = _config_from_payload(trace.compose)
        for action, property_name in candidates[:budget]:
            control_failure = factory(config).replay(_control_prefix(trace, action.index))
            row = {
                "operation": action.name,
                "fault": str(action.params.get("fault") or "none"),
                "property": property_name,
                "action_index": action.index,
            }
            if control_failure is not None:
                mutations.append(
                    {
                        **row,
                        "status": "inconclusive",
                        "reason": "unmodified control prefix did not replay cleanly",
                    }
                )
                continue
            mutant = _mutated_property_trace(
                trace,
                action_index=action.index,
                property_name=property_name,
            )
            if mutant is None:
                continue
            observed = factory(config).replay(mutant)
            killed = observed is not None and observed.action_index == action.index
            mutations.append(
                {
                    **row,
                    "status": "killed" if killed else "survived",
                    "observed_failure_kind": observed.kind if observed is not None else None,
                }
            )

    killed = sum(row["status"] == "killed" for row in mutations)
    survived = sum(row["status"] == "survived" for row in mutations)
    inconclusive = sum(row["status"] == "inconclusive" for row in mutations)
    tested = killed + survived
    if failed_cells:
        status = "weak"
        protects = False
        summary = f"{len(failed_cells)} reliability cell(s) failed"
    elif survived:
        status = "weak"
        protects: bool | None = False
        summary = f"{survived}/{tested} workload oracle mutation(s) survived"
    elif unexercised:
        status = "weak"
        protects = False
        summary = f"{len(unexercised)} reliability cell(s) were not exercised"
    elif inconclusive:
        status = "inconclusive"
        protects = None
        summary = f"{inconclusive} workload mutation control(s) were unstable"
    elif tested <= 0:
        status = "inconclusive"
        protects = None
        summary = (
            "workload mutation strength was not requested"
            if budget == 0
            else "no observed mutable response properties were available"
        )
    else:
        status = "protective_within_measured_scope"
        protects = True
        summary = f"all {tested} tested workload oracle mutation(s) were killed"
    mutation_score = f"{killed}/{tested} ({killed / tested:.0%})" if tested else None
    return {
        "label": "compose workload protection",
        "status": status,
        "protects": protects,
        "summary": summary,
        "mutation_scope": "recorded Compose response oracles, not service source code",
        "mutation_score": mutation_score,
        "killed_mutants": killed,
        "tested_mutants": tested,
        "surviving_mutants": survived,
        "inconclusive_mutants": inconclusive,
        "failed_properties": failed_cells,
        "unexercised_properties": unexercised,
        "mutations": mutations,
    }


def build_compose_finding_evidence(
    trace: ComposeTrace,
    *,
    replay: ComposeReplayReport | None,
    coverage: Mapping[str, Any],
    protection: Mapping[str, Any],
    trace_path: Path | None = None,
) -> dict[str, Any]:
    """Build the shared bounded evidence-card schema for a Compose failure."""
    from ordeal.finding_evidence import _build_compose_finding_evidence

    return _build_compose_finding_evidence(
        trace.to_dict(),
        replay=replay.to_dict() if replay is not None else None,
        coverage=coverage,
        protection=protection,
        trace_path=trace_path.as_posix() if trace_path is not None else None,
    )


def run_compose_exploration(
    config: ComposeConfig,
    *,
    seed: int | None = None,
    max_time: float | None = None,
    replay_attempts: int | None = None,
) -> ComposeExplorationResult:
    """Run the Compose harness, save its exact trace, and replay failures."""
    effective = replace(
        config,
        seed=config.seed if seed is None else seed,
        max_time=config.max_time if max_time is None else max_time,
        replay_attempts=(config.replay_attempts if replay_attempts is None else replay_attempts),
    )
    if effective.max_time <= 0:
        raise ValueError("max_time must be > 0")
    if effective.replay_attempts < 1:
        raise ValueError("replay_attempts must be >= 1")
    trace = ComposeRunner(effective).run()
    trace_dir = Path(effective.trace_dir)
    trace_path = trace_dir / f"compose-{effective.seed}-{trace.content_hash()}.json"
    report = None
    if trace.failure is not None:
        report = replay_compose_trace(trace, attempts=effective.replay_attempts)
        trace.replay = report
    coverage = compose_reliability_coverage(trace)
    protection = measure_compose_workload_strength(
        trace,
        budget=effective.workload_mutations,
    )
    evidence = (
        build_compose_finding_evidence(
            trace,
            replay=report,
            coverage=coverage,
            protection=protection,
            trace_path=trace_path,
        )
        if trace.failure is not None
        else None
    )
    trace.save(trace_path)
    return ComposeExplorationResult(
        trace=trace,
        trace_path=trace_path,
        replay=report,
        requests=sum(action.kind == "request" for action in trace.actions),
        faults=sum(action.kind == "fault" for action in trace.actions),
        duration=trace.duration,
        coverage=coverage,
        protection=protection,
        evidence=evidence,
    )


def _portable_workspace_path(value: object, workspace: Path) -> object:
    """Render an in-workspace path portably while leaving external paths explicit."""
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _portable_trace_payload(trace: ComposeTrace, workspace: Path) -> dict[str, Any]:
    """Return a trace payload with repository-local config paths made relative."""
    payload = trace.to_dict()
    compose = payload.get("compose")
    if isinstance(compose, dict):
        for key in ("file", "trace_dir"):
            if key in compose:
                compose[key] = _portable_workspace_path(compose[key], workspace)
    return payload


def _evidence_with_compose_regression(
    evidence: Mapping[str, Any],
    *,
    trace_path: str,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one committed Compose trace binding to a copied evidence card."""
    copied = copy.deepcopy(dict(evidence))
    witness = copied.get("witness")
    if isinstance(witness, Mapping):
        witness_input = witness.get("input")
        if isinstance(witness_input, Mapping):
            portable_input = {**dict(witness_input), "trace_path": trace_path}
            from ordeal.finding_evidence import _sha256_json

            copied["witness"] = {
                **dict(witness),
                "input": portable_input,
                "sha256": _sha256_json(portable_input),
            }
    replay = copied.get("replay")
    if isinstance(replay, Mapping):
        copied["replay"] = {
            **dict(replay),
            "command": f"ordeal replay {trace_path}",
        }
    copied["regression"] = {
        "status": "saved",
        "path": trace_path,
        "test_name": None,
        "binding": dict(binding),
    }
    copied["ci_guard"] = {
        "status": "ready",
        "command": "uv run ordeal verify --ci",
        "acceptance": "Every replay attempt must complete without any failure after the fix.",
    }
    subject = copied.get("subject")
    if isinstance(subject, Mapping):
        copied["subject"] = {
            **dict(subject),
            "trace_sha256": binding.get("trace_sha256"),
        }
    workflow = copied.get("workflow")
    if isinstance(workflow, Mapping):
        copied["workflow"] = {
            **dict(workflow),
            "save_regression": "saved",
            "guard_ci": "ready",
        }
    return copied


def save_compose_regression(
    result: ComposeExplorationResult,
    *,
    workspace: str | Path = ".",
    regression_dir: str | Path = "tests/ordeal-compose-regressions",
    manifest_path: str | Path = "tests/ordeal-regressions.json",
) -> ComposeRegressionArtifacts | None:
    """Save a replay-backed Compose failure as a portable durable regression.

    Findings that never reproduced are deliberately not promoted. The manifest
    keeps the existing v1 envelope and distinguishes Compose records with
    ``runner=compose`` plus a canonical trace binding.
    """
    if (
        result.trace.failure is None
        or result.trace.failure.kind not in _DURABLE_FAILURE_KINDS
        or result.replay is None
        or result.replay.reproduced < 1
        or result.evidence is None
    ):
        return None
    root = Path(workspace).resolve()
    compose_file_value = str(result.trace.compose.get("file") or "").strip()
    compose_file = Path(compose_file_value)
    if not compose_file.is_absolute():
        compose_file = root / compose_file
    try:
        compose_file.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "durable Compose regressions require a Compose file in the workspace"
        ) from exc
    if not compose_file_value:
        raise ValueError("durable Compose regressions require a recorded Compose file")
    payload = _portable_trace_payload(result.trace, root)
    identity = {
        "failure_signature": result.trace.failure_signature,
        "actions": [
            {
                "kind": action.kind,
                "name": action.name,
                "params": action.params,
            }
            for action in result.trace.actions
        ],
    }
    canonical_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    finding_id = "fnd_compose_" + hashlib.sha256(canonical_identity.encode()).hexdigest()[:16]
    trace_root = Path(regression_dir)
    if not trace_root.is_absolute():
        trace_root = root / trace_root
    trace_path = trace_root / f"{finding_id}.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    from ordeal.regression_evidence import _compose_regression_binding

    binding = _compose_regression_binding(payload)
    relative_trace = trace_path.resolve().relative_to(root).as_posix()
    evidence = _evidence_with_compose_regression(
        result.evidence,
        trace_path=relative_trace,
        binding=binding,
    )
    result.evidence = evidence
    target_manifest = Path(manifest_path)
    if not target_manifest.is_absolute():
        target_manifest = root / target_manifest
    manifest: dict[str, Any] = {
        "schema": "ordeal.regression-manifest/v1",
        "regressions": [],
    }
    if target_manifest.is_file():
        try:
            loaded = json.loads(target_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("existing regression manifest is not valid JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("schema") != manifest["schema"]
            or not isinstance(loaded.get("regressions"), list)
        ):
            raise ValueError("existing regression manifest does not use the supported v1 schema")
        manifest["regressions"] = list(loaded["regressions"])
    records = {
        str(record.get("finding_id")): record
        for record in manifest["regressions"]
        if isinstance(record, Mapping) and record.get("finding_id")
    }
    records[finding_id] = {
        "finding_id": finding_id,
        "runner": "compose",
        "trace_file": relative_trace,
        "binding": binding,
        "failure_signature": result.trace.failure_signature,
        "replay_policy": {
            "attempts": result.replay.attempted,
            "expected": "clean",
            "maximum_failures": 0,
        },
        "evidence": evidence,
    }
    manifest["regressions"] = [records[key] for key in sorted(records)]
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    target_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return ComposeRegressionArtifacts(
        finding_id=finding_id,
        trace_path=trace_path,
        manifest_path=target_manifest,
        binding=binding,
    )
