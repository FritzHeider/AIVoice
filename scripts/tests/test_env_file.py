"""``scripts/env_file.py``: dotenv updates keep comments, line endings, unrelated lines, and the mode."""

from __future__ import annotations

import os
from pathlib import Path

from env_file import update_env_file

IDS = {
    "TELNYX_CALL_CONTROL_APP_ID": "app-1",
    "TELNYX_OUTBOUND_VOICE_PROFILE_ID": "ovp-1",
    "TELNYX_MESSAGING_PROFILE_ID": "mp-1",
}


def test_update_replaces_values_and_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = (
        "# header\n"
        "TELNYX_CALL_CONTROL_APP_ID=  # platform app\n"
        "OTHER=value  # untouched\n"
        "TELNYX_MESSAGING_PROFILE_ID=old\n"
        "TELNYX_OUTBOUND_VOICE_PROFILE_ID=stale   # voice profile\n"
    )
    path.write_text(original, encoding="utf-8")
    assert update_env_file(path, IDS) is True
    assert path.read_text(encoding="utf-8") == (
        "# header\n"
        "TELNYX_CALL_CONTROL_APP_ID=app-1  # platform app\n"
        "OTHER=value  # untouched\n"
        "TELNYX_MESSAGING_PROFILE_ID=mp-1\n"
        "TELNYX_OUTBOUND_VOICE_PROFILE_ID=ovp-1   # voice profile\n"
    )


def test_update_appends_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("VA_ENV=local\nTELNYX_MESSAGING_PROFILE_ID=x", encoding="utf-8")
    assert update_env_file(path, IDS) is True
    assert path.read_text(encoding="utf-8") == (
        "VA_ENV=local\n"
        "TELNYX_MESSAGING_PROFILE_ID=mp-1\n"
        "TELNYX_CALL_CONTROL_APP_ID=app-1\n"
        "TELNYX_OUTBOUND_VOICE_PROFILE_ID=ovp-1\n"
    )


def test_update_creates_an_absent_file(tmp_path: Path) -> None:
    path = tmp_path / "new.env"
    assert update_env_file(path, IDS) is True
    assert path.read_text(encoding="utf-8").splitlines() == [f"{k}={v}" for k, v in IDS.items()]


def test_unchanged_file_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}  # c\r\n" for k, v in IDS.items()), encoding="utf-8")
    os.utime(path, (1_000_000, 1_000_000))
    assert update_env_file(path, IDS) is False
    assert path.stat().st_mtime == 1_000_000
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_update_keeps_the_file_mode(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TELNYX_CALL_CONTROL_APP_ID=x\n", encoding="utf-8")
    path.chmod(0o600)
    update_env_file(path, IDS)
    assert path.stat().st_mode & 0o777 == 0o600
