from __future__ import annotations
# ruff: noqa
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
