"""Small independent targets for audit worker determinism tests."""


def transform_0(value: int) -> int:
    return value + 1


def transform_1(value: int) -> int:
    return value - 2


def transform_2(value: int) -> int:
    return value * 3


def transform_3(value: int) -> int:
    return value + 4


def transform_4(value: int) -> int:
    return value - 5


def transform_5(value: int) -> int:
    return value * 6


def transform_6(value: int) -> int:
    return value + 7


def transform_7(value: int) -> int:
    return value - 8
