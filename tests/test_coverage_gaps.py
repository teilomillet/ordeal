"""Tests for coverage gap reporting — line tracking and branch gap detection."""

from __future__ import annotations

from ordeal.explore import CoverageCollector, _find_branch_lines


class TestFindBranchLines:
    def test_finds_if_statements(self):
        source = """
def foo(x):
    if x > 0:
        return x
    else:
        return -x
"""
        branches = _find_branch_lines(source)
        lines = [ln for ln, _ in branches]
        assert 3 in lines  # the 'if x > 0:' line

    def test_finds_for_loops(self):
        source = """
def foo(items):
    for item in items:
        pass
"""
        branches = _find_branch_lines(source)
        lines = [ln for ln, _ in branches]
        assert 3 in lines

    def test_finds_while_loops(self):
        source = """
def foo():
    while True:
        break
"""
        branches = _find_branch_lines(source)
        lines = [ln for ln, _ in branches]
        assert 3 in lines

    def test_finds_try_except(self):
        source = """
def foo():
    try:
        pass
    except ValueError:
        pass
"""
        branches = _find_branch_lines(source)
        lines = [ln for ln, _ in branches]
        assert 3 in lines  # try
        assert 5 in lines  # except

    def test_finds_assert(self):
        source = """
def foo(x):
    assert x > 0
"""
        branches = _find_branch_lines(source)
        lines = [ln for ln, _ in branches]
        assert 3 in lines

    def test_finds_raise(self):
        source = """
def foo():
    raise ValueError("boom")
"""
        branches = _find_branch_lines(source)
        lines = [ln for ln, _ in branches]
        assert 3 in lines

    def test_empty_source(self):
        assert _find_branch_lines("") == []

    def test_syntax_error_returns_empty(self):
        assert _find_branch_lines("def broken(") == []


class TestCoverageCollectorLineTracking:
    def test_tracks_lines(self):
        """CoverageCollector should track which lines are visited."""
        import ordeal.demo as demo_mod

        collector = CoverageCollector(["ordeal/demo"])
        collector.start()
        demo_mod.score(0.5)
        collector.stop()

        lines = collector.lines_hit
        demo_files = [f for f in lines if "demo" in f]
        assert demo_files, f"Expected demo lines, got: {list(lines.keys())}"
        demo_lines = lines[demo_files[0]]
        assert len(demo_lines) > 0


class TestExplorationResultCoverageGaps:
    def test_summary_includes_coverage(self):
        from ordeal.explore import ExplorationResult

        result = ExplorationResult(
            lines_covered=80,
            lines_total=100,
            coverage_gaps=[
                {"file": "myapp/foo.py", "line": 10, "code": "if x > 0:"},
                {"file": "myapp/foo.py", "line": 20, "code": "except ValueError:"},
            ],
        )
        s = result.summary()
        assert "Line coverage: 80/100 (80%)" in s
        assert "Not reached" in s
        assert "2 branch" in s
        assert "if x > 0:" in s

    def test_summary_includes_reachable_suggestions(self):
        from ordeal.explore import ExplorationResult

        result = ExplorationResult(
            total_runs=50,
            coverage_gaps=[
                {"file": "myapp/foo.py", "line": 10, "code": "if x > 0:"},
            ],
        )
        s = result.summary()
        assert "reachable(" in s
        assert "myapp/foo.py:10" in s
        assert "Not reached" in s
        assert "50 runs" in s


class TestReachabilitySuggestions:
    def test_generates_suggestions_from_gaps(self):
        from ordeal.explore import ExplorationResult

        result = ExplorationResult(
            total_runs=100,
            coverage_gaps=[
                {"file": "myapp/foo.py", "line": 10, "code": "if x > 0:"},
                {"file": "myapp/bar.py", "line": 5, "code": "for item in items:"},
            ],
        )
        suggestions = result.reachability_suggestions()
        assert len(suggestions) == 2
        assert suggestions[0]["file"] == "myapp/foo.py"
        assert suggestions[0]["line"] == 10
        assert "reachable(" in suggestions[0]["suggestion"]
        assert "if x > 0:" in suggestions[0]["suggestion"]
        # Epistemic fields
        assert suggestions[0]["confidence"] == "not_reached"
        assert suggestions[0]["runs"] == 100

    def test_empty_gaps_empty_suggestions(self):
        from ordeal.explore import ExplorationResult

        result = ExplorationResult(coverage_gaps=[])
        assert result.reachability_suggestions() == []
