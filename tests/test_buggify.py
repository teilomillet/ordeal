"""Tests for ordeal.buggify — FoundationDB-style inline fault injection."""
from ordeal.buggify import activate, buggify, buggify_value, deactivate, is_active, set_seed


class TestBuggify:
    def teardown_method(self):
        deactivate()

    def test_inactive_by_default(self):
        assert not is_active()
        assert buggify() is False

    def test_activate_deactivate(self):
        activate()
        assert is_active()
        deactivate()
        assert not is_active()

    def test_buggify_returns_false_when_inactive(self):
        # Even with probability=1.0, inactive means False
        assert buggify(probability=1.0) is False

    def test_buggify_always_true_with_prob_1(self):
        activate(probability=1.0)
        assert buggify() is True

    def test_buggify_always_false_with_prob_0(self):
        activate(probability=0.0)
        assert buggify() is False

    def test_buggify_probability_override(self):
        activate(probability=0.0)
        assert buggify(probability=1.0) is True

    def test_seed_determinism(self):
        activate()
        set_seed(42)
        results_a = [buggify() for _ in range(100)]
        set_seed(42)
        results_b = [buggify() for _ in range(100)]
        assert results_a == results_b

    def test_different_seeds_differ(self):
        activate()
        set_seed(42)
        results_a = [buggify() for _ in range(100)]
        set_seed(99)
        results_b = [buggify() for _ in range(100)]
        assert results_a != results_b


class TestBuggifyValue:
    def teardown_method(self):
        deactivate()

    def test_returns_normal_when_inactive(self):
        assert buggify_value("normal", "faulty") == "normal"

    def test_returns_faulty_with_prob_1(self):
        activate(probability=1.0)
        assert buggify_value("normal", "faulty") == "faulty"

    def test_returns_normal_with_prob_0(self):
        activate(probability=0.0)
        assert buggify_value("normal", "faulty") == "normal"
