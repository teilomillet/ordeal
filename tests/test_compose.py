"""Tests for the long-lived Docker Compose service runner."""

from __future__ import annotations

import json
import re
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
    compose_reliability_coverage,
    measure_compose_workload_strength,
    replay_compose_trace,
    save_compose_regression,
)
from ordeal.config import ComposeConfig, ComposeRequestConfig, ConfigError, load_config
from ordeal.finding_evidence import _sha256_json
from scripts.verify_compose_e2e_trace import EXPECTED_ACTIONS, verify_trace


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
        assert cfg.compose.workload_mutations == 0
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
            ('base_url = "http://localhost"\nworkload_mutations = -1', ">= 0"),
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
        "compose-evidence-loop.md",
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


class TestComposeEndToEndGate:
    def test_checked_in_gate_uses_real_runner_and_blocks_publish(self) -> None:
        root = Path(__file__).parents[1]
        fixture = root / "tests" / "fixtures" / "compose_e2e"
        cfg = load_config(fixture / "ordeal.toml").compose
        assert cfg is not None
        assert cfg.file == str((fixture / "compose.yaml").resolve())
        assert cfg.services == ["api"]
        assert cfg.steps == 2
        assert cfg.seed == 0
        assert cfg.fault_probability == 0.5
        assert cfg.faults == ["kill"]
        assert cfg.replay_attempts == 3
        assert cfg.requests[0].expect_json == {
            "json.status": "ok",
            "json.service": "compose-e2e",
        }

        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = workflow.split("  compose-e2e:\n", 1)[1].split("  bump-and-publish:\n", 1)[0]
        assert "docker compose" in job
        assert "verify_compose_evidence_loop.py" in job
        assert "verify_compose_service_matrix.py" in job
        assert "compose-evidence-loop.json" in job
        assert "compose-service-matrix.json" in job
        assert "shared_memory_failure_falls_back_in_parent" in job
        service = (fixture / "service.py").read_text(encoding="utf-8")
        compose = (fixture / "compose.yaml").read_text(encoding="utf-8")
        assert {'"buggy"', '"fixed"'} <= set(re.findall(r'"[a-z]+"', service))
        assert "ORDEAL_SERVICE_VARIANT" in compose
        publish_needs = workflow.split("  bump-and-publish:\n", 1)[1].split("    runs-on:", 1)[0]
        assert "      - compose-e2e\n" in publish_needs

    def test_service_matrix_fixtures_cover_persistence_concurrency_and_broader_faults(
        self,
    ) -> None:
        root = Path(__file__).parents[1]
        persistence = load_config(
            root / "tests" / "fixtures" / "compose_persistence" / "ordeal.toml"
        ).compose
        concurrency = load_config(
            root / "tests" / "fixtures" / "compose_concurrency" / "ordeal.toml"
        ).compose
        assert persistence is not None and concurrency is not None
        assert persistence.services == ["api"]
        assert persistence.faults == ["restart"]
        assert persistence.steps == 2
        assert concurrency.faults == ["delay_response", "corrupt_response"]
        assert concurrency.steps == 3
        assert concurrency.requests[0].expect_json["json.max_concurrency"] == 8
        matrix = (root / "scripts" / "verify_compose_service_matrix.py").read_text(
            encoding="utf-8"
        )
        assert 'persistence["services"] == ["api", "store"]' in matrix
        assert 'concurrency["services"] == ["api", "worker"]' in matrix
        assert '{"delay_response", "corrupt_response"}' in matrix

    def test_checked_in_service_regression_is_portable_and_replay_bounded(self) -> None:
        root = Path(__file__).parents[1]
        fixture = root / "tests" / "fixtures" / "compose_e2e"
        manifest_path = fixture / "tests" / "ordeal-regressions.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert manifest["schema"] == "ordeal.regression-manifest/v1"
        assert len(manifest["regressions"]) == 1
        record = manifest["regressions"][0]
        assert record["runner"] == "compose"
        assert not Path(record["trace_file"]).is_absolute()
        assert record["replay_policy"] == {
            "attempts": 3,
            "expected": "clean",
            "maximum_failures": 0,
        }
        evidence = record["evidence"]
        witness = evidence["witness"]
        assert witness["input"]["trace_path"] == record["trace_file"]
        assert witness["sha256"] == _sha256_json(witness["input"])
        assert evidence["replay"]["command"] == f"ordeal replay {record['trace_file']}"
        assert str(root) not in json.dumps(record)
        trace = ComposeTrace.load(fixture / record["trace_file"])
        assert trace.compose["file"] == "compose.yaml"
        assert trace.replay is not None
        assert trace.replay.attempted == 3
        assert trace.replay.reproduced == 3
        assert trace.replay.observed_signatures == [trace.failure_signature] * 3
        assert [(action.kind, action.name) for action in trace.actions] == EXPECTED_ACTIONS[:-1]
        control = evidence["post_fix_control"]
        assert control["status"] == "passed"
        assert control["fixed_state"]["status"] == "complete"
        assert control["fixed_state"]["reliability_coverage"]["summary"] == {
            "pass": 9,
            "not_exercised": 0,
            "fail": 0,
            "total": 9,
        }
        assert (
            control["fixed_state"]["workload_protection"]["status"]
            == "protective_within_measured_scope"
        )
        assert control["fixed_state"]["workload_protection"]["mutation_score"] == ("4/4 (100%)")
        assert control["fixed_state_sha256"] == _sha256_json(control["fixed_state"])
        assert evidence["workflow"]["verify_fix"] == "passed"

    def test_fixed_fixture_shape_covers_all_cells_and_controls_both_paths(self) -> None:
        root = Path(__file__).parents[1]
        fixture = root / "tests" / "fixtures" / "compose_e2e"
        config = load_config(fixture / "ordeal.toml").compose
        assert config is not None
        fixed_body = b'{"service":"compose-e2e","status":"ok","variant":"fixed"}'
        trace = ComposeRunner(
            config,
            controller=FakeController(),
            transport=FakeTransport(
                [
                    response(),
                    response(body=fixed_body),
                    response(),
                    response(),
                    response(body=fixed_body),
                ]
            ),
            sleep=lambda _seconds: None,
        ).run()
        assert trace.failure is None
        assert compose_reliability_coverage(trace)["summary"] == {
            "pass": 9,
            "not_exercised": 0,
            "fail": 0,
            "total": 9,
        }

        class OracleRunner:
            def __init__(self, replay_config: ComposeConfig) -> None:
                assert replay_config.project_name == "ordeal-compose-e2e"

            def replay(self, source: ComposeTrace) -> ComposeFailure | None:
                action = next(
                    candidate
                    for candidate in reversed(source.actions)
                    if candidate.kind == "request" and candidate.params.get("validate")
                )
                if action.params.get("expect_status") != [200]:
                    return ComposeFailure("unexpected_status", "mutant", action.index, action.name)
                expected = {"json.status": "ok", "json.service": "compose-e2e"}
                if action.params.get("expect_json") != expected:
                    return ComposeFailure("unexpected_json", "mutant", action.index, action.name)
                return None

        protection = measure_compose_workload_strength(
            trace,
            budget=4,
            runner_factory=OracleRunner,
        )
        assert protection["status"] == "protective_within_measured_scope"
        assert protection["mutation_score"] == "4/4 (100%)"
        assert {row["fault"] for row in protection["mutations"]} == {"none", "kill"}

    def test_documentation_exposes_the_gate_from_main_entrypoints(self) -> None:
        root = Path(__file__).parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        docs_index = (root / "docs" / "index.md").read_text(encoding="utf-8")
        overview = (root / "docs" / "guides" / "compose-runner.md").read_text(encoding="utf-8")
        operations = (root / "docs" / "guides" / "compose-operations.md").read_text(
            encoding="utf-8"
        )

        assert "Compose CI and operations" in readme
        assert "Put real Compose recovery in CI" in docs_index
        assert "Real Docker evidence in this repository" in overview
        assert "The real gate in this repository" in operations
        assert "What a green job establishes" in operations
        assert "fault model" in operations

    def test_trace_verifier_requires_the_full_kill_recovery_sequence(self, tmp_path: Path) -> None:
        results = [
            {"owned_cleanup": True},
            {"attempts": 1, "status": 200},
            {"expected_fault_window": False, "status": 200},
            {"service": "api", "signal": "SIGKILL"},
            {"expected_fault_window": True, "request_error": "connection refused"},
            {"service": "api", "started": True},
            {"attempts": 1, "status": 200},
            {"expected_fault_window": False, "status": 200},
            {"stopped": True},
        ]
        trace = ComposeTrace(
            seed=42,
            compose={"base_url": "http://127.0.0.1:18080", "requests": []},
            actions=[
                compose_module.ComposeTraceAction(
                    index=index,
                    kind=kind,
                    name=name,
                    result=result,
                )
                for index, ((kind, name), result) in enumerate(zip(EXPECTED_ACTIONS, results))
            ],
        )
        trace_path = tmp_path / "compose-42-test.json"
        trace.save(trace_path)

        assert verify_trace(tmp_path) == trace_path


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

    def test_reports_operation_fault_property_coverage(self) -> None:
        request = ComposeRequestConfig(
            name="read",
            path="/items",
            expect_status=[200],
            expect_json={"json.ok": True},
            capture={"item_id": "json.id"},
        )
        cfg = compose_config(
            requests=[request],
            fault_probability=1.0,
            faults=["corrupt_response"],
        )
        trace = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=FakeTransport(
                [
                    response(),
                    response(body=b'{"ok":true,"id":1}'),
                    response(body=b'{"ok":true,"id":1}'),
                ]
            ),
            sleep=lambda _seconds: None,
        ).run()

        coverage = compose_reliability_coverage(trace)
        rows = {(row["operation"], row["fault"], row["property"]): row for row in coverage["rows"]}

        assert coverage["dimensions"] == ["operation", "fault", "property"]
        assert rows[("read", "corrupt_response", "status:200")]["status"] == "PASS"
        assert rows[("read", "corrupt_response", "valid_json")]["status"] == "PASS"
        assert rows[("read", "corrupt_response", "json:json.ok")]["status"] == "PASS"
        assert rows[("read", "corrupt_response", "capture:item_id")]["status"] == "PASS"
        assert rows[("read", "none", "status:200")]["status"] == "NOT EXERCISED"
        assert coverage["summary"] == {
            "pass": 4,
            "not_exercised": 4,
            "fail": 0,
            "total": 8,
        }

    def test_failed_property_is_not_erased_by_failure_metadata(self) -> None:
        cfg = compose_config(requests=[ComposeRequestConfig(name="read", expect_status=[200])])
        trace = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=FakeTransport([response(), response(503)]),
            sleep=lambda _seconds: None,
        ).run()

        request_action = next(action for action in trace.actions if action.kind == "request")
        assert request_action.result["property_results"] == [
            {"property": "status:200", "type": "always", "passed": False}
        ]
        coverage = compose_reliability_coverage(trace)
        assert coverage["rows"] == [
            {
                "operation": "read",
                "fault": "none",
                "property": "status:200",
                "type": "always",
                "status": "FAIL",
                "hits": 1,
                "passes": 0,
                "failures": 1,
            }
        ]

    def test_recovery_readiness_failure_is_a_fault_property_cell(self) -> None:
        cfg = compose_config(
            requests=[ComposeRequestConfig(name="read")],
            faults=["kill"],
        )
        ticks = iter([0.0, 0.0, 2.0])
        runner = ComposeRunner(
            cfg,
            controller=FakeController(),
            transport=FakeTransport([]),
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )
        runner._started_at = 0.0
        trace = ComposeTrace(seed=1, compose=compose_module._config_payload(cfg))
        action = runner._ready_action(trace, operation="read", fault="kill")
        trace.actions.append(action)
        trace.failure = runner._wait_ready(action)

        coverage = compose_reliability_coverage(trace)
        rows = {(row["operation"], row["fault"], row["property"]): row for row in coverage["rows"]}

        assert trace.failure is not None
        assert trace.failure.kind == "readiness_timeout"
        assert rows[("read", "kill", "service_ready")]["status"] == "FAIL"


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


class TestComposeEvidenceLoop:
    def test_workload_mutations_require_a_clean_control_and_detect_oracle_changes(self) -> None:
        action = compose_module.ComposeTraceAction(
            index=0,
            kind="request",
            name="read",
            params={
                "method": "GET",
                "url": "http://service/items",
                "validate": True,
                "fault": "restart",
                "expect_status": [200],
                "expect_json": {"json.ok": True},
                "capture": {},
            },
            result={
                "property_results": [
                    {"property": "status:200", "type": "always", "passed": True},
                    {"property": "valid_json", "type": "always", "passed": True},
                    {"property": "json:json.ok", "type": "always", "passed": True},
                ]
            },
        )
        trace = ComposeTrace(
            seed=1,
            compose={
                "base_url": "http://service",
                "file": "compose.yaml",
                "requests": [],
                "replay_attempts": 2,
            },
            actions=[action],
        )

        class OracleRunner:
            def __init__(self, config: ComposeConfig) -> None:
                assert config.base_url == "http://service"

            def replay(self, source: ComposeTrace) -> ComposeFailure | None:
                request = source.actions[-1]
                if request.params["expect_status"] != [200]:
                    return ComposeFailure("unexpected_status", "mutant", 0, "read")
                if request.params["expect_json"] != {"json.ok": True}:
                    return ComposeFailure("unexpected_json", "mutant", 0, "read")
                return None

        protection = measure_compose_workload_strength(
            trace,
            budget=3,
            runner_factory=OracleRunner,
        )

        assert protection["status"] == "protective_within_measured_scope"
        assert protection["mutation_score"] == "2/2 (100%)"
        assert protection["tested_mutants"] == 2
        assert protection["killed_mutants"] == 2
        assert protection["inconclusive_mutants"] == 0
        assert {row["property"] for row in protection["mutations"]} == {
            "status:200",
            "json:json.ok",
        }

    def test_compose_failure_uses_scan_evidence_schema(self) -> None:
        failure = ComposeFailure("unexpected_status", "boom", 0, "read")
        trace = ComposeTrace(
            seed=1,
            compose={"base_url": "http://service", "file": "compose.yaml", "requests": []},
            failure=failure,
        )
        replay = ComposeReplayReport(3, 2, failure.signature)
        coverage = {
            "dimensions": ["operation", "fault", "property"],
            "rows": [],
            "summary": {"pass": 1, "not_exercised": 0, "fail": 1, "total": 2},
        }
        protection = {
            "status": "weak",
            "protects": False,
            "summary": "1 workload mutation survived",
        }

        evidence = compose_module.build_compose_finding_evidence(
            trace,
            replay=replay,
            coverage=coverage,
            protection=protection,
            trace_path=Path(".ordeal/traces/failure.json"),
        )

        assert evidence["schema"] == "ordeal.finding-evidence/v1"
        assert evidence["status"] == "supported"
        assert evidence["subject"]["runner"] == "compose"
        assert evidence["observation"]["failure_signature"] == failure.signature
        assert evidence["replay"]["exact_matches"] == 2
        assert evidence["reliability_coverage"] == coverage
        assert evidence["test_protection"] == protection
        assert "2/3" in evidence["boundaries"]["establishes"]

    def test_replay_backed_failure_becomes_portable_manifest_entry(
        self,
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "compose.yaml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        failure = ComposeFailure("unexpected_status", "boom", 0, "read")
        trace = ComposeTrace(
            seed=1,
            compose={
                "base_url": "http://service",
                "file": str(compose_file),
                "trace_dir": str(tmp_path / ".ordeal" / "traces"),
                "requests": [],
                "replay_attempts": 3,
            },
            failure=failure,
            replay=ComposeReplayReport(3, 2, failure.signature),
        )
        source_trace_path = tmp_path / ".ordeal" / "traces" / "failure.json"
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=source_trace_path,
            replay=trace.replay,
            requests=1,
            faults=1,
            duration=0.1,
            evidence=compose_module.build_compose_finding_evidence(
                trace,
                replay=trace.replay,
                coverage=compose_reliability_coverage(trace),
                protection={"status": "inconclusive", "protects": None},
                trace_path=source_trace_path,
            ),
        )

        artifacts = save_compose_regression(result, workspace=tmp_path)

        assert artifacts is not None
        assert artifacts.trace_path.parent == tmp_path / "tests" / "ordeal-compose-regressions"
        saved = json.loads(artifacts.trace_path.read_text())
        assert saved["compose"]["file"] == "compose.yaml"
        assert saved["compose"]["trace_dir"] == ".ordeal/traces"
        manifest = json.loads(artifacts.manifest_path.read_text())
        record = manifest["regressions"][0]
        assert manifest["schema"] == "ordeal.regression-manifest/v1"
        assert record["runner"] == "compose"
        assert record["trace_file"].startswith("tests/ordeal-compose-regressions/")
        assert record["binding"]["schema"] == "ordeal.compose-regression-binding/v1"
        assert record["failure_signature"] == failure.signature
        assert record["replay_policy"] == {
            "attempts": 3,
            "expected": "clean",
            "maximum_failures": 0,
        }
        assert record["evidence"]["regression"]["status"] == "saved"
        witness = record["evidence"]["witness"]
        assert witness["input"]["trace_path"] == record["trace_file"]
        assert witness["sha256"] == _sha256_json(witness["input"])
        assert record["evidence"]["replay"]["command"] == (f"ordeal replay {record['trace_file']}")

    def test_durable_promotion_rejects_external_compose_file(self, tmp_path: Path) -> None:
        failure = ComposeFailure("unexpected_status", "boom", 0, "read")
        replay = ComposeReplayReport(1, 1, failure.signature)
        trace = ComposeTrace(
            seed=1,
            compose={
                "base_url": "http://service",
                "file": str(tmp_path.parent / "outside-compose.yaml"),
                "requests": [],
            },
            failure=failure,
            replay=replay,
        )
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=tmp_path / "trace.json",
            replay=replay,
            requests=1,
            faults=0,
            duration=0.1,
            evidence=compose_module.build_compose_finding_evidence(
                trace,
                replay=replay,
                coverage=compose_reliability_coverage(trace),
                protection={"status": "inconclusive", "protects": None},
            ),
        )

        with pytest.raises(ValueError, match="Compose file in the workspace"):
            save_compose_regression(result, workspace=tmp_path)

        assert not (tmp_path / "tests" / "ordeal-regressions.json").exists()

    def test_reproducible_compose_setup_failure_is_not_promoted(self, tmp_path: Path) -> None:
        failure = ComposeFailure("compose_command", "docker was not found", 0, "up")
        replay = ComposeReplayReport(3, 3, failure.signature)
        trace = ComposeTrace(
            seed=1,
            compose={
                "base_url": "http://service",
                "file": str(tmp_path / "compose.yaml"),
                "requests": [],
            },
            failure=failure,
            replay=replay,
        )
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=tmp_path / "trace.json",
            replay=replay,
            requests=0,
            faults=0,
            duration=0.1,
            evidence=compose_module.build_compose_finding_evidence(
                trace,
                replay=replay,
                coverage=compose_reliability_coverage(trace),
                protection={"status": "inconclusive", "protects": None},
            ),
        )

        assert save_compose_regression(result, workspace=tmp_path) is None
        assert not (tmp_path / "tests" / "ordeal-regressions.json").exists()


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
    def test_clean_compose_cli_persists_complete_run_evidence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text('[compose]\nbase_url = "http://localhost:8000"\n')
        trace = ComposeTrace(
            seed=42,
            compose={"base_url": "http://localhost:8000", "requests": []},
        )
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=tmp_path / ".ordeal" / "traces" / "clean.json",
            replay=None,
            requests=2,
            faults=1,
            duration=0.1,
            coverage={"summary": {"pass": 2, "not_exercised": 0, "fail": 0, "total": 2}},
            protection={
                "status": "protective_within_measured_scope",
                "mutation_score": "2/2 (100%)",
            },
        )
        monkeypatch.setattr(compose_module, "run_compose_exploration", lambda *a, **k: result)

        code = main(
            [
                "explore",
                "--runner",
                "compose",
                "-c",
                str(config_path),
                "--save-artifacts",
                "--json",
            ]
        )

        assert code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["schema"] == "ordeal.compose-run/v1"
        assert payload["status"] == "clean"
        assert payload["reliability_coverage"] == result.coverage
        assert payload["workload_protection"] == result.protection
        evidence_path = result.trace_path.with_suffix(".evidence.json")
        assert json.loads(evidence_path.read_text(encoding="utf-8")) == payload
        assert "Complete run evidence" in captured.err

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

    def test_explore_save_artifacts_promotes_replay_backed_compose_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        config_path = tmp_path / "ordeal.toml"
        config_path.write_text(
            '[compose]\nbase_url = "http://localhost:8000"\n',
            encoding="utf-8",
        )
        failure = ComposeFailure("unexpected_status", "boom", 0, "root")
        trace = ComposeTrace(
            seed=42,
            compose={
                "base_url": "http://localhost:8000",
                "file": str(tmp_path / "compose.yaml"),
                "requests": [],
                "replay_attempts": 3,
            },
            failure=failure,
        )
        report = ComposeReplayReport(3, 2, failure.signature)
        trace.replay = report
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=tmp_path / ".ordeal" / "traces" / "trace.json",
            replay=report,
            requests=1,
            faults=1,
            duration=0.1,
            evidence=compose_module.build_compose_finding_evidence(
                trace,
                replay=report,
                coverage=compose_reliability_coverage(trace),
                protection={"status": "inconclusive", "protects": None},
            ),
        )
        monkeypatch.setattr(compose_module, "run_compose_exploration", lambda *a, **k: result)

        code = main(["explore", "--runner", "compose", "-c", str(config_path), "--save-artifacts"])

        assert code == 1
        manifest_path = tmp_path / "tests" / "ordeal-regressions.json"
        assert manifest_path.exists()
        record = json.loads(manifest_path.read_text())["regressions"][0]
        assert (tmp_path / record["trace_file"]).exists()
        stderr = capsys.readouterr().err
        assert "Durable trace: tests/ordeal-compose-regressions/" in stderr
        assert "Regression manifest: tests/ordeal-regressions.json" in stderr
        assert f"Verify fix: uv run ordeal verify {record['finding_id']}" in stderr
        assert "CI guard: uv run ordeal verify --ci" in stderr

    def test_verify_ci_replays_bound_compose_manifest_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        failure = ComposeFailure("unexpected_status", "boom", 0, "root")
        replay = ComposeReplayReport(2, 1, failure.signature)
        trace = ComposeTrace(
            seed=1,
            compose={
                "base_url": "http://service",
                "file": str(tmp_path / "compose.yaml"),
                "requests": [],
                "replay_attempts": 2,
            },
            failure=failure,
            replay=replay,
        )
        result = ComposeExplorationResult(
            trace=trace,
            trace_path=tmp_path / ".ordeal" / "traces" / "trace.json",
            replay=replay,
            requests=1,
            faults=1,
            duration=0.1,
            evidence=compose_module.build_compose_finding_evidence(
                trace,
                replay=replay,
                coverage=compose_reliability_coverage(trace),
                protection={"status": "inconclusive", "protects": None},
            ),
        )
        artifacts = save_compose_regression(result, workspace=tmp_path)
        assert artifacts is not None

        def fake_replay(
            source: ComposeTrace,
            *,
            attempts: int | None = None,
        ) -> ComposeReplayReport:
            assert Path(str(source.compose["file"])).is_absolute()
            assert attempts == 2
            return ComposeReplayReport(
                2,
                0,
                source.failure_signature,
                observed_signatures=[None, None],
            )

        monkeypatch.setattr(compose_module, "replay_compose_trace", fake_replay)

        fixed_trace = ComposeTrace(
            seed=1,
            compose={
                **trace.compose,
                "file": str(tmp_path / "compose.yaml"),
                "trace_dir": str(tmp_path / ".ordeal" / "traces"),
            },
        )
        fixed_result = ComposeExplorationResult(
            trace=fixed_trace,
            trace_path=tmp_path / ".ordeal" / "traces" / "fixed.json",
            replay=None,
            requests=1,
            faults=1,
            duration=0.1,
            coverage={
                "dimensions": ["operation", "fault", "property"],
                "rows": [],
                "summary": {"pass": 1, "not_exercised": 0, "fail": 0, "total": 1},
            },
            protection={
                "status": "protective_within_measured_scope",
                "protects": True,
                "mutation_score": "2/2 (100%)",
            },
        )
        monkeypatch.setattr(
            compose_module,
            "run_compose_exploration",
            lambda *args, **kwargs: fixed_result,
        )

        finding_id = json.loads(artifacts.manifest_path.read_text())["regressions"][0][
            "finding_id"
        ]
        code = main(
            [
                "verify",
                finding_id,
                "--manifest",
                str(artifacts.manifest_path),
                "--allow-unsafe-artifacts",
            ]
        )
        assert code == 0
        capsys.readouterr()

        code = main(["verify", "--ci", "--manifest", str(artifacts.manifest_path)])

        assert code == 0
        output = capsys.readouterr().out
        assert "Compose clean replays 2/2" in output
        assert "verify --ci: 1 passed, 0 failed, 0 error(s)" in output

        def reproducing_replay(
            source: ComposeTrace,
            *,
            attempts: int | None = None,
        ) -> ComposeReplayReport:
            assert attempts == 2
            return ComposeReplayReport(
                2,
                2,
                source.failure_signature,
                observed_signatures=[source.failure_signature, source.failure_signature],
            )

        monkeypatch.setattr(compose_module, "replay_compose_trace", reproducing_replay)
        code = main(["verify", "--ci", "--manifest", str(artifacts.manifest_path)])

        assert code == 1
        error = capsys.readouterr().err
        assert "Compose regression failed" in error
        assert "clean replays 0/2" in error

        monkeypatch.setattr(compose_module, "replay_compose_trace", fake_replay)

        code = main(
            [
                "verify",
                finding_id,
                "--manifest",
                str(artifacts.manifest_path),
                "--allow-unsafe-artifacts",
            ]
        )

        assert code == 0
        assert f"verified: {finding_id} (Compose clean replays 2/2)" in capsys.readouterr().out
        persisted = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
        persisted_record = persisted["regressions"][0]
        control = persisted_record["evidence"]["post_fix_control"]
        assert control["status"] == "passed"
        assert control["exact_replay"]["clean"] == 2
        assert control["fixed_state"]["reliability_coverage"]["summary"]["fail"] == 0
        assert control["fixed_state_sha256"] == _sha256_json(control["fixed_state"])
        assert (
            control["fixed_state"]["workload_protection"]["status"]
            == "protective_within_measured_scope"
        )
        assert persisted_record["verification"]["fixed_state_status"] == "complete"

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
