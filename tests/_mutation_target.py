"""Simple module used as a mutation testing target."""


def add(a, b):
    return a + b


def is_positive(x):
    if x > 0:
        return True
    return False


def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
