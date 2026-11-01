"""Send Call step 9 order and error bodies (specs/05 §4.1, Decision P05-T04a)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from compliance_rows import add_dnc, new_org, random_e164

from va_common.errors import AppError
from va_compliance.compliance import OrgFacts, check_call_compliance
from va_compliance.international import parse_allowed_countries
from va_db.session import Database
from va_domain.org import load_settings

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

PLATFORM = parse_allowed_countries("US,CA,GB")
ENFORCED = {"compliance": {"enforce_calling_hours": True}}
NY_NOON = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)  # 12:00 EDT
NY_THREE_AM = datetime(2026, 7, 15, 7, 0, tzinfo=UTC)  # 03:00 EDT


async def check(  # noqa: PLR0913 - keyword-only test knobs
    database: Database,
    org_id: uuid.UUID,
    destination: str,
    *,
    region: str | None = "US",
    settings: Mapping[str, Any] | None = None,
    limits: Mapping[str, object] | None = None,
    plan: str = "build",
    at: datetime = NY_NOON,
    now: datetime = NY_NOON,
    scheduled: bool = False,
) -> None:
    facts = OrgFacts(
        org_id=org_id, plan_id=plan, kyc_level=0, limits=limits or {}, settings=load_settings(settings or {})
    )
    async with database.tenant_session(org_id) as session:
        await check_call_compliance(
            session,
            facts,
            destination=destination,
            region=region,
            at=at,
            now=now,
            scheduled=scheduled,
            allowed_countries=PLATFORM,
        )


async def rejected(database: Database, org_id: uuid.UUID, destination: str, **knobs: Any) -> dict[str, Any]:
    with pytest.raises(AppError) as caught:
        await check(database, org_id, destination, **knobs)
    return {"status": caught.value.http_status, **caught.value.to_error_item()}


BLOCKED = {
    "status": 403,
    "error": "NUMBER_BLOCKED",
    "message": "This number is blocked.",
    "field": "phone_number",
}


async def test_clean_destination_passes(database: Database) -> None:
    await check(database, await new_org(database), random_e164())


async def test_premium_prefix_is_blocked_before_any_lookup(database: Database) -> None:
    org = await new_org(database)
    assert await rejected(database, org, "+19005550123") == BLOCKED


async def test_enterprise_allowance_reads_limits(database: Database) -> None:
    org = await new_org(database)
    limits: dict[str, object] = {"allowed_high_risk_prefixes": ["+882"]}
    gb_ok = {"international_enabled": True, "allowed_countries": ["GB"]}
    assert await rejected(database, org, "+88212345678", region=None, plan="build", limits=limits) == BLOCKED
    # Unblocked as a prefix, then refused as a non-geographic destination.
    blocked = await rejected(database, org, "+88212345678", region=None, plan="enterprise", limits=limits)
    assert blocked["error"] == "INTERNATIONAL_NOT_ENABLED"
    malformed: dict[str, object] = {"allowed_high_risk_prefixes": "+882"}
    assert await rejected(database, org, "+88212345678", plan="enterprise", limits=malformed) == BLOCKED
    await check(
        database, org, "+447911123456", region="GB", settings=gb_ok, limits={"international_enabled": True}
    )


async def test_dnc_block_uses_the_same_body_for_every_list(database: Database) -> None:
    org, own, platform = await new_org(database), random_e164(), random_e164()
    await add_dnc(database, org, own)
    await add_dnc(database, None, platform, source="platform")
    assert await rejected(database, org, own) == BLOCKED
    assert await rejected(database, org, platform) == BLOCKED


async def test_dnc_wins_over_international(database: Database) -> None:
    org, number = await new_org(database), random_e164()
    await add_dnc(database, org, number)
    assert await rejected(database, org, number, region="GB") == BLOCKED


async def test_international_wins_over_calling_hours(database: Database) -> None:
    org = await new_org(database)
    body = await rejected(
        database, org, "+442071234567", region="GB", settings=ENFORCED, at=NY_THREE_AM, now=NY_THREE_AM
    )
    assert body == {
        "status": 403,
        "error": "INTERNATIONAL_NOT_ENABLED",
        "message": "International calls to GB are not enabled for this organization.",
        "field": "phone_number",
    }


async def test_unknown_region_message(database: Database) -> None:
    body = await rejected(database, await new_org(database), "+80012345678", region=None)
    assert body["message"] == "International calls to this destination are not enabled for this organization."


async def test_outside_calling_hours(database: Database) -> None:
    org, number = await new_org(database), random_e164()
    assert await rejected(database, org, number, settings=ENFORCED, at=NY_THREE_AM, now=NY_THREE_AM) == {
        "status": 400,
        "error": "OUTSIDE_CALLING_HOURS",
        "message": "The destination's local time is outside the allowed calling hours.",
        "field": "phone_number",
    }


async def test_calling_hours_are_not_enforced_by_default(database: Database) -> None:
    await check(database, await new_org(database), random_e164(), at=NY_THREE_AM, now=NY_THREE_AM)


async def test_scheduled_calls_are_checked_at_the_scheduled_instant(database: Database) -> None:
    org, number = await new_org(database), random_e164()
    await check(database, org, number, settings=ENFORCED, at=NY_NOON, now=NY_THREE_AM, scheduled=True)
    body = await rejected(
        database, org, number, settings=ENFORCED, at=NY_THREE_AM, now=NY_NOON, scheduled=True
    )
    assert body["error"] == "OUTSIDE_CALLING_HOURS"
    assert body["field"] == "start_time"
