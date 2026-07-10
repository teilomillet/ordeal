from __future__ import annotations
# ruff: noqa
def _pairwise_fault_config(self, n_rules: int, all_fault_names: list[str]) -> SwarmConfig | None:
    """Bias one config toward an uncovered or under-covered fault pair."""
    missing_pairs = self._uncovered_fault_pairs(all_fault_names)
    candidate_pairs = missing_pairs
    if not candidate_pairs and len(all_fault_names) >= 2:
        all_pairs = list(combinations(sorted(dict.fromkeys(all_fault_names)), 2))
        if not all_pairs:
            return None
        min_hits = min(self._fault_pair_hits.get(pair, 0) for pair in all_pairs)
        candidate_pairs = [
            pair for pair in all_pairs if self._fault_pair_hits.get(pair, 0) == min_hits
        ]
    if not candidate_pairs:
        return None

    pair = self.rng.choice(candidate_pairs)
    if n_rules > 1:
        rule_mask = self.rng.randint(1, (1 << n_rules) - 1)
        active_rules = [self._rules[i].name for i in range(n_rules) if rule_mask & (1 << i)]
    else:
        active_rules = [self._rules[0].name]

    active_faults = list(pair)
    for fname in all_fault_names:
        if fname in pair:
            continue
        if self.rng.random() < 0.35:
            active_faults.append(fname)

    cfg = SwarmConfig(active_rules=active_rules, active_faults=sorted(active_faults))
    key = cfg.key
    if key not in self._swarm_configs:
        self._swarm_configs[key] = cfg
    self._swarm_configs[key].times_used += 1
    return self._swarm_configs[key]
_pairwise_fault_config.__qualname__ = "Explorer._pairwise_fault_config"
Explorer._pairwise_fault_config = _pairwise_fault_config
del _pairwise_fault_config
