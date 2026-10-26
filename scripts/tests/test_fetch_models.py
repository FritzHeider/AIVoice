"""`scripts/fetch_models.py`: first fetch records, later fetches verify, `--check` catches corruption."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

import fetch_models as fm

ROOT = Path(__file__).resolve().parents[2]
SPEC_07 = ROOT / "specs" / "07-media-pipeline.md"


class FakeFetch:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        return self.data


def _refuse(url: str) -> bytes:
    msg = f"unexpected download of {url}"
    raise AssertionError(msg)


@pytest.fixture
def model_bytes() -> bytes:
    return b"\x08\x08\x12\x04onnx-fixture" + os.urandom(256)


@pytest.fixture
def fetched_root(tmp_path: Path, model_bytes: bytes) -> Path:
    assert fm.main([], root=tmp_path, fetch=FakeFetch(model_bytes)) == 0
    return tmp_path


def _manifest(root: Path) -> dict[str, fm.ManifestEntry]:
    return fm.load_manifest(root / fm.MANIFEST_REL)


def _model_path(root: Path) -> Path:
    return root / fm.SILERO.path


def test_first_fetch_records_sha_and_bytes(
    tmp_path: Path, model_bytes: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeFetch(model_bytes)
    assert fm.main([], root=tmp_path, fetch=fake) == 0
    assert fake.calls == [fm.SILERO.url]
    assert _model_path(tmp_path).read_bytes() == model_bytes
    entry = _manifest(tmp_path)["silero_vad"]
    assert entry.sha256 == hashlib.sha256(model_bytes).hexdigest()
    assert entry.bytes == len(model_bytes)
    assert (entry.url, entry.tag, entry.license, entry.source) == (
        fm.SILERO.url,
        fm.SILERO.tag,
        "MIT",
        "snakers4/silero-vad",
    )
    assert "models/silero_vad.onnx: recorded" in capsys.readouterr().out


def test_manifest_is_canonical_json(fetched_root: Path) -> None:
    text = (fetched_root / fm.MANIFEST_REL).read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert text == fm.render_manifest(_manifest(fetched_root))
    assert json.loads(text)["version"] == 1


def test_second_run_does_not_download(fetched_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert fm.main([], root=fetched_root, fetch=_refuse) == 0
    assert "models/silero_vad.onnx: ok" in capsys.readouterr().out


def test_check_passes_on_intact_model(fetched_root: Path) -> None:
    assert fm.main(["--check"], root=fetched_root, fetch=_refuse) == 0


def test_check_fails_on_corrupted_model(fetched_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _model_path(fetched_root)
    data = bytearray(path.read_bytes())
    data[10] ^= 0xFF
    path.write_bytes(bytes(data))
    capsys.readouterr()
    assert fm.main(["--check"], root=fetched_root, fetch=_refuse) == 1
    err = capsys.readouterr().err
    assert "models/silero_vad.onnx: sha256 mismatch" in err
    assert hashlib.sha256(bytes(data)).hexdigest() in err


def test_check_fails_when_missing(fetched_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _model_path(fetched_root).unlink()
    assert fm.main(["--check"], root=fetched_root, fetch=_refuse) == 1
    assert "models/silero_vad.onnx: missing" in capsys.readouterr().err


def test_check_fails_when_unrecorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fm.main(["--check"], root=tmp_path, fetch=_refuse) == 1
    assert "not recorded in manifest" in capsys.readouterr().err


def test_corrupted_file_is_redownloaded_when_digest_matches_manifest(
    fetched_root: Path, model_bytes: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    _model_path(fetched_root).write_bytes(b"garbage")
    fake = FakeFetch(model_bytes)
    capsys.readouterr()
    assert fm.main([], root=fetched_root, fetch=fake) == 0
    assert fake.calls == [fm.SILERO.url]
    assert _model_path(fetched_root).read_bytes() == model_bytes
    assert "models/silero_vad.onnx: downloaded" in capsys.readouterr().out


def test_download_mismatching_manifest_is_rejected_and_not_written(
    fetched_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _model_path(fetched_root).unlink()
    before = (fetched_root / fm.MANIFEST_REL).read_bytes()
    assert fm.main([], root=fetched_root, fetch=FakeFetch(b"tampered upstream")) == 1
    assert not _model_path(fetched_root).exists()
    assert (fetched_root / fm.MANIFEST_REL).read_bytes() == before
    assert "sha256 mismatch" in capsys.readouterr().err


def test_update_accepts_new_digest(fetched_root: Path) -> None:
    new = b"new upstream build"
    assert fm.main(["--update"], root=fetched_root, fetch=FakeFetch(new)) == 0
    assert _manifest(fetched_root)["silero_vad"].sha256 == hashlib.sha256(new).hexdigest()
    assert _model_path(fetched_root).read_bytes() == new


def test_manifest_pinned_to_other_tag_is_rejected(
    fetched_root: Path, model_bytes: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = _manifest(fetched_root)["silero_vad"]
    stale = replace(entry, tag="v4.0", url=entry.url.replace("v5.1.2", "v4.0"))
    fm.write_manifest(fetched_root / fm.MANIFEST_REL, {"silero_vad": stale})
    assert fm.main([], root=fetched_root, fetch=_refuse) == 1
    assert "--update" in capsys.readouterr().err
    assert fm.main(["--update"], root=fetched_root, fetch=FakeFetch(model_bytes)) == 0
    assert _manifest(fetched_root)["silero_vad"].tag == "v5.1.2"


def test_check_and_update_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        fm.main(["--check", "--update"], root=tmp_path, fetch=_refuse)


@pytest.mark.parametrize(
    "exc", [urllib.error.URLError("no route"), TimeoutError("timed out"), ValueError("too big")]
)
def test_network_error_exits_1(tmp_path: Path, exc: Exception, capsys: pytest.CaptureFixture[str]) -> None:
    def failing(url: str) -> bytes:
        raise exc

    assert fm.main([], root=tmp_path, fetch=failing) == 1
    assert capsys.readouterr().err.startswith("fetch_models: ")
    assert not _model_path(tmp_path).exists()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 2, "models": []}',
        '{"version": 1}',
        '{"version": 1, "models": ["x"]}',
        '{"version": 1, "models": [{"name": "x"}]}',
        (
            '{"version": 1, "models": [{"name": "x", "path": "p", "url": "u", "source": "s", "tag": "t",'
            ' "license": "l", "sha256": "h", "bytes": "12"}]}'
        ),
    ],
)
def test_malformed_manifest_exits_1(tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / fm.MANIFEST_REL
    manifest.parent.mkdir(parents=True)
    manifest.write_text(content, encoding="utf-8")
    assert fm.main(["--check"], root=tmp_path, fetch=_refuse) == 1
    assert "fetch_models: " in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://example.com/m.onnx", "file:///etc/passwd", "ftp://x/y"])
def test_http_fetch_rejects_non_https(url: str) -> None:
    with pytest.raises(ValueError, match="non-https"):
        fm.http_fetch(url)


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, n: int) -> bytes:
        return self._data[:n]


def test_http_fetch_reads_with_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(url: str, timeout: float) -> _FakeResponse:
        seen.update(url=url, timeout=timeout)
        return _FakeResponse(b"model")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert fm.http_fetch("https://example.com/m.onnx") == b"model"
    assert seen == {"url": "https://example.com/m.onnx", "timeout": fm.HTTP_TIMEOUT_S}


def test_http_fetch_size_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fm, "MAX_DOWNLOAD_BYTES", 4)

    def oversized(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"123456")

    monkeypatch.setattr(urllib.request, "urlopen", oversized)
    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        fm.http_fetch("https://example.com/m.onnx")


def test_committed_manifest_matches_spec() -> None:
    entries = fm.load_manifest(ROOT / fm.MANIFEST_REL)
    assert list(entries) == ["silero_vad"]
    entry = entries["silero_vad"]
    assert entry.url.endswith("/v5.1.2/src/silero_vad/data/silero_vad.onnx")
    assert re.fullmatch(r"[0-9a-f]{64}", entry.sha256)
    assert entry.bytes > 1_000_000
    assert (entry.url, entry.tag, entry.path) == (fm.SILERO.url, fm.SILERO.tag, fm.SILERO.path)
    spec = " ".join(SPEC_07.read_text(encoding="utf-8").split())
    match = re.search(r"repository path `(?P<path>[^`]+)` at tag `(?P<tag>[^`]+)`", spec)
    assert match is not None
    assert entry.tag == match["tag"]
    assert entry.url == f"https://raw.githubusercontent.com/{entry.source}/{match['tag']}/{match['path']}"
    assert (ROOT / fm.MANIFEST_REL).read_text(encoding="utf-8") == fm.render_manifest(entries)


def test_shell_wrapper() -> None:
    wrapper = ROOT / "scripts" / "fetch_models.sh"
    assert os.access(wrapper, os.X_OK)
    text = wrapper.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert 'exec uv run --no-sync python scripts/fetch_models.py "$@"' in text


@pytest.mark.skipif(
    not (ROOT / fm.SILERO.path).exists(), reason="model not fetched (scripts/fetch_models.sh)"
)
def test_local_model_matches_committed_manifest() -> None:
    assert fm.main(["--check"], root=ROOT, fetch=_refuse) == 0
