"""Structural guardrails for the decomposed implementation modules."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ordeal"
PARTS = SOURCE / "parts"
MAX_LINES = 500
ONE_WORD = re.compile(r"[a-z][a-z0-9]*")


def _implementation_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.glob("*.py") if path.name != "__init__.py")


def _module_folders() -> list[Path]:
    return sorted(
        path for path in PARTS.iterdir() if path.is_dir() and (path / "__init__.py").exists()
    )


def test_every_decomposed_module_has_multiple_one_word_parts() -> None:
    folders = _module_folders()

    assert folders
    for folder in folders:
        implementation_files = _implementation_files(folder)
        assert len(implementation_files) >= 2, folder
        assert all(ONE_WORD.fullmatch(path.stem) for path in implementation_files)


def test_every_source_file_stays_within_the_line_ceiling() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in SOURCE.rglob("*.py")
        if len(path.read_text(encoding="utf-8").splitlines()) > MAX_LINES
    }

    assert offenders == {}


def test_public_facades_point_to_existing_part_folders() -> None:
    facades = []
    for path in SOURCE.rglob("*.py"):
        if PARTS in path.parents:
            continue
        source = path.read_text(encoding="utf-8")
        if "_load_facade_parts" in source:
            facades.append(path)

    assert len(facades) == len(_module_folders())
    for facade in facades:
        source = facade.read_text(encoding="utf-8")
        matching_folders = [
            folder for folder in _module_folders() if f'/ "{folder.name}"' in source
        ]
        assert len(matching_folders) == 1, facade
        assert len(_implementation_files(matching_folders[0])) >= 2


def test_every_facade_uses_a_safe_loading_path() -> None:
    facades = [
        path
        for path in SOURCE.rglob("*.py")
        if PARTS not in path.parents and "_load_facade_parts" in path.read_text(encoding="utf-8")
    ]
    isolated = {SOURCE / "_diff_worker.py", SOURCE / "_observation.py"}

    assert isolated <= set(facades)
    for facade in facades:
        source = facade.read_text(encoding="utf-8")
        if facade in isolated:
            assert "from ordeal" not in source, facade
            assert ".read_bytes()" in source, facade
            assert "exec(compile(" in source, facade
        else:
            assert ".read_bytes()" not in source, facade
            assert "exec(compile(" not in source, facade
            assert "from ordeal._facade_loader import load_parts as _load_parts" in source, facade
            assert "_load_parts(globals(), root, _PART_FILES)" in source, facade


def _stub_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
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


def _part_public_names(folder: Path) -> set[str]:
    names: set[str] = set()
    deleted: set[str] = set()
    for path in _implementation_files(folder):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(
                    target.id
                    for target in targets
                    if isinstance(target, ast.Name) and not target.id.startswith("_")
                )
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "__all__" for target in targets
                ):
                    names.update(ast.literal_eval(node.value))
            elif isinstance(node, ast.Delete):
                deleted.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
    return names - deleted


def _part_deleted_names(folder: Path) -> set[str]:
    return {
        target.id
        for path in _implementation_files(folder)
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Delete)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _rebound_members(folder: Path) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in _implementation_files(folder)]
    class_names = {
        node.name for tree in trees for node in tree.body if isinstance(node, ast.ClassDef)
    }
    for tree in trees:
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in class_names
            ):
                if target.attr != "__qualname__" and (
                    not target.attr.startswith("_")
                    or target.attr in {"__enter__", "__exit__", "__init__"}
                ):
                    members.setdefault(target.value.id, set()).add(target.attr)
    return members


def _stub_class_members(path: Path) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef):
            members[node.name] = {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return members


def test_facade_stubs_cover_runtime_public_names() -> None:
    for facade in sorted(
        path
        for path in SOURCE.rglob("*.py")
        if PARTS not in path.parents and "_load_facade_parts" in path.read_text(encoding="utf-8")
    ):
        relative = facade.relative_to(ROOT).with_suffix("")
        module_name = ".".join(relative.parts)
        if module_name.endswith(".__init__"):
            module_name = module_name.removesuffix(".__init__")
        if module_name == "ordeal":
            module = importlib.import_module(module_name)
            runtime_names = {
                name
                for name in dir(module)
                if not name.startswith("_")
                and not isinstance(getattr(module, name), ModuleType)
                and getattr(getattr(module, name), "__module__", None) != "__future__"
            }
        else:
            source = facade.read_text(encoding="utf-8")
            matching_folders = [
                folder for folder in _module_folders() if f'/ "{folder.name}"' in source
            ]
            assert len(matching_folders) == 1, facade
            runtime_names = _part_public_names(matching_folders[0])
        stub = facade.with_suffix(".pyi")

        assert stub.exists(), facade
        assert runtime_names <= _stub_names(stub), (
            facade,
            sorted(runtime_names - _stub_names(stub)),
        )
        source = facade.read_text(encoding="utf-8")
        matching_folders = [
            folder for folder in _module_folders() if f'/ "{folder.name}"' in source
        ]
        assert len(matching_folders) == 1, facade
        assert not (_part_deleted_names(matching_folders[0]) & _stub_names(stub)), (
            facade,
            sorted(_part_deleted_names(matching_folders[0]) & _stub_names(stub)),
        )
        stub_members = _stub_class_members(stub)
        for class_name, rebound in _rebound_members(matching_folders[0]).items():
            assert rebound <= stub_members.get(class_name, set()), (
                facade,
                class_name,
                sorted(rebound - stub_members.get(class_name, set())),
            )
