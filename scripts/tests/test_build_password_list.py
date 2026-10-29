"""scripts/build_password_list.py: normalisation, reproducible gzip output, and checksum verification."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import httpx
import pytest

import build_password_list as bpl
from va_common.auth.passwords import COMMON_PASSWORDS_RESOURCE

SOURCE = "Password\npassword\n\n  dragon  \n\uff24\uff32\uff21\uff27\uff2f\uff2e\n123456\nStraße\n\r\n"


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.txt"
    path.write_bytes(SOURCE.encode("utf-8"))
    return path


def test_normalise_entries() -> None:
    assert bpl.normalise_entries(SOURCE.splitlines()) == ["123456", "dragon", "password", "strasse"]


def test_build_from_local_source_is_reproducible(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _source(tmp_path)
    first, second = tmp_path / "a.txt.gz", tmp_path / "b.txt.gz"
    assert bpl.main(["--source", str(source), "--no-verify", "--output", str(first)]) == 0
    assert bpl.main(["--source", str(source), "--no-verify", "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert gzip.decompress(first.read_bytes()).decode("utf-8") == "123456\ndragon\npassword\nstrasse\n"
    assert "wrote 4 entries" in capsys.readouterr().out


def test_pinned_checksum_is_enforced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "out.txt.gz"
    assert bpl.main(["--source", str(_source(tmp_path)), "--output", str(output)]) == 1
    assert "does not match the pinned" in capsys.readouterr().err
    assert not output.exists()


def test_download_path_verifies_checksum(tmp_path: Path) -> None:
    output = tmp_path / "out.txt.gz"
    assert bpl.main(["--output", str(output)], fetch=lambda: b"tampered\n") == 1
    assert not output.exists()


def test_download_matching_checksum_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = SOURCE.encode("utf-8")
    monkeypatch.setattr(bpl, "SOURCE_SHA256", hashlib.sha256(payload).hexdigest())
    output = tmp_path / "out.txt.gz"
    assert bpl.main(["--output", str(output)], fetch=lambda: payload) == 0
    assert gzip.decompress(output.read_bytes()).splitlines() == [
        b"123456",
        b"dragon",
        b"password",
        b"strasse",
    ]


def test_download_http_error_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def failing() -> bytes:
        raise httpx.ConnectError("unreachable")

    assert bpl.main(["--output", str(tmp_path / "out.gz")], fetch=failing) == 1
    assert "unreachable" in capsys.readouterr().err


def test_download_uses_pinned_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == bpl.SOURCE_URL
        return httpx.Response(200, content=b"payload")

    transport = httpx.MockTransport(handler)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert kwargs["timeout"] == bpl.HTTP_TIMEOUT_S
        with httpx.Client(transport=transport, follow_redirects=True) as client:
            return client.get(url)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert bpl.download() == b"payload"
    assert bpl.SOURCE_COMMIT in bpl.SOURCE_URL


def test_no_verify_requires_source(capsys: pytest.CaptureFixture[str]) -> None:
    assert bpl.main(["--no-verify"]) == 1
    assert "--no-verify requires --source" in capsys.readouterr().err


def test_output_path_is_the_bundled_resource() -> None:
    assert bpl.OUTPUT_REL.endswith("va_common/auth/" + COMMON_PASSWORDS_RESOURCE)
    assert (bpl.REPO_ROOT / bpl.OUTPUT_REL).is_file()
