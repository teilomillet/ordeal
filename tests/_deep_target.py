"""Deep state machine that benefits from checkpoint sharing.

The bug is only reachable through a 4-phase sequence:
  Phase 1: accumulate() 5+ times (counter >= 5)
  Phase 2: pivot() transitions to "pivoted" state
  Phase 3: climb() 3+ times while pivoted (altitude >= 3)
  Phase 4: strike() triggers the deep bug

Random exploration almost never finds this because:
  - pivot() only works after 5+ accumulates
  - climb() only works in pivoted state
  - strike() only triggers at altitude >= 3

Checkpoint sharing helps because:
  - Worker A might reach phase 2 (pivoted state) and publish
  - Worker B loads that checkpoint and explores climb/strike from there
  - Without sharing, each worker independently needs the full sequence
"""


class DeepService:
    def __init__(self) -> None:
        self.counter = 0
        self.state = "idle"
        self.altitude = 0
        self.log: list[str] = []
        self.bug_triggered = False

    def accumulate(self) -> None:
        self.counter += 1
        self.log.append(f"acc({self.counter})")
        if self.counter >= 5 and self.state == "idle":
            self.state = "ready"

    def pivot(self) -> None:
        self.log.append(f"pivot({self.state})")
        if self.state == "ready":
            self.state = "pivoted"
            # Reset counter so we don't skip ahead
            self.counter = 0

    def climb(self) -> None:
        self.log.append(f"climb({self.state},{self.altitude})")
        if self.state == "pivoted":
            self.altitude += 1
        # In wrong state, climb does nothing (coverage dead-end)

    def strike(self) -> None:
        self.log.append(f"strike({self.state},{self.altitude})")
        if self.state == "pivoted" and self.altitude >= 3:
            self.bug_triggered = True
            self.state = "deep_bug"

    def reset_state(self) -> None:
        """Partial reset — keeps counter but resets state."""
        self.log.append("reset")
        self.state = "idle"
        self.altitude = 0

    def noop(self) -> None:
        """No-op — dilutes the action space."""
        self.log.append("noop")

    @property
    def phase(self) -> int:
        if self.state == "deep_bug":
            return 4
        if self.state == "pivoted" and self.altitude >= 3:
            return 3
        if self.state == "pivoted":
            return 2
        if self.state == "ready":
            return 1
        return 0
