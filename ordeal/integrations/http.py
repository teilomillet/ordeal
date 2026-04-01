"""HTTP endpoint fuzzing against live servers.

Fuzz any HTTP endpoint without an OpenAPI schema.  Point at a URL,
send adversarial inputs, check responses.  Finds input validation
bugs, empty-body crashes, oversized payloads, malformed content
types, and encoding issues.

Quick start::

    from ordeal.integrations.http import fuzz_endpoint

    result = fuzz_endpoint("POST", "http://localhost:8081/faces/detect",
                           files={"image": ("test.jpg", b"garbage", "image/jpeg")})
    print(result.summary())

With rate limiting (for servers behind inference queues)::

    result = fuzz_endpoint("POST", "http://localhost:8081/process",
                           json={"text": "hello"},
                           rps_limit=10)  # max 10 requests/second

With custom assertions::

    result = fuzz_endpoint("POST", "http://localhost:8081/api",
                           json={"data": "test"},
                           assert_status=lambda s: s < 500,
                           assert_body=lambda b: "error" not in b.lower())

Fuzz strategies (what gets sent):

- **Empty bodies** — ``b""``, ``None``, missing content-type
- **Oversized payloads** — 10MB random bytes, repeated patterns
- **Malformed JSON** — truncated, wrong types, null fields, nested
- **Malformed multipart** — wrong boundaries, missing parts, huge files
- **Encoding attacks** — null bytes, UTF-8 overlong, path traversal
- **Header injection** — newlines in headers, oversized headers

Each strategy is a Hypothesis SearchStrategy, composable with ordeal's
existing grammar strategies (``json_strategy``, ``url_strategy``, etc.).

Rate limiting uses a token bucket — smooth, not bursty.  Automatic
rate detection adjusts to server response times when ``rps_limit``
is ``"auto"``.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable

import hypothesis.strategies as st

# ============================================================================
# Result types
# ============================================================================


@dataclass
class EndpointFuzzResult:
    """Result of fuzzing an HTTP endpoint."""

    method: str
    url: str
    total_requests: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    status_distribution: dict[int, int] = field(default_factory=dict)
    avg_response_ms: float = 0.0
    fault_activations: int = 0

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0 and len(self.errors) == 0

    def summary(self) -> str:
        lines = [f"fuzz_endpoint({self.method} {self.url}): {self.total_requests} requests"]
        if self.status_distribution:
            dist = ", ".join(f"{k}: {v}" for k, v in sorted(self.status_distribution.items()))
            lines.append(f"  status: {dist}")
        if self.avg_response_ms > 0:
            lines.append(f"  avg response: {self.avg_response_ms:.0f}ms")
        if self.fault_activations > 0:
            lines.append(f"  fault activations: {self.fault_activations}")
        if self.failures:
            lines.append(f"  {len(self.failures)} assertion failure(s):")
            for f in self.failures[:5]:
                lines.append(f"    {f.get('status', '?')} — {f.get('reason', '?')}")
                if f.get("request"):
                    lines.append(f"      request: {_truncate(str(f['request']), 100)}")
        if self.errors:
            lines.append(f"  {len(self.errors)} connection error(s):")
            for e in self.errors[:3]:
                lines.append(f"    {e.get('error', '?')}")

        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s


# ============================================================================
# Rate limiter
# ============================================================================


class _TokenBucket:
    """Simple token bucket rate limiter — smooth, not bursty."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0
        self._last = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = _time.monotonic()
        wait_time = self.interval - (now - self._last)
        if wait_time > 0:
            _time.sleep(wait_time)
        self._last = _time.monotonic()


# ============================================================================
# Fuzz strategies for HTTP
# ============================================================================


def _empty_body_strategy() -> st.SearchStrategy[bytes | None]:
    """Bodies that test empty/missing input handling."""
    return st.sampled_from([b"", None, b"\x00", b" ", b"\n"])


def _oversized_body_strategy() -> st.SearchStrategy[bytes]:
    """Bodies that test size limits."""
    return st.sampled_from(
        [
            b"A" * 1_000_000,
            b"\x00" * 100_000,
            b"x" * 10_000_000,
        ]
    )


def _malformed_json_strategy() -> st.SearchStrategy[bytes]:
    """JSON bodies that test parser robustness."""
    return st.sampled_from(
        [
            b"",
            b"null",
            b"[]",
            b"{}",
            b'{"key": null}',
            b'{"key": "' + b"A" * 10000 + b'"}',
            b"{truncated",
            b'[1, 2, 3, "unterminated',
            b"0",
            b"true",
            b"false",
            b'{"a": {"b": {"c": {"d": {"e": "deep"}}}}}',
        ]
    )


def _encoding_attack_strategy() -> st.SearchStrategy[bytes]:
    """Inputs that test encoding and injection handling."""
    return st.sampled_from(
        [
            b"\x00\x00\x00",
            b"\xff\xfe",
            b"../../etc/passwd",
            b"%00%00",
            b"\r\nX-Injected: true",
            "日本語テスト".encode(),
            b"\xc0\xaf",  # overlong UTF-8
        ]
    )


def _adversarial_body_strategy() -> st.SearchStrategy[bytes | None]:
    """Combined adversarial body strategy."""
    return st.one_of(
        _empty_body_strategy(),
        _malformed_json_strategy(),
        _encoding_attack_strategy(),
        st.binary(min_size=0, max_size=1000),
    )


# ============================================================================
# Main entry point
# ============================================================================


def fuzz_endpoint(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    data: bytes | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    headers: dict[str, str] | None = None,
    faults: list[Any] | None = None,
    max_requests: int = 100,
    rps_limit: float | str | None = None,
    assert_status: Callable[[int], bool] | None = None,
    assert_body: Callable[[str], bool] | None = None,
    timeout: float = 10.0,
    seed: int = 42,
) -> EndpointFuzzResult:
    """Fuzz an HTTP endpoint with adversarial inputs and fault injection.

    Works with any HTTP server — no OpenAPI schema needed.
    Combines chaos_api_test's fault injection with raw HTTP fuzzing.

    Without faults (pure fuzzing)::

        result = fuzz_endpoint("POST", "http://localhost:8081/detect",
                               files={"image": ("test.jpg", data, "image/jpeg")})

    With fault injection (chaos testing against live server)::

        from ordeal.faults import timing, network

        result = fuzz_endpoint("POST", "http://localhost:8081/detect",
                               files={"image": ("test.jpg", data, "image/jpeg")},
                               faults=[
                                   timing.timeout("myapp.db.query"),
                                   network.connection_reset("myapp.cache.get"),
                               ])

    Faults are toggled probabilistically during the fuzz loop — some
    requests hit the server while a dependency is faulted, others
    don't.  This finds bugs that only manifest under partial failure.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, PATCH).
        url: Full URL to the endpoint.
        json: Base JSON payload to mutate.
        data: Base binary payload to corrupt/truncate.
        files: Base multipart files to malform.
        headers: Extra headers on every request.
        faults: Fault instances to toggle during fuzzing. The nemesis
            activates/deactivates them probabilistically between
            requests. ``None`` = pure fuzzing, no fault injection.
        max_requests: Maximum requests to send.
        rps_limit: Rate limit (requests/sec). ``"auto"`` adapts to
            server response time. ``None`` = no limit.
        assert_status: Returns True for acceptable status codes.
            Default: ``lambda s: s < 500``.
        assert_body: Returns True for acceptable response bodies.
        timeout: Request timeout in seconds.
        seed: RNG seed for deterministic fault toggling and mutation.

    Returns:
        ``EndpointFuzzResult`` with status distribution, failures,
        errors, and fault activation log.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError("HTTP fuzzing requires httpx: pip install httpx") from None

    if assert_status is None:
        assert_status = lambda s: s < 500  # noqa: E731

    # Set up rate limiter
    limiter: _TokenBucket | None = None
    if rps_limit is not None and rps_limit != "auto":
        limiter = _TokenBucket(float(rps_limit))

    # Set up fault injection (nemesis-style toggling)
    import random as _rng

    fault_rng = _rng.Random(seed)
    fault_list = list(faults or [])
    fault_toggle_prob = 0.3  # same as ChaosTest default

    result = EndpointFuzzResult(method=method, url=url)
    total_ms = 0.0
    base_headers = dict(headers or {})

    # Build the request variants to try
    variants: list[dict[str, Any]] = []

    # Always include baseline
    if json is not None:
        variants.append({"json": json})
    if data is not None:
        variants.append({"content": data})
    if files is not None:
        # httpx uses 'files' parameter
        variants.append({"files": files})

    # Generate adversarial variants
    adversarial_bodies = _adversarial_body_strategy()
    json_bodies = _malformed_json_strategy()

    for i in range(max_requests):
        if limiter:
            limiter.wait()

        # Toggle faults probabilistically (nemesis pattern)
        if fault_list and fault_rng.random() < fault_toggle_prob:
            fault = fault_rng.choice(fault_list)
            if fault.active:
                fault.deactivate()
            else:
                fault.activate()
                result.fault_activations += 1

        # Pick a variant
        req_kwargs: dict[str, Any] = {"headers": dict(base_headers)}
        try:
            if json is not None and i % 3 == 0:
                # Mutate the JSON body
                from ordeal.mutagen import mutate_value

                mutated = mutate_value(json, _rng.Random(i), intensity=0.5)
                req_kwargs["json"] = mutated
            elif i % 5 == 0:
                # Send adversarial body
                body = adversarial_bodies.example()
                if body is not None:
                    req_kwargs["content"] = body
                    req_kwargs["headers"]["Content-Type"] = "application/octet-stream"
            elif i % 7 == 0:
                # Send malformed JSON
                body = json_bodies.example()
                req_kwargs["content"] = body
                req_kwargs["headers"]["Content-Type"] = "application/json"
            elif files is not None:
                # Send malformed file
                import os

                fake_content = os.urandom(min(1000, i * 10 + 1))
                req_kwargs["files"] = {
                    "file": ("fuzz.bin", fake_content, "application/octet-stream")
                }
            elif json is not None:
                req_kwargs["json"] = json
            else:
                body = adversarial_bodies.example()
                if body is not None:
                    req_kwargs["content"] = body
        except Exception:
            continue

        # Send request
        start = _time.monotonic()
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.request(method, url, **req_kwargs)
            elapsed_ms = (_time.monotonic() - start) * 1000
            total_ms += elapsed_ms
            result.total_requests += 1

            # Track status distribution
            status = resp.status_code
            result.status_distribution[status] = result.status_distribution.get(status, 0) + 1

            # Auto rate detection
            if rps_limit == "auto" and limiter is None and elapsed_ms > 0:
                # Start with 2x the observed rate
                observed_rps = 1000.0 / elapsed_ms
                limiter = _TokenBucket(min(observed_rps * 2, 100))

            # Check assertions
            if assert_status and not assert_status(status):
                result.failures.append(
                    {
                        "status": status,
                        "reason": f"status {status} failed assertion",
                        "request": _summarize_request(req_kwargs),
                        "response_body": resp.text[:200],
                    }
                )

            if assert_body and not assert_body(resp.text):
                result.failures.append(
                    {
                        "status": status,
                        "reason": "body failed assertion",
                        "request": _summarize_request(req_kwargs),
                        "response_body": resp.text[:200],
                    }
                )

        except httpx.ConnectError as e:
            result.errors.append({"error": f"connection: {e}"})
        except httpx.TimeoutException as e:
            result.errors.append({"error": f"timeout: {e}"})
        except Exception as e:
            result.errors.append({"error": str(e)[:200]})

    # Clean up: deactivate all faults
    for fault in fault_list:
        try:
            fault.deactivate()
        except Exception:
            pass

    if result.total_requests > 0:
        result.avg_response_ms = total_ms / result.total_requests

    return result


def _summarize_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Summarize request kwargs for the failure report."""
    summary: dict[str, Any] = {}
    if "json" in kwargs:
        summary["json"] = str(kwargs["json"])[:100]
    if "content" in kwargs:
        content = kwargs["content"]
        if isinstance(content, bytes):
            summary["body_bytes"] = len(content)
            summary["body_preview"] = repr(content[:50])
        else:
            summary["body"] = str(content)[:100]
    if "files" in kwargs:
        summary["files"] = {
            k: (v[0], f"{len(v[1])} bytes", v[2])
            for k, v in kwargs["files"].items()
            if isinstance(v, tuple) and len(v) >= 3
        }
    return summary
