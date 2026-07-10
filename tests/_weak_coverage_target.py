"""Fixture whose weak tests execute every line without protecting behavior."""


def classify(value: int) -> str:
    """Return a coarse sign label."""
    if value > 0:
        return "positive"
    return "nonpositive"
