"""Pure parts of org provisioning: slugs and the webhook secret AAD (specs/04-auth-tenancy-rbac.md §4.1)."""

from __future__ import annotations

import re
import uuid

import pytest

from va_db.provisioning import (
    SLUG_ATTEMPTS,
    SLUG_MAX_BASE,
    SLUG_SUFFIX_LEN,
    WEBHOOK_SECRET_PREFIX,
    make_slug,
    random_slug_suffix,
    slug_base,
    webhook_secret_aad,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Corp", "acme-corp"),
        ("  Ünïcödé Café!! ", "unicode-cafe"),
        ("日本", "org"),
        ("---", "org"),
        ("", "org"),
        ("Dr. Smith's  Dental & Co.", "dr-smith-s-dental-co"),
        ("ﬁne ① plan", "fine-1-plan"),
        ("a" * 39 + " b" + "c" * 20, "a" * 39),
        ("x" * 60, "x" * 40),
    ],
)
def test_slug_base(name: str, expected: str) -> None:
    assert slug_base(name) == expected


def test_slug_base_is_bounded_and_trimmed() -> None:
    base = slug_base("word " * 30)
    assert len(base) <= SLUG_MAX_BASE
    assert not base.startswith("-")
    assert not base.endswith("-")


def test_make_slug_appends_random_suffix() -> None:
    slug = make_slug("Acme Corp")
    assert re.fullmatch(r"acme-corp-[a-z0-9]{4}", slug)
    assert make_slug("Acme Corp", suffix="zz99") == "acme-corp-zz99"
    assert re.fullmatch(r"^[a-z0-9-]+-[a-z0-9]{4}$", make_slug("日本"))


def test_random_slug_suffix_alphabet() -> None:
    suffixes = {random_slug_suffix() for _ in range(200)}
    assert all(re.fullmatch(r"[a-z0-9]{4}", suffix) for suffix in suffixes)
    assert len(suffixes) > 150


def test_constants_match_spec() -> None:
    assert (SLUG_MAX_BASE, SLUG_SUFFIX_LEN, SLUG_ATTEMPTS, WEBHOOK_SECRET_PREFIX) == (40, 4, 5, "whsec_")


def test_webhook_secret_aad() -> None:
    org_id = uuid.UUID("01890000-0000-7000-8000-00000000000a")
    assert webhook_secret_aad(org_id) == f"orgs.webhook_signing_secret_enc:{org_id}"
