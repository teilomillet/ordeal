from __future__ import annotations

# ruff: noqa


def _parse_toml_fixtures(raw: dict[str, str]) -> dict[str, Any] | None:
    """Convert TOML fixture strings to Hypothesis strategies.

    Supports: ``"a,b,c"`` → ``sampled_from(["a","b","c"])``.
    """
    import hypothesis.strategies as _st

    if not raw:
        return None
    fixtures: dict[str, Any] = {}
    for name, value in raw.items():
        if isinstance(value, str) and "," in value:
            fixtures[name] = _st.sampled_from(value.split(","))
        elif isinstance(value, str):
            fixtures[name] = _st.just(value)
        else:
            fixtures[name] = _st.just(value)
    return fixtures
