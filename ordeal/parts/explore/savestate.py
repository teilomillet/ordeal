from __future__ import annotations
# ruff: noqa
def save_state(self, path: str | Path) -> None:
    """Save exploration state to disk for later resumption.

    Persists the checkpoint corpus, discovered edges, state hashes,
    satisfied properties, and RNG state.  The file is a pickle — not
    intended for cross-version portability, but reliable for
    resume-after-interrupt on the same codebase.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Filter checkpoints to picklable snapshots
    cp_data: list[dict[str, Any]] = []
    for cp in self._checkpoints:
        try:
            # Filter state_dict to only picklable values
            safe_state: dict[str, Any] = {}
            for k, v in cp.snapshot.state_dict.items():
                try:
                    pickle.dumps(v)
                    safe_state[k] = v
                except Exception:
                    pass
            if not safe_state:
                continue
            cp_data.append(
                {
                    "state_dict": safe_state,
                    "fault_active": cp.snapshot.fault_active,
                    "new_edge_count": cp.new_edge_count,
                    "step": cp.step,
                    "run_id": cp.run_id,
                    "energy": cp.energy,
                    "times_selected": cp.times_selected,
                }
            )
        except Exception:
            continue

    payload = {
        "version": 1,
        "total_edges": self._total_edges,
        "total_states": self._total_states,
        "satisfied_properties": self._satisfied_properties,
        "checkpoints": cp_data,
        "rng_state": self.rng.getstate(),
        "seed": self.seed,
    }

    tmp = p.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(payload, f)
    tmp.rename(p)  # atomic on POSIX
save_state.__qualname__ = "Explorer.save_state"
Explorer.save_state = save_state
del save_state
