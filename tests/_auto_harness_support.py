"""Importable harness hooks for exact stateful replay tests."""

from __future__ import annotations

from tests._auto_harness_target import ReplayBox


def make_replay_box() -> ReplayBox:
    """Create the target object."""
    return ReplayBox("ready")


def prepare_replay_box(instance: ReplayBox) -> ReplayBox:
    """Prepare the object before each method invocation."""
    instance.ready = True
    return instance


def make_replay_box_state(instance: ReplayBox) -> dict[str, str]:
    """Create the runtime state omitted from generated method inputs."""
    return {"token": instance.prefix}


def close_replay_box(instance: ReplayBox) -> None:
    """Mark the object closed after each invocation."""
    instance.ready = False
