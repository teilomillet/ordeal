"""Tests for extra mutants, LLM-enhanced mutation, concern-driven generation, and hardening.

Validates:
- extra_mutants: primary interface for AI assistants and humans
- Validation pipeline (compile failure, comment-only, duplicate filtering)
- LLM mutant generation via llm= callable (convenience wrapper)
- LLM equivalence detection
- Concern parameter for targeted mutation generation
- Hardening loop: 3-assurance verification (buildable, valid regression, kills mutant)
- Backward compatibility: extra_mutants=None, llm=None preserves existing behavior
"""

from __future__ import annotations

import textwrap

from ordeal.mutations import (
    HardeningResult,
    Mutant,
    MutationResult,
    VerifiedTest,
    _generate_llm_mutants,
    _is_llm_equivalent,
    _parse_llm_response,
    _strip_python_comments,
    _validate_extra_mutants,
    generate_mutants,
)

# ============================================================================
# Target function source for all tests
# ============================================================================

_SAMPLE_SOURCE = textwrap.dedent("""\
    def compute(a: int, b: int) -> int:
        if a < 0:
            return 0
        return a + b
""")

# ============================================================================
# Extra mutant source strings (as an AI assistant would write them)
# ============================================================================

_EXTRA_MUTANT_OFF_BY_ONE = textwrap.dedent("""\
    def compute(a: int, b: int) -> int:
        if a <= 0:
            return 0
        return a + b
""")

_EXTRA_MUTANT_WRONG_OP = textwrap.dedent("""\
    def compute(a: int, b: int) -> int:
        if a < 0:
            return 0
        return a - b
""")

_EXTRA_MUTANT_WRONG_RETURN = textwrap.dedent("""\
    def compute(a: int, b: int) -> int:
        if a < 0:
            return -1
        return a + b
""")

_EXTRA_MUTANT_WRONG_VAR = textwrap.dedent("""\
    def compute(a: int, b: int) -> int:
        if b < 0:
            return 0
        return a + b
""")

_EXTRA_MUTANT_SYNTAX_ERROR = "def compute(a, b):\n    return a +"

_EXTRA_MUTANT_COMMENT_ONLY = textwrap.dedent("""\
    def compute(a: int, b: int) -> int:
        if a < 0:  # check negative
            return 0
        return a + b  # sum
""")


# ============================================================================
# _validate_extra_mutants — the primary validation pipeline
# ============================================================================


class TestValidateExtraMutants:
    def test_accepts_source_strings(self):
        results = _validate_extra_mutants(
            _SAMPLE_SOURCE, [_EXTRA_MUTANT_OFF_BY_ONE, _EXTRA_MUTANT_WRONG_RETURN]
        )
        assert len(results) == 2
        for mutant, tree in results:
            assert isinstance(mutant, Mutant)
            assert mutant.operator == "extra"

    def test_accepts_description_source_tuples(self):
        results = _validate_extra_mutants(
            _SAMPLE_SOURCE,
            [
                ("off-by-one", _EXTRA_MUTANT_OFF_BY_ONE),
                ("wrong return", _EXTRA_MUTANT_WRONG_RETURN),
            ],
        )
        assert len(results) == 2
        assert results[0][0].description == "off-by-one"
        assert results[1][0].description == "wrong return"

    def test_accepts_mixed_strings_and_tuples(self):
        results = _validate_extra_mutants(
            _SAMPLE_SOURCE,
            [
                _EXTRA_MUTANT_OFF_BY_ONE,
                ("wrong var", _EXTRA_MUTANT_WRONG_VAR),
            ],
        )
        assert len(results) == 2

    def test_filters_syntax_errors(self):
        results = _validate_extra_mutants(
            _SAMPLE_SOURCE,
            [_EXTRA_MUTANT_OFF_BY_ONE, _EXTRA_MUTANT_SYNTAX_ERROR],
        )
        assert len(results) == 1

    def test_filters_comment_only_changes(self):
        results = _validate_extra_mutants(_SAMPLE_SOURCE, [_EXTRA_MUTANT_COMMENT_ONLY])
        assert len(results) == 0

    def test_deduplicates_identical_mutants(self):
        results = _validate_extra_mutants(
            _SAMPLE_SOURCE,
            [_EXTRA_MUTANT_OFF_BY_ONE, _EXTRA_MUTANT_OFF_BY_ONE],
        )
        assert len(results) == 1

    def test_empty_list_returns_empty(self):
        results = _validate_extra_mutants(_SAMPLE_SOURCE, [])
        assert results == []

    def test_mutant_has_line_info(self):
        results = _validate_extra_mutants(_SAMPLE_SOURCE, [_EXTRA_MUTANT_OFF_BY_ONE])
        mutant = results[0][0]
        assert mutant.line >= 1
        assert mutant.source_line


# ============================================================================
# generate_mutants — extra_mutants parameter
# ============================================================================


class TestGenerateMutantsWithExtraMutants:
    def test_extra_mutants_none_returns_only_rule_based(self):
        results = generate_mutants(_SAMPLE_SOURCE, operators=["arithmetic"])
        extra = [m for m, _ in results if m.operator == "extra"]
        assert extra == []

    def test_extra_mutants_added_alongside_rule_based(self):
        results = generate_mutants(
            _SAMPLE_SOURCE,
            operators=["arithmetic"],
            extra_mutants=[_EXTRA_MUTANT_WRONG_VAR],
        )
        rule_based = [m for m, _ in results if m.operator not in ("extra", "llm")]
        extras = [m for m, _ in results if m.operator == "extra"]
        assert len(rule_based) > 0
        assert len(extras) > 0

    def test_extra_mutants_deduplicated_against_rule_based(self):
        # The wrong-op mutant (a - b) should be a duplicate of arithmetic + -> -
        results = generate_mutants(
            _SAMPLE_SOURCE,
            operators=["arithmetic"],
            extra_mutants=[
                _EXTRA_MUTANT_WRONG_OP,  # duplicate of arithmetic a+b -> a-b
                _EXTRA_MUTANT_WRONG_VAR,  # unique
            ],
        )
        extras = [m for m, _ in results if m.operator == "extra"]
        # wrong_var should survive; wrong_op may be deduplicated
        assert len(extras) >= 1

    def test_extra_mutants_with_descriptions(self):
        results = generate_mutants(
            _SAMPLE_SOURCE,
            operators=["arithmetic"],
            extra_mutants=[("wrong variable used", _EXTRA_MUTANT_WRONG_VAR)],
        )
        extras = [m for m, _ in results if m.operator == "extra"]
        assert len(extras) == 1
        assert extras[0].description == "wrong variable used"


# ============================================================================
# _strip_python_comments
# ============================================================================


class TestStripPythonComments:
    def test_removes_inline_comments(self):
        result = _strip_python_comments("x = 1  # set x")
        assert result == "x = 1"

    def test_removes_full_line_comments(self):
        result = _strip_python_comments("# this is a comment\nx = 1")
        assert result == "x = 1"

    def test_preserves_non_comment_code(self):
        source = "def f():\n    return 42"
        assert _strip_python_comments(source) == source

    def test_identical_after_strip_detects_comment_only_diff(self):
        original = "x = 1\ny = 2"
        with_comments = "x = 1  # set x\ny = 2  # set y"
        assert _strip_python_comments(original) == _strip_python_comments(with_comments)


# ============================================================================
# Mock LLM responses (for llm= convenience wrapper tests)
# ============================================================================


def _mock_llm_good(prompt: str) -> str:
    """Mock LLM that returns valid, diverse mutants."""
    return textwrap.dedent("""\
        ---MUTANT---
        Description: Off-by-one: changed < to <= in boundary check
        ```python
        def compute(a: int, b: int) -> int:
            if a <= 0:
                return 0
            return a + b
        ```

        ---MUTANT---
        Description: Wrong operator: subtraction instead of addition
        ```python
        def compute(a: int, b: int) -> int:
            if a < 0:
                return 0
            return a - b
        ```

        ---MUTANT---
        Description: Wrong return value on edge case
        ```python
        def compute(a: int, b: int) -> int:
            if a < 0:
                return -1
            return a + b
        ```
    """)


def _mock_llm_with_invalid(prompt: str) -> str:
    """Mock LLM that returns a mix of valid and invalid mutants."""
    return textwrap.dedent("""\
        ---MUTANT---
        Description: Valid mutant — swapped variables
        ```python
        def compute(a: int, b: int) -> int:
            if b < 0:
                return 0
            return a + b
        ```

        ---MUTANT---
        Description: Invalid syntax — will not parse
        ```python
        def compute(a: int, b: int) -> int:
            if a < 0:
                return 0
            return a +
        ```

        ---MUTANT---
        Description: Comment-only change — should be filtered
        ```python
        def compute(a: int, b: int) -> int:
            if a < 0:  # check negative
                return 0
            return a + b  # sum
        ```
    """)


def _mock_llm_with_duplicates(prompt: str) -> str:
    """Mock LLM that returns duplicate mutants."""
    return textwrap.dedent("""\
        ---MUTANT---
        Description: Changed < to <=
        ```python
        def compute(a: int, b: int) -> int:
            if a <= 0:
                return 0
            return a + b
        ```

        ---MUTANT---
        Description: Boundary check: <= instead of <
        ```python
        def compute(a: int, b: int) -> int:
            if a <= 0:
                return 0
            return a + b
        ```
    """)


def _mock_llm_empty(prompt: str) -> str:
    return "I cannot generate mutants for this function."


def _mock_llm_raises(prompt: str) -> str:
    raise ConnectionError("LLM service unavailable")


# ============================================================================
# _parse_llm_response
# ============================================================================


class TestParseLlmResponse:
    def test_parses_valid_response(self):
        response = _mock_llm_good("ignored")
        results = _parse_llm_response(response)
        assert len(results) == 3
        assert all(isinstance(r, tuple) and len(r) == 2 for r in results)
        assert "Off-by-one" in results[0][0]
        assert "def compute" in results[0][1]

    def test_parses_empty_response(self):
        assert _parse_llm_response("") == []

    def test_parses_no_code_blocks(self):
        assert _parse_llm_response("---MUTANT---\nDescription: no code here\n") == []

    def test_extracts_description(self):
        results = _parse_llm_response(_mock_llm_good("ignored"))
        assert "Off-by-one" in results[0][0]
        assert "Wrong operator" in results[1][0]
        assert "Wrong return value" in results[2][0]


# ============================================================================
# _generate_llm_mutants — LLM convenience wrapper
# ============================================================================


class TestGenerateLlmMutants:
    def test_generates_valid_mutants(self):
        results = _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_good)
        assert len(results) == 3
        for mutant, tree in results:
            assert isinstance(mutant, Mutant)
            assert mutant.operator == "llm"

    def test_filters_syntax_errors(self):
        results = _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_with_invalid)
        descriptions = [m.description for m, _ in results]
        assert any("swapped" in d.lower() for d in descriptions)
        assert not any("invalid" in d.lower() for d in descriptions)

    def test_filters_comment_only_changes(self):
        results = _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_with_invalid)
        descriptions = [m.description for m, _ in results]
        assert not any("comment-only" in d.lower() for d in descriptions)

    def test_deduplicates_identical_mutants(self):
        results = _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_with_duplicates)
        assert len(results) == 1

    def test_returns_empty_on_llm_failure(self):
        assert _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_raises) == []

    def test_returns_empty_on_empty_response(self):
        assert _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_empty) == []

    def test_mutant_has_line_info(self):
        for mutant, _ in _generate_llm_mutants(_SAMPLE_SOURCE, _mock_llm_good):
            assert mutant.line >= 1
            assert mutant.source_line


# ============================================================================
# _is_llm_equivalent
# ============================================================================


def _mock_llm_yes(prompt: str) -> str:
    return "YES\nBoth functions return the same values."


def _mock_llm_no(prompt: str) -> str:
    return "NO\nThe mutant returns -1 instead of 0."


def _mock_llm_yes_lower(prompt: str) -> str:
    return "yes\nThey are equivalent."


class TestIsLlmEquivalent:
    def test_yes_response_means_equivalent(self):
        assert _is_llm_equivalent("def f(): return 1", "def f(): return 1", _mock_llm_yes) is True

    def test_no_response_means_not_equivalent(self):
        assert _is_llm_equivalent("def f(): return 0", "def f(): return -1", _mock_llm_no) is False

    def test_llm_failure_defaults_to_not_equivalent(self):
        assert _is_llm_equivalent("def f(): pass", "def f(): pass", _mock_llm_raises) is False

    def test_case_insensitive(self):
        assert _is_llm_equivalent("def f(): pass", "def f(): pass", _mock_llm_yes_lower) is True


# ============================================================================
# generate_mutants — llm= parameter (convenience for automated pipelines)
# ============================================================================


class TestGenerateMutantsWithLlm:
    def test_llm_none_returns_only_rule_based(self):
        results = generate_mutants(_SAMPLE_SOURCE, operators=["arithmetic"])
        non_rule = [m for m, _ in results if m.operator in ("llm", "extra")]
        assert non_rule == []

    def test_llm_adds_mutants_alongside_rule_based(self):
        results = generate_mutants(_SAMPLE_SOURCE, operators=["arithmetic"], llm=_mock_llm_good)
        rule_based = [m for m, _ in results if m.operator not in ("llm", "extra")]
        llm_mutants = [m for m, _ in results if m.operator == "llm"]
        assert len(rule_based) > 0
        assert len(llm_mutants) > 0

    def test_llm_failure_falls_back_gracefully(self):
        results = generate_mutants(_SAMPLE_SOURCE, operators=["arithmetic"], llm=_mock_llm_raises)
        assert len(results) > 0
        assert all(m.operator not in ("llm", "extra") for m, _ in results)


# ============================================================================
# Remediation for extra and llm operators
# ============================================================================


def test_extra_remediation_exists():
    from ordeal.mutations import _REMEDIATION

    assert "extra" in _REMEDIATION
    assert len(_REMEDIATION["extra"]) > 0


def test_llm_remediation_exists():
    from ordeal.mutations import _REMEDIATION

    assert "llm" in _REMEDIATION
    assert len(_REMEDIATION["llm"]) > 0


# ============================================================================
# Concern parameter
# ============================================================================


class TestConcernParameter:
    def test_concern_included_in_llm_prompt(self):
        """Concern text should appear in the prompt sent to the LLM."""
        captured_prompts: list[str] = []

        def capturing_llm(prompt: str) -> str:
            captured_prompts.append(prompt)
            return ""  # empty response — we just want to inspect the prompt

        generate_mutants(
            _SAMPLE_SOURCE,
            operators=["arithmetic"],
            llm=capturing_llm,
            concern="privacy: user IDs must not appear in logs",
        )
        assert len(captured_prompts) == 1
        assert "privacy" in captured_prompts[0]
        assert "user IDs" in captured_prompts[0]
        assert "CONCERN" in captured_prompts[0]

    def test_no_concern_no_concern_block(self):
        """Without concern, the prompt should not contain CONCERN."""
        captured_prompts: list[str] = []

        def capturing_llm(prompt: str) -> str:
            captured_prompts.append(prompt)
            return ""

        generate_mutants(_SAMPLE_SOURCE, operators=["arithmetic"], llm=capturing_llm)
        assert len(captured_prompts) == 1
        assert "CONCERN" not in captured_prompts[0]

    def test_concern_stored_in_mutation_result(self):
        result = MutationResult(
            target="test.func",
            concern="data integrity: sums must be exact",
        )
        assert result.concern == "data integrity: sums must be exact"

    def test_concern_appears_in_summary(self):
        result = MutationResult(
            target="test.func",
            concern="off-by-one errors in pagination",
        )
        summary = result.summary()
        assert "concern: off-by-one errors in pagination" in summary

    def test_no_concern_not_in_summary(self):
        result = MutationResult(target="test.func")
        summary = result.summary()
        assert "concern" not in summary


# ============================================================================
# Hardening loop — 3-assurance verification
# ============================================================================

# For hardening tests we need a real importable function.
# Use ordeal.demo.add which is a simple a + b function.


class TestHardeningResult:
    def test_empty_result(self):
        r = HardeningResult()
        assert r.verified == []
        assert r.invalid == []
        assert r.ineffective == []
        assert r.total_kills == 0

    def test_summary_empty(self):
        r = HardeningResult()
        s = r.summary()
        assert "0 verified" in s

    def test_summary_with_verified(self):
        m = Mutant(operator="arithmetic", description="+ -> -", line=1, col=0)
        vt = VerifiedTest(name="test_add", source="...", kills=[m])
        r = HardeningResult(verified=[vt])
        s = r.summary()
        assert "1 verified" in s
        assert "test_add" in s
        assert "+ -> -" in s

    def test_total_kills_deduplicates(self):
        m = Mutant(operator="arithmetic", description="+ -> -", line=1, col=0)
        vt1 = VerifiedTest(name="test_a", source="...", kills=[m])
        vt2 = VerifiedTest(name="test_b", source="...", kills=[m])
        r = HardeningResult(verified=[vt1, vt2])
        assert r.total_kills == 1  # same mutant killed by two tests


class TestHarden:
    def _make_result_with_survivors(self) -> MutationResult:
        """Build a MutationResult with surviving mutants for ordeal.demo.clamp."""
        result = MutationResult(target="ordeal.demo.clamp")
        # Surviving mutant: min instead of max (wrong clamping direction)
        result.mutants.append(
            Mutant(
                operator="arithmetic",
                description="max -> min",
                line=3,
                col=11,
                killed=False,
                source_line="return min(lo, min(hi, value))",
                _mutant_source="def clamp(value, lo, hi):\n    return min(lo, min(hi, value))\n",
            )
        )
        # Surviving mutant: returns 0 instead of clamped value
        result.mutants.append(
            Mutant(
                operator="return_none",
                description="return -> return 0",
                line=3,
                col=4,
                killed=False,
                source_line="return 0",
                _mutant_source="def clamp(value, lo, hi):\n    return 0\n",
            )
        )
        return result

    def test_harden_with_killing_test(self):
        result = self._make_result_with_survivors()
        hardened = result.harden(
            [
                textwrap.dedent("""\
                def test_clamp_middle():
                    from ordeal.demo import clamp
                    assert clamp(5, 0, 10) == 5
            """),
            ]
        )
        # clamp(5, 0, 10) == 5 on original
        # return 0 mutant → returns 0, not 5 → killed
        assert len(hardened.verified) >= 1
        assert hardened.verified[0].name == "test_clamp_middle"
        assert len(hardened.verified[0].kills) >= 1

    def test_harden_invalid_test_syntax(self):
        result = self._make_result_with_survivors()
        hardened = result.harden(["def test_bad(:\n    pass"])
        assert len(hardened.invalid) == 1
        assert len(hardened.verified) == 0

    def test_harden_test_fails_on_original(self):
        result = self._make_result_with_survivors()
        hardened = result.harden(
            [
                textwrap.dedent("""\
                def test_wrong_expectation():
                    from ordeal.demo import clamp
                    assert clamp(5, 0, 10) == 999
            """),
            ]
        )
        assert len(hardened.invalid) == 1
        assert len(hardened.verified) == 0

    def test_harden_ineffective_test(self):
        result = self._make_result_with_survivors()
        # clamp(0, 0, 10) == 0 for both original and return-0 mutant
        # and for min(lo, min(hi, value)) with lo=0, it also returns 0
        hardened = result.harden(
            [
                textwrap.dedent("""\
                def test_zero():
                    from ordeal.demo import clamp
                    assert clamp(0, 0, 10) == 0
            """),
            ]
        )
        assert len(hardened.ineffective) == 1
        assert len(hardened.verified) == 0

    def test_harden_empty_survivors(self):
        result = MutationResult(target="ordeal.demo.clamp")
        result.mutants.append(
            Mutant(
                operator="arithmetic",
                description="max -> min",
                line=1,
                col=0,
                killed=True,
            )
        )
        hardened = result.harden(["def test_x(): pass"])
        assert len(hardened.verified) == 0

    def test_harden_empty_tests(self):
        result = self._make_result_with_survivors()
        hardened = result.harden([])
        assert len(hardened.verified) == 0

    def test_harden_multiple_tests_multiple_mutants(self):
        result = self._make_result_with_survivors()
        hardened = result.harden(
            [
                textwrap.dedent("""\
                def test_clamp_positive():
                    from ordeal.demo import clamp
                    assert clamp(5, 0, 10) == 5
            """),
                textwrap.dedent("""\
                def test_clamp_low():
                    from ordeal.demo import clamp
                    assert clamp(-5, 0, 10) == 0
            """),
            ]
        )
        assert len(hardened.verified) >= 1
        assert hardened.total_kills >= 1


# ============================================================================
# Mutant._mutant_source populated
# ============================================================================


class TestMutantSourceStored:
    def test_rule_based_mutants_have_source(self):
        results = generate_mutants(_SAMPLE_SOURCE, operators=["arithmetic"])
        for mutant, _ in results:
            assert mutant._mutant_source is not None

    def test_extra_mutants_have_source(self):
        results = _validate_extra_mutants(_SAMPLE_SOURCE, [_EXTRA_MUTANT_OFF_BY_ONE])
        assert results[0][0]._mutant_source is not None


# ============================================================================
# ExplorationState + hardening integration
# ============================================================================


class TestFunctionStateHardening:
    def test_hardened_fields_default_false(self):
        from ordeal.state import FunctionState

        fs = FunctionState(name="f")
        assert fs.hardened is False
        assert fs.hardened_kills == 0

    def test_hardening_boosts_confidence(self):
        from ordeal.state import FunctionState

        # Function with low mutation score: 2/4 killed
        fs = FunctionState(
            name="f",
            mined=True,
            properties=[{"universal": True}],
            mutated=True,
            mutation_score=0.5,
            killed_mutants=2,
            survived_mutants=2,
        )
        conf_before = fs.confidence

        # Harden: 1 more survivor killed
        fs.hardened = True
        fs.hardened_kills = 1
        conf_after = fs.confidence

        # Effective score: (2 + 1) / 4 = 0.75 > 0.5
        assert conf_after > conf_before

    def test_frontier_shows_unhardened_survivors(self):
        from ordeal.state import FunctionState

        fs = FunctionState(
            name="f",
            mutated=True,
            mutation_score=0.5,
            killed_mutants=2,
            survived_mutants=2,
        )
        frontier = fs.frontier
        assert any("unhardened" in g for g in frontier)

    def test_frontier_no_unhardened_when_all_hardened(self):
        from ordeal.state import FunctionState

        fs = FunctionState(
            name="f",
            mutated=True,
            mutation_score=0.5,
            killed_mutants=2,
            survived_mutants=2,
            hardened=True,
            hardened_kills=2,
        )
        frontier = fs.frontier
        # mutation score is still < 0.8, but no unhardened survivors
        assert not any("unhardened" in g for g in frontier)
