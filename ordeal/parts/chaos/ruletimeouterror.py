from __future__ import annotations
# ruff: noqa
from contextlib import contextmanager
import functools
import signal
import threading
from typing import TYPE_CHECKING, Any, ClassVar
import hypothesis.strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule
from ordeal.faults import Fault
if TYPE_CHECKING:
    from ordeal.explore import CoverageCollector
class RuleTimeoutError(Exception):
    """A ChaosTest rule exceeded its ``rule_timeout``.

    This usually means a fault (buggify / PatchFault) caused the code
    under test to block indefinitely — a real resilience finding.
    """
def _can_use_sigalrm() -> bool:
    """Return True if SIGALRM-based timeouts are available."""
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return False
    try:
        return threading.current_thread() is threading.main_thread()
    except RuntimeError:
        return False


_rule_timeout_context_depth = 0
_active_rule_timeout: tuple[str, float] | None = None


def _timeout_error(rule_name: str, timeout: float) -> RuleTimeoutError:
    """Build the stable timeout error used by both timeout paths."""
    return RuleTimeoutError(
        f"ChaosTest rule {rule_name!r} timed out after {timeout}s. "
        f"A fault likely caused the code under test to block "
        f"(buggify-induced hang, slow I/O, unresponsive inference, etc.). "
        f"This is a real resilience finding — the code has no timeout. "
        f"Set rule_timeout = 0 to disable."
    )


def _context_timeout_handler(signum: int, frame: Any) -> None:
    """Raise for the rule currently armed by ``rule_timeout_context``."""
    rule_name, timeout = _active_rule_timeout or ("<unknown>", 0.0)
    raise _timeout_error(rule_name, timeout)


@contextmanager
def rule_timeout_context() -> Any:
    """Install SIGALRM once while Explorer executes a batch of rules.

    Individual wrapped rules still arm and disarm their timers. Standalone
    Hypothesis runs retain their per-rule handler setup for compatibility.
    """
    global _rule_timeout_context_depth
    if not _can_use_sigalrm():
        yield
        return
    if _rule_timeout_context_depth:
        _rule_timeout_context_depth += 1
        try:
            yield
        finally:
            _rule_timeout_context_depth -= 1
        return

    old_handler = signal.signal(signal.SIGALRM, _context_timeout_handler)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    _rule_timeout_context_depth = 1
    try:
        yield
    finally:
        _rule_timeout_context_depth = 0
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] or old_timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)
def _wrap_rule_with_timeout(fn: Any, timeout: float) -> Any:
    """Wrap *fn* so it raises ``RuleTimeoutError`` after *timeout* seconds."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if (
            _rule_timeout_context_depth
            and threading.current_thread() is threading.main_thread()
        ):
            global _active_rule_timeout
            previous_timeout = _active_rule_timeout
            _active_rule_timeout = (fn.__name__, timeout)
            signal.setitimer(signal.ITIMER_REAL, timeout)
            try:
                return fn(*args, **kwargs)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                _active_rule_timeout = previous_timeout

        if not _can_use_sigalrm():
            return fn(*args, **kwargs)

        def _handler(signum: int, frame: Any) -> None:
            raise _timeout_error(fn.__name__, timeout)

        old_handler = signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return fn(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    return wrapper
# ---------------------------------------------------------------------------
# Adaptive fault scheduling constants
#
# These govern how fault energy evolves based on coverage feedback.
# The mechanism is analogous to AFL++'s MOpt (mutator optimisation):
# operators that produce new coverage get higher selection probability,
# operators that plateau decay toward a minimum floor.
#
#   energy_new  = energy_old * _FAULT_ENERGY_REWARD   (on new edges)
#   energy_new  = energy_old * _FAULT_ENERGY_DECAY    (no new edges)
#   energy      = max(energy, _FAULT_ENERGY_MIN)      (never fully dead)
#
# The minimum floor ensures every fault retains a small chance of being
# selected — important because a fault that was useless early may become
# critical after the system reaches a different state.
# ---------------------------------------------------------------------------
_FAULT_ENERGY_REWARD: float = 1.5
_FAULT_ENERGY_DECAY: float = 0.9
_FAULT_ENERGY_MIN: float = 0.1
