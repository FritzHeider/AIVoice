"""API key pepper settings (specs/02-repo-layout-and-conventions.md §6; specs/28-security.md §2)."""

from __future__ import annotations

import secrets

import pytest
from api_helpers import make_settings
from pydantic import ValidationError

from va_common.auth import generate_api_key


def test_empty_pepper_is_allowed_until_the_hasher_is_built() -> None:
    settings = make_settings(api_key_pepper="")
    assert settings.api_key_pepper_version == 1
    with pytest.raises(RuntimeError, match="API_KEY_PEPPER is not configured"):
        settings.api_key_hasher()


@pytest.mark.parametrize("field", ["api_key_pepper", "api_key_pepper_previous"])
@pytest.mark.parametrize("value", ["ab" * 31, "xy" * 32, "ab" * 33])
def test_malformed_pepper_is_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="64 hex characters"):
        make_settings(**{field: value, "api_key_pepper_version": 2})


def test_previous_pepper_requires_rotated_version() -> None:
    with pytest.raises(ValidationError, match="API_KEY_PEPPER_VERSION of at least 2"):
        make_settings(api_key_pepper_previous=secrets.token_hex(32))


def test_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_key_pepper_version=0)


def test_hasher_wires_current_and_previous_versions() -> None:
    old, new = secrets.token_hex(32), secrets.token_hex(32).upper()
    key = generate_api_key("live")
    before = make_settings(api_key_pepper=old).api_key_hasher()
    after = make_settings(
        api_key_pepper=new, api_key_pepper_previous=old, api_key_pepper_version=2
    ).api_key_hasher()
    assert before.current_version == 1
    assert after.current_version == 2
    assert after.verify(key, before.hash(key), 1)
    assert after.hash(key) != before.hash(key)


def test_hasher_without_previous_pepper_rejects_old_version() -> None:
    hasher = make_settings(api_key_pepper_version=3).api_key_hasher()
    key = generate_api_key("test")
    assert not hasher.verify(key, hasher.hash(key), 2)


def test_local_master_key_may_be_empty_but_not_malformed() -> None:
    assert make_settings(local_master_key="").local_master_key.get_secret_value() == ""
    with pytest.raises(ValidationError, match="LOCAL_MASTER_KEY must be 64 hex characters"):
        make_settings(local_master_key="ab" * 31)
