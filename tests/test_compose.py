"""Tests for the long-lived Docker Compose service runner."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import ordeal.compose as compose_module
from ordeal.cli import main
from ordeal.compose import (
    ComposeCommandError,
    ComposeController,
    ComposeExplorationResult,
    ComposeFailure,
    ComposeReplayReport,
    ComposeRunner,
    ComposeTrace,
    HttpResponse,
    HttpTransport,
    replay_compose_trace,
)
from ordeal.config import ComposeConfig, ComposeRequestConfig, ConfigError, load_config


class FakeController:
    def __init__(self, *, owned: bool = True) -> None:
        self.owned = owned
        self.calls: list[tuple[str, ...]] = []

    def start(self) -> bool:
        self.calls.append(("start",))
        return self.owned

    def stop(self) -> None:
        self.calls.append(("stop",))

    def kill(self, service: str) -> None:
        self.calls.append(("kill", service))

    def start_service(self, service: str) -> None:
        self.calls.append(("start_service", service))

    def restart(self, service: str) -> None:
        self.calls.append(("restart", service))


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, object | None]] = []
        self.header_calls: list[dict[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: object | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append((method, url, json_body))
        self.header_calls.append(dict(headers))
        assert timeout > 0
        assert isinstance(headers, dict)
        return self.responses.pop(0)


def response(
    status: int = 200,
    body: bytes = b"{}",
    *,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers or {"content-type": "application/json"},
        body=body,
        elapsed=0.01,
    )


def compose_config(**overrides: object) -> ComposeConfig:
    values: dict[str, object] = {
        "base_url": "http://service.test",
        "file": "/tmp/compose.yaml",
        "services": ["worker"],
        "requests": [ComposeRequestConfig(name="read", path="/items")],
        "steps": 1,
        "fault_probability": 0.0,
        "faults": [],
        "max_time": 10.0,
        "startup_timeout": 1.0,
    }
    values.update(overrides)
    return ComposeConfig(**values)


class TestComposeConfig:
    def test_loads_requests_state_faults_and_relative_paths(self, tmp_path: Path) -> None:
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(
            """
[compose]
file = "docker-compose.yml"
base_url = "http://127.0.0.1:8080"
health_path = "/ready"
services = ["api", "worker"]
faults = ["kill", "restart", "delay_response", "corrupt_response"]
steps = 12
replay_attempts = 7
initial_state = {tenant = "acme"}

[[compose.requests]]
name = "create"
method = "POST"
path = "/{tenant}/items"
json = {name = "demo"}
expect_status = [200, 201]
capture = {item_id = "json.id"}

[[compose.requests]]
name = "read"
path = "/items/{item_id}"
requires = ["item_id"]
expect_json = {"json.active" = true}
""",
            encoding="utf-8",
        )

        cfg = load_config(config_path)

        assert cfg.compose is not None
        assert cfg.compose.file == str(tmp_path / "docker-compose.yml")
        assert cfg.compose.trace_dir == str(tmp_path / ".ordeal/traces")
        assert cfg.compose.initial_state == {"tenant": "acme"}
        assert cfg.compose.replay_attempts == 7
        assert cfg.compose.requests[0].faultable is False
        assert cfg.compose.requests[1].faultable is True
        assert cfg.compose.requests[1].requires == ["item_id"]

    def test_defaults_to_root_request_and_transport_faults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text('[compose]\nbase_url = "http://localhost:8000"\n', encoding="utf-8")

        cfg = load_config(config_path)

        assert cfg.compose is not None
        assert [request.name for request in cfg.compose.requests] == ["root"]
        assert cfg.compose.faults == ["delay_response", "corrupt_response"]

    @pytest.mark.parametrize(
        "body, message",
        [
            ('base_url = "localhost:8000"', "absolute"),
            ('base_url = "http://localhost"\nfault_probability = 2', "between"),
            ('base_url = "http://localhost"\nfaults = ["kill"]', "services"),
            ('base_url = "http://localhost"\nreplay_attempts = 0', ">= 1"),
        ],
    )
    def test_rejects_invalid_service_config(self, tmp_path: Path, body: str, message: str) -> None:
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(f"[compose]\n{body}\n", encoding="utf-8")

        with pytest.raises(ConfigError, match=message):
            load_config(config_path)

    @pytest.mark.parametrize(
        "body, message",
        [
            (
                'base_url = "http://localhost"\nkeep_running = "false"',
                "compose.keep_running must be a boolean",
            ),
            (
                'base_url = "http://localhost"\n'
                '[[compose.requests]]\nname = "write"\nmethod = "POST"\n'
                'faultable = "false"',
                "compose.requests.0.faultable must be a boolean",
            ),
        ],
    )
    def test_rejects_string_boolean_safety_settings(
        self,
        tmp_path: Path,
        body: str,
        message: str,
    ) -> None:
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(f"[compose]\n{body}\n", encoding="utf-8")

        with pytest.raises(ConfigError, match=message):
            load_config(config_path)


class TestComposeDocumentation:
    pages = (
        "compose-runner.md",
        "compose-quickstart.md",
        "compose-configuration.md",
        "compose-stateful-workflows.md",
        "compose-fault-model.md",
        "compose-traces.md",
        "compose-operations.md",
        "compose-troubleshooting.md",
    )

    def test_configuration_reference_lists_every_public_field(self) -> None:
        docs_dir = Path(__file__).parents[1] / "docs" / "guides"
        reference = (docs_dir / "compose-configuration.md").read_text(encoding="utf-8")
        config_keys = {field.name for field in fields(ComposeConfig)}
        request_keys = {
            "json" if field.name == "json_body" else field.name
            for field in fields(ComposeRequestConfig)
        }

        for key in config_keys | request_keys:
            assert f"`{key}`" in reference, f"Compose docs omit {key!r}"

    def test_all_compose_pages_are_navigated_and_under_line_limit(self) -> None:
        root = Path(__file__).parents[1]
        nav = (root / "mkdocs.yml").read_text(encoding="utf-8")

        for filename in self.pages:
            page = root / "docs" / "guides" / filename
            assert len(page.read_text(encoding="utf-8").splitlines()) <= 130
            assert f"guides/{filename}" in nav


class TestComposeController:
    def test_uses_argv_and_only_owns_new_topology(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            stdout = "" if argv[-2:] == ["ps", "-q"] else "ok"
            assert kwargs["check"] is False
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        cfg = compose_config(project_name="ordeal-test")
        controller = ComposeController(cfg, run_command=fake_run)

        assert controller.start() is True
        controller.kill("worker")
        controller.start_service("worker")
        controller.restart("worker")
        controller.stop()

        prefix = [
            "docker",
            "compose",
            "-f",
            "/tmp/compose.yaml",
            "--project-name",
            "ordeal-test",
        ]
        assert calls[0] == [*prefix, "ps", "-q"]
        assert calls[1] == [*prefix, "up", "-d"]
        assert [*prefix, "kill", "-s", "SIGKILL", "worker"] in calls
        assert [*prefix, "down", "--remove-orphans"] == calls[-1]

    def test_missing_docker_is_actionable(self) -> None:
        def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError

        controller = ComposeController(compose_config(), run_command=missing)

        with pytest.raises(ComposeCommandError, match="docker was not found"):
            controller.start()


class TestComposeRunner:
    def test_trace_preserves_env_placeholders_and_redacts_raw_secrets(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("SERVICE_TOKEN", "transport-token")
        monkeypatch.setenv("SERVICE_PASSWORD", "transport-password")
        request = ComposeRequestConfig(
            name="authenticated",
            path="/items",
            headers={"Authorization": "Bearer ${SERVICE_TOKEN}"},
            json_body={"password": "${SERVICE_PASSWORD}", "name": "demo"},
        )
        transport = FakeTransport(
            [
                response(),
                response(
                    headers={
                        "Set-Cookie": "session=response-secret",
                        "Authentication-Info": "nextnonce=another-secret",
                    }
                ),
            ]
        )

        trace = ComposeRunner(
            compose_config(requests=[request]),
            controller=FakeController(),
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is None
        assert transport.header_calls[-1]["Authorization"] == "Bearer transport-token"
        assert transport.calls[-1][2] == {
            "password": "transport-password",
            "name": "demo",
        }
        serialized = json.dumps(trace.to_dict(), sort_keys=True)
        assert "transport-token" not in serialized
        assert "transport-password" not in serialized
        assert "response-secret" not in serialized
        assert "another-secret" not in serialized
        assert "${SERVICE_TOKEN}" in serialized
        assert "${SERVICE_PASSWORD}" in serialized
        request_action = next(action for action in trace.actions if action.kind == "request")
        assert "body_preview_base64" not in request_action.result

    def test_trace_redacts_literal_sensitive_fields(self) -> None:
        trace = ComposeTrace(
            seed=1,
            compose={
                "requests": [
                    {
                        "headers": {
                            "x-api-key": "raw-api-secret",
                            "auth": "Basic raw-prefix ${AUTH_TOKEN}",
                        },
                        "json_body": {
                            "accessToken": "raw-body-secret",
                            "credentials": {
                                "username": "raw-user",
                                "password": "${SERVICE_PASSWORD}",
                            },
                        },
                    }
                ]
            },
        )

        payload = trace.to_dict()
        serialized = json.dumps(payload, sort_keys=True)

        assert "raw-api-secret" not in serialized
        assert "raw-body-secret" not in serialized
        assert "raw-prefix" not in serialized
        assert "raw-user" not in serialized
        assert "${AUTH_TOKEN}" in serialized
        assert "${SERVICE_PASSWORD}" in serialized
        assert serialized.count("<redacted>") >= 4

    def test_unexpected_json_failure_omits_expected_and_observed_values(self) -> None:
        request = ComposeRequestConfig(
            name="check-token",
            path="/token",
            expect_json={"json.accessToken": "expected-secret"},
        )
        transport = FakeTransport(
            [response(), response(body=b'{"accessToken":"observed-secret"}')]
        )

        trace = ComposeRunner(
            compose_config(requests=[request]),
            controller=FakeController(),
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is not None
        assert trace.failure.kind == "unexpected_json"
        serialized = json.dumps(trace.to_dict(), sort_keys=True)
        assert "expected-secret" not in serialized
        assert "observed-secret" not in serialized
        assert "response differed" in serialized

    def test_sensitive_capture_path_redacts_neutral_state_name(self) -> None:
        request = ComposeRequestConfig(
            name="capture-token",
            path="/token",
            capture={"value": "json.token"},
        )
        transport = FakeTransport([response(), response(body=b'{"token":"captured-secret"}')])

        trace = ComposeRunner(
            compose_config(requests=[request]),
            controller=FakeController(),
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.final_state["value"] == "captured-secret"
        payload = trace.to_dict()
        serialized = json.dumps(payload, sort_keys=True)
        assert "captured-secret" not in serialized
        assert payload["final_state"] == {"value": "<redacted>"}
        request_payload = next(
            action for action in payload["actions"] if action["kind"] == "request"
        )
        assert request_payload["result"]["captured_state"] == {"value": "<redacted>"}

    def test_captured_state_is_reused_by_later_requests(self) -> None:
        request = ComposeRequestConfig(
            name="read",
            path="/items/{item_id}",
            capture={"item_id": "json.id"},
            requires=["item_id"],
        )
        cfg = compose_config(
            requests=[request],
            initial_state={"item_id": "seed"},
            steps=2,
        )
        transport = FakeTransport(
            [
                response(),
                response(body=b'{"id":"captured"}'),
                response(body=b'{"id":"final"}'),
            ]
        )

        trace = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is None
        request_urls = [url for method, url, body in transport.calls if "/items/" in url]
        assert request_urls == [
            "http://service.test/items/seed",
            "http://service.test/items/captured",
        ]
        assert trace.final_state == {"item_id": "final"}

    def test_kills_recovers_repeats_request_and_preserves_state(self) -> None:
        request = ComposeRequestConfig(
            name="read",
            path="/items/{item_id}",
            capture={"item_id": "json.id"},
            requires=["item_id"],
        )
        cfg = compose_config(
            requests=[request],
            initial_state={"item_id": "seed"},
            fault_probability=1.0,
            faults=["kill"],
        )
        controller = FakeController()
        transport = FakeTransport(
            [
                response(),
                response(503),
                response(),
                response(body=b'{"id":"captured"}'),
            ]
        )

        trace = ComposeRunner(
            cfg,
            controller=controller,
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is None
        assert trace.final_state == {"item_id": "captured"}
        assert [(action.kind, action.name) for action in trace.actions] == [
            ("lifecycle", "up"),
            ("lifecycle", "wait_ready"),
            ("fault", "kill"),
            ("request", "read"),
            ("lifecycle", "start_service"),
            ("lifecycle", "wait_ready"),
            ("request", "read"),
            ("lifecycle", "down"),
        ]
        request_actions = [action for action in trace.actions if action.kind == "request"]
        assert request_actions[0].params["validate"] is False
        assert request_actions[1].params["validate"] is True
        assert controller.calls == [
            ("start",),
            ("kill", "worker"),
            ("start_service", "worker"),
            ("stop",),
        ]

    def test_corrupts_only_fault_window_then_validates_recovery(self) -> None:
        cfg = compose_config(fault_probability=1.0, faults=["corrupt_response"])
        transport = FakeTransport([response(), response(body=b'{"ok":true}'), response()])

        trace = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is None
        requests = [action for action in trace.actions if action.kind == "request"]
        assert requests[0].result["body_sha256"] != requests[0].result["original_body_sha256"]
        assert requests[0].result["response_fault"] == "corrupt_response"
        assert requests[1].result["response_fault"] is None

    def test_restarts_worker_waits_and_validates_request(self) -> None:
        cfg = compose_config(fault_probability=1.0, faults=["restart"])
        controller = FakeController()
        transport = FakeTransport([response(), response(), response()])

        trace = ComposeRunner(
            cfg,
            controller=controller,
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is None
        assert ("restart", "worker") in controller.calls
        assert [(action.kind, action.name) for action in trace.actions][2:5] == [
            ("fault", "restart"),
            ("lifecycle", "wait_ready"),
            ("request", "read"),
        ]

    def test_delay_is_recorded_at_transport_boundary(self) -> None:
        sleeps: list[float] = []
        cfg = compose_config(
            fault_probability=1.0,
            faults=["delay_response"],
            delay_seconds=0.25,
        )
        transport = FakeTransport([response(), response(), response()])

        trace = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=transport,
            sleep=sleeps.append,
        ).run()

        assert trace.failure is None
        assert 0.25 in sleeps
        faulted = next(action for action in trace.actions if action.kind == "request")
        assert faulted.result["elapsed_seconds"] == pytest.approx(0.26)

    def test_records_stable_failure_signature(self) -> None:
        cfg = compose_config()
        transport = FakeTransport([response(), response(500)])

        trace = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=transport,
            sleep=lambda _seconds: None,
        ).run()

        assert trace.failure is not None
        assert trace.failure.kind == "unexpected_status"
        assert trace.failure_signature == trace.failure.signature
        assert len(trace.failure_signature) == 64


class TestComposeTraceReplay:
    def test_trace_round_trip_preserves_exact_actions(self, tmp_path: Path) -> None:
        trace = ComposeTrace(
            seed=7,
            compose={"base_url": "http://service", "requests": []},
            actions=[
                compose_module.ComposeTraceAction(
                    index=0,
                    kind="fault",
                    name="delay_response",
                    params={"seconds": 0.2},
                    result={"armed_for_next_request": True},
                )
            ],
            final_state={"id": 4},
        )
        path = tmp_path / "trace.json.gz"

        trace.save(path)
        loaded = ComposeTrace.load(path)

        assert loaded.to_dict() == trace.to_dict()
        assert loaded.content_hash() == trace.content_hash()
        assert ComposeTrace.is_trace_file(path)

    def test_replay_reports_attempted_and_exact_reproductions(self) -> None:
        expected = ComposeFailure(
            kind="unexpected_status",
            message="GET http://service/items expected 2xx, got 500",
            action_index=2,
            action_name="read",
        )
        trace = ComposeTrace(
            seed=1,
            compose={
                "base_url": "http://service",
                "file": "/tmp/compose.yaml",
                "requests": [],
                "replay_attempts": 3,
            },
            failure=expected,
        )

        class AlternatingRunner:
            calls = 0

            def __init__(self, config: ComposeConfig) -> None:
                assert config.base_url == "http://service"

            def replay(self, source: ComposeTrace) -> ComposeFailure | None:
                assert source is trace
                type(self).calls += 1
                return expected if type(self).calls != 2 else None

        report = replay_compose_trace(trace, runner_factory=AlternatingRunner)

        assert report.attempted == 3
        assert report.reproduced == 2
        assert report.observed_signatures == [expected.signature, None, expected.signature]
        assert "not deterministic" in report.boundary

    def test_actual_runner_replays_recorded_failure_actions(self) -> None:
        cfg = compose_config()
        source = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=FakeTransport([response(), response(500)]),
            sleep=lambda _seconds: None,
        ).run()
        assert source.failure is not None

        def runner_factory(replay_config: ComposeConfig) -> ComposeRunner:
            return ComposeRunner(
                replay_config,
                controller=FakeController(),
                transport=FakeTransport([response(), response(500)]),
                sleep=lambda _seconds: None,
            )

        report = replay_compose_trace(source, attempts=2, runner_factory=runner_factory)

        assert report.attempted == 2
        assert report.reproduced == 2
        assert report.observed_signatures == [source.failure_signature] * 2

    def test_run_wrapper_saves_trace_and_replay_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = compose_config(trace_dir=str(tmp_path), replay_attempts=4)
        failure = ComposeFailure("unexpected_status", "boom", 2, "read")
        trace = ComposeTrace(
            seed=cfg.seed,
            compose=compose_module._config_payload(cfg),
            failure=failure,
        )

        class StubRunner:
            def __init__(self, config: ComposeConfig) -> None:
                assert config is not cfg

            def run(self) -> ComposeTrace:
                return trace

        expected_report = ComposeReplayReport(4, 3, failure.signature)
        monkeypatch.setattr(compose_module, "ComposeRunner", StubRunner)
        monkeypatch.setattr(
            compose_module,
            "replay_compose_trace",
            lambda source, *, attempts: expected_report,
        )

        result = compose_module.run_compose_exploration(cfg)

        assert result.trace_path.exists()
        assert result.replay == expected_report
        assert ComposeTrace.load(result.trace_path).replay == expected_report


class TestHttpTransport:
    def test_returns_http_errors_as_observable_responses(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                self.send_response(418)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"teapot"}')

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response_value = HttpTransport().request(
                "GET",
                f"http://127.0.0.1:{server.server_port}/",
                headers={},
                json_body=None,
                timeout=1.0,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response_value.status == 418
        assert json.loads(response_value.body) == {"status": "teapot"}


class TestComposeCLI:
    def test_explore_runner_compose_reports_trace_and_replay_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text('[compose]\nbase_url = "http://localhost:8000"\n', encoding="utf-8")
        trace = ComposeTrace(
            seed=42,
            compose={"base_url": "http://localhost:8000", "requests": []},
            failure=ComposeFailure("unexpected_status", "boom", 2, "root"),
        )
        report = ComposeReplayReport(3, 2, trace.failure_signature)
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=tmp_path / "trace.json",
            replay=report,
            requests=2,
            faults=1,
            duration=0.1,
        )
        monkeypatch.setattr(compose_module, "run_compose_exploration", lambda *a, **k: result)

        code = main(["explore", "--runner", "compose", "-c", str(config_path)])

        assert code == 1
        stderr = capsys.readouterr().err
        assert "Actions: 0 exact, requests=2, faults=1" in stderr
        assert "Replay attempted 3 times, reproduced 2 times." in stderr
        assert "not deterministic" in stderr

    def test_replay_compose_trace_accepts_attempt_count_and_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        trace = ComposeTrace(
            seed=1,
            compose={"base_url": "http://service", "requests": []},
            failure=ComposeFailure("request_error", "closed", 1, "root"),
        )
        path = tmp_path / "trace.json"
        trace.save(path)

        def fake_replay(
            source: ComposeTrace, *, attempts: int | None = None
        ) -> ComposeReplayReport:
            assert source.failure_signature == trace.failure_signature
            assert attempts == 5
            return ComposeReplayReport(5, 3, trace.failure_signature)

        monkeypatch.setattr(compose_module, "replay_compose_trace", fake_replay)

        code = main(["replay", str(path), "--attempts", "5", "--json"])

        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["runner"] == "compose"
        assert payload["replay"]["attempted"] == 5
        assert payload["replay"]["reproduced"] == 3
