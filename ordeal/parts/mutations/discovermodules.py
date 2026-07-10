from __future__ import annotations
# ruff: noqa
def _discover_modules(target: str) -> list[str]:
    """Find all importable modules under *target* (package or single module)."""
    _ensure_importable(target)
    try:
        mod = importlib.import_module(target)
    except ImportError:
        return []

    if not hasattr(mod, "__path__"):
        return [target]

    modules = [target]
    for info in pkgutil.walk_packages(mod.__path__, prefix=target + "."):
        if info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        modules.append(info.name)
    return modules
# Common test directory names and patterns
_TEST_DIRS = ["tests", "test", "src/tests", "src/test"]
def _find_test_dirs() -> list[Path]:
    """Discover all directories that contain test files."""
    cwd = Path.cwd()
    found: list[Path] = []
    # Check well-known locations
    for name in _TEST_DIRS:
        d = cwd / name
        if d.is_dir():
            found.append(d)
    # Also check for test files at project root (rare but valid)
    if list(cwd.glob("test_*.py")):
        found.append(cwd)
    return found or [cwd / "tests"]  # default to tests/ even if missing
def _mutation_test_dirs(module_name: str) -> list[Path]:
    """Return candidate test directories for *module_name*."""
    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        dirs.append(resolved)

    for path in _find_test_dirs():
        _add(path)

    try:
        module = importlib.import_module(module_name)
    except Exception:
        module = None
    if module is not None:
        source_file = getattr(module, "__file__", None)
        if source_file:
            module_path = Path(source_file).resolve()
            for parent in module_path.parents:
                candidate = parent / "tests"
                if candidate.is_dir():
                    _add(candidate)

    return dirs
def _has_tests(module_name: str, test_dir: str = "tests") -> str | None:
    """Return the existing test file path if one exists, else None.

    Searches the specified *test_dir* and also auto-discovers common
    test directory layouts (``tests/``, ``test/``, ``src/tests/``, nested
    subdirectories).
    """
    short = module_name.rsplit(".", 1)[-1]

    # Build list of directories to search
    dirs_to_check: list[Path] = []
    specified = Path(test_dir)
    if specified.is_dir():
        dirs_to_check.append(specified)
    for d in _find_test_dirs():
        if d not in dirs_to_check:
            dirs_to_check.append(d)

    for d in dirs_to_check:
        # Exact match: test_{name}.py
        exact = d / f"test_{short}.py"
        if exact.exists():
            return str(exact)
        # Prefix match: test_{name}_*.py (e.g. test_mutations_presets.py)
        for match in d.glob(f"test_{short}_*.py"):
            return str(match)
        # Also search subdirectories (tests/unit/test_X.py, tests/integration/test_X.py)
        for match in d.rglob(f"test_{short}.py"):
            return str(match)
        for match in d.rglob(f"test_{short}_*.py"):
            return str(match)

    return None
def init_project(
    target: str | None = None,
    *,
    output_dir: str = "tests",
    dry_run: bool = False,
) -> list[dict[str, str]]:
    """Bootstrap test files for a Python package.

    Scans *target* for public modules, checks which ones already have
    tests, and generates starter smoke tests for the rest.

    Args:
        target: Dotted package path (e.g. ``"myapp"``).  When ``None``,
            auto-detects from the current directory.
        output_dir: Directory to write test files to (default ``"tests"``).
        dry_run: If True, no files are written and no functions are executed.
            Generates stub tests from signatures and type hints only, so
            ``--dry-run`` is safe even on packages with side effects.

    Returns:
        List of dicts with keys ``module``, ``status``, ``path``, ``content``.
        Status is one of ``"generated"``, ``"exists"``, ``"empty"``.
    """
    if target is None:
        target = _detect_package()
        if target is None:
            return []

    modules = _discover_modules_static(target) if dry_run else _discover_modules(target)
    results: list[dict[str, str]] = []
    out = Path(output_dir)

    for mod_name in modules:
        existing = _has_tests(mod_name, output_dir)
        if existing:
            results.append(
                {
                    "module": mod_name,
                    "status": "exists",
                    "path": existing,
                    "content": "",
                }
            )
            continue

        content = generate_starter_tests(mod_name, dry_run=dry_run)
        if not content:
            results.append(
                {
                    "module": mod_name,
                    "status": "empty",
                    "path": "",
                    "content": "",
                }
            )
            continue

        short = mod_name.rsplit(".", 1)[-1]
        dest = out / f"test_{short}.py"

        if not dry_run:
            out.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        results.append(
            {
                "module": mod_name,
                "status": "generated",
                "path": str(dest),
                "content": content,
            }
        )

    # Generate ordeal.toml if it doesn't exist
    if not dry_run and not Path("ordeal.toml").exists():
        generated_mods = [r["module"] for r in results if r["status"] == "generated"]
        all_mods = [r["module"] for r in results]
        if all_mods:
            _generate_toml(target, all_mods, generated_mods, output_dir)

    return results
def _generate_toml(
    target: str,
    modules: list[str],
    generated_modules: list[str],
    test_dir: str,
) -> None:
    """Generate ordeal.toml with explorer + mutation config."""
    # Find chaos test classes in generated test files
    test_classes: list[str] = []
    for mod in generated_modules:
        short = mod.rsplit(".", 1)[-1]
        test_file = Path(test_dir) / f"test_{short}.py"
        if test_file.exists():
            content = test_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "= chaos_for(" in line:
                    cls_name = line.split("=")[0].strip()
                    test_mod = f"{test_dir}.test_{short}".replace("/", ".")
                    test_classes.append(f"{test_mod}:{cls_name}")

    top_pkg = target.split(".")[0]

    lines = [
        f"# ordeal.toml — generated by ordeal init for {target}",
        "#",
        "# Run:  ordeal explore     (coverage-guided state exploration)",
        "#       ordeal mutate      (mutation testing)",
        "#       ordeal audit <mod> (test coverage audit)",
        "",
        "[explorer]",
        f'target_modules = ["{top_pkg}"]',
        "max_time = 30",
        "seed = 42",
        "rule_swarm = true",
        "verbose = true",
        "",
    ]

    for cls in test_classes:
        lines.append("[[tests]]")
        lines.append(f"class = '{cls}'")
        lines.append("")

    # Mutation targets: all function-containing modules
    func_targets = [m for m in modules if m != target]
    if not func_targets:
        func_targets = modules
    target_strs = ", ".join(f'"{m}"' for m in func_targets[:10])

    lines.extend(
        [
            "[mutations]",
            f"targets = [{target_strs}]",
            'preset = "standard"',
            "threshold = 0.8",
            "",
            "[report]",
            'format = "text"',
            "verbose = true",
            "",
        ]
    )

    Path("ordeal.toml").write_text("\n".join(lines), encoding="utf-8")
def _detect_package() -> str | None:
    """Auto-detect the top-level package from the current directory.

    Checks (in order):
    1. ``pyproject.toml`` ``[project] name`` (PEP 621)
    2. ``setup.cfg`` ``[metadata] name``
    3. ``setup.py`` ``name=`` argument
    4. Directories with ``__init__.py`` (flat layout)
    5. ``src/`` subdirectories with ``__init__.py``

    For each candidate, verifies the directory actually exists before returning.
    """
    cwd = Path.cwd()

    candidates = _candidates_from_pyproject(cwd)
    candidates.extend(_candidates_from_setup_cfg(cwd))
    candidates.extend(_candidates_from_setup_py(cwd))

    # Verify each candidate exists as a real package
    for name in candidates:
        pkg = name.replace("-", "_")
        if _verify_package(cwd, pkg):
            return pkg

    # Fall back: scan for directories with __init__.py
    for search_root in [cwd, cwd / "src"]:
        if not search_root.is_dir():
            continue
        for child in sorted(search_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            if (child / "__init__.py").exists():
                return child.name

    return None
def _candidates_from_pyproject(cwd: Path) -> list[str]:
    """Extract package name from pyproject.toml [project] section."""
    path = cwd / "pyproject.toml"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if stripped.startswith("[") and in_project:
            break  # left [project] section
        if in_project and stripped.startswith("name"):
            _, _, val = stripped.partition("=")
            val = val.strip().strip("\"'")
            if val:
                return [val]
    return []
def _candidates_from_setup_cfg(cwd: Path) -> list[str]:
    """Extract package name from setup.cfg [metadata] section."""
    path = cwd / "setup.cfg"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    in_metadata = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[metadata]":
            in_metadata = True
            continue
        if stripped.startswith("[") and in_metadata:
            break
        if in_metadata and stripped.startswith("name"):
            _, _, val = stripped.partition("=")
            val = val.strip()
            if val:
                return [val]
    return []
def _candidates_from_setup_py(cwd: Path) -> list[str]:
    """Extract package name from setup.py (best-effort regex)."""
    path = cwd / "setup.py"
    if not path.exists():
        return []
    import re

    text = path.read_text(encoding="utf-8")
    m = re.search(r"""name\s*=\s*["']([^"']+)["']""", text)
    return [m.group(1)] if m else []
def _verify_package(cwd: Path, name: str) -> bool:
    """Check that *name* exists as a package directory in cwd or cwd/src."""
    for root in [cwd, cwd / "src"]:
        pkg_dir = root / name
        if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
            return True
    return False
# ============================================================================
# AST mutation operators
# ============================================================================


class _Applicator(ast.NodeTransformer):
    """Apply exactly the Nth possible mutation of a specific type."""

    def __init__(self, target_idx: int):
        self.target_idx = target_idx
        self.current_idx = 0
        self.description = ""
        self.line = 0
        self.col = 0
        self.applied = False
class _ArithmeticApplicator(_Applicator):
    SWAPS: dict[type, tuple[type, str, str]] = {
        ast.Add: (ast.Sub, "+", "-"),
        ast.Sub: (ast.Add, "-", "+"),
        ast.Mult: (ast.Div, "*", "/"),
        ast.Div: (ast.Mult, "/", "*"),
        ast.Mod: (ast.Mult, "%", "*"),
    }

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        entry = self.SWAPS.get(type(node.op))
        if entry and not self.applied:
            if self.current_idx == self.target_idx:
                new_cls, old_sym, new_sym = entry
                node = copy.deepcopy(node)
                node.op = new_cls()
                self.description = f"{old_sym} -> {new_sym}"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node
class _ComparisonApplicator(_Applicator):
    SWAPS: dict[type, tuple[type, str, str]] = {
        ast.Lt: (ast.LtE, "<", "<="),
        ast.LtE: (ast.Lt, "<=", "<"),
        ast.Gt: (ast.GtE, ">", ">="),
        ast.GtE: (ast.Gt, ">=", ">"),
        ast.Eq: (ast.NotEq, "==", "!="),
        ast.NotEq: (ast.Eq, "!=", "=="),
    }

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            entry = self.SWAPS.get(type(op))
            if entry and not self.applied:
                if self.current_idx == self.target_idx:
                    new_cls, old_sym, new_sym = entry
                    node = copy.deepcopy(node)
                    node.ops[i] = new_cls()
                    self.description = f"{old_sym} -> {new_sym}"
                    self.line = node.lineno
                    self.col = node.col_offset
                    self.applied = True
                self.current_idx += 1
        return node
class _NegateApplicator(_Applicator):
    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
                ast.fix_missing_locations(node)
                self.description = "negate if-condition"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        self.generic_visit(node)
        if not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
                ast.fix_missing_locations(node)
                self.description = "negate while-condition"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node
class _ReturnNoneApplicator(_Applicator):
    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if node.value is not None and not self.applied:
            if self.current_idx == self.target_idx:
                node = copy.deepcopy(node)
                node.value = ast.Constant(value=None)
                self.description = "return None"
                self.line = node.lineno
                self.col = node.col_offset
                self.applied = True
            self.current_idx += 1
        return node
# -- Counters (same traversal logic, just counting) --


class _Counter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.count = 0
class _ArithmeticCounter(_Counter):
    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.generic_visit(node)
        if type(node.op) in _ArithmeticApplicator.SWAPS:
            self.count += 1
class _ComparisonCounter(_Counter):
    def visit_Compare(self, node: ast.Compare) -> None:
        self.generic_visit(node)
        for op in node.ops:
            if type(op) in _ComparisonApplicator.SWAPS:
                self.count += 1
class _NegateCounter(_Counter):
    def visit_If(self, node: ast.If) -> None:
        self.generic_visit(node)
        self.count += 1

    def visit_While(self, node: ast.While) -> None:
        self.generic_visit(node)
        self.count += 1
class _ReturnNoneCounter(_Counter):
    def visit_Return(self, node: ast.Return) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self.count += 1
