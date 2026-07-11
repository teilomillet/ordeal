"""Regression tests for bytecode-aware facade part loading."""

from __future__ import annotations

import os
import pickle
import py_compile
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import cache_from_source
from pathlib import Path
from types import ModuleType

from ordeal._facade_loader import load_parts


def _cache_flags(source: Path) -> int:
    cache = Path(cache_from_source(str(source))).read_bytes()
    return int.from_bytes(cache[4:8], "little")


def test_load_parts_reuses_bytecode_and_shared_facade_namespace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "first.py"
    first.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "VALUE = 4\n"
        "@dataclass\n"
        "class Payload:\n"
        "    value: int\n"
        "def multiply(value: int) -> int:\n"
        "    return VALUE * value\n",
        encoding="utf-8",
    )
    second = tmp_path / "second.py"
    second.write_text(
        "from __future__ import annotations\ndef result() -> int:\n    return multiply(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    initial = ModuleType("test_facade_initial")
    monkeypatch.setitem(sys.modules, initial.__name__, initial)
    load_parts(initial.__dict__, tmp_path, ("first.py", "second.py"))

    assert initial.result() == 12
    assert initial.multiply.__module__ == "test_facade_initial"
    assert initial.multiply.__globals__ is initial.__dict__
    assert initial.multiply.__code__.co_filename == str(first)
    assert pickle.loads(pickle.dumps(initial.Payload(5))) == initial.Payload(5)
    assert Path(cache_from_source(str(first))).is_file()
    assert Path(cache_from_source(str(second))).is_file()
    assert _cache_flags(first) == 0b11
    assert _cache_flags(second) == 0b11

    def reject_recompile(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("valid facade bytecode should be reused")

    monkeypatch.setattr(SourceFileLoader, "source_to_code", reject_recompile)
    cached = ModuleType("test_facade_cached")
    monkeypatch.setitem(sys.modules, cached.__name__, cached)
    load_parts(cached.__dict__, tmp_path, ("first.py", "second.py"))

    assert cached.result() == 12
    assert cached.result.__module__ == "test_facade_cached"
    assert not any(name.startswith("test_facade_cached.__facade_parts__") for name in sys.modules)


def test_load_parts_invalidates_stale_bytecode(tmp_path: Path) -> None:
    part = tmp_path / "value.py"
    part.write_text("VALUE = 1\n", encoding="utf-8")
    initial = ModuleType("test_facade_before_edit")
    load_parts(initial.__dict__, tmp_path, ("value.py",))
    original_stat = part.stat()

    part.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(part, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    refreshed = ModuleType("test_facade_after_edit")
    load_parts(refreshed.__dict__, tmp_path, ("value.py",))

    assert refreshed.VALUE == 2


def test_load_parts_replaces_unsafe_timestamp_cache(tmp_path: Path) -> None:
    part = tmp_path / "timestamp.py"
    part.write_text("VALUE = 1\n", encoding="utf-8")
    original_stat = part.stat()
    py_compile.compile(
        str(part),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    part.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(part, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert _cache_flags(part) == 0
    module = ModuleType("test_facade_timestamp_cache")

    load_parts(module.__dict__, tmp_path, ("timestamp.py",))

    assert module.VALUE == 2
    assert _cache_flags(part) == 0b11


def test_load_parts_replaces_unchecked_hash_cache(tmp_path: Path) -> None:
    part = tmp_path / "unchecked.py"
    part.write_text("VALUE = 3\n", encoding="utf-8")
    py_compile.compile(
        str(part),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    assert _cache_flags(part) == 0b01
    module = ModuleType("test_facade_unchecked_cache")

    load_parts(module.__dict__, tmp_path, ("unchecked.py",))

    assert module.VALUE == 3
    assert _cache_flags(part) == 0b11


def test_load_parts_works_when_bytecode_writes_are_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    part = tmp_path / "readonly.py"
    part.write_text("VALUE = 7\n", encoding="utf-8")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = ModuleType("test_facade_no_bytecode")

    load_parts(module.__dict__, tmp_path, ("readonly.py",))

    assert module.VALUE == 7
    assert not Path(cache_from_source(str(part))).exists()


def test_load_parts_ignores_stale_timestamp_when_writes_are_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    part = tmp_path / "stale.py"
    part.write_text("VALUE = 1\n", encoding="utf-8")
    original_stat = part.stat()
    py_compile.compile(
        str(part),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    part.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(part, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = ModuleType("test_facade_stale_no_writes")

    load_parts(module.__dict__, tmp_path, ("stale.py",))

    assert module.VALUE == 2
    assert _cache_flags(part) == 0


def test_load_parts_falls_back_when_checked_cache_cannot_be_written(
    tmp_path: Path,
    monkeypatch,
) -> None:
    part = tmp_path / "unwritable.py"
    part.write_text("VALUE = 8\n", encoding="utf-8")

    def reject_cache_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("read-only cache")

    monkeypatch.setattr(py_compile, "compile", reject_cache_write)
    module = ModuleType("test_facade_unwritable_cache")

    load_parts(module.__dict__, tmp_path, ("unwritable.py",))

    assert module.VALUE == 8
    assert not Path(cache_from_source(str(part))).exists()


def test_load_parts_recovers_from_corrupt_bytecode(tmp_path: Path) -> None:
    part = tmp_path / "corrupt.py"
    part.write_text("VALUE = 9\n", encoding="utf-8")
    initial = ModuleType("test_facade_before_corruption")
    load_parts(initial.__dict__, tmp_path, ("corrupt.py",))
    cache_path = Path(cache_from_source(str(part)))
    cache_path.write_bytes(cache_path.read_bytes()[:16] + b"not valid bytecode")
    recovered = ModuleType("test_facade_after_corruption")

    load_parts(recovered.__dict__, tmp_path, ("corrupt.py",))

    assert recovered.VALUE == 9
