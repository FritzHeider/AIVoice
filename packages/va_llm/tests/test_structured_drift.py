"""Forced function names cannot drift (specs/10 §3; Decision 2026-11-01 P03-T14a rule 2): every
``call_structured(..., function_name=...)`` and ``register_structured_function(...)`` call site in the
repository uses a literal name from ``TOOL_NAMES``, and FakeLLM's own copy equals the set."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from va_llm import TOOL_NAMES
from va_sim.llm.heuristics import STRUCTURED_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class CallSite:
    path: Path
    line: int
    name: str | None  # None: the name is not a string literal

    def where(self) -> str:
        return f"{self.path}:{self.line}"


def _callee(node: ast.Call) -> str | None:
    match node.func:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case _:
            return None


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in node.keywords if item.arg == name), None)


def _name_argument(node: ast.Call) -> tuple[bool, ast.expr | None]:
    """Whether ``node`` names a forced function, and the expression naming it."""
    match _callee(node):
        case "call_structured":
            return True, _keyword(node, "function_name")
        case "register_structured_function":
            return True, node.args[0] if node.args else _keyword(node, "name")
        case _:
            return False, None


def structured_call_sites(paths: Iterable[Path]) -> list[CallSite]:
    """Every call naming a forced function in ``paths`` (Python files)."""
    sites: list[CallSite] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            relevant, argument = _name_argument(node)
            if relevant:
                sites.append(CallSite(path, node.lineno, _literal(argument)))
    return sorted(sites, key=lambda site: (str(site.path), site.line))


def _source_files() -> list[Path]:
    return sorted(
        path
        for pattern in ("apps/*/src/**/*.py", "packages/*/src/**/*.py")
        for path in ROOT.glob(pattern)
        if path.parent.name != "__pycache__"
    )


def test_fakellm_names_equal_the_registry() -> None:
    assert frozenset(STRUCTURED_TOOL_NAMES) == TOOL_NAMES


def test_every_call_site_uses_a_registered_literal_name() -> None:
    sites = structured_call_sites(_source_files())
    bad = [site.where() for site in sites if site.name not in TOOL_NAMES]
    assert bad == []


def test_the_scanner_finds_literal_and_computed_names(tmp_path: Path) -> None:
    source = tmp_path / "caller.py"
    source.write_text(
        "async def run(gateway, name):\n"
        "    await gateway.call_structured('router', M, function_name='select_path', description='')\n"
        "    await gateway.call_structured('router', M, function_name=name, description='')\n"
        "    await gateway.call_structured('router', M, description='')\n"
        "    register_structured_function('pick_path', M)\n"
        "    register_structured_function(name=NAME, schema=M)\n"
        "    gateway.complete('router', messages=[])\n",
        encoding="utf-8",
    )
    sites = structured_call_sites([source])
    assert [(site.line, site.name) for site in sites] == [
        (2, "select_path"),
        (3, None),
        (4, None),
        (5, "pick_path"),
        (6, None),
    ]
    assert [site.where() for site in sites if site.name not in TOOL_NAMES] == [
        f"{source}:3",
        f"{source}:4",
        f"{source}:5",
        f"{source}:6",
    ]
