"""Hard ablation target: deep phase-gated edges, moderate noise checkpoints.

The design creates conditions where energy scheduling demonstrably wins:

1. **Noise creates real checkpoints**: Each noise method has 3-5 branches
   that fire on different call counts. First few calls discover edges and
   save checkpoints. But those checkpoints are dead-ends — further
   exploration from them yields nothing new.

2. **Phase transitions unlock deep chains**: Each phase gate creates 15-25
   unique edges from a deep call tree. These are the productive checkpoints
   — exploring from them can reach the next phase.

3. **Energy decays noise, rewards phases**: After branching from a noise
   checkpoint and finding nothing new, energy decays. After branching from
   a phase-boundary checkpoint and finding next-phase edges, energy grows.

4. **Tight budget**: With 200 runs and 17 rules, uniform wastes ~80% of
   checkpoint-based runs on noise dead-ends. Energy concentrates on the
   ~20% of checkpoints that are actually productive.
"""


class HardService:
    """5-phase gated state machine with productive and dead-end checkpoints."""

    def __init__(self) -> None:
        self.phase = 0
        self.gate_progress = 0
        self._nc = 0  # noise call counters
        self._nt = False
        self._na = 0
        self._ny = 0
        self._nf = False
        self._np = 0
        self._ns_a = 0
        self._ns_b = 0
        self._nm = 0
        self._nk = 0
        self._nr = 1
        self._nw = 0

    # -- Critical path: 5 phases, 5 steps each = 25 total --

    def advance_a(self) -> None:
        if self.phase == 0:
            self.gate_progress += 1
            if self.gate_progress >= 5:
                self.phase = 1
                self.gate_progress = 0
                self._enter_phase_1()

    def advance_b(self) -> None:
        if self.phase == 1:
            self.gate_progress += 1
            if self.gate_progress >= 5:
                self.phase = 2
                self.gate_progress = 0
                self._enter_phase_2()

    def advance_c(self) -> None:
        if self.phase == 2:
            self.gate_progress += 1
            if self.gate_progress >= 5:
                self.phase = 3
                self.gate_progress = 0
                self._enter_phase_3()

    def advance_d(self) -> None:
        if self.phase == 3:
            self.gate_progress += 1
            if self.gate_progress >= 5:
                self.phase = 4
                self.gate_progress = 0
                self._enter_phase_4()

    def advance_e(self) -> None:
        if self.phase == 4:
            self.gate_progress += 1
            if self.gate_progress >= 5:
                self.phase = 5
                self.gate_progress = 0
                self._enter_phase_5()

    # -- Phase call chains: each progressively deeper --

    def _enter_phase_1(self) -> None:
        self._p1_root()

    def _p1_root(self) -> None:
        self._p1_left()
        self._p1_right()

    def _p1_left(self) -> None:
        self._p1_ll()

    def _p1_right(self) -> None:
        self._p1_rl()

    def _p1_ll(self) -> None:
        pass

    def _p1_rl(self) -> None:
        pass

    def _enter_phase_2(self) -> None:
        self._p2_root()

    def _p2_root(self) -> None:
        self._p2_a()
        self._p2_b()
        self._p2_c()

    def _p2_a(self) -> None:
        self._p2_a1()

    def _p2_b(self) -> None:
        self._p2_b1()
        if self.phase >= 2:
            self._p2_b2()

    def _p2_c(self) -> None:
        self._p2_c1()

    def _p2_a1(self) -> None:
        pass

    def _p2_b1(self) -> None:
        pass

    def _p2_b2(self) -> None:
        pass

    def _p2_c1(self) -> None:
        pass

    def _enter_phase_3(self) -> None:
        self._p3_root()

    def _p3_root(self) -> None:
        self._p3_a()
        self._p3_b()
        self._p3_c()
        self._p3_d()

    def _p3_a(self) -> None:
        self._p3_a1()
        self._p3_a2()

    def _p3_b(self) -> None:
        self._p3_b1()

    def _p3_c(self) -> None:
        self._p3_c1()
        if self.phase >= 3:
            self._p3_c2()

    def _p3_d(self) -> None:
        self._p3_d1()
        self._p3_d2()

    def _p3_a1(self) -> None:
        pass

    def _p3_a2(self) -> None:
        pass

    def _p3_b1(self) -> None:
        pass

    def _p3_c1(self) -> None:
        pass

    def _p3_c2(self) -> None:
        pass

    def _p3_d1(self) -> None:
        pass

    def _p3_d2(self) -> None:
        pass

    def _enter_phase_4(self) -> None:
        self._p4_root()

    def _p4_root(self) -> None:
        self._p4_a()
        self._p4_b()
        self._p4_c()
        self._p4_d()
        self._p4_e()

    def _p4_a(self) -> None:
        self._p4_a1()
        self._p4_a2()

    def _p4_b(self) -> None:
        self._p4_b1()
        self._p4_b2()

    def _p4_c(self) -> None:
        self._p4_c1()

    def _p4_d(self) -> None:
        self._p4_d1()
        if self.phase >= 4:
            self._p4_d2()

    def _p4_e(self) -> None:
        self._p4_e1()

    def _p4_a1(self) -> None:
        pass

    def _p4_a2(self) -> None:
        pass

    def _p4_b1(self) -> None:
        pass

    def _p4_b2(self) -> None:
        pass

    def _p4_c1(self) -> None:
        pass

    def _p4_d1(self) -> None:
        pass

    def _p4_d2(self) -> None:
        pass

    def _p4_e1(self) -> None:
        pass

    def _enter_phase_5(self) -> None:
        self._p5_root()

    def _p5_root(self) -> None:
        self._p5_a()
        self._p5_b()
        self._p5_c()
        self._p5_d()
        self._p5_e()
        self._p5_f()

    def _p5_a(self) -> None:
        self._p5_a1()
        self._p5_a2()

    def _p5_b(self) -> None:
        self._p5_b1()
        self._p5_b2()

    def _p5_c(self) -> None:
        self._p5_c1()
        self._p5_c2()

    def _p5_d(self) -> None:
        self._p5_d1()

    def _p5_e(self) -> None:
        self._p5_e1()
        if self.phase >= 5:
            self._p5_e2()

    def _p5_f(self) -> None:
        self._p5_f1()
        self._p5_f2()

    def _p5_a1(self) -> None:
        pass

    def _p5_a2(self) -> None:
        pass

    def _p5_b1(self) -> None:
        pass

    def _p5_b2(self) -> None:
        pass

    def _p5_c1(self) -> None:
        pass

    def _p5_c2(self) -> None:
        pass

    def _p5_d1(self) -> None:
        pass

    def _p5_e1(self) -> None:
        pass

    def _p5_e2(self) -> None:
        pass

    def _p5_f1(self) -> None:
        pass

    def _p5_f2(self) -> None:
        pass

    # -- Noise methods: moderate branches that create dead-end checkpoints --
    # Each method has branches gated by call count. First few calls
    # create new edges (checkpoint-worthy), but those states lead nowhere.

    def noise_counter(self) -> None:
        self._nc += 1
        if self._nc == 2:
            self._nc_branch_a()
        elif self._nc == 5:
            self._nc_branch_b()
        elif self._nc == 10:
            self._nc_branch_c()

    def noise_toggle(self) -> None:
        self._nt = not self._nt
        if self._nt:
            self._nt_on()
        else:
            self._nt_off()

    def noise_accumulate(self) -> None:
        self._na += 1
        if self._na == 3:
            self._na_branch_a()
        elif self._na == 7:
            self._na_branch_b()

    def noise_cycle(self) -> None:
        self._ny = (self._ny + 1) % 4
        if self._ny == 0:
            self._ny_zero()
        elif self._ny == 2:
            self._ny_two()

    def noise_flag(self) -> None:
        was = self._nf
        self._nf = True
        if not was:
            self._nf_first()

    def noise_reset(self) -> None:
        was = self._nf
        self._nf = False
        if was:
            self._nf_cleared()

    def noise_pulse(self) -> None:
        self._np += 1
        if self._np == 4:
            self._np_branch_a()
        elif self._np == 8:
            self._np_branch_b()

    def noise_swap(self) -> None:
        self._ns_a, self._ns_b = self._ns_b + 1, self._ns_a
        if self._ns_a > self._ns_b:
            self._nsw_gt()

    def noise_modulo(self) -> None:
        self._nm = (self._nm + 1) % 3
        if self._nm == 0:
            self._nm_zero()

    def noise_cascade(self) -> None:
        self._nk += 1
        if self._nk == 3:
            self._nk_branch_a()
        elif self._nk == 6:
            self._nk_branch_b()

    def noise_mirror(self) -> None:
        self._nr = -self._nr
        if self._nr > 0:
            self._nr_pos()
        else:
            self._nr_neg()

    def noise_wave(self) -> None:
        self._nw = (self._nw + 3) % 11
        if self._nw < 3:
            self._nw_low()
        elif self._nw > 8:
            self._nw_high()

    # -- Noise branch leaves --

    def _nc_branch_a(self) -> None:
        pass

    def _nc_branch_b(self) -> None:
        pass

    def _nc_branch_c(self) -> None:
        pass

    def _nt_on(self) -> None:
        pass

    def _nt_off(self) -> None:
        pass

    def _na_branch_a(self) -> None:
        pass

    def _na_branch_b(self) -> None:
        pass

    def _ny_zero(self) -> None:
        pass

    def _ny_two(self) -> None:
        pass

    def _nf_first(self) -> None:
        pass

    def _nf_cleared(self) -> None:
        pass

    def _np_branch_a(self) -> None:
        pass

    def _np_branch_b(self) -> None:
        pass

    def _nsw_gt(self) -> None:
        pass

    def _nm_zero(self) -> None:
        pass

    def _nk_branch_a(self) -> None:
        pass

    def _nk_branch_b(self) -> None:
        pass

    def _nr_pos(self) -> None:
        pass

    def _nr_neg(self) -> None:
        pass

    def _nw_low(self) -> None:
        pass

    def _nw_high(self) -> None:
        pass

    def reset(self) -> None:
        self.__init__()
