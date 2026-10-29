"""Write the API's OpenAPI documents to `openapi/openapi.json` and `openapi/internal.json` (`make openapi`,
specs/26 §1).

The text comes from `check_openapi_drift.render_documents` (the single canonical serializer), so an export
always passes the drift gate. Each file is replaced atomically and only when its content changes.

Exit codes: 0 on success (`wrote <path>` or `unchanged <path>` per file), 1 when the application cannot be
loaded, 2 on invalid arguments.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import check_openapi_drift

DEFAULT_OUT = check_openapi_drift.DEFAULT_SPEC
DEFAULT_INTERNAL_OUT = check_openapi_drift.DEFAULT_INTERNAL_SPEC


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the API's public and internal OpenAPI documents.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the directory above scripts/)",
    )
    parser.add_argument(
        "--app",
        type=check_openapi_drift.app_target,
        default=check_openapi_drift.DEFAULT_APP,
        help="application as module:attribute",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="public document, relative to --root")
    parser.add_argument(
        "--internal-out",
        type=Path,
        default=DEFAULT_INTERNAL_OUT,
        help="internal document, relative to --root",
    )
    return parser.parse_args(argv)


def write_if_changed(path: Path, text: str) -> bool:
    """Atomically replace ``path`` with ``text``; return False (and leave it alone) when already equal."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root: Path = args.root.resolve()
    try:
        documents = check_openapi_drift.render_documents(
            check_openapi_drift.load_app(args.app), public=args.out, internal=args.internal_out
        )
    except Exception as exc:  # any import or schema failure is reported in one line
        print(f"export_openapi: cannot load {args.app}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for relative, text in documents.items():
        print(f"{'wrote' if write_if_changed(root / relative, text) else 'unchanged'} {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
