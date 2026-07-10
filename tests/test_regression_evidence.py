"""Tests for semantic bindings on generated witness regressions."""

from __future__ import annotations

from ordeal.regression_evidence import _regression_binding, _regression_binding_matches


def test_binding_ignores_formatting_but_rejects_semantic_changes() -> None:
    original = (
        "from pkg.math import divide\n\n"
        "def test_divide_crash_regression() -> None:\n"
        "    args = {'a': 1, 'b': 0}\n"
        "    divide(**args)\n"
    )
    reformatted = (
        "from pkg.math import divide\n\n\n"
        "def test_divide_crash_regression() -> None:\n"
        "    args={'a':1,'b':0}\n"
        "    divide(**args)\n"
    )
    changed_import = reformatted.replace("from pkg.math", "from pkg.other")
    changed_witness = reformatted.replace("'b':0", "'b':2")

    expected = _regression_binding(original, "test_divide_crash_regression")
    same = _regression_binding(reformatted, "test_divide_crash_regression")
    wrong_import = _regression_binding(changed_import, "test_divide_crash_regression")
    wrong_witness = _regression_binding(changed_witness, "test_divide_crash_regression")

    assert expected is not None
    assert same is not None
    assert wrong_import is not None
    assert wrong_witness is not None
    assert _regression_binding_matches(expected, same)
    assert not _regression_binding_matches(expected, wrong_import)
    assert not _regression_binding_matches(expected, wrong_witness)


def test_binding_rejects_later_shadow_import_and_assignment() -> None:
    original = (
        "from pkg.math import divide\n\n"
        "def test_divide_crash_regression() -> None:\n"
        "    divide(1, 0)\n"
    )
    shadow_import = original + "\nfrom pkg.safe import divide\n"
    shadow_assignment = original + "\ndivide = lambda *args: 0\n"

    expected = _regression_binding(original, "test_divide_crash_regression")
    imported = _regression_binding(shadow_import, "test_divide_crash_regression")
    assigned = _regression_binding(shadow_assignment, "test_divide_crash_regression")

    assert expected is not None
    assert imported is not None
    assert assigned is not None
    assert not _regression_binding_matches(expected, imported)
    assert not _regression_binding_matches(expected, assigned)


def test_binding_allows_unrelated_import_and_test() -> None:
    original = (
        "from pkg.math import divide\n\n"
        "def test_divide_crash_regression() -> None:\n"
        "    divide(1, 0)\n"
    )
    extended = (
        "import decimal\n" + original + "\ndef test_unrelated() -> None:\n"
        "    assert decimal.Decimal('1') == 1\n"
    )

    expected = _regression_binding(original, "test_divide_crash_regression")
    observed = _regression_binding(extended, "test_divide_crash_regression")

    assert expected is not None
    assert observed is not None
    assert _regression_binding_matches(expected, observed)
