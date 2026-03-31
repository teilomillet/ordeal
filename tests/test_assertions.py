"""Tests for ordeal.assertions — Antithesis-style property assertions."""

import pytest

from ordeal.assertions import (
    Property,
    PropertyTracker,
    always,
    reachable,
    sometimes,
    tracker,
    unreachable,
)


class TestProperty:
    def test_always_passes_when_never_violated(self):
        p = Property(name="test", type="always", hits=10, passes=10)
        assert p.passed is True

    def test_always_fails_on_violation(self):
        p = Property(name="test", type="always", hits=10, passes=9, failures=1)
        assert p.passed is False

    def test_always_fails_when_never_hit(self):
        p = Property(name="test", type="always", hits=0)
        assert p.passed is False

    def test_sometimes_passes_when_true_once(self):
        p = Property(name="test", type="sometimes", hits=100, passes=1, failures=99)
        assert p.passed is True

    def test_sometimes_fails_when_never_true(self):
        p = Property(name="test", type="sometimes", hits=100, passes=0, failures=100)
        assert p.passed is False

    def test_reachable_passes_when_hit(self):
        p = Property(name="test", type="reachable", hits=1)
        assert p.passed is True

    def test_reachable_fails_when_never_hit(self):
        p = Property(name="test", type="reachable", hits=0)
        assert p.passed is False

    def test_unreachable_passes_when_never_hit(self):
        p = Property(name="test", type="unreachable", hits=0)
        assert p.passed is True

    def test_unreachable_fails_when_hit(self):
        p = Property(name="test", type="unreachable", hits=1)
        assert p.passed is False

    def test_summary_includes_name_and_type(self):
        p = Property(name="no corruption", type="always", hits=5, passes=5)
        assert "no corruption" in p.summary
        assert "PASS" in p.summary


class TestPropertyTracker:
    def test_inactive_tracker_ignores_records(self):
        t = PropertyTracker()
        t.record("test", "always", True)
        assert len(t.results) == 0

    def test_active_tracker_records(self):
        t = PropertyTracker()
        t.active = True
        t.record("test", "always", True)
        assert len(t.results) == 1
        assert t.results[0].hits == 1

    def test_same_name_aggregates(self):
        t = PropertyTracker()
        t.active = True
        t.record("test", "always", True)
        t.record("test", "always", True)
        t.record("test", "always", False)
        assert len(t.results) == 1
        assert t.results[0].hits == 3
        assert t.results[0].passes == 2
        assert t.results[0].failures == 1

    def test_reset_clears(self):
        t = PropertyTracker()
        t.active = True
        t.record("test", "always", True)
        t.reset()
        assert len(t.results) == 0

    def test_failures_property(self):
        t = PropertyTracker()
        t.active = True
        t.record("good", "always", True)
        t.record("bad", "always", False)
        assert len(t.failures) == 1
        assert t.failures[0].name == "bad"


class TestAlways:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_passes_silently(self):
        always(True, "good")
        assert tracker.results[0].passes == 1

    def test_raises_on_violation(self):
        with pytest.raises(AssertionError, match="always violated: bad"):
            always(False, "bad")

    def test_details_in_message(self):
        with pytest.raises(AssertionError, match="value"):
            always(False, "check", value=42)


class TestSometimes:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_never_raises(self):
        # sometimes never raises, even when condition is False
        sometimes(False, "rare")
        sometimes(False, "rare")
        assert tracker.results[0].hits == 2
        assert tracker.results[0].passes == 0


class TestSometimesWarn:
    """Test sometimes(warn=True) — visible without --chaos."""

    def setup_method(self):
        tracker.active = False  # explicitly inactive
        tracker.reset()

    def test_warn_true_prints_pass(self, capsys):
        sometimes(True, "ratio check", warn=True)
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "ratio check" in captured.out

    def test_warn_true_prints_observe(self, capsys):
        sometimes(False, "low ratio", warn=True)
        captured = capsys.readouterr()
        assert "OBSERVE" in captured.out
        assert "low ratio" in captured.out

    def test_warn_false_does_not_print(self, capsys):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sometimes(True, "silent", warn=False)
        captured = capsys.readouterr()
        assert captured.out == ""


class TestReport:
    """Test report() structured summary."""

    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_report_returns_passed_and_failed(self):
        from ordeal.assertions import report

        always(True, "good-prop")
        sometimes(False, "never-seen")
        r = report()
        assert "passed" in r
        assert "failed" in r
        assert len(r["passed"]) == 1
        assert r["passed"][0]["name"] == "good-prop"
        assert r["passed"][0]["status"] == "PASS"

    def test_report_empty_when_nothing_tracked(self):
        from ordeal.assertions import report

        r = report()
        assert r == {"passed": [], "failed": []}


class TestReachable:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_records_hit(self):
        reachable("code-path")
        assert tracker.results[0].hits == 1


class TestUnreachable:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_raises_when_reached(self):
        with pytest.raises(AssertionError, match="unreachable code reached"):
            unreachable("dead-code")
