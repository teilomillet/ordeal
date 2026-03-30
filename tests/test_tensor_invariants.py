"""Tests for tensor and statistical invariants."""

import numpy as np
import pytest

from ordeal.invariants import (
    mean_bounded,
    orthonormal,
    positive_semi_definite,
    rank_bounded,
    symmetric,
    unit_normalized,
    variance_bounded,
)

# ---------------------------------------------------------------------------
# unit_normalized
# ---------------------------------------------------------------------------


class TestUnitNormalized:
    def test_passes_unit_vector_1d(self):
        v = np.array([1.0, 0.0, 0.0])
        unit_normalized()(v)

    def test_passes_normalized_vector(self):
        v = np.array([1.0, 1.0, 1.0])
        v = v / np.linalg.norm(v)
        unit_normalized()(v)

    def test_fails_unnormalized_1d(self):
        v = np.array([2.0, 0.0, 0.0])
        with pytest.raises(AssertionError, match="norm=2.0"):
            unit_normalized()(v)

    def test_passes_unit_rows_2d(self):
        basis = np.eye(3)[:2]
        unit_normalized()(basis)

    def test_fails_unnormalized_row_2d(self):
        basis = np.array([[1.0, 0.0], [2.0, 0.0]])
        with pytest.raises(AssertionError, match="row 1"):
            unit_normalized()(basis)

    def test_composes_with_finite(self):
        from ordeal.invariants import finite

        check = finite & unit_normalized()
        v = np.array([1.0, 0.0])
        check(v)


# ---------------------------------------------------------------------------
# orthonormal
# ---------------------------------------------------------------------------


class TestOrthonormal:
    def test_passes_identity(self):
        orthonormal()(np.eye(3))

    def test_passes_orthonormal_submatrix(self):
        basis = np.eye(4)[:2]
        orthonormal()(basis)

    def test_fails_non_orthogonal(self):
        basis = np.array([[1.0, 0.0], [1.0, 0.0]])
        with pytest.raises(AssertionError, match="violated"):
            orthonormal()(basis)

    def test_fails_non_unit_rows(self):
        basis = np.array([[2.0, 0.0], [0.0, 2.0]])
        with pytest.raises(AssertionError, match="violated"):
            orthonormal()(basis)

    def test_rejects_1d(self):
        with pytest.raises(AssertionError, match="2-D"):
            orthonormal()(np.array([1.0, 0.0]))


# ---------------------------------------------------------------------------
# symmetric
# ---------------------------------------------------------------------------


class TestSymmetric:
    def test_passes_symmetric(self):
        m = np.array([[1.0, 2.0], [2.0, 3.0]])
        symmetric()(m)

    def test_fails_asymmetric(self):
        m = np.array([[1.0, 2.0], [3.0, 4.0]])
        with pytest.raises(AssertionError, match="violated"):
            symmetric()(m)

    def test_identity_is_symmetric(self):
        symmetric()(np.eye(5))

    def test_rejects_non_square(self):
        with pytest.raises(AssertionError, match="square"):
            symmetric()(np.ones((2, 3)))


# ---------------------------------------------------------------------------
# positive_semi_definite
# ---------------------------------------------------------------------------


class TestPositiveSemiDefinite:
    def test_passes_identity(self):
        positive_semi_definite()(np.eye(3))

    def test_passes_gram_matrix(self):
        a = np.random.randn(3, 5)
        gram = a @ a.T
        positive_semi_definite()(gram)

    def test_fails_negative_eigenvalue(self):
        m = np.array([[-1.0, 0.0], [0.0, 1.0]])
        with pytest.raises(AssertionError, match="eigenvalue"):
            positive_semi_definite()(m)


# ---------------------------------------------------------------------------
# rank_bounded
# ---------------------------------------------------------------------------


class TestRankBounded:
    def test_passes_within_bounds(self):
        rank_bounded(1, 3)(np.eye(3))

    def test_fails_below_min(self):
        with pytest.raises(AssertionError, match="min_rank"):
            rank_bounded(min_rank=2)(np.zeros((3, 3)))

    def test_fails_above_max(self):
        with pytest.raises(AssertionError, match="max_rank"):
            rank_bounded(max_rank=1)(np.eye(3))


# ---------------------------------------------------------------------------
# mean_bounded
# ---------------------------------------------------------------------------


class TestMeanBounded:
    def test_passes_in_range(self):
        mean_bounded(0, 1)([0.3, 0.5, 0.7])

    def test_fails_out_of_range(self):
        with pytest.raises(AssertionError, match="mean"):
            mean_bounded(0, 0.3)([0.3, 0.5, 0.7])

    def test_works_with_numpy(self):
        mean_bounded(0, 1)(np.array([0.1, 0.5, 0.9]))


# ---------------------------------------------------------------------------
# variance_bounded
# ---------------------------------------------------------------------------


class TestVarianceBounded:
    def test_passes_low_variance(self):
        variance_bounded(0, 0.01)([1.0, 1.0, 1.0])

    def test_fails_high_variance(self):
        with pytest.raises(AssertionError, match="variance"):
            variance_bounded(0, 0.01)([0.0, 100.0])

    def test_works_with_numpy(self):
        variance_bounded(0, 1)(np.array([0.5, 0.5, 0.5]))


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestTensorComposition:
    def test_orthonormal_and_unit_normalized(self):
        check = orthonormal() & unit_normalized()
        check(np.eye(3)[:2])

    def test_symmetric_and_psd(self):
        a = np.random.randn(3, 5)
        gram = a @ a.T
        check = symmetric() & positive_semi_definite()
        check(gram)
