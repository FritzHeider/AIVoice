"""OpenAPI drift gate (specs/02 §9, specs/26 §1), run by `make check`.

Imports the API application (`va_api.main:app` by default), renders its public document (`app.openapi()`) and
its internal document (`app.state.internal_openapi()`) with `render()`, and compares them with the committed
`openapi/openapi.json` and `openapi/internal.json`; each file is reported separately. `render()` is the single
canonical serializer: `scripts/export_openapi.py` (`make openapi`) must import and use it, through
`render_documents()`, so export and check can never disagree.

Exit codes: 0 when both committed documents are current, 1 on drift in either, when a committed document is
missing, when only the committed public document exists without the application package, or when the
application fails to import, 2 on invalid arguments. While neither the application package nor the committed
public document exists (repository bootstrap, before P00-T11) the check is skipped with exit 0.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

DEFAULT_APP: Final = "va_api.main:app"
DEFAULT_SPEC: Final = Path("openapi/openapi.json")
DEFAULT_INTERNAL_SPEC: Final = Path("openapi/internal.json")
DIFF_LINE_LIMIT: Final = 200
TRUNCATION_NOTE: Final = f"[diff truncated after {DIFF_LINE_LIMIT} lines]"


class AppState(Protocol):
    def internal_openapi(self) -> dict[str, Any]: ...


class AppLike(Protocol):
    @property
    def state(self) -> AppState: ...

    def openapi(self) -> dict[str, Any]: ...


def render(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def app_target(value: str) -> str:
    module, sep, attr = value.partition(":")
    if not sep or not module or not attr:
        msg = f"expected 'module:attribute', got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def load_app(target: str) -> AppLike:
    module_name, _, attr = app_target(target).partition(":")
    module = importlib.import_module(module_name)
    app = getattr(module, attr)
    if not callable(getattr(app, "openapi", None)):
        msg = f"{target} has no callable openapi()"
        raise TypeError(msg)
    if not callable(getattr(getattr(app, "state", None), "internal_openapi", None)):
        msg = f"{target} has no callable state.internal_openapi()"
        raise TypeError(msg)
    loaded: AppLike = app
    return loaded


def render_documents(app: AppLike, *, public: Path, internal: Path) -> dict[Path, str]:
    """The rendered public and internal documents keyed by their paths."""
    return {public: render(app.openapi()), internal: render(app.state.internal_openapi())}


def compare(expected: str, actual: str) -> list[str]:
    diff = list(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="committed",
            tofile="generated",
            lineterm="",
        )
    )
    if len(diff) > DIFF_LINE_LIMIT:
        return [*diff[:DIFF_LINE_LIMIT], TRUNCATION_NOTE]
    return diff


def package_present(target: str) -> bool:
    top_level = target.partition(":")[0].split(".")[0]
    return importlib.util.find_spec(top_level) is not None


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when openapi/openapi.json or openapi/internal.json differs from the app's schemas."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the directory above scripts/)",
    )
    parser.add_argument("--app", type=app_target, default=DEFAULT_APP, help="application as module:attribute")
    parser.add_argument(
        "--spec", type=Path, default=DEFAULT_SPEC, help="committed public document, relative to --root"
    )
    parser.add_argument(
        "--internal-spec",
        type=Path,
        default=DEFAULT_INTERNAL_SPEC,
        help="committed internal document, relative to --root",
    )
    return parser.parse_args(argv)


def _presence_problem(app: str, root: Path, specs: Sequence[Path], has_package: bool) -> str | None:
    if not has_package:
        return f"cannot import {app}: package not found but {specs[0]} exists"
    for spec in specs:
        if not (root / spec).is_file():
            return f"missing committed spec {spec}; run: make openapi"
    return None


def _drifted(root: Path, documents: Mapping[Path, str]) -> list[Path]:
    """Print the diff of every out-of-date document; the out-of-date paths."""
    drifted: list[Path] = []
    for spec, actual in documents.items():
        diff = compare((root / spec).read_text(encoding="utf-8"), actual)
        if diff:
            print("\n".join(diff))
            drifted.append(spec)
        else:
            print(f"check_openapi_drift: {spec} is current")
    return drifted


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root: Path = args.root.resolve()
    specs = [args.spec, args.internal_spec]
    has_package = package_present(args.app)
    if not has_package and not (root / args.spec).is_file():
        print(f"check_openapi_drift: skipped: {args.app} not present yet (lands in P00-T11)", file=sys.stderr)
        return 0
    problem = _presence_problem(args.app, root, specs, has_package)
    if problem is not None:
        print(f"check_openapi_drift: {problem}", file=sys.stderr)
        return 1
    try:
        documents = render_documents(load_app(args.app), public=args.spec, internal=args.internal_spec)
    except Exception as exc:  # any import or schema failure is a gate failure with its message
        print(f"check_openapi_drift: cannot load {args.app}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    drifted = _drifted(root, documents)
    for spec in drifted:
        print(f"check_openapi_drift: {spec} is out of date; run: make openapi", file=sys.stderr)
    return 1 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
