from __future__ import annotations
# ruff: noqa
# ============================================================================
# Module-level mutation testing
# ============================================================================


@contextmanager
def _mutated_module(module_name: str, mutated_tree: ast.Module):
    """Temporarily replace a module's contents with mutated code.

    Patches the original module object in-place so that both
    ``import mod; mod.func(...)`` and ``from mod import func``
    (captured before the swap) see the mutated definitions.

    .. note::

       This is **process-local** — subprocesses (Ray workers,
       ``multiprocessing`` spawn) reimport from disk and will not see
       the mutation.  Use :func:`_mutated_module_on_disk` when the
       test suite may spawn child processes.
    """
    original = sys.modules.get(module_name)
    if original is None:
        raise ImportError(f"Module {module_name!r} not in sys.modules")

    # Save original dict contents
    saved = dict(original.__dict__)

    # Compile and exec mutated code into a temp namespace
    code = compile(mutated_tree, getattr(original, "__file__", "<mutated>"), "exec")
    mutated_ns: dict[str, object] = {}
    exec(code, mutated_ns)  # noqa: S102

    # Patch original module in-place: replace only the names that the
    # mutated code defines (preserves __name__, __file__, etc.)
    for name, value in mutated_ns.items():
        if not name.startswith("__"):
            setattr(original, name, value)

    try:
        yield original
    finally:
        # Restore original contents
        # Remove names that the mutation added
        for name in mutated_ns:
            if not name.startswith("__") and name not in saved:
                try:
                    delattr(original, name)
                except AttributeError:
                    pass
        # Restore original values
        for name, value in saved.items():
            if not name.startswith("__"):
                setattr(original, name, value)


_CROSS_PROCESS_IMPORTS = frozenset(
    {
        "ray",
        "multiprocessing",
        "subprocess",
        "concurrent",
        "celery",
        "dask",
        "joblib",
    }
)
_CROSS_PROCESS_CALLS = frozenset(
    {
        "ray.remote",
        "ray.get",
        "ray.put",
        "multiprocessing.Pool",
        "multiprocessing.Process",
        "concurrent.futures.ProcessPoolExecutor",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
    }
)


def _has_cross_process_imports(source: str) -> bool:
    """Check whether source code contains cross-process imports or decorators."""
    try:
        tree = ast.parse(source)
    except Exception:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _CROSS_PROCESS_IMPORTS:
                    return True
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _CROSS_PROCESS_IMPORTS:
                return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                dec_name = _decorator_name(dec)
                if dec_name and any(dec_name.startswith(p) for p in _CROSS_PROCESS_IMPORTS):
                    return True
    return False


def _read_module_source(module_name: str) -> str | None:
    """Read source for a module — works even when the module can't be imported."""
    source_file = None
    try:
        module = importlib.import_module(module_name)
        source_file = getattr(module, "__file__", None)
    except Exception:
        try:
            spec = importlib.util.find_spec(module_name)
            if spec and spec.origin:
                source_file = spec.origin
        except Exception:
            pass
    if source_file is None:
        return None
    try:
        with open(source_file) as f:
            return f.read()
    except Exception:
        return None


def _needs_disk_mutation(target: str) -> bool:
    """Auto-detect whether *target* or its tests use cross-process patterns.

    Scans both the target module's AST and the corresponding test files
    for imports of Ray, multiprocessing, subprocess, etc.  The target
    module itself may be pure Python while the *tests* call it through
    Ray ``.remote()`` — scanning only the target would miss this.
    """
    module_name = _split_mutation_target(target)[0]

    # Scan the target module
    source = _read_module_source(module_name)
    if source and _has_cross_process_imports(source):
        return True

    # Scan likely test files (tests/test_<module>.py, tests/conftest.py)
    short_name = module_name.split(".")[-1]
    test_candidates = [
        f"tests/test_{short_name}.py",
        "tests/conftest.py",
        f"test_{short_name}.py",
    ]
    for test_path in test_candidates:
        try:
            with open(test_path) as f:
                test_source = f.read()
            if _has_cross_process_imports(test_source):
                return True
        except FileNotFoundError:
            continue
        except Exception:
            continue

    return False


def _decorator_name(node: ast.expr) -> str | None:
    """Extract dotted name from a decorator AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _resolve_disk_mutation(disk_mutation: bool | None, target: str) -> bool:
    """Resolve disk_mutation: None means auto-detect."""
    if disk_mutation is not None:
        return disk_mutation
    needed = _needs_disk_mutation(target)
    if needed:
        import warnings

        warnings.warn(
            f"Auto-enabling disk_mutation for {target!r} — "
            "cross-process imports detected (Ray/multiprocessing/subprocess). "
            "Mutations will be written to disk so subprocesses see them. "
            "Suppress with disk_mutation=False.",
            stacklevel=3,
        )
    return needed


def _clear_pyc(source_path: str) -> None:
    """Remove ``__pycache__`` bytecode for *source_path*.

    Python caches compiled bytecode in ``__pycache__/<stem>.cpython-*.pyc``.
    After rewriting a ``.py`` file the stale ``.pyc`` must be removed,
    otherwise the interpreter may load the old bytecode instead of
    re-compiling from the modified source.
    """
    from pathlib import Path

    source = Path(source_path)
    cache_dir = source.parent / "__pycache__"
    if cache_dir.exists():
        stem = source.stem
        for pyc in cache_dir.glob(f"{stem}.*.pyc"):
            pyc.unlink(missing_ok=True)


@contextmanager
def _mutated_source_file(source_path: str, mutated_source: str):
    """Write mutated source to disk, clear bytecode, restore on exit.

    Subprocesses (Ray workers, ``multiprocessing`` spawn, ``subprocess``)
    reimport from disk, so in-memory patching alone is invisible to them.
    This writes the mutation to the actual ``.py`` file so **any** process
    that imports the module picks up the mutated code.
    """
    with open(source_path) as f:
        original_source = f.read()

    _clear_pyc(source_path)
    with open(source_path, "w") as f:
        f.write(mutated_source)

    try:
        yield
    finally:
        with open(source_path, "w") as f:
            f.write(original_source)
        _clear_pyc(source_path)


@contextmanager
def _mutated_module_on_disk(module_name: str, mutated_tree: ast.Module):
    """Mutate a module both in-memory and on disk.

    Combines :func:`_mutated_module` (for in-process visibility) with
    :func:`_mutated_source_file` (for subprocess visibility).  This is
    the correct strategy when the test suite may spawn child processes
    — e.g. Ray workers, ``multiprocessing`` pools, or subprocess calls.
    """
    original = sys.modules.get(module_name)
    if original is None:
        raise ImportError(f"Module {module_name!r} not in sys.modules")

    source_path = getattr(original, "__file__", None)
    if source_path is None:
        raise ValueError(f"Cannot locate source file for {module_name!r}")

    try:
        mutated_source = ast.unparse(mutated_tree)
    except Exception:
        # Fallback: in-memory only if unparse fails (rare)
        with _mutated_module(module_name, mutated_tree) as mod:
            yield mod
        return

    with (
        _mutated_source_file(source_path, mutated_source),
        _mutated_module(module_name, mutated_tree) as mod,
    ):
        importlib.invalidate_caches()
        yield mod


@contextmanager
def _function_mutated_on_disk(
    target_spec: _ResolvedMutationTarget,
    mutated_func_tree: ast.Module,
):
    """Rewrite a single function on disk inside its module.

    Reads the full module source, replaces the target function's AST
    node with the mutated version, writes to disk, and restores on exit.
    Used alongside :class:`PatchFault` for full cross-process coverage.
    """
    module = target_spec.module
    source_path = getattr(module, "__file__", None)
    if source_path is None:
        yield  # nothing to do — no source file
        return

    with open(source_path) as f:
        module_source = f.read()

    # Extract the mutated FunctionDef from the single-function AST
    mutated_func_node = None
    for node in ast.walk(mutated_func_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mutated_func_node = node
            break

    if mutated_func_node is None:
        yield  # no function found — skip disk mutation
        return

    # Parse full module, replace the target function
    module_tree = ast.parse(module_source)
    replaced = False
    target_name = target_spec.leaf_name
    if target_name is None:
        yield  # module-level target has no replaceable function name
        return

    body = module_tree.body
    class_path = list(target_spec.qualname_parts)

    def _replace_in_body(nodes: list[ast.stmt], path: list[str]) -> bool:
        if not path:
            for i, node in enumerate(nodes):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    node.name == target_name
                ):
                    mutated_func_node.lineno = node.lineno
                    mutated_func_node.col_offset = node.col_offset
                    nodes[i] = mutated_func_node
                    return True
            return False

        head = path[0]
        tail = path[1:]
        for node in nodes:
            if isinstance(node, ast.ClassDef) and node.name == head:
                return _replace_in_body(node.body, tail)
        return False

    replaced = _replace_in_body(body, class_path)

    if not replaced:
        yield  # function not found in module — skip disk mutation
        return

    ast.fix_missing_locations(module_tree)
    try:
        mutated_module_source = ast.unparse(module_tree)
    except Exception:
        yield  # unparse failed — skip disk mutation
        return

    with _mutated_source_file(source_path, mutated_module_source):
        importlib.invalidate_caches()
        yield


def _module_is_equivalent(
    original: types.ModuleType,
    mutated_tree: ast.Module,
    n_samples: int = 10,
) -> bool:
    """Heuristic: compare all public callables in original vs mutated module.

    Returns ``True`` (skip) when every callable produces identical outputs
    on random inputs.  Falls back to ``False`` (test it) on any error.
    """
    try:
        mutated = types.ModuleType(original.__name__)
        mutated.__file__ = getattr(original, "__file__", "<mutated>")
        code = compile(mutated_tree, mutated.__file__, "exec")
        exec(code, mutated.__dict__)  # noqa: S102
    except Exception:
        return False

    for name, obj in sorted(vars(original).items()):
        if name.startswith("__"):
            continue
        if not callable(obj) or isinstance(obj, type):
            continue
        orig_fn = _unwrap_func(obj)
        if getattr(orig_fn, "__ordeal_requires_factory__", False):
            continue
        if getattr(orig_fn, "__module__", None) not in {original.__name__, None}:
            continue
        mut_fn = _resolve_dotted_attr(mutated, name)
        if mut_fn is None:
            return False
        if not _is_runtime_equivalent(orig_fn, mut_fn, n_samples):
            return False
    return True


@dataclass(frozen=True)
class _EquivalenceSamplePlan:
    """Prepared argument tuples reused across runtime-equivalence checks."""

    samples: tuple[tuple[object, ...], ...]


@functools.lru_cache(maxsize=128)
def _split_mutation_target(target: str) -> tuple[str, str | None]:
    """Return ``(module_name, func_name)`` for a mutation target."""
    if ":" in target:
        module_name, _, attr_path = target.partition(":")
        attr_parts = [part for part in attr_path.split(".") if part]
        return module_name, (attr_parts[-1] if attr_parts else None)

    if _local_module_exists(target):
        return target, None

    parts = target.split(".")
    for idx in range(len(parts) - 1, 0, -1):
        module_candidate = ".".join(parts[:idx])
        if _local_module_exists(module_candidate):
            return module_candidate, parts[-1]

    resolved = _resolve_mutation_target(target)
    return resolved.module_name, resolved.leaf_name


def _mutation_test_name_variants(module_name: str) -> tuple[str, ...]:
    """Return likely filename stems for tests covering *module_name*."""
    short = module_name.rsplit(".", 1)[-1]
    variants = [short]
    normalized = short.lstrip("_")
    if normalized and normalized != short:
        variants.append(normalized)
    return tuple(variants)


@functools.lru_cache(maxsize=8)
def _all_test_files() -> tuple[str, ...]:
    """Return every discovered ``test_*.py`` file under the project."""
    seen: set[str] = set()
    found: list[str] = []
    for test_dir in _find_test_dirs():
        patterns = ("test_*.py",)
        for pattern in patterns:
            for path in sorted(test_dir.glob(pattern)):
                resolved = str(path)
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
            for path in sorted(test_dir.rglob(pattern)):
                resolved = str(path)
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
    return tuple(found)


@functools.lru_cache(maxsize=128)
def _named_mutation_test_candidates(module_name: str) -> tuple[str, ...]:
    """Return likely test files based on the target module name."""
    seen: set[str] = set()
    found: list[str] = []
    for test_dir in _mutation_test_dirs(module_name):
        for short in _mutation_test_name_variants(module_name):
            exact = test_dir / f"test_{short}.py"
            if exact.exists():
                resolved = str(exact)
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
            for match in sorted(test_dir.glob(f"test_{short}_*.py")):
                resolved = str(match)
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
            for match in sorted(test_dir.rglob(f"test_{short}.py")):
                resolved = str(match)
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
            for match in sorted(test_dir.rglob(f"test_{short}_*.py")):
                resolved = str(match)
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
    return tuple(found)


def _additional_mutation_test_candidates(module_name: str) -> tuple[str, ...]:
    """Return broader test-file candidates for content-based scoring."""
    seen: set[str] = set()
    found: list[str] = []
    for test_dir in _mutation_test_dirs(module_name):
        for path in sorted(test_dir.rglob("test_*.py")):
            resolved = str(path)
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)
    return tuple(found)
