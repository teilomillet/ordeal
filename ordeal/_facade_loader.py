"""Shared loader for source-split public facade modules."""

from __future__ import annotations

import py_compile
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import MAGIC_NUMBER, cache_from_source
from pathlib import Path
from types import CodeType

_CHECKED_HASH_FLAGS = 0b11


def _is_checked_hash_cache(cache_path: str) -> bool:
    """Return whether *cache_path* has a valid checked-hash header."""
    try:
        with open(cache_path, "rb") as cache_file:
            header = cache_file.read(16)
    except OSError:
        return False
    return (
        len(header) == 16
        and header[:4] == MAGIC_NUMBER
        and int.from_bytes(header[4:8], "little") == _CHECKED_HASH_FLAGS
    )


def _compile_source(loader: SourceFileLoader, path: Path) -> CodeType:
    """Compile *path* directly without consulting an unsafe cache."""
    source = loader.get_data(str(path))
    return loader.source_to_code(source, str(path))


def _write_checked_hash_cache(path: Path, cache_path: str) -> bool:
    """Best-effort write of a source-checked cache for *path*."""
    if sys.dont_write_bytecode:
        return False
    try:
        py_compile.compile(
            str(path),
            cfile=cache_path,
            dfile=str(path),
            doraise=True,
            optimize=-1,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
    except (OSError, py_compile.PyCompileError):
        return False
    return _is_checked_hash_cache(cache_path)


def _load_checked_code(
    loader: SourceFileLoader,
    part_name: str,
    path: Path,
) -> CodeType:
    """Load fresh code, using only source-checked cached bytecode."""
    try:
        cache_path = cache_from_source(str(path))
    except (NotImplementedError, ValueError):
        return _compile_source(loader, path)

    if not _is_checked_hash_cache(cache_path) and not _write_checked_hash_cache(path, cache_path):
        return _compile_source(loader, path)

    try:
        code = loader.get_code(part_name)
    except (EOFError, ImportError, TypeError, ValueError):
        if not _write_checked_hash_cache(path, cache_path):
            return _compile_source(loader, path)
        code = loader.get_code(part_name)
    if code is None:
        raise ImportError(f"could not load facade part {path}")
    return code


def load_parts(
    namespace: dict[str, object],
    root: Path,
    filenames: tuple[str, ...],
) -> None:
    """Execute ordered facade parts using Python's validated bytecode cache.

    The parts intentionally share the facade module's globals so existing
    monkeypatch points and cross-part definitions retain their historical
    behavior. ``SourceFileLoader`` keeps that execution model while avoiding
    a fresh source compilation in every process once a valid ``.pyc`` exists.
    """
    facade_name = str(namespace.get("__name__", "ordeal"))
    for filename in filenames:
        path = root / filename
        part_name = f"{facade_name}.__facade_parts__.{path.stem}"
        loader = SourceFileLoader(part_name, str(path))
        code = _load_checked_code(loader, part_name, path)
        exec(code, namespace, namespace)
