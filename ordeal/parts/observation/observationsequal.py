from __future__ import annotations
# ruff: noqa
def observations_equal(left: CanonicalObservation, right: CanonicalObservation) -> bool:
    """Compare structural observations without candidate equality methods."""
    return left.signature == right.signature and left.payload == right.payload
def exact_replay_match(
    expected: CanonicalObservation,
    observed: CanonicalObservation,
    *,
    recorded_expected_signature: str,
) -> bool:
    """Require the recorded, expected, and observed signatures to agree exactly."""
    if recorded_expected_signature != expected.signature:
        return False
    return (
        observed.signature == recorded_expected_signature and observed.payload == expected.payload
    )
def isolated_deepcopy(
    value: Any,
    *,
    label: str,
    disjoint_from: tuple[Any, ...] = (),
) -> Any:
    """Deep-copy one value and reject structural drift or retained mutable aliases."""
    source = observe(value, label=label)
    try:
        cloned = copy.deepcopy(value)
    except Exception as exc:
        raise ObservationError(
            f"could not reconstruct {label}: {type(exc).__name__}: {exc}"
        ) from exc
    clone = observe(cloned, label=f"cloned {label}")
    if not observations_equal(source, clone):
        raise ObservationError(f"could not reconstruct {label}: deepcopy changed structure")

    comparisons = ((value, source),) + tuple(
        (other, observe(other, label=f"isolation boundary for {label}")) for other in disjoint_from
    )
    for _other, other_observation in comparisons:
        if clone._mutable_ids & other_observation._mutable_ids:
            raise ObservationError(
                f"could not reconstruct {label}: deepcopy retained a shared mutable alias"
            )
    return cloned
