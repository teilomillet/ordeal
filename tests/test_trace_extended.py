"""Tests for ordeal.trace — gzip, generate_tests, sanitize, encoder/decoder."""

from __future__ import annotations

import json

from hypothesis.stateful import invariant, rule

from ordeal.chaos import ChaosTest
from ordeal.faults import LambdaFault
from ordeal.trace import (
    Trace,
    TraceFailure,
    TraceStep,
    _get_invariant_methods,
    _import_class,
    _replay_fault_toggle,
    _replay_rule,
    _replay_steps,
    _sanitize,
    _test_body,
    _test_docstring,
    _test_fn_name,
    _TraceDecoder,
    _TraceEncoder,
    generate_tests,
)

# ============================================================================
# Test fixtures
# ============================================================================


class _WithInvariant(ChaosTest):
    faults = []

    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def tick(self):
        self.counter += 1
        if self.counter >= 3:
            raise ValueError("boom")

    @invariant()
    def positive(self):
        assert self.counter >= 0


class _WithFaults(ChaosTest):
    faults = [
        LambdaFault("test_fault", lambda: None, lambda: None),
    ]

    def __init__(self):
        super().__init__()
        self.value = 0

    @rule()
    def increment(self):
        self.value += 1

    @rule()
    def check(self):
        if self.value > 2 and self._faults[0].active:
            raise RuntimeError("fault-triggered")


# ============================================================================
# Gzip save/load
# ============================================================================


class TestGzipSaveLoad:
    def test_gzip_round_trip(self, tmp_path):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="tests.test_trace:_BombAt3",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="tick", params={}, active_faults=[], edge_count=5),
                TraceStep(kind="rule", name="tick", params={"x": 10}),
            ],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=1),
            edges_discovered=5,
            duration=0.1,
        )
        path = tmp_path / "trace.json.gz"
        trace.save(path)
        loaded = Trace.load(path)
        assert loaded.run_id == 1
        assert loaded.seed == 42
        assert len(loaded.steps) == 2
        assert loaded.failure.error_type == "ValueError"

    def test_gzip_smaller_than_json(self, tmp_path):
        steps = [TraceStep(kind="rule", name=f"step_{i}", params={"i": i}) for i in range(100)]
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="x:Y",
            from_checkpoint=None,
            steps=steps,
        )
        json_path = tmp_path / "trace.json"
        gz_path = tmp_path / "trace.json.gz"
        trace.save(json_path)
        trace.save(gz_path)
        assert gz_path.stat().st_size < json_path.stat().st_size

    def test_gzip_with_bytes_params(self, tmp_path):
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="x:Y",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="f", params={"data": b"\x00\xff\xab"})],
        )
        path = tmp_path / "trace.json.gz"
        trace.save(path)
        loaded = Trace.load(path)
        assert loaded.steps[0].params["data"] == b"\x00\xff\xab"


# ============================================================================
# _sanitize
# ============================================================================


class TestSanitize:
    def test_bytes_to_base64(self):
        result = _sanitize(b"\x00\xff")
        assert "__bytes__" in result

    def test_set_to_sorted_list(self):
        result = _sanitize({3, 1, 2})
        assert result == {"__set__": [1, 2, 3]}

    def test_frozenset_to_sorted_list(self):
        result = _sanitize(frozenset(["c", "a", "b"]))
        assert result == {"__set__": ["a", "b", "c"]}

    def test_mixed_type_set(self):
        # Mixed int/str can't be sorted — should fall back to list
        result = _sanitize({1, "a"})
        assert "__set__" in result
        assert set(result["__set__"]) == {1, "a"}

    def test_exception_to_dict(self):
        result = _sanitize(ValueError("boom"))
        assert result["__error__"] == "boom"
        assert result["__type__"] == "ValueError"

    def test_arbitrary_object_to_repr(self):
        class Foo:
            pass

        result = _sanitize(Foo())
        assert "__repr__" in result

    def test_primitives_pass_through(self):
        assert _sanitize(42) == 42
        assert _sanitize("hello") == "hello"
        assert _sanitize(3.14) == 3.14
        assert _sanitize(True) is True
        assert _sanitize(None) is None

    def test_nested_dict(self):
        result = _sanitize({"key": b"\x00", "nested": {"inner": {1, 2}}})
        assert "__bytes__" in result["key"]
        assert result["nested"]["inner"] == {"__set__": [1, 2]}

    def test_nested_list(self):
        result = _sanitize([b"\x01", {3, 1}])
        assert "__bytes__" in result[0]
        assert result[1] == {"__set__": [1, 3]}


# ============================================================================
# _TraceEncoder / _TraceDecoder
# ============================================================================


class TestTraceEncoder:
    def test_encodes_bytes(self):
        encoded = json.dumps({"payload": b"\x00\x01\x02"}, cls=_TraceEncoder)
        assert "__bytes__" in encoded

    def test_encodes_set(self):
        encoded = json.dumps({"tags": {1, 2, 3}}, cls=_TraceEncoder)
        assert "__set__" in encoded

    def test_encodes_frozenset(self):
        encoded = json.dumps({"tags": frozenset([4, 5])}, cls=_TraceEncoder)
        assert "__set__" in encoded

    def test_encodes_bytearray(self):
        encoded = json.dumps({"buf": bytearray(b"\xab\xcd")}, cls=_TraceEncoder)
        assert "__bytes__" in encoded

    def test_encodes_exception(self):
        encoded = json.dumps({"err": ValueError("oops")}, cls=_TraceEncoder)
        parsed = json.loads(encoded)
        assert parsed["err"]["__error__"] == "oops"
        assert parsed["err"]["__type__"] == "ValueError"

    def test_encodes_unknown_as_repr(self):
        class Widget:
            def __repr__(self):
                return "Widget()"

        encoded = json.dumps({"w": Widget()}, cls=_TraceEncoder)
        parsed = json.loads(encoded)
        assert parsed["w"]["__repr__"] == "Widget()"


class TestTraceDecoder:
    def test_decodes_bytes(self):
        import base64

        b64 = base64.b64encode(b"\x00\xff").decode()
        data = json.loads(f'{{"__bytes__": "{b64}"}}', cls=_TraceDecoder)
        assert data == b"\x00\xff"

    def test_decodes_set(self):
        data = json.loads('{"__set__": [1, 2, 3]}', cls=_TraceDecoder)
        assert data == {1, 2, 3}

    def test_round_trip_bytes(self):
        original = {"payload": b"\xde\xad\xbe\xef"}
        encoded = json.dumps(original, cls=_TraceEncoder)
        decoded = json.loads(encoded, cls=_TraceDecoder)
        assert decoded["payload"] == b"\xde\xad\xbe\xef"

    def test_round_trip_set(self):
        original = {"tags": {10, 20, 30}}
        encoded = json.dumps(original, cls=_TraceEncoder)
        decoded = json.loads(encoded, cls=_TraceDecoder)
        assert decoded["tags"] == {10, 20, 30}

    def test_normal_dict_passthrough(self):
        data = json.loads('{"key": "value", "n": 42}', cls=_TraceDecoder)
        assert data == {"key": "value", "n": 42}


# ============================================================================
# _import_class
# ============================================================================


class TestImportClass:
    def test_imports_known_class(self):
        cls = _import_class("ordeal.chaos:ChaosTest")
        assert cls is ChaosTest

    def test_imports_test_class(self):
        cls = _import_class("tests.test_trace_extended:_WithInvariant")
        assert cls is _WithInvariant


# ============================================================================
# _replay_fault_toggle
# ============================================================================


class TestReplayFaultToggle:
    def test_activate_fault(self):
        f = LambdaFault("f1", lambda: None, lambda: None)
        index = {"f1": f}
        _replay_fault_toggle(index, "+f1")
        assert f.active
        f.reset()

    def test_deactivate_fault(self):
        f = LambdaFault("f1", lambda: None, lambda: None)
        f.activate()
        index = {"f1": f}
        _replay_fault_toggle(index, "-f1")
        assert not f.active

    def test_unknown_fault_is_noop(self):
        _replay_fault_toggle({}, "+missing")

    def test_no_prefix_is_noop(self):
        f = LambdaFault("f1", lambda: None, lambda: None)
        _replay_fault_toggle({"f1": f}, "f1")
        assert not f.active


# ============================================================================
# _replay_rule / _replay_steps
# ============================================================================


class TestReplayRule:
    def test_calls_rule_with_params(self):
        machine = _WithInvariant()
        _replay_rule(machine, "tick", {})
        assert machine.counter == 1
        machine.teardown()

    def test_fallback_no_args(self):
        machine = _WithInvariant()
        _replay_rule(machine, "tick", {"unexpected": 42})
        assert machine.counter == 1
        machine.teardown()


class TestReplaySteps:
    def test_reproduces_failure(self):
        steps = [TraceStep(kind="rule", name="tick") for _ in range(3)]
        error = _replay_steps(steps, _WithInvariant)
        assert error is not None
        assert "boom" in str(error)

    def test_no_failure_with_few_steps(self):
        steps = [TraceStep(kind="rule", name="tick") for _ in range(2)]
        assert _replay_steps(steps, _WithInvariant) is None

    def test_fault_toggle_in_steps(self):
        steps = [
            TraceStep(kind="fault_toggle", name="+test_fault"),
            TraceStep(kind="rule", name="increment"),
            TraceStep(kind="rule", name="increment"),
            TraceStep(kind="rule", name="increment"),
            TraceStep(kind="rule", name="check"),
        ]
        error = _replay_steps(steps, _WithFaults)
        assert error is not None
        assert "fault-triggered" in str(error)


# ============================================================================
# _get_invariant_methods
# ============================================================================


class TestGetInvariantMethods:
    def test_finds_invariants(self):
        methods = _get_invariant_methods(_WithInvariant)
        assert len(methods) >= 1

    def test_no_invariants(self):
        class _NoInv(ChaosTest):
            faults = []

            @rule()
            def tick(self):
                pass

        assert len(_get_invariant_methods(_NoInv)) == 0

    def test_caches_result(self):
        m1 = _get_invariant_methods(_WithInvariant)
        m2 = _get_invariant_methods(_WithInvariant)
        assert m1 is m2


# ============================================================================
# generate_tests
# ============================================================================


class TestGenerateTests:
    def test_empty_traces(self):
        assert generate_tests([]) == ""

    def test_single_failure_trace(self):
        trace = Trace(
            run_id=1,
            seed=42,
            test_class="mymod:MyTest",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="rule", name="do_stuff"),
                TraceStep(kind="rule", name="do_stuff", params={"x": 10}),
            ],
            failure=TraceFailure(error_type="ValueError", error_message="boom", step=1),
        )
        code = generate_tests([trace])
        assert "from mymod import MyTest" in code
        assert "def test_fail_r1" in code
        assert "machine.do_stuff()" in code
        assert "machine.do_stuff(x=10)" in code
        assert "machine.teardown()" in code

    def test_non_failure_trace(self):
        trace = Trace(
            run_id=5,
            seed=0,
            test_class="mod:Cls",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="step1")],
        )
        code = generate_tests([trace])
        assert "def test_path_r5" in code

    def test_fault_toggle_steps(self):
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="mod:Cls",
            from_checkpoint=None,
            steps=[
                TraceStep(kind="fault_toggle", name="+my_fault"),
                TraceStep(kind="rule", name="act"),
                TraceStep(kind="fault_toggle", name="-my_fault"),
            ],
        )
        code = generate_tests([trace])
        assert "activate" in code
        assert "deactivate" in code
        assert "my_fault" in code

    def test_multiple_traces(self):
        traces = [
            Trace(
                run_id=i,
                seed=0,
                test_class="mod:Cls",
                from_checkpoint=None,
                steps=[TraceStep(kind="rule", name="tick")],
            )
            for i in range(3)
        ]
        code = generate_tests(traces)
        assert "test_path_r0" in code
        assert "test_path_r1" in code
        assert "test_path_r2" in code

    def test_class_path_override(self):
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="wrong:Class",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="x")],
        )
        code = generate_tests([trace], class_path="correct.mod:Real")
        assert "from correct.mod import Real" in code

    def test_trace_with_edges(self):
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="mod:Cls",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="x")],
            edges_discovered=42,
        )
        code = generate_tests([trace])
        assert "42 new edges" in code


# ============================================================================
# _test_fn_name / _test_docstring / _test_body
# ============================================================================


class TestTestHelpers:
    def test_fn_name_failure(self):
        trace = Trace(
            run_id=7,
            seed=0,
            test_class="x:Y",
            from_checkpoint=None,
            failure=TraceFailure("E", "msg", 0),
        )
        assert _test_fn_name(trace) == "test_fail_r7"

    def test_fn_name_no_failure(self):
        trace = Trace(run_id=3, seed=0, test_class="x:Y", from_checkpoint=None)
        assert _test_fn_name(trace) == "test_path_r3"

    def test_docstring_with_failure(self):
        trace = Trace(
            run_id=1,
            seed=0,
            test_class="x:Y",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="x") for _ in range(5)],
            failure=TraceFailure("TypeError", "bad arg", 4),
        )
        doc = _test_docstring(trace)
        assert "Run 1" in doc
        assert "TypeError" in doc
        assert "5 steps" in doc

    def test_docstring_with_edges(self):
        trace = Trace(
            run_id=2,
            seed=0,
            test_class="x:Y",
            from_checkpoint=None,
            steps=[TraceStep(kind="rule", name="x")],
            edges_discovered=10,
        )
        assert "10 new edges" in _test_docstring(trace)

    def test_body_rule_no_params(self):
        steps = [TraceStep(kind="rule", name="click")]
        trace = Trace(run_id=0, seed=0, test_class="x:Y", from_checkpoint=None, steps=steps)
        assert "machine.click()" in _test_body(trace)

    def test_body_rule_with_params(self):
        steps = [TraceStep(kind="rule", name="set_val", params={"x": 42, "y": "hello"})]
        trace = Trace(run_id=0, seed=0, test_class="x:Y", from_checkpoint=None, steps=steps)
        body = _test_body(trace)
        assert any("set_val(" in s and "x=42" in s for s in body)

    def test_body_filters_data_param(self):
        steps = [TraceStep(kind="rule", name="fn", params={"data": "proxy", "x": 1})]
        trace = Trace(run_id=0, seed=0, test_class="x:Y", from_checkpoint=None, steps=steps)
        joined = " ".join(_test_body(trace))
        assert "data=" not in joined
        assert "x=1" in joined


# ============================================================================
# Trace.from_dict
# ============================================================================


class TestTraceFromDict:
    def test_minimal(self):
        t = Trace.from_dict({"run_id": 1, "seed": 0, "test_class": "x:Y"})
        assert t.run_id == 1
        assert t.steps == []
        assert t.failure is None

    def test_with_failure(self):
        t = Trace.from_dict(
            {
                "run_id": 1,
                "seed": 0,
                "test_class": "x:Y",
                "failure": {"error_type": "E", "error_message": "m", "step": 0},
            }
        )
        assert t.failure.error_type == "E"

    def test_with_steps(self):
        t = Trace.from_dict(
            {
                "run_id": 1,
                "seed": 0,
                "test_class": "x:Y",
                "steps": [
                    {
                        "kind": "rule",
                        "name": "tick",
                        "params": {},
                        "active_faults": [],
                        "edge_count": 0,
                        "timestamp_offset": 0.0,
                    }
                ],
            }
        )
        assert len(t.steps) == 1
        assert t.steps[0].kind == "rule"
