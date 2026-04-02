"""Tests for mutation testing resume/cache — correctness invariant.

The core guarantee: resume must never report a better score than fresh.
If it does, the cache is hiding bugs.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from ordeal.mutations import (
    MutationResult,
    _load_cache,
    _module_source_hash,
    _save_cache,
    mutate,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def clean_cache():
    """Remove .ordeal/mutate/ before and after each test."""
    cache_dir = Path(".ordeal") / "mutate"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    yield
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


@pytest.fixture()
def sample_module(tmp_path):
    """Create a simple module on disk for testing resume."""
    mod_dir = tmp_path / "resumemod"
    mod_dir.mkdir()
    source = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n\n"
        "def double(x: int) -> int:\n"
        "    return x * 2\n"
    )
    (mod_dir / "__init__.py").write_text(source)
    sys.path.insert(0, str(tmp_path))
    yield "resumemod", mod_dir
    sys.path.remove(str(tmp_path))
    for key in list(sys.modules):
        if key.startswith("resumemod"):
            del sys.modules[key]


# ============================================================================
# Cache helpers
# ============================================================================


class TestCacheHelpers:
    def test_module_source_hash_deterministic(self):
        """Same source produces same hash."""
        h1 = _module_source_hash("ordeal.demo")
        h2 = _module_source_hash("ordeal.demo")
        assert h1 == h2

    def test_module_source_hash_changes_on_edit(self, sample_module):
        """Editing the module produces a different hash."""
        mod_name, mod_dir = sample_module
        h1 = _module_source_hash(mod_name)

        # Edit the source
        init = mod_dir / "__init__.py"
        init.write_text(init.read_text() + "\nX = 42\n")
        # Force reimport
        for key in list(sys.modules):
            if key.startswith(mod_name):
                del sys.modules[key]

        h2 = _module_source_hash(mod_name)
        assert h1 != h2

    def test_save_and_load_roundtrip(self, clean_cache):
        """Cache save → load produces the same result."""
        result = MutationResult(
            target="test.target",
            operators_used=["arithmetic"],
            preset_used="essential",
        )
        from ordeal.mutations import Mutant

        result.mutants = [
            Mutant(
                operator="arithmetic",
                description="+ -> -",
                line=1,
                col=5,
                killed=True,
                error="assertion failed",
                source_line="return a + b",
                killed_by="test_add",
            ),
            Mutant(
                operator="comparison",
                description="< -> <=",
                line=3,
                col=8,
                killed=False,
                error=None,
                source_line="if a < b:",
            ),
        ]

        _save_cache("test.target", result, "abc123")
        loaded = _load_cache("test.target", "abc123", "essential", ["arithmetic"])
        assert loaded is not None
        assert loaded.total == 2
        assert loaded.killed == 1
        assert loaded.mutants[0].operator == "arithmetic"
        assert loaded.mutants[0].killed is True
        assert loaded.mutants[1].killed is False

    def test_load_rejects_different_hash(self, clean_cache):
        """Cache miss when module source changed."""
        result = MutationResult(target="test.target", preset_used="essential")
        _save_cache("test.target", result, "old_hash")
        loaded = _load_cache("test.target", "new_hash", "essential", None)
        assert loaded is None

    def test_load_rejects_different_preset(self, clean_cache):
        """Cache miss when preset changed."""
        result = MutationResult(target="test.target", preset_used="essential")
        _save_cache("test.target", result, "same_hash")
        loaded = _load_cache("test.target", "same_hash", "standard", None)
        assert loaded is None

    def test_load_rejects_different_operators(self, clean_cache):
        """Cache miss when operators changed."""
        result = MutationResult(
            target="test.target",
            operators_used=["arithmetic"],
            preset_used=None,
        )
        _save_cache("test.target", result, "same_hash")
        loaded = _load_cache("test.target", "same_hash", None, ["arithmetic", "comparison"])
        assert loaded is None

    def test_load_returns_none_when_no_cache(self, clean_cache):
        """Cache miss when no cache file exists."""
        loaded = _load_cache("nonexistent.target", "any_hash", None, None)
        assert loaded is None


# ============================================================================
# The invariant: resume ≥ fresh
# ============================================================================


class TestResumeInvariant:
    """The core safety guarantee: resume must never hide bugs."""

    def test_resume_matches_fresh_on_unchanged_source(self, clean_cache):
        """When source hasn't changed, resume returns identical results."""
        fresh = mutate("ordeal.demo.score", preset="essential")
        resumed = mutate("ordeal.demo.score", preset="essential", resume=True)
        assert resumed.total == fresh.total
        assert resumed.killed == fresh.killed
        assert resumed.score == fresh.score

    def test_cache_invalidated_on_source_change(self, sample_module, clean_cache):
        """When source changes, cache is discarded and tests re-run."""
        mod_name, mod_dir = sample_module

        # First run: populate cache
        mutate(f"{mod_name}.add", preset="essential", resume=True)

        # Edit the source
        init = mod_dir / "__init__.py"
        init.write_text(init.read_text().replace("a + b", "a - b"))
        for key in list(sys.modules):
            if key.startswith(mod_name):
                del sys.modules[key]

        # Second run: cache should be invalidated
        r2 = mutate(f"{mod_name}.add", preset="essential", resume=True)
        # r2 should have re-run (not returned stale r1)
        assert r2.diagnostics.get("cached", 0) == 0

    def test_resume_reports_cached_count(self, clean_cache):
        """Resume diagnostics show how many results came from cache."""
        # First run: fresh
        mutate("ordeal.demo.score", preset="essential", resume=True)
        # Second run: from cache
        r2 = mutate("ordeal.demo.score", preset="essential", resume=True)
        assert r2.diagnostics.get("cached", 0) > 0
        assert r2.diagnostics.get("retested", 0) == 0

    def test_fresh_run_no_cached_diagnostic(self, clean_cache):
        """Fresh run (resume=False) should not have cached diagnostic."""
        result = mutate("ordeal.demo.score", preset="essential", resume=False)
        assert result.diagnostics.get("cached") is None
