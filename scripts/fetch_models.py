"""Download and verify the ML models listed in specs/07 §5.3, recorded in `models/MANIFEST.json`.

`scripts/fetch_models.sh` is the entry point (`make setup` runs it). Modes:

- default: download every model that is missing or whose file does not match the recorded sha256, and record
  the digest and size of a model fetched for the first time. A download whose digest differs from the recorded
  one is rejected and never written.
- `--check`: verify only, never download; exit 1 when a model is missing, unrecorded, or does not match.
- `--update`: accept a new digest or a new pinned release (url/tag) and re-record it.

Exit codes: 0 success, 1 any verification, checksum, manifest, or network failure.
"""

from __future__ import annotations

import argparse
import enum
import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
MANIFEST_REL: Final = "models/MANIFEST.json"
MANIFEST_VERSION: Final = 1
MAX_DOWNLOAD_BYTES: Final = 50 * 1024 * 1024
HTTP_TIMEOUT_S: Final = 60.0
CHUNK: Final = 1024 * 1024


class ManifestError(Exception):
    """The manifest is malformed or pinned to a different release than the code."""


class ChecksumError(Exception):
    """A downloaded file does not match the digest recorded in the manifest."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    path: str
    url: str
    source: str
    tag: str
    license: str


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    name: str
    path: str
    url: str
    source: str
    tag: str
    license: str
    sha256: str
    bytes: int


class VerifyResult(enum.Enum):
    OK = "ok"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNRECORDED = "unrecorded"


SILERO: Final = ModelSpec(
    name="silero_vad",
    path="models/silero_vad.onnx",
    url="https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx",
    source="snakers4/silero-vad",
    tag="v5.1.2",
    license="MIT",
)
MODELS: Final[tuple[ModelSpec, ...]] = (SILERO,)

Fetcher = Callable[[str], bytes]


def _entry_from_json(raw: object) -> ManifestEntry:
    if not isinstance(raw, dict):
        msg = "manifest model entry is not an object"
        raise ManifestError(msg)
    try:
        entry = ManifestEntry(**raw)
    except TypeError as exc:
        msg = f"manifest model entry has wrong fields: {exc}"
        raise ManifestError(msg) from exc
    text_fields = (entry.name, entry.path, entry.url, entry.source, entry.tag, entry.license, entry.sha256)
    if not all(isinstance(v, str) for v in text_fields) or type(entry.bytes) is not int:
        msg = f"manifest model entry {entry.name!r} has wrong field types"
        raise ManifestError(msg)
    return entry


def load_manifest(path: Path) -> dict[str, ManifestEntry]:
    """Read the manifest; a missing file is an empty manifest."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{path.name} is not valid JSON: {exc}"
        raise ManifestError(msg) from exc
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        msg = f"{path.name} must be an object with version {MANIFEST_VERSION}"
        raise ManifestError(msg)
    models = data.get("models")
    if not isinstance(models, list):
        msg = f"{path.name} has no models list"
        raise ManifestError(msg)
    entries = [_entry_from_json(m) for m in models]
    return {e.name: e for e in entries}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def render_manifest(entries: Mapping[str, ManifestEntry]) -> str:
    models = [asdict(entries[name]) for name in sorted(entries)]
    return json.dumps({"version": MANIFEST_VERSION, "models": models}, indent=2, sort_keys=True) + "\n"


def write_manifest(path: Path, entries: Mapping[str, ManifestEntry]) -> None:
    _atomic_write(path, render_manifest(entries).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def http_fetch(url: str) -> bytes:
    """Download `url` over HTTPS with a timeout and a size guard."""
    if not url.startswith("https://"):
        msg = f"refusing non-https model url: {url}"
        raise ValueError(msg)
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 - scheme checked above
        data: bytes = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        msg = f"model download exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}"
        raise ValueError(msg)
    return data


def verify(spec: ModelSpec, entry: ManifestEntry | None, root: Path) -> VerifyResult:
    if entry is None:
        return VerifyResult.UNRECORDED
    target = root / spec.path
    if not target.is_file():
        return VerifyResult.MISSING
    return VerifyResult.OK if sha256_file(target) == entry.sha256 else VerifyResult.MISMATCH


def _check_pin(spec: ModelSpec, entry: ManifestEntry) -> None:
    pinned = (entry.url, entry.tag, entry.path)
    if pinned != (spec.url, spec.tag, spec.path):
        msg = (
            f"{spec.path}: manifest is pinned to {entry.tag} ({entry.url}), code expects {spec.tag}; "
            "run with --update to re-pin"
        )
        raise ManifestError(msg)


def _new_entry(spec: ModelSpec, data: bytes) -> ManifestEntry:
    return ManifestEntry(
        name=spec.name,
        path=spec.path,
        url=spec.url,
        source=spec.source,
        tag=spec.tag,
        license=spec.license,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
    )


def fetch_one(
    spec: ModelSpec, entry: ManifestEntry | None, root: Path, fetch: Fetcher, *, update: bool = False
) -> tuple[ManifestEntry, str]:
    """Ensure one model file is present and matches; return its manifest entry and a status word."""
    if entry is not None and not update:
        _check_pin(spec, entry)
        if verify(spec, entry, root) is VerifyResult.OK:
            return entry, "ok"
    data = fetch(spec.url)
    fresh = _new_entry(spec, data)
    if entry is not None and not update and fresh.sha256 != entry.sha256:
        msg = f"{spec.path}: sha256 mismatch (expected {entry.sha256}, got {fresh.sha256})"
        raise ChecksumError(msg)
    _atomic_write(root / spec.path, data)
    return fresh, ("recorded" if entry is None else "downloaded")


def _check_message(spec: ModelSpec, entry: ManifestEntry | None, result: VerifyResult, root: Path) -> str:
    if result is VerifyResult.MISMATCH and entry is not None:
        got = sha256_file(root / spec.path)
        return f"{spec.path}: sha256 mismatch (expected {entry.sha256}, got {got})"
    if result is VerifyResult.MISSING:
        return f"{spec.path}: missing"
    return f"{spec.path}: not recorded in manifest"


def run_check(manifest: Mapping[str, ManifestEntry], root: Path) -> int:
    failures = 0
    for spec in MODELS:
        entry = manifest.get(spec.name)
        result = verify(spec, entry, root)
        if result is VerifyResult.OK:
            print(f"{spec.path}: ok")
            continue
        failures += 1
        print(_check_message(spec, entry, result, root), file=sys.stderr)
    return 1 if failures else 0


def run_fetch(manifest: dict[str, ManifestEntry], root: Path, fetch: Fetcher, *, update: bool) -> int:
    for spec in MODELS:
        entry, status = fetch_one(spec, manifest.get(spec.name), root, fetch, update=update)
        manifest[spec.name] = entry
        print(f"{spec.path}: {status}")
    write_manifest(root / MANIFEST_REL, manifest)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download and verify ML models (models/MANIFEST.json).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify only; never download")
    mode.add_argument("--update", action="store_true", help="accept and re-record new digests or pins")
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path = REPO_ROOT, fetch: Fetcher = http_fetch) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(root / MANIFEST_REL)
        if args.check:
            return run_check(manifest, root)
        return run_fetch(manifest, root, fetch, update=args.update)
    except (ManifestError, ChecksumError, ValueError, urllib.error.URLError, TimeoutError) as exc:
        print(f"fetch_models: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
