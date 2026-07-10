"""Normal baseline test discovered only by the migration audit stage."""

from tests.migration_fixture_base import increment


def test_increment_baseline() -> None:
    """Protect the baseline behavior used by the audit engine."""
    assert increment(1) == 2
