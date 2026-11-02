"""Inbound call input loading against Postgres (`va_db.call_inputs.loader.load_inbound_inputs`, specs/16 §4,
Decision P05-T12a): runners, caller ID fixed to the called number, BYO accounts, anonymous callers, and the
same reference errors as Send Call."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from call_input_seed import MODELS, Seed, graph, random_e164
from identity_db import IdentityDb

from va_common.errors import AppError
from va_db.call_inputs.loader import CallInputs, InboundNumber, load_inbound_inputs, resolve_inputs
from va_db.models import TelnyxAccount
from va_domain.call_resolution import inbound_request
from va_domain.numbers import load_inbound_config
from va_domain.org import load_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
CALLER = "+14155550123"


@pytest.fixture
async def seed(identity_db: IdentityDb) -> Seed:
    seeded = await Seed.create(identity_db.database)
    await seeded.voice("Karen")
    return seeded


async def _number(seed: Seed, account_id: uuid.UUID | None = None) -> InboundNumber:
    number_id, e164 = await seed.number(telnyx_account_id=account_id)
    return InboundNumber(phone_number_id=number_id, e164=e164, telnyx_account_id=account_id)


async def _load(
    seed: Seed,
    stored: dict[str, Any],
    number: InboundNumber,
    *,
    caller: str = CALLER,
    org: dict[str, Any] | None = None,
) -> tuple[CallInputs, Any]:
    settings = load_settings(org or {"default_voice": "Karen"})
    request = inbound_request(
        load_inbound_config(stored), caller=caller, called=number.e164, org=settings, org_name="Bella Pasta"
    )
    async with seed.database.tenant_session(seed.org_id) as session:
        inputs = await load_inbound_inputs(
            session, seed.org_id, request, plan_id="enterprise", org=settings, number=number, models=MODELS
        )
    return inputs, resolve_inputs(inputs, request, NOW).config


async def test_a_prompt_number_resolves_a_task_call(seed: Seed) -> None:
    voice = f"Nova{secrets.token_hex(3)}"
    voice_id = await seed.voice(voice)
    number = await _number(seed)
    stored = {"prompt": "Take a reservation.", "record": True}
    inputs, config = await _load(seed, stored, number, org={"default_voice": voice})
    assert (config.phone_number, config.from_number, config.phone_number_id) == (
        CALLER,
        number.e164,
        number.phone_number_id,
    )
    assert config.task == "Take a reservation."
    assert config.voice_id == voice_id  # the org default voice
    assert config.record is True
    assert (inputs.country, inputs.telnyx_account, config.telnyx_account_id) == ("US", None, None)


async def test_a_number_without_runner_uses_the_default_prompt(seed: Seed) -> None:
    number = await _number(seed)
    _, config = await _load(seed, {}, number)
    assert config.task == "You are a helpful receptionist for Bella Pasta. Take a message."


async def test_a_pathway_number_resolves_the_production_version(seed: Seed) -> None:
    pathway_id = await seed.pathway({1: {}, 2: {"nodes": graph("ask")}}, production=2)
    number = await _number(seed)
    _, config = await _load(seed, {"pathway_id": str(pathway_id)}, number)
    assert (config.pathway_id, config.pathway_version) == (pathway_id, 2)


async def test_an_agent_number_resolves_the_agent(seed: Seed) -> None:
    agent_id, (version_id,) = await seed.agent(
        [{"semver": "1.0.0", "call_settings": {"task": "Agent prompt."}}], env_pins={"production": "1.0.0"}
    )
    number = await _number(seed)
    _, config = await _load(seed, {"agent_id": str(agent_id)}, number)
    assert (config.agent_id, config.agent_version_id) == (agent_id, version_id)
    assert config.task == "Agent prompt."


async def test_a_byo_number_dials_through_its_account(seed: Seed) -> None:
    account_id = await seed.row(
        TelnyxAccount,
        encrypted_key_handle=f"tlx_{secrets.token_hex(8)}",
        status="active",
        call_control_app_id="cc-byo",
    )
    number = await _number(seed, account_id)
    inputs, config = await _load(seed, {"prompt": "Hi"}, number)
    assert inputs.telnyx_account is not None
    assert (inputs.telnyx_account.account_id, inputs.telnyx_account.call_control_app_id) == (
        account_id,
        "cc-byo",
    )
    assert config.telnyx_account_id == account_id


@pytest.mark.parametrize("status", ["pending", "error", "deleted"])
async def test_an_unusable_byo_account_is_a_carrier_error(seed: Seed, status: str) -> None:
    account_id = await seed.row(
        TelnyxAccount, encrypted_key_handle=f"tlx_{secrets.token_hex(8)}", status=status
    )
    number = await _number(seed, account_id)
    with pytest.raises(AppError) as caught:
        await _load(seed, {"prompt": "Hi"}, number)
    assert caught.value.code == "CARRIER_CONNECTION_ERROR"


async def test_a_missing_byo_account_is_a_carrier_error(seed: Seed) -> None:
    number_id, e164 = await seed.number()
    number = InboundNumber(phone_number_id=number_id, e164=e164, telnyx_account_id=uuid.uuid4())
    with pytest.raises(AppError) as caught:
        await _load(seed, {"prompt": "Hi"}, number)
    assert caught.value.code == "CARRIER_CONNECTION_ERROR"


@pytest.mark.parametrize("caller", ["anonymous", "restricted", "12"])
async def test_an_undialable_caller_is_kept_raw(seed: Seed, caller: str) -> None:
    number = await _number(seed)
    inputs, config = await _load(seed, {"prompt": "Hi"}, number, caller=caller)
    assert (config.phone_number, inputs.country) == (caller, None)


async def test_a_national_caller_is_normalized_with_the_called_region(seed: Seed) -> None:
    number = await _number(seed)
    national = random_e164("212")
    inputs, config = await _load(seed, {"prompt": "Hi"}, number, caller=national.removeprefix("+1"))
    assert (config.phone_number, inputs.country) == (national, "US")


async def test_an_international_caller_keeps_its_region(seed: Seed) -> None:
    number = await _number(seed)
    inputs, config = await _load(seed, {"prompt": "Hi"}, number, caller="+442071838750")
    assert (config.phone_number, inputs.country) == ("+442071838750", "GB")


async def test_an_unknown_voice_fails_like_send_call(seed: Seed) -> None:
    number = await _number(seed)
    with pytest.raises(AppError) as caught:
        await _load(seed, {"prompt": "Hi", "voice": f"Ghost{secrets.token_hex(3)}"}, number)
    assert (caught.value.code, caught.value.field) == ("VOICE_NOT_FOUND", "voice")


async def test_a_missing_tool_fails_like_send_call(seed: Seed) -> None:
    number = await _number(seed)
    with pytest.raises(AppError) as caught:
        await _load(seed, {"prompt": "Hi", "tools": [f"TL-{uuid.uuid4()}"]}, number)
    assert caught.value.code == "TOOL_NOT_FOUND"


async def test_a_missing_pathway_fails_like_send_call(seed: Seed) -> None:
    number = await _number(seed)
    with pytest.raises(AppError) as caught:
        await _load(seed, {"pathway_id": str(uuid.uuid4())}, number)
    assert (caught.value.code, caught.value.field) == ("NOT_FOUND", "pathway_id")
