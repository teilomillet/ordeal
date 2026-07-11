from __future__ import annotations


# ruff: noqa
_MUTATION_ATTRIBUTED_MODULES: dict[str, tuple[str, ...]] = {}


def _mutation_source_module_name(
    path: Path,
    *,
    package_root: Path,
    top_package: str,
) -> str:
    """Return a stable module-like key for one local attribution source."""
    try:
        relative = path.resolve().relative_to(package_root.resolve())
    except ValueError:
        return f"__ordeal_test__.{path.resolve()}"
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join((top_package, *parts))


def _mutation_import_dependencies(path: Path, module_name: str) -> set[str]:
    """Extract absolute and resolved-relative import dependencies."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
    except Exception:
        return set()

    dependencies: set[str] = set()
    package_parts = module_name.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = max(0, len(package_parts) - node.level + 1)
            base_parts = package_parts[:keep]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base:
            dependencies.add(base)
        for alias in node.names:
            if alias.name != "*" and base:
                dependencies.add(f"{base}.{alias.name}")
    return dependencies


def _mutation_test_node_paths(path: str) -> tuple[str, ...]:
    """Return every statically declared pytest node in one test file."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ()
    nodes: list[str] = []
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
            "test_"
        ):
            nodes.append(f"{path}::{item.name}")
        elif isinstance(item, ast.ClassDef) and item.name.startswith("Test"):
            nodes.extend(
                f"{path}::{item.name}::{child.name}"
                for child in item.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test_")
            )
    return tuple(nodes)


@functools.lru_cache(maxsize=128)
def _attributed_mutation_test_candidates(module_name: str) -> tuple[str, ...]:
    """Find test files connected to a target through reverse local imports."""
    try:
        module = importlib.import_module(module_name)
        source_path = Path(module.__file__).resolve()
    except Exception:
        return ()

    top_package = module_name.split(".", 1)[0]
    depth = len(module_name.split(".")) - (1 if source_path.name == "__init__.py" else 2)
    package_root = source_path.parent
    for _ in range(max(0, depth)):
        package_root = package_root.parent

    source_paths = {
        path.resolve()
        for path in package_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    for test_dir in _find_test_dirs():
        source_paths.update(
            path.resolve()
            for path in test_dir.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    test_paths = {Path(path).resolve() for path in _all_test_files()}

    records: list[tuple[Path, str]] = []
    sources: dict[Path, str] = {}
    for path in sorted(source_paths):
        source_module = _mutation_source_module_name(
            path,
            package_root=package_root,
            top_package=top_package,
        )
        records.append((path, source_module))
        try:
            sources[path] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            sources[path] = ""

    reached = {module_name}
    parsed_dependencies: dict[Path, set[str]] = {}
    changed = True
    while changed:
        changed = False
        hints = reached | {item.rsplit(".", 1)[-1] for item in reached}
        for path, source_module in records:
            if source_module in reached:
                continue
            if not any(hint in sources[path] for hint in hints):
                continue
            dependencies = parsed_dependencies.get(path)
            if dependencies is None:
                dependencies = _mutation_import_dependencies(path, source_module)
                parsed_dependencies[path] = dependencies
            if dependencies.intersection(reached):
                reached.add(source_module)
                changed = True

    _MUTATION_ATTRIBUTED_MODULES[module_name] = tuple(sorted(reached))

    return tuple(
        str(path)
        for path, source_module in records
        if path in test_paths and source_module in reached
    )


def _attributed_mutation_modules(module_name: str) -> tuple[str, ...]:
    """Return local modules proven to depend on the mutation target."""
    _attributed_mutation_test_candidates(module_name)
    return _MUTATION_ATTRIBUTED_MODULES.get(module_name, (module_name,))


def _mutation_test_baseline_fingerprint(
    target: str,
    selection: _MutationTestSelection,
    *,
    excluded_test: _PytestItemIdentity | None = None,
) -> str:
    """Fingerprint selected test bodies, target source, and pytest configuration."""
    digest = hashlib.sha256()
    digest.update((selection.k_filter or "").encode())
    if excluded_test is not None:
        digest.update(b"\0excluded-pytest-item\0")
        digest.update("\0".join(excluded_test).encode())
    content_paths = {Path(item.split("::", 1)[0]).resolve() for item in selection.paths}
    if not content_paths:
        content_paths.update(Path(path).resolve() for path in _all_test_files())
    closure_paths: set[Path] = set()
    try:
        target_module = _resolve_mutation_target(target).module
        source_file = target_module.__file__
        if source_file is not None:
            source_path = Path(source_file).resolve()
            content_paths.add(source_path)
            parts = target_module.__name__.split(".")
            depth = len(parts) - (1 if source_path.name == "__init__.py" else 2)
            package_root = source_path.parent
            for _ in range(max(0, depth)):
                package_root = package_root.parent
            closure_paths.update(package_root.rglob("*.py"))
    except Exception:
        pass
    root_conftest = Path.cwd() / "conftest.py"
    if root_conftest.exists():
        content_paths.add(root_conftest.resolve())
    for test_dir in _find_test_dirs():
        closure_paths.update(test_dir.rglob("*.py"))
        conftest = (test_dir / "conftest.py").resolve()
        if conftest.exists():
            content_paths.add(conftest)
    for path in sorted(content_paths):
        digest.update(str(path).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    for path in sorted({path.resolve() for path in closure_paths} - content_paths):
        digest.update(str(path).encode())
        try:
            stat = path.stat()
            digest.update(f"{stat.st_mtime_ns}:{stat.st_size}".encode())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()
