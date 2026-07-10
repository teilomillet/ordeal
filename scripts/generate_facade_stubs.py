"""Generate static type stubs for the runtime facade modules.

The public modules under ``ordeal/`` execute ordered source fragments from
``ordeal/parts`` so monkeypatching and module globals keep their historical
behaviour.  Static analyzers cannot see names introduced by ``exec``.  This
script reconstructs each facade in a temporary package, asks Pyright to build
stubs from that static source, and copies only the facade stubs back.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ordeal"
PART_FOLDER = re.compile(r'root = root / "parts" / "([a-z0-9]+)"')


def _facade_spec(path: Path) -> tuple[str, tuple[str, ...]] | None:
    """Return the part folder and ordered filenames for one facade."""
    source = path.read_text(encoding="utf-8")
    if "_load_facade_parts" not in source:
        return None
    match = PART_FOLDER.search(source)
    if match is None:
        raise ValueError(f"cannot find part folder in {path}")
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_PART_FILES" for target in node.targets
        ):
            values = ast.literal_eval(node.value)
            return match.group(1), tuple(str(value) for value in values)
    raise ValueError(f"cannot find _PART_FILES in {path}")


def _facades() -> dict[Path, tuple[str, tuple[str, ...]]]:
    """Discover every facade relative to the package root."""
    found: dict[Path, tuple[str, tuple[str, ...]]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if PACKAGE / "parts" in path.parents:
            continue
        spec = _facade_spec(path)
        if spec is not None:
            found[path.relative_to(PACKAGE)] = spec
    return found


def _stub_names(source: str) -> set[str]:
    """Return names that a stub exposes directly."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _root_reexports(stub: str) -> str:
    """Add explicit root-package exports that runtime lazy loading hides."""
    package = importlib.import_module("ordeal")
    known = _stub_names(stub)
    exports = tuple(name for name in dir(package) if not name.startswith("_"))
    stateful = set(getattr(package, "_STATEFUL_EXPORTS", ()))
    lazy_modules = tuple(getattr(package, "_LAZY_SUBMODULES", ()))
    lines: list[str] = []
    for name in exports:
        if name in known:
            continue
        module_name: str | None = None
        exported_name = name
        value = getattr(package, name)
        defined_in = str(getattr(value, "__module__", ""))
        if defined_in == "__future__":
            continue
        external = False
        if name in stateful:
            module_name = "hypothesis.stateful"
            external = True
        elif defined_in.startswith("ordeal."):
            module_name = defined_in.removeprefix("ordeal.")
        else:
            for candidate in lazy_modules:
                module = importlib.import_module(candidate)
                if name in vars(module):
                    module_name = candidate.removeprefix("ordeal.")
                    break
        if module_name is None:
            value = vars(package).get(name)
            if isinstance(value, type(importlib)) and value.__name__.startswith("ordeal."):
                module_name = value.__name__.removeprefix("ordeal.")
                exported_name = ""
        if module_name is None:
            raise ValueError(f"cannot resolve root export {name!r}")
        prefix = "" if external else "."
        if exported_name:
            lines.append(f"from {prefix}{module_name} import {exported_name} as {name}")
        else:
            lines.append(f"from . import {module_name} as {name}")
    if not lines:
        return stub
    marker = '"""\n\nimport '
    insertion = "\n".join(lines) + "\n\n"
    if marker in stub:
        return stub.replace(marker, f'"""\n\n{insertion}import ', 1)
    return insertion + stub


def _restore_rebound_members(stub: str, implementation: str) -> str:
    """Nest functions rebound with ``Class.member = function`` in the stub."""
    stub_tree = ast.parse(stub)
    implementation_tree = ast.parse(implementation)
    classes = {node.name: node for node in stub_tree.body if isinstance(node, ast.ClassDef)}
    functions = {
        node.name: node
        for node in stub_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in implementation_tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in classes
        ):
            continue
        if target.attr.startswith("_") and target.attr not in {
            "__enter__",
            "__exit__",
            "__init__",
        }:
            continue
        wrapper: str | None = None
        value = node.value
        if isinstance(value, ast.Name):
            function_name = value.id
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"classmethod", "property", "staticmethod"}
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
        ):
            wrapper = value.func.id
            function_name = value.args[0].id
        else:
            continue
        function = functions.get(function_name)
        if function is None:
            raise ValueError(
                f"cannot find stub function {function_name!r} for {target.value.id}.{target.attr}"
            )
        class_node = classes[target.value.id]
        existing = {
            member.name
            for member in class_node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if target.attr in existing:
            continue
        method = copy.deepcopy(function)
        method.name = target.attr
        if wrapper is not None:
            method.decorator_list.insert(0, ast.Name(id=wrapper, ctx=ast.Load()))
        class_node.body.append(method)
    deleted_names = {
        target.id
        for node in implementation_tree.body
        if isinstance(node, ast.Delete)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    stub_tree.body = [
        node
        for node in stub_tree.body
        if not (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in deleted_names
        )
        and not (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in deleted_names
                for target in node.targets
            )
        )
        and not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in deleted_names
        )
    ]
    ast.fix_missing_locations(stub_tree)
    return ast.unparse(stub_tree) + "\n"


def _clean_stub(relative: Path, source: str, implementation: str) -> str:
    """Repair known generator ambiguities without weakening annotations."""
    source = _restore_rebound_members(source, implementation)
    source = "# ruff: noqa\nimport builtins\n" + source.replace(
        "@property\n", "@builtins.property\n"
    )
    if relative == Path("trace.py"):
        source = source.replace(
            "def default(self, obj: Any) -> Any:",
            "def default(self, o: Any) -> Any:",
        )
    if relative == Path("diff.py"):
        original = (
            "from ordeal.system_diff import FaultEvent, PerformanceBudget, "
            "SystemDiffResult, SystemEvent"
        )
        replacement = (
            "from ordeal.system_diff import FaultEvent, Operation, PerformanceBudget, "
            "SystemDiffResult, SystemEvent"
        )
        source = source.replace(
            original,
            replacement,
        )
    if relative == Path("__init__.py"):
        source = _root_reexports(source)
    return source


def generate(pyright: str, ruff: str) -> int:
    """Generate adjacent ``.pyi`` files for all discovered facades."""
    facades = _facades()
    with tempfile.TemporaryDirectory(prefix="ordeal-facade-stubs-") as raw_tmp:
        tmp = Path(raw_tmp)
        rebuilt = tmp / "ordeal"
        shutil.copytree(
            PACKAGE,
            rebuilt,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyi"),
        )
        for relative, (folder, filenames) in facades.items():
            fragments = [
                (PACKAGE / "parts" / folder / filename).read_text(encoding="utf-8")
                for filename in filenames
            ]
            (rebuilt / relative).write_text("\n".join(fragments), encoding="utf-8")

        modules = []
        for relative in facades:
            parts = relative.with_suffix("").parts
            if parts == ("__init__",):
                modules.append("ordeal")
            else:
                modules.append(".".join(("ordeal", *parts)))
        for module in modules:
            subprocess.run(
                [pyright, "--createstub", module, "--pythonpath", sys.executable],
                cwd=tmp,
                check=True,
            )
        generated = tmp / "typings" / "ordeal"
        written: list[Path] = []
        for relative in facades:
            stub_relative = relative.with_suffix(".pyi")
            source = (generated / stub_relative).read_text(encoding="utf-8")
            destination = PACKAGE / stub_relative
            destination.write_text(
                _clean_stub(
                    relative,
                    source,
                    (rebuilt / relative).read_text(encoding="utf-8"),
                ),
                encoding="utf-8",
            )
            written.append(destination)
        subprocess.run([ruff, "format", *map(str, written)], cwd=ROOT, check=True)
    print(f"generated {len(facades)} facade stubs")
    return 0


def main() -> int:
    """Parse command-line arguments and generate the facade stubs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyright", default=shutil.which("pyright"))
    parser.add_argument("--ruff", default=shutil.which("ruff"))
    args = parser.parse_args()
    if not args.pyright:
        parser.error("pyright is required; pass --pyright /path/to/pyright")
    if not args.ruff:
        parser.error("ruff is required; pass --ruff /path/to/ruff")
    return generate(str(args.pyright), str(args.ruff))


if __name__ == "__main__":
    raise SystemExit(main())
