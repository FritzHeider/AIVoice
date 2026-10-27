"""The va_db import graph is acyclic, every mapper configures, and every specs/03 §3-§9a table is mapped."""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Final

import pytest
from model_introspection import spec_table_names
from sqlalchemy.orm import configure_mappers

import va_db
import va_db.models  # registers every table on Base.metadata
from va_db.base import Base

_SRC: Final = Path(va_db.__file__).parent


def _module_name(path: Path) -> str:
    parts = path.relative_to(_SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_modules(tree: ast.Module) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            yield node.module


def import_graph() -> dict[str, set[str]]:
    """Each va_db module mapped to the va_db modules it imports (a package import means its ``__init__``)."""
    modules = {_module_name(path): path for path in sorted(_SRC.rglob("*.py"))}
    return {
        name: {
            imported
            for imported in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
            if imported in modules and imported != name
        }
        for name, path in modules.items()
    }


def test_import_graph_covers_every_model_module() -> None:
    graph = import_graph()
    models = {name for name in graph if name.startswith("va_db.models.")}
    assert {
        "va_db.models.billing",
        "va_db.models.memory",
        "va_db.models.messaging",
        "va_db.models.security",
    } <= models
    assert graph["va_db.models"] >= models - {"va_db.models._checks", "va_db.models._columns"}


def test_va_db_imports_have_no_cycles() -> None:
    order = list(TopologicalSorter(import_graph()).static_order())
    assert order.index("va_db.base") < order.index("va_db.models")


def test_cycle_detection_reports_a_cycle() -> None:
    with pytest.raises(CycleError):
        list(TopologicalSorter({"a": {"b"}, "b": {"a"}}).static_order())


def _sibling_model_imports() -> Iterator[tuple[str, str, str]]:
    """``(importer, model module, name)`` for every name a model module imports from another model module."""
    for path in sorted((_SRC / "models").glob("*.py")):
        importer = _module_name(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("va_db.models."):
                source = str(node.module)
                if importer != "va_db.models" and not source.startswith("va_db.models._"):
                    yield from ((importer, source, alias.name) for alias in node.names)


def test_model_modules_share_only_constants_and_literals() -> None:
    """Model modules reference each other's tables through string foreign keys, never by importing models."""
    shared = set(_sibling_model_imports())
    assert ("va_db.models.memory", "va_db.models.library", "EMBEDDING_DIMENSIONS") in shared
    for importer, source, name in shared:
        value = getattr(importlib.import_module(source), name)
        assert not (isinstance(value, type) and issubclass(value, Base)), (importer, source, name)


def test_mappers_configure() -> None:
    configure_mappers()


def test_every_spec_table_is_mapped() -> None:
    tables = set(spec_table_names())
    assert {"orgs", "calls", "memories", "plans", "org_data_keys"} <= tables
    assert tables == set(Base.metadata.tables)
