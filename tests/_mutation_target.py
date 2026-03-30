"""Simple module used as a mutation testing target."""


def add(a: int, b: int) -> int:
    return a + b


def is_positive(x: int) -> bool:
    if x > 0:
        return True
    return False


def clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
