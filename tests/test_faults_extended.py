"""Tests for fault internals — io, numerical, timing, concurrency, base Fault."""

from __future__ import annotations

import copy
import errno
import math
import pickle
import threading

import pytest

from ordeal.faults import LambdaFault, PatchFault, _resolve_target

# ============================================================================
# Module-level helpers for PatchFault targets
# ============================================================================


def returns_float() -> float:
    return 42.0


def returns_str() -> str:
    return "hello world"


def returns_bytes() -> bytes:
    return b"binary data here!!"


def returns_list() -> list:
    return [1, 2, 3, 4, 5, 6, 7, 8]


def returns_int() -> int:
    return 100


def _returns_tuple():
    return (1, 2, 3, 4, 5, 6)


# ============================================================================
# _resolve_target
# ============================================================================


class TestResolveTarget:
    def test_resolves_module_function(self):
        parent, attr = _resolve_target("json.loads")
        import json

        assert parent is json
        assert attr == "loads"

    def test_resolves_nested(self):
        parent, attr = _resolve_target("os.path.join")
        import os.path

        assert parent is os.path
        assert attr == "join"

    def test_no_dot_raises(self):
        with pytest.raises(ValueError, match="dotted path"):
            _resolve_target("nodot")

    def test_unresolvable_raises(self):
        with pytest.raises((ImportError, AttributeError)):
            _resolve_target("nonexistent.module.func")


# ============================================================================
# Fault.__deepcopy__
# ============================================================================


class TestFaultDeepCopy:
    def test_deepcopy_creates_new_lock(self):
        f = LambdaFault("test", lambda: None, lambda: None)
        f2 = copy.deepcopy(f)
        assert f._state_lock is not f2._state_lock
        assert f2.name == "test"

    def test_deepcopy_preserves_state(self):
        f = LambdaFault("test", lambda: None, lambda: None)
        f.activate()
        f2 = copy.deepcopy(f)
        assert f2.active == f.active
        f.deactivate()

    def test_deepcopy_independent(self):
        f = LambdaFault("orig", lambda: None, lambda: None)
        f2 = copy.deepcopy(f)
        f.activate()
        assert not f2.active
        f.deactivate()


# ============================================================================
# Fault.__getstate__ / __setstate__ (pickle)
# ============================================================================


def _pickle_on():
    pass


def _pickle_off():
    pass


class TestFaultPickle:
    def test_pickle_round_trip(self):
        f = LambdaFault("pickled", _pickle_on, _pickle_off)
        data = pickle.dumps(f)
        f2 = pickle.loads(data)
        assert f2.name == "pickled"
        assert not f2.active
        assert hasattr(f2, "_state_lock")

    def test_pickle_preserves_active_state(self):
        f = LambdaFault("act", _pickle_on, _pickle_off)
        f._active = True
        data = pickle.dumps(f)
        f2 = pickle.loads(data)
        assert f2._active is True
        assert isinstance(f2._state_lock, type(threading.Lock()))


# ============================================================================
# PatchFault reset
# ============================================================================


class TestPatchFaultReset:
    def test_reset_allows_re_resolve(self):
        fault = PatchFault(
            f"{__name__}.returns_int",
            lambda orig: lambda *a, **k: -1,
        )
        fault.activate()
        assert returns_int() == -1
        fault.reset()
        assert returns_int() == 100
        fault.activate()
        assert returns_int() == -1
        fault.deactivate()


# ============================================================================
# I/O faults — disk_full, permission_denied, corrupt_output, truncate
# ============================================================================


class TestDiskFull:
    def test_write_raises_enospc(self, tmp_path):
        from ordeal.faults.io import disk_full

        fault = disk_full()
        fault.activate()
        try:
            with pytest.raises(OSError) as exc_info:
                open(tmp_path / "test.txt", "w")
            assert exc_info.value.errno == errno.ENOSPC
        finally:
            fault.deactivate()

    def test_read_still_works(self, tmp_path):
        from ordeal.faults.io import disk_full

        p = tmp_path / "existing.txt"
        p.write_text("hello")
        fault = disk_full()
        fault.activate()
        try:
            assert p.read_text() == "hello"
        finally:
            fault.deactivate()

    def test_restores_builtins(self):
        import builtins

        from ordeal.faults.io import disk_full

        original_open = builtins.open
        fault = disk_full()
        fault.activate()
        assert builtins.open is not original_open
        fault.deactivate()
        assert builtins.open is original_open

    def test_os_write_raises(self):
        import os
        import tempfile

        from ordeal.faults.io import disk_full

        fault = disk_full()
        fault.activate()
        try:
            fd = os.open(
                os.path.join(tempfile.gettempdir(), "ordeal_test_diskfull"),
                os.O_WRONLY | os.O_CREAT,
            )
            try:
                with pytest.raises(OSError) as exc_info:
                    os.write(fd, b"data")
                assert exc_info.value.errno == errno.ENOSPC
            finally:
                os.close(fd)
        finally:
            fault.deactivate()


class TestPermissionDenied:
    def test_write_raises_permission_error(self, tmp_path):
        from ordeal.faults.io import permission_denied

        fault = permission_denied()
        fault.activate()
        try:
            with pytest.raises(PermissionError) as exc_info:
                open(tmp_path / "test.txt", "w")
            assert exc_info.value.errno == errno.EACCES
        finally:
            fault.deactivate()

    def test_read_still_works(self, tmp_path):
        from ordeal.faults.io import permission_denied

        p = tmp_path / "existing.txt"
        p.write_text("data")
        fault = permission_denied()
        fault.activate()
        try:
            assert p.read_text() == "data"
        finally:
            fault.deactivate()

    def test_restores_builtins(self):
        import builtins

        from ordeal.faults.io import permission_denied

        original_open = builtins.open
        fault = permission_denied()
        fault.activate()
        fault.deactivate()
        assert builtins.open is original_open

    def test_append_also_denied(self, tmp_path):
        from ordeal.faults.io import permission_denied

        fault = permission_denied()
        fault.activate()
        try:
            with pytest.raises(PermissionError):
                open(tmp_path / "test.txt", "a")
        finally:
            fault.deactivate()


class TestCorruptOutput:
    def test_corrupts_bytes(self):
        from ordeal.faults.io import corrupt_output

        fault = corrupt_output(f"{__name__}.returns_bytes")
        fault.activate()
        result = returns_bytes()
        assert isinstance(result, bytes)
        assert len(result) == len(b"binary data here!!")
        assert result != b"binary data here!!"
        fault.deactivate()

    def test_corrupts_string(self):
        from ordeal.faults.io import corrupt_output

        fault = corrupt_output(f"{__name__}.returns_str")
        fault.activate()
        result = returns_str()
        assert isinstance(result, str)
        assert len(result) == len("hello world")
        fault.deactivate()

    def test_non_bytes_passthrough(self):
        from ordeal.faults.io import corrupt_output

        fault = corrupt_output(f"{__name__}.returns_int")
        fault.activate()
        assert returns_int() == 100
        fault.deactivate()


class TestTruncateOutput:
    def test_truncates_list(self):
        from ordeal.faults.io import truncate_output

        fault = truncate_output(f"{__name__}.returns_list", fraction=0.5)
        fault.activate()
        result = returns_list()
        assert result == [1, 2, 3, 4]
        assert isinstance(result, list)
        fault.deactivate()

    def test_truncates_tuple(self):
        import tests.test_faults_extended as mod
        from ordeal.faults.io import truncate_output

        fault = truncate_output("tests.test_faults_extended._returns_tuple", fraction=0.5)
        fault.activate()
        result = mod._returns_tuple()
        assert result == (1, 2, 3)
        assert isinstance(result, tuple)
        fault.deactivate()

    def test_non_sequence_passthrough(self):
        from ordeal.faults.io import truncate_output

        fault = truncate_output(f"{__name__}.returns_int", fraction=0.5)
        fault.activate()
        assert returns_int() == 100
        fault.deactivate()


# ============================================================================
# Numerical faults
# ============================================================================


class TestCorruptNumeric:
    def test_corrupts_int(self):
        from ordeal.faults.numerical import _corrupt_numeric

        assert math.isnan(_corrupt_numeric(42, float("nan")))

    def test_corrupts_float(self):
        from ordeal.faults.numerical import _corrupt_numeric

        assert _corrupt_numeric(3.14, float("inf")) == float("inf")

    def test_corrupts_list(self):
        from ordeal.faults.numerical import _corrupt_numeric

        result = _corrupt_numeric([1, 2, "text", 3], float("nan"))
        assert math.isnan(result[0])
        assert result[2] == "text"

    def test_corrupts_dict(self):
        from ordeal.faults.numerical import _corrupt_numeric

        result = _corrupt_numeric({"a": 1.0, "b": "text"}, float("inf"))
        assert result["a"] == float("inf")
        assert result["b"] == "text"

    def test_non_numeric_passthrough(self):
        from ordeal.faults.numerical import _corrupt_numeric

        assert _corrupt_numeric("hello", float("nan")) == "hello"


class TestWrongShape:
    def test_returns_nested_list_without_numpy(self):
        from ordeal.faults.numerical import wrong_shape

        fault = wrong_shape(f"{__name__}.returns_float", expected=(1, 512), actual=(2, 3))
        fault.activate()
        result = returns_float()
        assert len(result) == 2
        assert len(result[0]) == 3
        fault.deactivate()

    def test_name_shows_shapes(self):
        from ordeal.faults.numerical import wrong_shape

        fault = wrong_shape("mod.fn", expected=(1,), actual=(2,))
        assert "(1,)" in fault.name and "(2,)" in fault.name


class TestCorruptedFloats:
    def test_nan_when_active(self):
        from ordeal.faults.numerical import corrupted_floats

        fault = corrupted_floats("nan")
        assert fault.value() == 0.0
        fault.activate()
        assert math.isnan(fault.value())
        fault.deactivate()

    def test_inf(self):
        from ordeal.faults.numerical import corrupted_floats

        fault = corrupted_floats("inf")
        fault.activate()
        assert fault.value() == float("inf")
        fault.deactivate()

    def test_neg_inf(self):
        from ordeal.faults.numerical import corrupted_floats

        fault = corrupted_floats("-inf")
        fault.activate()
        assert fault.value() == float("-inf")
        fault.deactivate()

    def test_max(self):
        from ordeal.faults.numerical import corrupted_floats

        fault = corrupted_floats("max")
        fault.activate()
        assert fault.value() == 1.7976931348623157e308
        fault.deactivate()

    def test_min(self):
        from ordeal.faults.numerical import corrupted_floats

        fault = corrupted_floats("min")
        fault.activate()
        assert fault.value() == 5e-324
        fault.deactivate()

    def test_unknown_defaults_nan(self):
        from ordeal.faults.numerical import corrupted_floats

        fault = corrupted_floats("unknown")
        fault.activate()
        assert math.isnan(fault.value())
        fault.deactivate()

    def test_inactive_returns_zero(self):
        from ordeal.faults.numerical import corrupted_floats

        assert corrupted_floats("nan").value() == 0.0


# ============================================================================
# Timing faults — slow, jitter
# ============================================================================


class TestSlowFault:
    def test_simulate_mode_no_sleep(self):
        import time

        from ordeal.faults.timing import slow

        fault = slow(f"{__name__}.returns_int", delay=10.0, mode="simulate")
        fault.activate()
        start = time.monotonic()
        assert returns_int() == 100
        assert time.monotonic() - start < 1.0
        fault.deactivate()

    def test_real_mode_sleeps(self):
        import time

        from ordeal.faults.timing import slow

        fault = slow(f"{__name__}.returns_int", delay=0.05, mode="real")
        fault.activate()
        start = time.monotonic()
        returns_int()
        assert time.monotonic() - start >= 0.03
        fault.deactivate()

    def test_invalid_mode_raises(self):
        from ordeal.faults.timing import slow

        with pytest.raises(ValueError, match="mode"):
            slow("mod.fn", mode="invalid")


class TestJitterFault:
    def test_adds_jitter_to_numeric(self):
        from ordeal.faults.timing import jitter

        fault = jitter(f"{__name__}.returns_float", magnitude=0.1)
        fault.activate()
        r1 = returns_float()
        r2 = returns_float()
        assert r1 != 42.0 or r2 != 42.0
        fault.deactivate()

    def test_non_numeric_passthrough(self):
        from ordeal.faults.timing import jitter

        fault = jitter(f"{__name__}.returns_str", magnitude=0.1)
        fault.activate()
        assert returns_str() == "hello world"
        fault.deactivate()

    def test_reset_clears_counter(self):
        from ordeal.faults.timing import jitter

        fault = jitter(f"{__name__}.returns_float", magnitude=1.0)
        fault.activate()
        returns_float()
        returns_float()
        fault.reset()
        fault.activate()
        r = returns_float()
        assert r != 42.0
        fault.deactivate()
