"""Target module for ordeal.auto tests."""


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"


def divide(a: float, b: float) -> float:
    return a / b  # crashes on b=0


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def no_hints(x, y):
    """Function with no type hints — should be skipped."""
    return x + y


def _private(x: int) -> int:
    """Private function — should be skipped."""
    return x
