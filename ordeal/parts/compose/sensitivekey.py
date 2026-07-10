from __future__ import annotations
# ruff: noqa
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
