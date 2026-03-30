"""Tests for ordeal.faults.network — HTTP and network fault injections."""

import pytest

from ordeal.faults.network import (
    HTTPFaultError,
    auth_failure,
    connection_reset,
    dns_failure,
    http_error,
    intermittent_http_error,
    partial_response,
    rate_limited,
)

# -- Helpers ----------------------------------------------------------------

_call_log: list[str] = []


def api_call(payload: str) -> str:
    _call_log.append(f"called({payload})")
    return f"response_for_{payload}"


def api_call_bytes(payload: str) -> bytes:
    return b"binary_response_data"


class _FakeResult:
    def __init__(self, text: str):
        self.output_text = text


def api_call_object(payload: str) -> _FakeResult:
    return _FakeResult("full response text here")


# -- Tests ------------------------------------------------------------------


class TestHTTPError:
    def setup_method(self):
        _call_log.clear()

    def test_raises_with_status_code(self):
        fault = http_error(f"{__name__}.api_call", status_code=500)
        fault.activate()
        with pytest.raises(HTTPFaultError) as exc_info:
            api_call("test")
        assert exc_info.value.status_code == 500
        assert exc_info.value.response.status_code == 500
        fault.deactivate()

    def test_custom_message(self):
        fault = http_error(f"{__name__}.api_call", 503, "Service Unavailable")
        fault.activate()
        with pytest.raises(HTTPFaultError, match="503"):
            api_call("test")
        fault.deactivate()

    def test_restores_original(self):
        fault = http_error(f"{__name__}.api_call", 500)
        fault.activate()
        fault.deactivate()
        assert api_call("test") == "response_for_test"

    def test_response_json(self):
        fault = http_error(f"{__name__}.api_call", 422, "Validation Error")
        fault.activate()
        try:
            api_call("test")
        except HTTPFaultError as e:
            j = e.response.json()
            assert j["status"] == 422
            assert j["error"] == "Validation Error"
        fault.deactivate()


class TestConnectionReset:
    def test_raises_connection_error(self):
        fault = connection_reset(f"{__name__}.api_call")
        fault.activate()
        with pytest.raises(ConnectionError, match="Connection reset"):
            api_call("test")
        fault.deactivate()

    def test_restores_original(self):
        fault = connection_reset(f"{__name__}.api_call")
        fault.activate()
        fault.deactivate()
        assert api_call("x") == "response_for_x"


class TestRateLimited:
    def test_raises_429(self):
        fault = rate_limited(f"{__name__}.api_call", retry_after=60.0)
        fault.activate()
        with pytest.raises(HTTPFaultError) as exc_info:
            api_call("test")
        assert exc_info.value.status_code == 429
        assert exc_info.value.response.headers["Retry-After"] == "60.0"
        fault.deactivate()


class TestAuthFailure:
    def test_raises_401(self):
        fault = auth_failure(f"{__name__}.api_call", 401)
        fault.activate()
        with pytest.raises(HTTPFaultError) as exc_info:
            api_call("test")
        assert exc_info.value.status_code == 401
        fault.deactivate()

    def test_raises_403(self):
        fault = auth_failure(f"{__name__}.api_call", 403)
        fault.activate()
        with pytest.raises(HTTPFaultError) as exc_info:
            api_call("test")
        assert exc_info.value.status_code == 403
        fault.deactivate()


class TestDNSFailure:
    def test_raises_os_error(self):
        fault = dns_failure(f"{__name__}.api_call")
        fault.activate()
        with pytest.raises(OSError, match="Name or service not known"):
            api_call("test")
        fault.deactivate()


class TestPartialResponse:
    def test_truncates_string(self):
        fault = partial_response(f"{__name__}.api_call", fraction=0.5)
        fault.activate()
        result = api_call("hello")
        # "response_for_hello" is 18 chars, half = 9
        assert len(result) == 9
        assert result == "response_"
        fault.deactivate()

    def test_truncates_bytes(self):
        fault = partial_response(f"{__name__}.api_call_bytes", fraction=0.5)
        fault.activate()
        result = api_call_bytes("x")
        assert len(result) == 10  # half of 20
        fault.deactivate()

    def test_truncates_object_output_text(self):
        fault = partial_response(f"{__name__}.api_call_object", fraction=0.5)
        fault.activate()
        result = api_call_object("x")
        original_len = len("full response text here")
        assert len(result.output_text) == original_len // 2
        fault.deactivate()


class TestIntermittentHTTPError:
    def setup_method(self):
        _call_log.clear()

    def test_fails_every_n(self):
        fault = intermittent_http_error(f"{__name__}.api_call", every_n=3, status_code=503)
        fault.activate()
        assert api_call("1") == "response_for_1"  # call 1: ok
        assert api_call("2") == "response_for_2"  # call 2: ok
        with pytest.raises(HTTPFaultError) as exc_info:
            api_call("3")  # call 3: fail
        assert exc_info.value.status_code == 503
        assert api_call("4") == "response_for_4"  # call 4: ok
        fault.deactivate()

    def test_reset_clears_counter(self):
        fault = intermittent_http_error(f"{__name__}.api_call", every_n=2)
        fault.activate()
        api_call("1")  # call 1
        fault.reset()
        fault.activate()
        assert api_call("1") == "response_for_1"  # call 1 again after reset
        fault.deactivate()
