"""``GET/PATCH /v1/org/settings`` and ``settings`` on ``PATCH /v1/org`` against a real database (specs/04 §7,
§8.1 Decision "Org settings endpoints (P02-T12b)")."""

from __future__ import annotations

import uuid
from typing import Any, get_args

import fakeredis
import httpx
import pytest
from identity_db import IdentityDb
from session_helpers import (
    SessionContainer,
    audit_rows,
    error_of,
    make_member,
    set_org,
    signed_in,
    store_api_key,
    stored_org,
    sub_account,
    write_headers,
)

from va_api.routers.org_settings import (
    BAA_ADMIN_ONLY_MESSAGE,
    HIPAA_NEEDS_BAA_MESSAGE,
    ZERO_RETENTION_PLAN_MESSAGE,
)
from va_common import json
from va_db.models import MemberRole
from va_db.org_settings import OrgSettingsReader, settings_cache_key
from va_domain.org import OrgSettings, load_settings

SETTINGS = "/v1/org/settings"
ORG = "/v1/org"
ROLES = get_args(MemberRole)
VOICE_A = "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e6f"
VOICE_B = "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e70"
BAA = "2026-10-01T12:00:00+00:00"


async def patch(
    client: httpx.AsyncClient, body: dict[str, Any], path: str = SETTINGS, **headers: str
) -> httpx.Response:
    return await client.patch(path, json=body, headers={**write_headers(client), **headers})


async def patched(client: httpx.AsyncClient, body: dict[str, Any], path: str = SETTINGS) -> dict[str, Any]:
    response = await patch(client, body, path)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def defaults(**stored: Any) -> dict[str, Any]:
    return load_settings(stored).model_dump(mode="json")


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


async def stored_settings(identity: IdentityDb, org_id: uuid.UUID) -> dict[str, Any]:
    return dict((await stored_org(identity, org_id)).settings)


async def test_a_fresh_org_reads_the_defaults(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    response = await client.get(SETTINGS)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == OrgSettings().model_dump(mode="json")
    assert (await client.get(ORG)).json()["data"]["settings"] == OrgSettings().model_dump(mode="json")


async def test_patch_stores_only_what_was_sent(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    data = await patched(client, {"timezone": "America/New_York"})
    assert data == defaults(timezone="America/New_York")
    assert await stored_settings(identity, member.org.id) == {"timezone": "America/New_York"}


async def test_groups_merge_and_other_values_are_replaced(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await patched(client, {"llm": {"dialogue_model": "acme-large"}, "default_webhook_events": ["call"]})
    await patched(client, {"voice_overrides": {VOICE_A: {}}, "default_from_number": "+14155550100"})
    data = await patched(
        client,
        {
            "llm": {"temperature_default": 0.2},
            "default_webhook_events": ["queue", "tool"],
            "voice_overrides": {VOICE_B: {}},
            "default_from_number": None,
        },
    )
    stored = await stored_settings(identity, member.org.id)
    assert stored["llm"] == {"dialogue_model": "acme-large", "temperature_default": 0.2}
    assert stored["default_webhook_events"] == ["queue", "tool"]
    assert list(stored["voice_overrides"]) == [VOICE_B]
    assert stored["default_from_number"] is None
    assert data == defaults(**stored)


async def test_an_empty_patch_returns_the_current_settings(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await set_org(identity, member.org.id, settings={"default_record": True})
    assert await patched(client, {}) == defaults(default_record=True)
    assert await audit_rows(identity, member.org.id, "org.update") == []


async def test_values_are_stored_normalized(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    # 127.0.0.1 is the test transport's peer: an allowlist must admit the request setting it.
    data = await patched(client, {"ip_allowlist": ["127.0.0.1", "10.0.0.1/8", "2001:db8::1/32"]})
    normalized = ["127.0.0.1/32", "10.0.0.0/8", "2001:db8::/32"]
    assert data["ip_allowlist"] == normalized
    assert await stored_settings(identity, member.org.id) == {"ip_allowlist": normalized}


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"timezone": None}, "timezone"),
        ({"timezone": "Mars/Olympus_Mons"}, "timezone"),
        (
            {"compliance": {"calling_hours": [{"days": ["mon"], "start": "09:00", "end": "25:00"}]}},
            "compliance.calling_hours[0].end",
        ),
        ({"ip_allowlist": ["10.0.0.0/8", "not-a-network"]}, "ip_allowlist[1]"),
        ({"bogus": 1}, "bogus"),
        ({"llm": {"bogus": 1}}, "llm.bogus"),
        ({"voice_overrides": {"not-a-uuid": {}}}, "voice_overrides.not-a-uuid"),
    ],
)
async def test_invalid_settings_name_the_field(
    client: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    body: dict[str, Any],
    field: str,
) -> None:
    member = await signed_in(client, identity, container)
    assert error_of(await patch(client, body)) == (400, "INVALID_INPUT", field)
    assert error_of(await patch(client, {"settings": body}, ORG)) == (
        400,
        "INVALID_INPUT",
        f"settings.{field}",
    )
    assert await stored_settings(identity, member.org.id) == {}


@pytest.mark.parametrize("role", ROLES)
async def test_every_role_reads_the_settings(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member = await signed_in(client, identity, container, role=role)
    await set_org(identity, member.org.id, settings={"timezone": "America/Chicago"})
    response = await client.get(SETTINGS)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == defaults(timezone="America/Chicago")


@pytest.mark.parametrize("role", ROLES)
async def test_only_owners_and_admins_change_settings(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole
) -> None:
    member = await signed_in(client, identity, container, role=role)
    for body in (
        {"default_record": True},
        {"mfa_required": True, "compliance": {"state_quiet_hours": False}},
    ):
        response = await patch(client, body)
        if role in {"owner", "admin"}:
            assert response.status_code == 200, response.text
        else:
            assert error_of(response)[:2] == (403, "FORBIDDEN")
    expected = {"default_record": True, "mfa_required": True, "compliance": {"state_quiet_hours": False}}
    assert await stored_settings(identity, member.org.id) == (expected if role in {"owner", "admin"} else {})


async def test_compliance_settings_need_the_compliance_permission(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    narrow, _ = await store_api_key(container, member.org.id, ("org:read", "org:write"))
    wide, _ = await store_api_key(container, member.org.id, ("*",))
    for body in (
        {"mfa_required": True},
        {"compliance": {"state_quiet_hours": False}},
        {"privacy": {"redact_transcripts": True}},
    ):
        response = await client.patch(SETTINGS, json=body, headers={"Authorization": narrow})
        assert error_of(response)[:2] == (403, "FORBIDDEN")
        assert message_of(response) == "Missing required permission 'org.compliance'."
        response = await client.patch(ORG, json={"settings": body}, headers={"Authorization": narrow})
        assert error_of(response)[:2] == (403, "FORBIDDEN")
    response = await client.patch(SETTINGS, json={"default_record": True}, headers={"Authorization": narrow})
    assert response.status_code == 200, response.text
    response = await client.patch(SETTINGS, json={"mfa_required": True}, headers={"Authorization": wide})
    assert response.status_code == 200, response.text
    assert await stored_settings(identity, member.org.id) == {"default_record": True, "mfa_required": True}


async def test_only_platform_administrators_set_the_baa(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    for value in (BAA, None):
        response = await patch(client, {"baa_signed_at": value})
        assert error_of(response) == (403, "FORBIDDEN", "baa_signed_at")
        assert message_of(response) == BAA_ADMIN_ONLY_MESSAGE
    response = await patch(client, {"settings": {"baa_signed_at": BAA}}, ORG)
    assert error_of(response) == (403, "FORBIDDEN", "settings.baa_signed_at")
    assert await stored_settings(identity, member.org.id) == {}


async def test_hipaa_mode_needs_a_signed_baa(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    response = await patch(client, {"hipaa_mode": True})
    assert error_of(response) == (400, "INVALID_INPUT", "hipaa_mode")
    assert message_of(response) == HIPAA_NEEDS_BAA_MESSAGE
    response = await patch(client, {"settings": {"hipaa_mode": True}}, ORG)
    assert error_of(response) == (400, "INVALID_INPUT", "settings.hipaa_mode")
    await set_org(identity, member.org.id, settings={"baa_signed_at": BAA})
    assert (await patched(client, {"hipaa_mode": True}))["hipaa_mode"] is True
    # A BAA removed out of band does not block unrelated changes.
    await set_org(identity, member.org.id, settings={"hipaa_mode": True})
    assert (await patched(client, {"default_record": True}))["hipaa_mode"] is True
    assert (await patched(client, {"hipaa_mode": False}))["hipaa_mode"] is False


async def test_zero_retention_needs_the_enterprise_plan(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await set_org(identity, member.org.id, plan_id="build")
    response = await patch(client, {"privacy": {"zero_retention": True}})
    assert error_of(response) == (403, "FEATURE_NOT_IN_PLAN", "privacy.zero_retention")
    assert message_of(response) == ZERO_RETENTION_PLAN_MESSAGE
    response = await patch(client, {"settings": {"privacy": {"zero_retention": True}}}, ORG)
    assert error_of(response) == (403, "FEATURE_NOT_IN_PLAN", "settings.privacy.zero_retention")
    assert (await patched(client, {"privacy": {"zero_retention": False}}))["privacy"][
        "zero_retention"
    ] is False

    enterprise = await signed_in(client, identity, container)
    await set_org(identity, enterprise.org.id, plan_id="enterprise")
    assert (await patched(client, {"privacy": {"zero_retention": True}}))["privacy"]["zero_retention"] is True


async def test_each_change_writes_one_audit_row(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await patched(
        client, {"timezone": "America/New_York", "default_record": False, "llm": {"base_url": None}}
    )
    await patched(client, {"timezone": "America/New_York"})
    await patched(
        client, {"compliance": {"calling_hours": [{"days": ["mon"], "start": "09:00", "end": "17:00"}]}}
    )
    response = await patch(client, {"name": "Acme Dental West", "settings": {"default_record": True}}, ORG)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["settings"]["default_record"] is True
    rows = await audit_rows(identity, member.org.id, "org.update")
    assert [(row.resource_type, row.resource_id, row.actor_id) for row in rows] == [
        ("org", str(member.org.id), member.user.id)
    ] * 3
    assert [row.changes for row in rows] == [
        {
            "before": {"settings.timezone": "America/Los_Angeles"},
            "after": {"settings.timezone": "America/New_York"},
        },
        {
            "before": {
                "settings.compliance.calling_hours": [
                    {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "08:00",
                        "end": "21:00",
                    }
                ]
            },
            "after": {
                "settings.compliance.calling_hours": [{"days": ["mon"], "start": "09:00", "end": "17:00"}]
            },
        },
        {
            "before": {"name": "Acme Dental", "settings.default_record": False},
            "after": {"name": "Acme Dental West", "settings.default_record": True},
        },
    ]


async def test_writes_invalidate_the_cache(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    key = settings_cache_key(member.org.id)
    assert (await client.get(SETTINGS)).json()["data"]["default_record"] is False
    raw = await container.redis.get(key)
    assert raw is not None
    assert json.loads(raw) == {}
    await patched(client, {"default_record": True})
    assert await container.redis.get(key) is None
    assert (await client.get(SETTINGS)).json()["data"]["default_record"] is True
    await patched(client, {"settings": {"timezone": "America/Denver"}}, ORG)
    assert await container.redis.get(key) is None
    assert (await client.get(SETTINGS)).json()["data"]["timezone"] == "America/Denver"


async def test_settings_work_without_redis(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await signed_in(client, identity, container)
    server = fakeredis.FakeServer()
    server.connected = False
    container.org_settings = OrgSettingsReader(container.database, fakeredis.FakeAsyncRedis(server=server))
    assert (await patched(client, {"default_record": True}))["default_record"] is True
    response = await client.get(SETTINGS)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["default_record"] is True


async def test_a_parent_changes_a_sub_account_settings(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    child_id, _ = await sub_account(client)
    response = await patch(client, {"timezone": "America/Denver"}, **{"X-VA-Org-Id": str(child_id)})
    assert response.status_code == 200, response.text
    assert await stored_settings(identity, child_id) == {"timezone": "America/Denver"}
    assert await stored_settings(identity, owner.org.id) == {}
    rows = await audit_rows(identity, child_id, "org.update")
    assert [row.changes["acting_parent_org_id"] for row in rows] == [str(owner.org.id)]


async def test_mfa_required_set_here_gates_members(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await patched(client, {"mfa_required": True})
    assert await stored_settings(identity, member.org.id) == {"mfa_required": True}
    assert error_of(await client.get(SETTINGS))[:2] == (401, "MFA_REQUIRED")


async def test_invalid_stored_settings_are_reported_and_repaired_by_a_patch(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await signed_in(client, identity, container)
    await set_org(identity, member.org.id, settings={"timezone": "Mars/Olympus_Mons"})
    assert error_of(await patch(client, {"default_record": True})) == (400, "INVALID_INPUT", "timezone")
    assert error_of(await patch(client, {"settings": {"default_record": True}}, ORG)) == (
        400,
        "INVALID_INPUT",
        "settings.timezone",
    )
    assert (await patched(client, {"timezone": "America/Denver"}))["timezone"] == "America/Denver"
    assert await stored_settings(identity, member.org.id) == {"timezone": "America/Denver"}
    rows = await audit_rows(identity, member.org.id, "org.update")
    assert [row.changes for row in rows] == [
        {
            "before": {"settings.timezone": "Mars/Olympus_Mons"},
            "after": {"settings.timezone": "America/Denver"},
        }
    ]
