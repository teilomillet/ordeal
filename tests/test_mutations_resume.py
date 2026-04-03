"""Tests for mutation testing resume/cache — correctness invariant.

The core guarantee: resume must never report a better score than fresh.
If it does, the cache is hiding bugs.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from types import SimpleNamespace

import pytest

from ordeal.mutations import (
    MutationResult,
    _load_cache,
    _module_source_hash,
    _save_cache,
    mutate,
    validate_mined_properties,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def clean_cache(tmp_path, monkeypatch):
    """Run each cache test in its own project-local cache directory."""
    monkeypatch.chdir(tmp_path)
    cache_dir = tmp_path / ".ordeal" / "mutate"
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    yield
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)


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
            concern="boundary behavior",
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

        _save_cache("test.target", result, "abc123", "cfg123")
        loaded = _load_cache("test.target", "abc123", "essential", ["arithmetic"], "cfg123")
        assert loaded is not None
        assert loaded.total == 2
        assert loaded.killed == 1
        assert loaded.concern == "boundary behavior"
        assert loaded.mutants[0].operator == "arithmetic"
        assert loaded.mutants[0].killed is True
        assert loaded.mutants[1].killed is False

    def test_load_rejects_different_hash(self, clean_cache):
        """Cache miss when module source changed."""
        result = MutationResult(target="test.target", preset_used="essential")
        _save_cache("test.target", result, "old_hash", "cfg123")
        loaded = _load_cache("test.target", "new_hash", "essential", None, "cfg123")
        assert loaded is None

    def test_load_rejects_different_preset(self, clean_cache):
        """Cache miss when preset changed."""
        result = MutationResult(target="test.target", preset_used="essential")
        _save_cache("test.target", result, "same_hash", "cfg123")
        loaded = _load_cache("test.target", "same_hash", "standard", None, "cfg123")
        assert loaded is None

    def test_load_rejects_different_operators(self, clean_cache):
        """Cache miss when operators changed."""
        result = MutationResult(
            target="test.target",
            operators_used=["arithmetic"],
            preset_used=None,
        )
        _save_cache("test.target", result, "same_hash", "cfg123")
        loaded = _load_cache(
            "test.target",
            "same_hash",
            None,
            ["arithmetic", "comparison"],
            "cfg123",
        )
        assert loaded is None

    def test_load_rejects_different_config(self, clean_cache):
        """Cache miss when non-source mutation settings change."""
        result = MutationResult(target="test.target", preset_used="essential")
        _save_cache("test.target", result, "same_hash", "cfg-old")
        loaded = _load_cache("test.target", "same_hash", "essential", None, "cfg-new")
        assert loaded is None

    def test_load_returns_none_when_no_cache(self, clean_cache):
        """Cache miss when no cache file exists."""
        loaded = _load_cache("nonexistent.target", "any_hash", None, None, "cfg123")
        assert loaded is None


# ============================================================================
# The invariant: resume ≥ fresh
# ============================================================================


class TestResumeInvariant:
    """The core safety guarantee: resume must never hide bugs."""

    def test_resume_matches_fresh_on_unchanged_source(self, clean_cache):
        """When source hasn't changed, resume returns identical results."""
        import tests._mutation_target as mod
        from ordeal.mutations import mutate_function_and_test

        def test_add():
            assert mod.add(1, 2) == 3
            assert mod.add(-1, 5) == 4

        fresh = mutate_function_and_test(
            "tests._mutation_target.add", test_fn=test_add, operators=["arithmetic"]
        )
        # Now test via mutate() with resume (which wraps mutate_function_and_test)
        resumed = mutate(
            "tests._mutation_target.add",
            test_fn=test_add,
            operators=["arithmetic"],
            resume=True,
        )
        # First call saves to cache; both should have same results
        assert resumed.total == fresh.total
        assert resumed.killed == fresh.killed

    def test_cache_invalidated_on_source_change(self, sample_module, clean_cache):
        """When source changes, cache is discarded and tests re-run."""
        mod_name, mod_dir = sample_module

        def test_add() -> None:
            mod = importlib.import_module(mod_name)
            assert mod.add(1, 2) == 3
            assert mod.add(-1, 5) == 4

        # First run: populate cache
        mutate(f"{mod_name}.add", test_fn=test_add, preset="essential", resume=True)

        # Edit the source
        init = mod_dir / "__init__.py"
        init.write_text(init.read_text().replace("a + b", "a - b"))
        for key in list(sys.modules):
            if key.startswith(mod_name):
                del sys.modules[key]

        # Second run: cache should be invalidated
        r2 = mutate(f"{mod_name}.add", test_fn=test_add, preset="essential", resume=True)
        # r2 should have re-run (not returned stale r1)
        assert r2.diagnostics.get("cached", 0) == 0

    def test_resume_reports_cached_count(self, clean_cache):
        """Resume diagnostics show how many results came from cache."""
        import tests._mutation_target as mod

        def test_add():
            assert mod.add(1, 2) == 3

        target = "tests._mutation_target.add"
        # First run: populates cache (explicit test_fn, not mine oracle)
        mutate(target, test_fn=test_add, operators=["arithmetic"], resume=True)
        # Second run: from cache
        r2 = mutate(target, test_fn=test_add, operators=["arithmetic"], resume=True)
        assert r2.diagnostics.get("cached", 0) > 0
        assert r2.diagnostics.get("retested", 0) == 0

    def test_resume_invalidated_when_test_fn_changes(self, sample_module, clean_cache):
        """A different custom test oracle must not reuse the old cache."""
        mod_name, _ = sample_module

        def weak_test() -> None:
            mod = importlib.import_module(mod_name)
            assert mod.add(1, 2) == 3

        def strong_test() -> None:
            mod = importlib.import_module(mod_name)
            assert mod.add(1, 2) == 3
            assert mod.add(-1, 5) == 4

        first = mutate(f"{mod_name}.add", test_fn=weak_test, preset="essential", resume=True)
        second = mutate(f"{mod_name}.add", test_fn=strong_test, preset="essential", resume=True)

        assert second.diagnostics.get("cached", 0) == 0
        assert second.killed >= first.killed

    def test_fresh_run_no_cached_diagnostic(self, clean_cache):
        """Fresh run (resume=False) should not have cached diagnostic."""
        import tests._mutation_target as mod

        def test_add():
            assert mod.add(1, 2) == 3

        result = mutate(
            "tests._mutation_target.add",
            test_fn=test_add,
            operators=["arithmetic"],
            resume=False,
        )
        assert result.diagnostics.get("cached") is None

    def test_mine_oracle_results_not_cached(self, clean_cache, tmp_path):
        """Mine oracle results are stochastic — must not be cached.

        mine() uses random inputs, so re-running can discover different
        properties. Results where killed_by='mine()' must not be saved
        to cache.
        """
        mod_dir = tmp_path / "oraclenomod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text(
            "def mystery_transform(x: int) -> int:\n    return (x * 2) + 1\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            r1 = mutate("oraclenomod.mystery_transform", preset="essential", resume=True)
            assert any(m.killed_by == "mine()" for m in r1.mutants)

            # Second run should NOT be cached — mine should run fresh again
            r2 = mutate("oraclenomod.mystery_transform", preset="essential", resume=True)
            assert r2.diagnostics.get("cached") is None
            assert any(m.killed_by == "mine()" for m in r2.mutants)
        finally:
            sys.path.remove(str(tmp_path))
            for key in list(sys.modules):
                if key.startswith("oraclenomod"):
                    del sys.modules[key]

    def test_llm_mutation_results_not_cached(self, clean_cache):
        """LLM-generated mutants are nondeterministic, so resume must stay fresh."""

        def fake_llm(prompt: str) -> str:
            return "```python\ndef add(a: int, b: int) -> int:\n    return a - b\n```"

        def test_add() -> None:
            import tests._mutation_target as mod

            assert mod.add(1, 2) == 3
            assert mod.add(-1, 5) == 4

        r1 = mutate(
            "tests._mutation_target.add",
            test_fn=test_add,
            preset="essential",
            llm=fake_llm,
            resume=True,
        )
        r2 = mutate(
            "tests._mutation_target.add",
            test_fn=test_add,
            preset="essential",
            llm=fake_llm,
            resume=True,
        )

        assert r1.total >= 1
        assert r2.diagnostics.get("cached") is None

    def test_validate_mined_properties_uses_precomputed_mine_result(self, monkeypatch):
        """A supplied mine result should bypass the initial mine() call."""
        import ordeal.mine as mine_mod
        import ordeal.mutations as mutations_mod

        fake_mine_result = SimpleNamespace(
            universal=[SimpleNamespace(name="deterministic")],
        )

        def fail_mine(*args, **kwargs):
            raise AssertionError("mine() should not run when mine_result is supplied")

        monkeypatch.setattr(mine_mod, "mine", fail_mine)
        monkeypatch.setattr(
            mutations_mod,
            "mutate_function_and_test",
            lambda target, test_fn, operators: MutationResult(
                target=target,
                operators_used=operators,
            ),
        )

        result = validate_mined_properties(
            "tests._mutation_target.add",
            operators=["arithmetic"],
            mine_result=fake_mine_result,
        )

        assert result.target == "tests._mutation_target.add"
        assert result.operators_used == ["arithmetic"]

    def test_validate_mined_properties_replays_samples_without_remining(self, monkeypatch):
        """Validation should use recorded mine() samples before falling back to re-mining."""
        import ordeal.mine as mine_mod
        import ordeal.mutations as mutations_mod

        fake_mine_result = SimpleNamespace(
            universal=[SimpleNamespace(name="deterministic")],
            collected_inputs=[{"a": 1, "b": 2}, {"a": -1, "b": 5}],
        )

        calls: dict[str, int] = {"test_fn": 0}

        def fail_mine(*args, **kwargs):
            raise AssertionError("mine() should not run when replay inputs are available")

        def fake_mutate(target, test_fn, operators):
            calls["test_fn"] += 1
            test_fn()
            return MutationResult(target=target, operators_used=operators)

        monkeypatch.setattr(mine_mod, "mine", fail_mine)
        monkeypatch.setattr(mutations_mod, "mutate_function_and_test", fake_mutate)

        result = validate_mined_properties(
            "tests._mutation_target.add",
            operators=["arithmetic"],
            mine_result=fake_mine_result,
        )

        assert calls["test_fn"] == 1
        assert result.target == "tests._mutation_target.add"

    def test_validate_mined_properties_deep_mode_remines_mutants(self, monkeypatch):
        """Deep validation should re-run mine() on the mutant path."""
        import ordeal.mine as mine_mod
        import ordeal.mutations as mutations_mod

        fake_prop = SimpleNamespace(name="deterministic", universal=True)
        fake_mine_result = SimpleNamespace(
            universal=[fake_prop],
            properties=[fake_prop],
            collected_inputs=[{"a": 1, "b": 2}],
        )

        calls: dict[str, object] = {"mine": 0, "test_fn": 0, "budgets": []}

        def fake_mine(*args, **kwargs):
            calls["mine"] = int(calls["mine"]) + 1
            budgets = calls["budgets"]
            assert isinstance(budgets, list)
            budgets.append(kwargs["max_examples"])
            return fake_mine_result

        def fake_mutate(target, test_fn, operators):
            calls["test_fn"] = int(calls["test_fn"]) + 1
            test_fn()
            return MutationResult(target=target, operators_used=operators)

        monkeypatch.setattr(mine_mod, "mine", fake_mine)
        monkeypatch.setattr(mutations_mod, "mutate_function_and_test", fake_mutate)

        result = validate_mined_properties(
            "tests._mutation_target.add",
            max_examples=50,
            operators=["arithmetic"],
            mine_result=fake_mine_result,
            validation_mode="deep",
        )

        assert calls["mine"] == 1
        assert calls["test_fn"] == 1
        budgets = calls["budgets"]
        assert isinstance(budgets, list)
        assert len(budgets) == 1
        assert budgets[0] >= 50
        assert result.target == "tests._mutation_target.add"


# ============================================================================
# Cache invalidation: test changes and dependency changes
# ============================================================================


class TestCacheInvalidation:
    """Cache must invalidate when tests or dependencies change."""

    def test_test_file_change_invalidates_cache(self, sample_module, clean_cache):
        """Editing test_<module>.py must invalidate the cache."""
        mod_name, mod_dir = sample_module
        test_dir = mod_dir.parent / "tests"
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / f"test_{mod_name}.py"

        # Write a test file and get the hash
        test_file.write_text(
            f"import {mod_name}\n\ndef test_add():\n    assert {mod_name}.add(1, 2) == 3\n"
        )

        h1 = _module_source_hash(mod_name)

        # Edit the test file
        test_file.write_text(
            f"import {mod_name}\n\n"
            f"def test_add():\n"
            f"    assert {mod_name}.add(1, 2) == 3\n"
            f"    assert {mod_name}.add(-1, 5) == 4  # new assertion\n"
        )

        h2 = _module_source_hash(mod_name)
        assert h1 != h2, "Test file change must produce different hash"

    def test_conftest_change_invalidates_cache(self, sample_module, clean_cache):
        """Editing conftest.py must invalidate the cache."""
        mod_name, mod_dir = sample_module
        test_dir = mod_dir.parent / "tests"
        test_dir.mkdir(exist_ok=True)
        conftest = test_dir / "conftest.py"

        # Create conftest and get hash
        conftest.write_text("# empty conftest\n")
        h1 = _module_source_hash(mod_name)

        # Edit conftest
        conftest.write_text(
            "import pytest\n\n@pytest.fixture(scope='session')\ndef ray_init():\n    pass\n"
        )
        h2 = _module_source_hash(mod_name)
        assert h1 != h2, "conftest.py change must produce different hash"

    def test_lockfile_change_invalidates_cache(self, sample_module, clean_cache, tmp_path):
        """Lockfile change (dependency upgrade) must invalidate the cache."""
        mod_name, mod_dir = sample_module

        # We can't easily modify the real uv.lock, so test the hash function
        # by creating a fake lockfile in cwd. The hash function looks in cwd.
        import os

        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            # Create the module structure in tmp_path for the hash function
            h1 = _module_source_hash(mod_name)

            # Create a lockfile
            (tmp_path / "uv.lock").write_text("numpy==1.26.0\n")
            h2 = _module_source_hash(mod_name)
            assert h1 != h2, "Adding lockfile must change hash"

            # Modify lockfile (dependency upgrade)
            (tmp_path / "uv.lock").write_text("numpy==2.0.0\n")
            h3 = _module_source_hash(mod_name)
            assert h2 != h3, "Lockfile change must change hash"
        finally:
            os.chdir(orig_cwd)

    def test_module_change_still_invalidates(self, sample_module, clean_cache):
        """Module source change must still invalidate (regression check)."""
        mod_name, mod_dir = sample_module
        h1 = _module_source_hash(mod_name)

        # Edit module source
        init = mod_dir / "__init__.py"
        init.write_text(init.read_text() + "\nNEW_CONSTANT = 42\n")
        for key in list(sys.modules):
            if key.startswith(mod_name):
                del sys.modules[key]

        h2 = _module_source_hash(mod_name)
        assert h1 != h2, "Module source change must produce different hash"

    def test_prefixed_test_file_change_invalidates(self, sample_module, clean_cache):
        """test_<module>_presets.py style files must also invalidate."""
        mod_name, mod_dir = sample_module
        test_dir = mod_dir.parent / "tests"
        test_dir.mkdir(exist_ok=True)

        h1 = _module_source_hash(mod_name)

        # Create a prefixed test file (like test_mutations_presets.py)
        prefixed = test_dir / f"test_{mod_name}_edge_cases.py"
        prefixed.write_text("def test_edge():\n    pass\n")

        h2 = _module_source_hash(mod_name)
        assert h1 != h2, "Prefixed test file must change the hash"

        # Edit it
        prefixed.write_text("def test_edge():\n    assert True\n")
        h3 = _module_source_hash(mod_name)
        assert h2 != h3, "Editing prefixed test file must change the hash"

    def test_no_change_same_hash(self, sample_module, clean_cache):
        """No changes → same hash (deterministic)."""
        mod_name, _ = sample_module
        h1 = _module_source_hash(mod_name)
        h2 = _module_source_hash(mod_name)
        assert h1 == h2
