"""``POST /internal/test/orgs``: the e2e harness seed endpoint (specs/30 §5 Decision P05-T17b)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from call_helpers import bearer, business_hours_today, send_call
from call_input_rows import random_e164
from identity_db import IdentityDb
from session_helpers import SessionContainer, error_of, session_app, session_client, stored_org
from sqlalchemy import select

from va_common.settings import Environment
from va_common.time import FrozenClock
from va_db.models import ApiKey, CreditLedger, PhoneNumber, Voice

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

SEED = "/internal/test/orgs"


@pytest.fixture
def container(identity: IdentityDb) -> SessionContainer:
    return SessionContainer(identity=identity, clock=FrozenClock(business_hours_today()))


def seed_body(**overrides: Any) -> dict[str, Any]:
    return {
        "voices": [{"name": "Karen", "provider_voice_id": "XrExE9yKIg1WjnnlVkGX"}],
        "numbers": [{"e164": random_e164("628")}],
        **overrides,
    }


async def seeded(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post(SEED, json=seed_body(**overrides))
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def numbers_with(container: SessionContainer, e164: str) -> list[PhoneNumber]:
    async with container.database.system_session() as session:
        statement = select(PhoneNumber).where(PhoneNumber.e164 == e164).order_by(PhoneNumber.created_at)
        return list((await session.scalars(statement)).all())


async def test_a_seeded_org_is_funded_backdated_and_its_key_authenticates(
    client: httpx.AsyncClient, container: SessionContainer, identity: IdentityDb
) -> None:
    inbound = {"prompt": "You answer for Bella Pasta.", "first_sentence": "Thanks for calling."}
    e164 = random_e164("628")
    data = await seeded(
        client,
        plan_id="scale",
        dollars="12.5",
        settings={"compliance": {"state_quiet_hours": False}},
        numbers=[{"e164": e164, "telnyx_phone_number_id": "1293384261075731499", "inbound_config": inbound}],
    )
    org_id = uuid.UUID(data["org_id"])
    me = await client.get("/v1/me", headers=bearer(data["api_key"]))
    assert me.status_code == 200, me.text
    org = await stored_org(identity, org_id)
    assert (org.plan_id, org.credits_balance_micros) == ("scale", 12_500_000)
    assert org.settings == {"compliance": {"state_quiet_hours": False}}
    assert org.created_at == container.clock.now() - timedelta(days=30)
    async with container.database.tenant_session(org_id) as session:
        [voice] = (await session.scalars(select(Voice))).all()
        [number] = (await session.scalars(select(PhoneNumber))).all()
        [key] = (await session.scalars(select(ApiKey))).all()
        [ledger] = (await session.scalars(select(CreditLedger))).all()
    assert data["voices"] == [{"id": str(voice.id), "name": "Karen"}]
    assert (voice.provider, voice.provider_voice_id) == ("elevenlabs", "XrExE9yKIg1WjnnlVkGX")
    assert data["numbers"] == [{"id": str(number.id), "e164": e164}]
    assert (number.status, number.country_code, number.capabilities) == ("active", "US", ["voice"])
    assert (number.telnyx_phone_number_id, number.inbound_config) == ("1293384261075731499", inbound)
    assert (key.environment, key.scopes, key.created_by) == ("live", ["*"], None)
    assert (ledger.amount_micros, ledger.idempotency_key) == (12_500_000, f"test-org:{org_id}")


async def test_a_seeded_org_can_send_a_task_call(
    client: httpx.AsyncClient, container: SessionContainer
) -> None:
    data = await seeded(client)
    body = {
        "phone_number": random_e164(),
        "from": data["numbers"][0]["e164"],
        "task": "You are Maya from Bella Pasta. Confirm tonight's reservation.",
        "voice": "Karen",
    }
    response = await send_call(client, data["api_key"], body)
    assert response.status_code == 200, response.text
    [started] = container.call_starter.started
    assert str(started.call_id) == response.json()["call_id"]


async def test_a_number_active_elsewhere_is_released_there_first(
    client: httpx.AsyncClient, container: SessionContainer
) -> None:
    e164 = random_e164("628")
    first = await seeded(client, numbers=[{"e164": e164}])
    second = await seeded(client, numbers=[{"e164": e164}])
    old, new = await numbers_with(container, e164)
    assert (str(old.org_id), old.status, old.released_at) == (
        first["org_id"],
        "released",
        container.clock.now(),
    )
    assert (str(new.org_id), new.status) == (second["org_id"], "active")


async def test_a_seeded_org_without_credit_has_no_ledger_row(
    client: httpx.AsyncClient, container: SessionContainer, identity: IdentityDb
) -> None:
    data = await seeded(client, dollars="0", numbers=[])
    org_id = uuid.UUID(data["org_id"])
    assert (await stored_org(identity, org_id)).credits_balance_micros == 0
    async with container.database.tenant_session(org_id) as session:
        assert (await session.scalars(select(CreditLedger))).all() == []
    assert data["numbers"] == []


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"numbers": [{"e164": "+1555", "inbound_config": {}}]}, "numbers[0].e164"),
        ({"numbers": [{"e164": "+16285550100", "inbound_config": {"max_duration": "x"}}]}, None),
        ({"numbers": [{"e164": "+16285550100", "inbound_config": {"phone_number": "+1"}}]}, None),
        ({"settings": {"compliance": {"calling_hours": []}}}, "settings.compliance.calling_hours"),
        ({"settings": {"nope": 1}}, "settings.nope"),
    ],
    ids=["bad-e164", "bad-inbound-type", "outbound-only-inbound", "bad-settings", "unknown-setting"],
)
async def test_invalid_input_names_the_field(
    client: httpx.AsyncClient, container: SessionContainer, overrides: dict[str, Any], field: str | None
) -> None:
    response = await client.post(SEED, json=seed_body(**overrides))
    status, code, where = error_of(response)
    assert (status, code) == (400, "INVALID_INPUT"), response.text
    if field is not None:
        assert where == field
    else:
        assert where is not None
        assert where.startswith("numbers[0].inbound_config")
    assert container.call_starter.started == []


async def test_unknown_body_fields_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(SEED, json=seed_body(owner="me"))
    assert error_of(response)[:2] == (400, "INVALID_INPUT")


@pytest.mark.parametrize("va_env", [Environment.STAGING, Environment.PRODUCTION])
async def test_the_route_is_absent_outside_local_and_test(
    container: SessionContainer, va_env: Environment
) -> None:
    app = session_app(container)
    app.state.settings = app.state.settings.model_copy(update={"va_env": va_env})
    async with session_client(app) as client:
        response = await client.post(SEED, json=seed_body())
    assert error_of(response)[:2] == (404, "NOT_FOUND")
