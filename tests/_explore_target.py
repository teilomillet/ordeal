"""Target module for Explorer coverage tests.

Has branches that are only reachable via specific multi-step sequences —
exactly the class of code paths the Explorer is designed to find.
"""


class BranchyService:
    """Service with a deep state machine.

    The ``_deep_path`` method is only reachable if:
    1. ``step_a()`` is called 3+ times (counter > 3)
    2. Then ``step_b()`` is called (state transitions a -> ab)
    3. Then ``step_c()`` is called (triggers the deep path)

    Random testing rarely reaches it. Coverage-guided exploration
    checkpoints after discovering state="ab" and explores from there.
    """

    def __init__(self) -> None:
        self.state = "init"
        self.counter = 0
        self.deep_reached = False

    def step_a(self) -> None:
        self.state = "a"
        self.counter += 1

    def step_b(self) -> None:
        if self.state == "a":
            self.state = "ab"
        else:
            self.state = "b"

    def step_c(self) -> None:
        if self.state == "ab" and self.counter > 3:
            self.state = "deep"
            self._deep_path()
        else:
            self.state = "c"

    def _deep_path(self) -> None:
        self.deep_reached = True
        if self.counter > 10:
            self._very_deep()

    def _very_deep(self) -> None:
        # Even harder to reach
        pass

    def reset(self) -> None:
        self.state = "init"
        self.counter = 0
        self.deep_reached = False
