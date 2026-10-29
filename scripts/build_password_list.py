"""Build the bundled breached-password list (specs/04-auth-tenancy-rbac.md §4.2).

Downloads the SecLists xato top-100 000 list at a pinned commit, verifies its sha256, normalises every entry
the way the policy looks passwords up (``va_common.auth.passwords.breached_list_entry``: NFKC, casefolded),
drops blanks and duplicates, sorts, and writes one entry per line to
``packages/va_common/src/va_common/auth/data/common_passwords.txt.gz``. The gzip header carries ``mtime=0``
and no file name, so rebuilding from the same source is byte-identical.

Usage: ``uv run python scripts/build_password_list.py`` (network) or ``--source PATH`` to build from a local
copy (still checked against the pinned sha256 unless ``--no-verify`` is given; tests use that with small
fixtures).

Exit codes: 0 success, 1 download or checksum failure.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Final

import httpx

from va_common.auth.passwords import breached_list_entry

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
OUTPUT_REL: Final = "packages/va_common/src/va_common/auth/data/common_passwords.txt.gz"
SOURCE_COMMIT: Final = "c205c36a445bff37f8e58a9ec829105cd4975c58"
SOURCE_PATH: Final = "Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt"
SOURCE_URL: Final = f"https://raw.githubusercontent.com/danielmiessler/SecLists/{SOURCE_COMMIT}/{SOURCE_PATH}"
SOURCE_SHA256: Final = "1472aafa2561df5e3293aee252aee3ca660c12b399a283cf808bb01b39be388b"
HTTP_TIMEOUT_S: Final = 30.0


class ChecksumError(Exception):
    """The source list does not match the pinned sha256."""


def download(url: str = SOURCE_URL) -> bytes:
    """GET ``url`` (redirects followed, 30 s timeout); ``httpx.HTTPError`` on failure."""
    response = httpx.get(url, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return response.content


def verify_sha256(data: bytes, expected: str) -> None:
    """Raise :class:`ChecksumError` when ``sha256(data)`` is not ``expected``."""
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        msg = f"source sha256 {actual} does not match the pinned {expected}"
        raise ChecksumError(msg)


def normalise_entries(lines: Iterable[str]) -> list[str]:
    """Stripped, NFKC-casefolded, non-blank, deduplicated, sorted entries."""
    entries = {breached_list_entry(line.strip()) for line in lines}
    entries.discard("")
    return sorted(entries)


def render(entries: Sequence[str]) -> bytes:
    """Gzip of the entries joined by newlines, with a fixed header (``mtime=0``, no file name)."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as stream:
        stream.write("".join(f"{entry}\n" for entry in entries).encode("utf-8"))
    return buffer.getvalue()


def build(source: bytes, output: Path) -> int:
    """Write the normalised list built from ``source`` to ``output``; returns the entry count."""
    entries = normalise_entries(source.decode("utf-8").splitlines())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render(entries))
    return len(entries)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="build from this local file instead of downloading")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / OUTPUT_REL, help="gzip file to write")
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the pinned sha256 check (local --source only)"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, fetch: Callable[[], bytes] = download) -> int:
    args = parse_args(argv)
    if args.no_verify and args.source is None:
        print("--no-verify requires --source", file=sys.stderr)
        return 1
    try:
        source = args.source.read_bytes() if args.source is not None else fetch()
        if not args.no_verify:
            verify_sha256(source, SOURCE_SHA256)
    except (OSError, httpx.HTTPError, ChecksumError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    count = build(source, args.output)
    print(f"wrote {count} entries to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
