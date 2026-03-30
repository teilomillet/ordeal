"""Hypothesis strategies for adversarial / chaos data generation.

Each function returns a ``hypothesis.strategies.SearchStrategy`` that can be
used in ``@rule()``, ``@given()``, or ``data.draw()``.
"""
from __future__ import annotations

import hypothesis.strategies as st


def corrupted_bytes(
    min_size: int = 0,
    max_size: int = 1024,
) -> st.SearchStrategy[bytes]:
    """Bytes biased toward edge cases: empty, all-zero, all-0xFF, truncated."""
    return st.one_of(
        st.binary(min_size=min_size, max_size=max_size),
        st.just(b""),
        st.just(b"\x00" * 128),
        st.just(b"\xff" * 128),
        # Truncated: generate then chop
        st.binary(min_size=max(1, min_size), max_size=max_size).map(
            lambda b: b[: max(1, len(b) // 2)]
        ),
    )


def adversarial_strings(
    min_size: int = 0,
    max_size: int = 256,
) -> st.SearchStrategy[str]:
    """Strings designed to break parsers and validators."""
    nasty = st.sampled_from(
        [
            "",
            "\x00",
            "null",
            "None",
            "undefined",
            "NaN",
            "Infinity",
            "-Infinity",
            "true",
            "false",
            "../../../etc/passwd",
            "<script>alert(1)</script>",
            "'; DROP TABLE users; --",
            "\n\r\n\r",
            "\t" * 50,
            "A" * 10_000,
            "\u0000\u0001\u0002\u001f",
        ]
    )
    return st.one_of(
        st.text(min_size=min_size, max_size=max_size),
        nasty,
    )


def nan_floats() -> st.SearchStrategy[float]:
    """Floats biased toward NaN, Inf, and boundary values."""
    return st.one_of(
        st.just(float("nan")),
        st.just(float("inf")),
        st.just(float("-inf")),
        st.just(0.0),
        st.just(-0.0),
        st.just(1.7976931348623157e308),   # sys.float_info.max
        st.just(5e-324),                    # min positive subnormal
        st.floats(allow_nan=True, allow_infinity=True),
    )


def edge_integers(bits: int = 64) -> st.SearchStrategy[int]:
    """Integers near common boundaries (0, +/-1, min/max for *bits*)."""
    half = bits - 1
    max_val = (1 << half) - 1
    min_val = -(1 << half)
    return st.one_of(
        st.just(0),
        st.just(1),
        st.just(-1),
        st.just(max_val),
        st.just(min_val),
        st.just(max_val + 1),
        st.just(min_val - 1),
        st.integers(),
    )


def mixed_types() -> st.SearchStrategy:
    """Values of mixed types — useful for testing type coercion / validation."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=64),
        st.binary(max_size=64),
        st.lists(st.integers(), max_size=8),
        st.dictionaries(st.text(max_size=8), st.integers(), max_size=4),
    )
