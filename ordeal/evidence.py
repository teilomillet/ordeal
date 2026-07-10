"""Executable, source-backed evidence records for benchmark cases."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_ORACLE_MARKER = "__ORDEAL_EVIDENCE_ORACLE__ "


def _stable_json(payload: Any) -> str:
    """Return canonical JSON used by declared backing-value hashes."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _sha256_bytes(payload: bytes) -> str:
    """Return one SHA-256 digest."""
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(payload: Any) -> str:
    """Return the SHA-256 digest of canonical JSON."""
    return _sha256_bytes(_stable_json(payload).encode("utf-8"))


@dataclass(frozen=True)
class EvidenceCheck:
    """One expected-versus-observed evidence check."""

    name: str
    passed: bool
    expected: Any
    actual: Any
    category: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly check payload."""
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "category": self.category,
        }


@dataclass(frozen=True)
class BugEvidenceVerification:
    """Observed verification result for one executable evidence record."""

    evidence_id: str
    record_path: str
    claim: str
    scope: str
    local_verified: bool
    sources_verified: bool
    verified: bool
    online_sources_required: bool
    checks: tuple[EvidenceCheck, ...]
    backing_values: dict[str, Any]
    limitations: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def summary(self) -> str:
        """Return a compact, claim-scoped verification summary."""
        status = "VERIFIED" if self.verified else "UNVERIFIED"
        passed = sum(check.passed for check in self.checks)
        lines = [
            f"Bug Evidence [{status}] {self.evidence_id}",
            f"  claim={self.claim}",
            f"  scope={self.scope}",
            (
                f"  checks={passed}/{len(self.checks)}, local={self.local_verified}, "
                f"sources={self.sources_verified}"
            ),
        ]
        lines.extend(f"  error: {error}" for error in self.errors)
        lines.extend(f"  limitation: {item}" for item in self.limitations)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly evidence result."""
        return {
            "schema": "ordeal.bug-evidence.verification/v1",
            "evidence_id": self.evidence_id,
            "record_path": self.record_path,
            "claim": self.claim,
            "scope": self.scope,
            "local_verified": self.local_verified,
            "sources_verified": self.sources_verified,
            "verified": self.verified,
            "online_sources_required": self.online_sources_required,
            "check_count": len(self.checks),
            "passed_check_count": sum(check.passed for check in self.checks),
            "checks": [check.to_dict() for check in self.checks],
            "backing_values": dict(self.backing_values),
            "limitations": list(self.limitations),
            "errors": list(self.errors),
            "summary": self.summary(),
        }

    def to_json(self) -> str:
        """Return stable JSON for CI evidence artifacts."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)


def _required_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a required TOML table."""
    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"Evidence record requires [{name}]")
    return value


def _required_tables(data: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Return a required TOML array of tables."""
    value = data.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, dict) for item in value)
    ):
        raise ValueError(f"Evidence record requires one or more [[{name}]] entries")
    return value


def _validate_sha256(value: Any, *, field_name: str) -> str:
    """Return a normalized SHA-256 value or raise."""
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{field_name} must be a 64-character SHA-256 digest")
    return digest


def _run_oracle(
    module: str,
    callable_name: str,
    kwargs: dict[str, Any],
    *,
    workspace: Path,
    python_executable: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one oracle observation in a fresh Python process."""
    script = textwrap.dedent(
        f"""
        import importlib
        import json
        from pathlib import Path

        module = importlib.import_module({module!r})
        module_path = Path(module.__file__ or "").resolve()
        workspace = Path({str(workspace)!r}).resolve()
        if not module_path.is_relative_to(workspace):
            payload = {{
                "outcome": "runner_error",
                "error": "module was not loaded from declared artifacts",
            }}
            print({_ORACLE_MARKER!r} + json.dumps(payload, sort_keys=True))
            raise SystemExit(0)
        target = getattr(module, {callable_name!r})
        kwargs = json.loads({_stable_json(kwargs)!r})
        try:
            result = target(**kwargs)
        except BaseException as exc:
            payload = {{
                "outcome": "exception",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            }}
        else:
            payload = {{"outcome": "return", "result": result}}
        print({_ORACLE_MARKER!r} + json.dumps(payload, sort_keys=True, default=str))
        """
    )
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = str(workspace)
    env["PYTHONPYCACHEPREFIX"] = str(workspace / ".pycache")
    try:
        proc = subprocess.run(
            [python_executable, "-c", script],
            cwd=str(workspace),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "runner_error", "timeout_seconds": timeout_seconds}
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_ORACLE_MARKER):
            return dict(json.loads(line[len(_ORACLE_MARKER) :]))
    return {
        "outcome": "runner_error",
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _expected_observation(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one declared oracle outcome."""
    outcome = str(raw.get("outcome", "")).strip()
    if outcome == "exception":
        return {
            "outcome": "exception",
            "exception_type": str(raw.get("exception_type", "")),
            "exception_message": str(raw.get("exception_message", "")),
        }
    if outcome == "return":
        return {
            "outcome": "return",
            "result": json.loads(str(raw.get("result_json", "null"))),
        }
    raise ValueError("Expected outcome must be 'exception' or 'return'")


def _python_runtime(python_executable: str, *, timeout_seconds: float) -> dict[str, Any]:
    """Inspect the exact interpreter used for replay."""
    script = textwrap.dedent(
        """
        import json
        import platform

        print(json.dumps({
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "version": platform.python_version(),
        }, sort_keys=True))
        """
    )
    try:
        proc = subprocess.run(
            [python_executable, "-c", script],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": str(exc)}
    if proc.returncode != 0:
        return {
            "status": "error",
            "exit_code": proc.returncode,
            "stderr": proc.stderr,
        }
    try:
        return dict(json.loads(proc.stdout.strip()))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"status": "error", "error": str(exc), "stdout": proc.stdout}


def verify_bug_evidence(
    path: str,
    *,
    online_sources: bool = False,
    python_executable: str | None = None,
) -> BugEvidenceVerification:
    """Verify one source-backed bug/fix evidence record and its executable oracle.

    ``online_sources=True`` fetches every pinned authoritative URL and is
    required when the record declares ``online_sources_required = true``.
    Each buggy and fixed replay runs in a fresh Python process.
    """
    declared_record_path = Path(path)
    record_path = declared_record_path.resolve()
    record_bytes = record_path.read_bytes()
    data = tomllib.loads(record_bytes.decode("utf-8"))
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError("Evidence record schema_version must be 1")
    online_required = data.get("online_sources_required", True)
    if not isinstance(online_required, bool):
        raise ValueError("online_sources_required must be a TOML boolean")

    evidence_id = str(data.get("evidence_id", "")).strip()
    claim = str(data.get("claim", "")).strip()
    scope = str(data.get("scope", "")).strip()
    if not evidence_id or not claim or not scope:
        raise ValueError("Evidence record requires evidence_id, claim, and scope")

    upstream = _required_table(data, "upstream")
    reproduction = _required_table(data, "reproduction")
    expected = _required_table(data, "expected")
    sources = _required_tables(data, "sources")
    artifacts = _required_tables(data, "artifacts")
    content_checks = _required_tables(data, "content_checks")
    checks: list[EvidenceCheck] = []
    errors: list[str] = []
    source_bytes: dict[str, bytes] = {}
    source_backing: dict[str, Any] = {}
    source_names: set[str] = set()

    for raw in sources:
        name = str(raw.get("name", "")).strip()
        url = str(raw.get("url", "")).strip()
        expected_sha = _validate_sha256(raw.get("sha256"), field_name=f"sources.{name}.sha256")
        expected_bytes = int(raw.get("bytes", -1))
        if not name or not url.startswith("https://") or expected_bytes < 0:
            raise ValueError(f"Invalid source record {name!r}")
        if name in source_names:
            raise ValueError(f"Duplicate source name {name!r}")
        source_names.add(name)
        actual: dict[str, Any] = {"status": "not_checked"}
        passed = False
        if online_sources:
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    payload = response.read()
                actual = {
                    "sha256": _sha256_bytes(payload),
                    "bytes": len(payload),
                    "url": url,
                }
                passed = actual["sha256"] == expected_sha and actual["bytes"] == expected_bytes
                source_bytes[name] = payload
            except Exception as exc:  # network boundary
                actual = {"status": "error", "error": str(exc), "url": url}
                errors.append(f"source {name!r} could not be verified: {exc}")
        checks.append(
            EvidenceCheck(
                name=f"source:{name}",
                passed=passed,
                expected={"sha256": expected_sha, "bytes": expected_bytes, "url": url},
                actual=actual,
                category="authoritative_source",
            )
        )
        source_backing[name] = actual

    for raw in content_checks:
        name = str(raw.get("name", "")).strip()
        source_name = str(raw.get("source", "")).strip()
        contains = raw.get("contains")
        absent = raw.get("absent")
        if not name or source_name not in {str(item.get("name", "")) for item in sources}:
            raise ValueError(f"Invalid content check {name!r}")
        if (contains is None) == (absent is None):
            raise ValueError(f"Content check {name!r} needs exactly one of contains or absent")
        text = source_bytes.get(source_name, b"").decode("utf-8", errors="replace")
        expected_text = str(contains if contains is not None else absent)
        passed = bool(source_name in source_bytes) and (
            expected_text in text if contains is not None else expected_text not in text
        )
        checks.append(
            EvidenceCheck(
                name=f"content:{name}",
                passed=passed,
                expected={
                    "source": source_name,
                    "contains" if contains is not None else "absent": expected_text,
                },
                actual={"source_available": source_name in source_bytes, "matched": passed},
                category="authoritative_content",
            )
        )

    artifact_backing: dict[str, Any] = {}
    artifact_payloads: dict[str, tuple[Path, bytes]] = {}
    artifact_names: set[str] = set()
    for raw in artifacts:
        name = str(raw.get("name", "")).strip()
        relative_path = str(raw.get("path", "")).strip()
        expected_sha = _validate_sha256(
            raw.get("sha256"),
            field_name=f"artifacts.{name}.sha256",
        )
        expected_bytes = int(raw.get("bytes", -1))
        if not name or not relative_path or expected_bytes < 0:
            raise ValueError(f"Invalid artifact record {name!r}")
        if name in artifact_names:
            raise ValueError(f"Duplicate artifact name {name!r}")
        artifact_names.add(name)
        artifact_path = (record_path.parent / relative_path).resolve()
        try:
            payload = artifact_path.read_bytes()
            artifact_payloads[name] = (artifact_path, payload)
            actual = {
                "path": relative_path,
                "sha256": _sha256_bytes(payload),
                "bytes": len(payload),
            }
        except OSError as exc:
            actual = {"path": relative_path, "error": str(exc)}
            errors.append(f"artifact {name!r} could not be read: {exc}")
        passed = actual.get("sha256") == expected_sha and actual.get("bytes") == expected_bytes
        checks.append(
            EvidenceCheck(
                name=f"artifact:{name}",
                passed=passed,
                expected={"sha256": expected_sha, "bytes": expected_bytes},
                actual=actual,
                category="local_artifact",
            )
        )
        artifact_backing[name] = actual

    kwargs = json.loads(str(reproduction.get("kwargs_json", "{}")))
    if not isinstance(kwargs, dict):
        raise ValueError("reproduction.kwargs_json must encode a JSON object")
    expected_kwargs_sha = _validate_sha256(
        reproduction.get("kwargs_sha256"),
        field_name="reproduction.kwargs_sha256",
    )
    actual_kwargs_sha = _sha256_json(kwargs)
    checks.append(
        EvidenceCheck(
            name="oracle:kwargs_sha256",
            passed=actual_kwargs_sha == expected_kwargs_sha,
            expected=expected_kwargs_sha,
            actual=actual_kwargs_sha,
            category="executable_oracle",
        )
    )

    workspace = (record_path.parent / str(reproduction.get("workspace", ""))).resolve()
    callable_name = str(reproduction.get("callable", "")).strip()
    modules = {
        "buggy": str(reproduction.get("buggy_module", "")).strip(),
        "fixed": str(reproduction.get("fixed_module", "")).strip(),
    }
    repetitions = int(reproduction.get("repetitions", 0))
    timeout_seconds = float(reproduction.get("timeout_seconds", 10.0))
    if (
        not workspace.is_dir()
        or not callable_name
        or not all(modules.values())
        or repetitions < 1
        or timeout_seconds <= 0
    ):
        raise ValueError(
            "Invalid [reproduction] workspace, modules, callable, repetitions, or timeout"
        )
    isolated_artifacts: dict[str, Path] = {}
    for name, (artifact_path, _) in artifact_payloads.items():
        try:
            isolated_artifacts[name] = artifact_path.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"Artifact {name!r} must be inside reproduction.workspace") from exc

    oracle_backing: dict[str, Any] = {
        "kwargs": kwargs,
        "kwargs_sha256": actual_kwargs_sha,
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
    }
    executable = python_executable or sys.executable
    requires_python = str(reproduction.get("requires_python", "")).strip()
    if not requires_python:
        raise ValueError("reproduction.requires_python is required")
    try:
        python_specifier = SpecifierSet(requires_python)
    except InvalidSpecifier as exc:
        raise ValueError("reproduction.requires_python is invalid") from exc
    runtime = _python_runtime(executable, timeout_seconds=timeout_seconds)
    actual_python = str(runtime.get("version", ""))
    try:
        runtime_matches = bool(actual_python) and Version(actual_python) in python_specifier
    except InvalidVersion:
        runtime_matches = False
    checks.append(
        EvidenceCheck(
            name="oracle:python_requirement",
            passed=runtime_matches,
            expected=requires_python,
            actual=runtime,
            category="executable_oracle",
        )
    )
    oracle_backing["runtime"] = runtime
    oracle_backing["requires_python"] = requires_python
    with tempfile.TemporaryDirectory(prefix="ordeal-evidence-") as temp_dir:
        isolated_workspace = Path(temp_dir)
        for name, relative_path in isolated_artifacts.items():
            destination = isolated_workspace / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(artifact_payloads[name][1])
        for revision, module in modules.items():
            raw_expected = expected.get(revision)
            if not isinstance(raw_expected, dict):
                raise ValueError(f"Evidence record requires [expected.{revision}]")
            expected_observation = _expected_observation(raw_expected)
            observations: list[dict[str, Any]] = []
            for index in range(1, repetitions + 1):
                observation = _run_oracle(
                    module,
                    callable_name,
                    kwargs,
                    workspace=isolated_workspace,
                    python_executable=executable,
                    timeout_seconds=timeout_seconds,
                )
                observations.append(observation)
                checks.append(
                    EvidenceCheck(
                        name=f"oracle:{revision}:{index}",
                        passed=observation == expected_observation,
                        expected=expected_observation,
                        actual=observation,
                        category="executable_oracle",
                    )
                )
            oracle_backing[revision] = {
                "module": module,
                "callable": callable_name,
                "expected": expected_observation,
                "observations": observations,
                "matching_replays": sum(item == expected_observation for item in observations),
            }
    oracle_backing["workspace_isolation"] = "declared_artifacts_only"

    source_checks = [check for check in checks if check.category.startswith("authoritative_")]
    local_checks = [check for check in checks if not check.category.startswith("authoritative_")]
    sources_verified = bool(source_checks) and all(check.passed for check in source_checks)
    local_verified = bool(local_checks) and all(check.passed for check in local_checks)
    verified = local_verified and (sources_verified or not online_required)
    return BugEvidenceVerification(
        evidence_id=evidence_id,
        record_path=str(declared_record_path),
        claim=claim,
        scope=scope,
        local_verified=local_verified,
        sources_verified=sources_verified,
        verified=verified,
        online_sources_required=online_required,
        checks=tuple(checks),
        backing_values={
            "record_sha256": _sha256_bytes(record_bytes),
            "upstream": dict(upstream),
            "sources": source_backing,
            "artifacts": artifact_backing,
            "oracle": oracle_backing,
        },
        limitations=tuple(str(item) for item in data.get("limitations", [])),
        errors=tuple(errors),
    )
