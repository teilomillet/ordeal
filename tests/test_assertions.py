"""Tests for ordeal.assertions — Antithesis-style property assertions."""

import json

import pytest

from ordeal.assertions import (
    Property,
    PropertyTracker,
    always,
    declare,
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

    def test_counter_delta_reports_only_properties_changed_since_mark(self):
        t = PropertyTracker()
        t.active = True
        t.record("already seen", "always", True)
        before = t.counter_snapshot()

        t.record("already seen", "always", False)
        t.record_hit("new path", "reachable")

        changes = {event["name"]: event for event in t.counter_delta(before)}
        assert changes["already seen"] == {
            "name": "already seen",
            "type": "always",
            "delta_hits": 1,
            "delta_passes": 0,
            "delta_failures": 1,
        }
        assert changes["new path"]["delta_hits"] == 1


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

    def test_report_includes_declared_unreached_property(self):
        from ordeal.assertions import report

        declare("timeout handler runs", "reachable")
        r = report()
        assert r["failed"][0]["name"] == "timeout handler runs"
        assert r["failed"][0]["type"] == "reachable"
        assert r["failed"][0]["evidence_status"] == "unexercised"


class TestReliabilityCoverage:
    """Operation × fault × property coverage is evidence-based and serializable."""

    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.reset()
        tracker.active = False

    def test_reports_pass_not_exercised_and_fail(self):
        from ordeal.assertions import report

        declare(
            "eventual_commit",
            "always",
            operation="create_order",
            fault="worker_restart",
        )
        always(
            True,
            "no_duplicate_charge",
            operation="create_order",
            fault="timeout",
        )
        always(
            False,
            "balance_conserved",
            mute=True,
            operation="refund",
            fault="stale_response",
        )

        coverage = report()["reliability_coverage"]
        rows = {(row["operation"], row["fault"], row["property"]): row for row in coverage["rows"]}

        assert coverage["dimensions"] == ["operation", "fault", "property"]
        assert rows[("create_order", "timeout", "no_duplicate_charge")]["status"] == "PASS"
        assert (
            rows[("create_order", "worker_restart", "eventual_commit")]["status"]
            == "NOT EXERCISED"
        )
        assert rows[("refund", "stale_response", "balance_conserved")]["status"] == "FAIL"
        assert coverage["summary"] == {
            "pass": 1,
            "not_exercised": 1,
            "fail": 1,
            "total": 3,
        }

    def test_contextual_declaration_does_not_become_global_failure(self):
        declare(
            "eventual_commit",
            "always",
            operation="create_order",
            fault="worker_restart",
        )

        assert tracker.results == []
        assert tracker.reliability_results[0].status == "NOT EXERCISED"

    def test_report_is_json_serializable(self):
        from ordeal.assertions import report

        always(True, "durable", operation="write", fault="process_crash")

        payload = json.loads(json.dumps(report()))
        row = payload["reliability_coverage"]["rows"][0]
        assert row == {
            "operation": "write",
            "fault": "process_crash",
            "property": "durable",
            "type": "always",
            "status": "PASS",
            "hits": 1,
            "passes": 1,
            "failures": 0,
        }

    def test_operation_and_fault_are_required_together(self):
        with pytest.raises(ValueError, match="provided together"):
            always(True, "durable", operation="write")
        with pytest.raises(ValueError, match="provided together"):
            declare("durable", "always", fault="process_crash")

    def test_same_property_is_independent_across_faults(self):
        always(True, "balance_conserved", operation="refund", fault="timeout")
        always(
            False,
            "balance_conserved",
            mute=True,
            operation="refund",
            fault="stale_response",
        )

        statuses = {cell.fault: cell.status for cell in tracker.reliability_results}
        assert statuses == {"stale_response": "FAIL", "timeout": "PASS"}

    def test_assertion_types_keep_their_existing_semantics(self):
        sometimes(False, "eventual", operation="write", fault="delay")
        assert tracker.reliability_results[0].status == "FAIL"
        sometimes(True, "eventual", operation="write", fault="delay")
        assert tracker.reliability_results[0].status == "PASS"

        reachable("recovery_path", operation="write", fault="process_crash")
        unreachable(
            "data_loss",
            mute=True,
            operation="write",
            fault="disk_full",
        )
        statuses = {cell.property: cell.status for cell in tracker.reliability_results}
        assert statuses["recovery_path"] == "PASS"
        assert statuses["data_loss"] == "FAIL"

    def test_immediate_sometimes_failure_records_fail(self):
        with pytest.raises(AssertionError, match="never true"):
            sometimes(
                lambda: False,
                "eventual_commit",
                attempts=2,
                operation="create_order",
                fault="worker_restart",
            )

        cell = tracker.reliability_results[0]
        assert cell.hits == 1
        assert cell.status == "FAIL"

    def test_reset_and_restore_include_reliability_cells(self):
        always(True, "durable", operation="write", fault="process_crash")
        snapshot = tracker.snapshot()
        tracker.reset()
        assert tracker.reliability_results == []

        tracker.restore(snapshot)
        assert tracker.reliability_results[0].status == "PASS"

    def test_worker_rows_merge_without_losing_declarations(self):
        tracker.merge_reliability(
            [
                {
                    "operation": "create_order",
                    "fault": "timeout",
                    "property": "no_duplicate_charge",
                    "type": "always",
                    "hits": 2,
                    "passes": 2,
                    "failures": 0,
                },
                {
                    "operation": "create_order",
                    "fault": "worker_restart",
                    "property": "eventual_commit",
                    "type": "always",
                    "hits": 0,
                    "passes": 0,
                    "failures": 0,
                },
            ]
        )
        tracker.merge_reliability(
            [
                {
                    "operation": "create_order",
                    "fault": "timeout",
                    "property": "no_duplicate_charge",
                    "type": "always",
                    "hits": 3,
                    "passes": 2,
                    "failures": 1,
                }
            ]
        )

        cells = {cell.fault: cell for cell in tracker.reliability_results}
        assert cells["timeout"].hits == 5
        assert cells["timeout"].status == "FAIL"
        assert cells["worker_restart"].status == "NOT EXERCISED"


class TestReachable:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_records_hit(self):
        reachable("code-path")
        assert tracker.results[0].hits == 1

    def test_declared_reachable_fails_when_never_hit(self):
        declare("declared-path", "reachable")
        assert tracker.failures[0].name == "declared-path"

    def test_declared_reachable_passes_once_hit(self):
        declare("declared-path", "reachable")
        reachable("declared-path")
        assert tracker.results[0].passed is True


class TestUnreachable:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_raises_when_reached(self):
        with pytest.raises(AssertionError, match="unreachable code reached"):
            unreachable("dead-code")


class TestDeclare:
    def setup_method(self):
        tracker.active = True
        tracker.reset()

    def teardown_method(self):
        tracker.active = False

    def test_declared_sometimes_fails_when_never_true(self):
        declare("cache warms", "sometimes")
        assert tracker.failures[0].name == "cache warms"

    def test_declared_sometimes_passes_when_observed(self):
        declare("cache warms", "sometimes")
        sometimes(True, "cache warms")
        assert tracker.results[0].passed is True

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="deferred property types"):
            declare("never", "always")
