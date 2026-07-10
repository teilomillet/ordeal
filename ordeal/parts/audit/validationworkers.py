from __future__ import annotations

# ruff: noqa

import multiprocessing
import pickle
import random
from concurrent.futures import ProcessPoolExecutor


@dataclass(frozen=True, slots=True)
class _AuditValidationTask:
    """Serializable, immutable input for one isolated validation target."""

    target_path: str
    max_examples: int
    validation_mode: AuditValidationMode
    seed: int
    disk_mutation: bool
    payload: bytes


@dataclass(frozen=True, slots=True)
class _AuditMutantEvidence:
    """Immutable evidence for one mutant returned by a validation worker."""

    mutant_id: str
    description: str
    location: str
    source_line: str
    remediation: str
    killed: bool
    killers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AuditValidationEvidence:
    """Immutable worker output merged into ``ModuleAudit`` by the parent."""

    target_path: str
    seed: int
    killed: int
    total: int
    view_json: str
    mutants: tuple[_AuditMutantEvidence, ...]
    stub: str


_AUDIT_VALIDATION_SOURCE_LOCK: Any | None = None


def _audit_validation_seed(target_path: str) -> int:
    """Return a stable, target-specific 32-bit validation seed."""
    digest = hashlib.sha256(f"ordeal-audit-validation-v1\0{target_path}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _audit_mutant_id(target_path: str, mutant: Any) -> str:
    """Return a stable content ID for one target mutant."""
    identity = {
        "target": target_path,
        "operator": str(mutant.operator),
        "description": str(mutant.description),
        "line": int(mutant.line),
        "col": int(mutant.col),
        "qualname": str(mutant.qualname or ""),
        "source_line": str(mutant.source_line or ""),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _freeze_validation_result(
    target_path: str,
    seed: int,
    mutation_result: Any,
) -> _AuditValidationEvidence:
    """Convert mutable mutation output to deterministic immutable evidence."""
    mutants: list[_AuditMutantEvidence] = []
    for mutant in mutation_result.mutants:
        property_killers = tuple(
            f"property:{name}" for name in mutant.metadata.get("killed_by_properties", [])
        )
        killers = property_killers or ((str(mutant.killed_by),) if mutant.killed_by else ())
        mutants.append(
            _AuditMutantEvidence(
                mutant_id=_audit_mutant_id(target_path, mutant),
                description=str(mutant.description),
                location=str(mutant.location),
                source_line=str(mutant.source_line or ""),
                remediation=str(mutant.remediation),
                killed=bool(mutant.killed),
                killers=killers,
            )
        )
    frozen_mutants = tuple(sorted(mutants, key=lambda item: item.mutant_id))
    view = dict(mutation_result.epistemic_view())
    view["validation_seed"] = seed
    view["mutant_ids"] = [item.mutant_id for item in frozen_mutants]
    view["killed_mutant_ids"] = [item.mutant_id for item in frozen_mutants if item.killed]
    return _AuditValidationEvidence(
        target_path=target_path,
        seed=seed,
        killed=int(mutation_result.killed),
        total=int(mutation_result.total),
        view_json=json.dumps(view, sort_keys=True, separators=(",", ":"), default=repr),
        mutants=frozen_mutants,
        stub=str(mutation_result.generate_test_stubs() or ""),
    )


def _initialize_audit_validation_worker(
    source_lock: Any,
    ready_queue: Any,
    start_event: Any,
) -> None:
    """Initialize one worker and wait until every peer imported clean source."""
    global _AUDIT_VALIDATION_SOURCE_LOCK
    _AUDIT_VALIDATION_SOURCE_LOCK = source_lock
    ready_queue.put(1)
    start_event.wait()


def _run_audit_validation(
    target_path: str,
    mine_result: MineResult,
    contract_context: dict[str, Any],
    *,
    max_examples: int,
    validation_mode: AuditValidationMode,
    seed: int,
    disk_mutation: bool,
) -> _AuditValidationEvidence:
    """Validate one target in the current process with deterministic RNG state."""
    from ordeal.mutations import validate_mined_properties

    random_state = random.getstate()
    random.seed(seed)
    numpy_module = None
    numpy_state = None
    try:
        try:
            import numpy as np

            numpy_module = np
            numpy_state = np.random.get_state()
            np.random.seed(seed)
        except ImportError:
            pass

        source_guard = (
            _AUDIT_VALIDATION_SOURCE_LOCK
            if disk_mutation and _AUDIT_VALIDATION_SOURCE_LOCK is not None
            else contextlib.nullcontext()
        )
        with source_guard:
            mutation_result = validate_mined_properties(
                target_path,
                max_examples=max_examples,
                preset="standard",
                mine_result=mine_result,
                validation_mode=validation_mode,
                contract_context=contract_context,
                _disk_mutation=disk_mutation,
            )
        return _freeze_validation_result(target_path, seed, mutation_result)
    finally:
        random.setstate(random_state)
        if numpy_module is not None and numpy_state is not None:
            numpy_module.random.set_state(numpy_state)


def _run_audit_validation_task(task: _AuditValidationTask) -> _AuditValidationEvidence:
    """Deserialize and run one process-isolated validation task."""
    mine_result, contract_context = pickle.loads(task.payload)
    return _run_audit_validation(
        task.target_path,
        mine_result,
        contract_context,
        max_examples=task.max_examples,
        validation_mode=task.validation_mode,
        seed=task.seed,
        disk_mutation=task.disk_mutation,
    )


def _audit_process_context() -> multiprocessing.context.BaseContext:
    """Return a clean process context that does not clone parent mutation state."""
    methods = multiprocessing.get_all_start_methods()
    method = "forkserver" if "forkserver" in methods else "spawn"
    return multiprocessing.get_context(method)


def _validate_audit_targets(
    targets: Sequence[tuple[str, MineResult, dict[str, Any]]],
    *,
    max_examples: int,
    workers: int,
    validation_mode: AuditValidationMode,
    warnings: list[str],
) -> list[_AuditValidationEvidence]:
    """Validate targets serially or in isolated processes and preserve target order."""
    from ordeal.mutations import _needs_disk_mutation

    worker_count = max(1, workers)
    if worker_count == 1 or len(targets) <= 1:
        evidence: list[_AuditValidationEvidence] = []
        for target_path, mine_result, contract_context in targets:
            try:
                evidence.append(
                    _run_audit_validation(
                        target_path,
                        mine_result,
                        contract_context,
                        max_examples=max_examples,
                        validation_mode=validation_mode,
                        seed=_audit_validation_seed(target_path),
                        disk_mutation=_needs_disk_mutation(target_path),
                    )
                )
            except Exception as exc:
                warnings.append(
                    f"mutation validation failed for {target_path}: {type(exc).__name__}: {exc}"
                )
        return evidence

    tasks: list[_AuditValidationTask] = []
    for target_path, mine_result, contract_context in targets:
        try:
            payload = pickle.dumps(
                (mine_result, dict(contract_context)),
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        except Exception as exc:
            warnings.append(
                f"mutation validation could not serialize {target_path}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        tasks.append(
            _AuditValidationTask(
                target_path=target_path,
                max_examples=max_examples,
                validation_mode=validation_mode,
                seed=_audit_validation_seed(target_path),
                disk_mutation=_needs_disk_mutation(target_path),
                payload=payload,
            )
        )

    if not tasks:
        return []

    evidence = []
    try:
        context = _audit_process_context()
        process_count = min(worker_count, len(tasks))
        source_lock = context.Lock()
        ready_queue = context.Queue()
        start_event = context.Event()
        with ProcessPoolExecutor(
            max_workers=process_count,
            mp_context=context,
            initializer=_initialize_audit_validation_worker,
            initargs=(source_lock, ready_queue, start_event),
        ) as executor:
            futures = [executor.submit(_run_audit_validation_task, task) for task in tasks]
            try:
                for _ in range(process_count):
                    ready_queue.get(timeout=SUBPROCESS_TIMEOUT_SECONDS)
            finally:
                start_event.set()
            for task, future in zip(tasks, futures, strict=True):
                try:
                    evidence.append(future.result())
                except Exception as exc:
                    warnings.append(
                        f"mutation validation failed for {task.target_path}: "
                        f"{type(exc).__name__}: {exc}"
                    )
    except Exception as exc:
        warnings.append(
            f"parallel mutation validation failed: {type(exc).__name__}: {exc}"
        )
    return evidence


def _record_validation_evidence(
    result: ModuleAudit,
    evidence: _AuditValidationEvidence,
    *,
    kill_counts: dict[str, int],
) -> None:
    """Merge one immutable validation result into the parent module audit."""
    result.mutation_targets.append(json.loads(evidence.view_json))
    for mutant in evidence.mutants:
        if mutant.killed:
            continue
        result.mutation_gaps.append(
            {
                "target": evidence.target_path,
                "mutant_id": mutant.mutant_id,
                "location": mutant.location,
                "description": mutant.description,
                "source_line": mutant.source_line,
                "remediation": mutant.remediation,
            }
        )

    for mutant in evidence.mutants:
        for test_name in mutant.killers:
            kill_counts[test_name] = kill_counts.get(test_name, 0) + 1

    if evidence.stub:
        result.mutation_gap_stubs.append(
            {"target": evidence.target_path, "content": evidence.stub}
        )
