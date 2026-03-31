"""Network and HTTP fault injections — 7 faults.

- http_error(target, status_code) — raise HTTP error (default 500)
- connection_reset(target) — simulate connection reset
- rate_limited(target, retry_after) — HTTP 429 Too Many Requests
- auth_failure(target, status_code) — HTTP 401/403
- dns_failure(target) — simulate DNS resolution failure
- partial_response(target, fraction) — truncate response content
- intermittent_http_error(target, every_n, status_code) — fail every Nth call

::

    from ordeal.faults.network import http_error, connection_reset, rate_limited
    faults = [http_error("myapp.client.post", status_code=503),
              connection_reset("myapp.client.post")]
"""

from __future__ import annotations

import functools
import threading
from typing import Any

from . import Fault, PatchFault

# ---------------------------------------------------------------------------
# Simulated HTTP error hierarchy
# ---------------------------------------------------------------------------


class HTTPFaultError(Exception):
    """Simulated HTTP error for fault injection.

    Carries ``status_code`` and a duck-typed ``response`` object so that
    downstream code using ``requests``, ``httpx``, or similar libraries
    can inspect the error in the usual way.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.response = _FakeResponse(status_code, message)
        super().__init__(f"HTTP {status_code}: {message}")


class _FakeResponse:
    """Minimal response object compatible with requests/httpx patterns."""

    __slots__ = ("status_code", "text", "headers", "content")

    def __init__(
        self,
        status_code: int,
        text: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.content = text.encode()

    def json(self) -> dict:
        return {"error": self.text, "status": self.status_code}


# ---------------------------------------------------------------------------
# Targeted faults (patch a specific function)
# ---------------------------------------------------------------------------


def http_error(
    target: str,
    status_code: int = 500,
    message: str = "Internal Server Error",
) -> PatchFault:
    """Make *target* raise an HTTP-like error with *status_code*.

    The raised ``HTTPFaultError`` carries a duck-typed ``response``
    attribute compatible with requests/httpx error handling patterns.
    """

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def failing(*args: Any, **kwargs: Any) -> Any:
            raise HTTPFaultError(status_code, message)

        return failing

    return PatchFault(target, wrapper, name=f"http_error({target}, {status_code})")


def connection_reset(target: str) -> PatchFault:
    """Simulate a connection reset / network failure on *target*."""

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def failing(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError(f"Connection reset by peer in {target}")

        return failing

    return PatchFault(target, wrapper, name=f"connection_reset({target})")


def rate_limited(
    target: str,
    retry_after: float = 30.0,
) -> PatchFault:
    """Simulate HTTP 429 Too Many Requests with Retry-After."""

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def failing(*args: Any, **kwargs: Any) -> Any:
            err = HTTPFaultError(429, "Too Many Requests")
            err.response.headers["Retry-After"] = str(retry_after)
            err.retry_after = retry_after  # type: ignore[attr-defined]
            raise err

        return failing

    return PatchFault(target, wrapper, name=f"rate_limited({target}, retry_after={retry_after}s)")


def auth_failure(
    target: str,
    status_code: int = 401,
) -> PatchFault:
    """Simulate authentication/authorization failure (HTTP 401 or 403)."""
    message = "Unauthorized" if status_code == 401 else "Forbidden"

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def failing(*args: Any, **kwargs: Any) -> Any:
            raise HTTPFaultError(status_code, message)

        return failing

    return PatchFault(target, wrapper, name=f"auth_failure({target}, {status_code})")


def dns_failure(target: str) -> PatchFault:
    """Simulate DNS resolution failure on *target*."""

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def failing(*args: Any, **kwargs: Any) -> Any:
            raise OSError(
                f"[Errno -2] Name or service not known: simulated DNS failure in {target}"
            )

        return failing

    return PatchFault(target, wrapper, name=f"dns_failure({target})")


def partial_response(target: str, fraction: float = 0.5) -> PatchFault:
    """Truncate *target*'s response to *fraction* of its content.

    For string/bytes results, slices directly.  For objects with an
    ``output_text`` or ``text`` attribute (common in HTTP response
    wrappers and provider results), truncates that attribute in-place.
    """

    def wrapper(original: Any) -> Any:
        @functools.wraps(original)
        def truncated(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if isinstance(result, str):
                return result[: max(0, int(len(result) * fraction))]
            if isinstance(result, bytes):
                return result[: max(0, int(len(result) * fraction))]
            # Duck-type: response objects with text or output_text
            for attr in ("output_text", "text", "content"):
                val = getattr(result, attr, None)
                if isinstance(val, str):
                    setattr(result, attr, val[: max(0, int(len(val) * fraction))])
                    return result
            return result

        return truncated

    return PatchFault(target, wrapper, name=f"partial_response({target}, {fraction})")


# ---------------------------------------------------------------------------
# Intermittent network fault (combines patterns)
# ---------------------------------------------------------------------------


class _IntermittentHTTPErrorFault(PatchFault):
    """Raises HTTP errors every *every_n* calls to *target*."""

    def __init__(
        self,
        target: str,
        every_n: int = 3,
        status_code: int = 503,
        message: str = "Service Unavailable",
    ) -> None:
        self._call_count = 0
        self._every_n = every_n
        self._status_code = status_code
        self._message = message
        self._counter_lock = threading.Lock()

        def wrapper(original: Any) -> Any:
            @functools.wraps(original)
            def maybe_failing(*args: Any, **kwargs: Any) -> Any:
                with self._counter_lock:
                    self._call_count += 1
                    count = self._call_count
                if count % self._every_n == 0:
                    raise HTTPFaultError(self._status_code, self._message)
                return original(*args, **kwargs)

            return maybe_failing

        super().__init__(
            target,
            wrapper,
            name=f"intermittent_http_error({target}, every {every_n}, {status_code})",
        )

    def reset(self) -> None:
        with self._counter_lock:
            self._call_count = 0
        super().reset()


def intermittent_http_error(
    target: str,
    every_n: int = 3,
    status_code: int = 503,
    message: str = "Service Unavailable",
) -> Fault:
    """Raise HTTP *status_code* every *every_n* calls to *target*.

    Calls between failures succeed normally.  Useful for testing retry
    logic and circuit breaker patterns.
    """
    return _IntermittentHTTPErrorFault(target, every_n, status_code, message)
