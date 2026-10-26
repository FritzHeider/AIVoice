"""OpenAPI drift gate (specs/02 §9), run by `make check`.

Imports the API application (`va_api.main:app` by default), renders its OpenAPI document with `render()`, and
compares the result with the committed `openapi/openapi.json`. `render()` is the single canonical serializer:
`scripts/export_openapi.py` (`make openapi`) must import and use it so export and check can never disagree.

Exit codes: 0 when the committed document is current, 1 on drift or when only one of {application package,
committed document} exists or the application fails to import, 2 on invalid arguments. While neither the
application package nor the committed document exists (repository bootstrap, before P00-T11) the check is
skipped with exit 0.
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
DIFF_LINE_LIMIT: Final = 200
TRUNCATION_NOTE: Final = f"[diff truncated after {DIFF_LINE_LIMIT} lines]"


class AppLike(Protocol):
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
    loaded: AppLike = app
    return loaded


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
        description="Fail when openapi/openapi.json differs from the app's schema."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the directory above scripts/)",
    )
    parser.add_argument("--app", type=app_target, default=DEFAULT_APP, help="application as module:attribute")
    parser.add_argument(
        "--spec", type=Path, default=DEFAULT_SPEC, help="committed OpenAPI document, relative to --root"
    )
    return parser.parse_args(argv)


def _presence_problem(app: str, spec_path: Path, has_package: bool) -> str | None:
    if not has_package:
        return f"cannot import {app}: package not found but {spec_path} exists"
    if not spec_path.is_file():
        return f"missing committed spec {spec_path}; run: make openapi"
    return None


def _generate(app: str) -> str:
    return render(load_app(app).openapi())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    spec_path: Path = args.root.resolve() / args.spec
    has_package = package_present(args.app)
    if not has_package and not spec_path.is_file():
        print(f"check_openapi_drift: skipped: {args.app} not present yet (lands in P00-T11)", file=sys.stderr)
        return 0
    problem = _presence_problem(args.app, spec_path, has_package)
    if problem is not None:
        print(f"check_openapi_drift: {problem}", file=sys.stderr)
        return 1
    try:
        actual = _generate(args.app)
    except Exception as exc:  # any import or schema failure is a gate failure with its message
        print(f"check_openapi_drift: cannot load {args.app}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    diff = compare(spec_path.read_text(encoding="utf-8"), actual)
    if not diff:
        print(f"check_openapi_drift: {args.spec} is current")
        return 0
    print("\n".join(diff))
    print(f"check_openapi_drift: {args.spec} is out of date; run: make openapi", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
