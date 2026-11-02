"""``POST /internal/test/orgs``: seed a ready-to-call org for the end-to-end harness (specs/30 §5 Decision
P05-T17b).

The route exists in every build but answers ``NOT_FOUND`` unless ``VA_ENV`` is ``local`` or ``test``: it needs
no credentials and returns a live ``*`` API key. One request creates the org (no members) on the requested
plan, backdated 30 days so new-account limits do not apply, stores the sparse org settings, grants the
starting credit through the ledger, inserts the org voices and active numbers, and mints the key, all in one
``tenant_session`` of the new org. A requested number that is still active in another org (a previous run
against a restarted simulator, whose inventory starts over) is released there first, in its owner's own tenant
session; sessions are opened one after another, never nested.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

import phonenumbers
from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import update

from va_api.audit import audit_exempt
from va_api.auth.api_keys import mint_api_key
from va_api.auth.scopes import ALL_SCOPES
from va_api.auth.session_auth import api_key_hasher_of, clock_of, database_of, provisioner_of
from va_api.envelopes import envelope_router
from va_api.responses import DataResponse
from va_api.routers.e2e_seed_schemas import (
    SeedNumber,
    SeedNumberCreated,
    SeedOrgCreated,
    SeedOrgRequest,
    SeedVoiceCreated,
)
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.money import dollars_to_micros
from va_common.settings import Environment
from va_db.models import PhoneNumber, Voice
from va_db.repositories import ApiKeyRepo, PhoneNumberRoutingRepo, grant_credits
from va_domain.numbers import InboundConfigError, load_inbound_config
from va_domain.org import OrgSettings, field_path, sparse_settings
from va_domain.phone import InvalidPhoneNumberError, normalize_e164

__all__ = [
    "SEED_ENVIRONMENTS",
    "SEED_KEY_NAME",
    "SEED_NOT_FOUND_MESSAGE",
    "internal_test_orgs_create",
    "internal_test_router",
]

SEED_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})
SEED_NOT_FOUND_MESSAGE: Final = "Not found."
SEED_KEY_NAME: Final = "e2e seed"
BACKDATE: Final = timedelta(days=30)
_CREDITS_DESCRIPTION: Final = "E2E seed credits"

internal_test_router = envelope_router(prefix="/internal/test", tags=["Internal"], include_in_schema=False)


def _settings(body: SeedOrgRequest) -> dict[str, object]:
    try:
        return sparse_settings(OrgSettings.model_validate(body.settings))
    except ValidationError as exc:
        error = exc.errors()[0]
        path = field_path(tuple(error["loc"]))
        raise AppError(
            "INVALID_INPUT", str(error["msg"]), field=f"settings.{path}" if path else "settings"
        ) from exc


def _number(index: int, number: SeedNumber) -> tuple[str, str]:
    """The number's E.164 form and ISO country (``INVALID_INPUT`` naming ``numbers[i]...``)."""
    try:
        e164 = normalize_e164(number.e164)
    except InvalidPhoneNumberError as exc:
        raise AppError("INVALID_INPUT", str(exc), field=f"numbers[{index}].e164") from exc
    try:
        load_inbound_config(number.inbound_config)
    except InboundConfigError as exc:
        suffix = f".{exc.field}" if exc.field else ""
        raise AppError(
            "INVALID_INPUT", exc.message, field=f"numbers[{index}].inbound_config{suffix}"
        ) from exc
    country = phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    return e164, country or "US"


async def _release_stale(request: Request, e164: str) -> None:
    """Release ``e164`` in the org where it is still active (test-only takeover of a simulator number)."""
    database = database_of(request)
    async with database.system_session() as session:
        stale = await PhoneNumberRoutingRepo(session).get_active(e164)
        owner = None if stale is None else (stale.org_id, stale.id)
    if owner is None:
        return
    org_id, number_id = owner
    async with database.tenant_session(org_id) as session:
        await session.execute(
            update(PhoneNumber)
            .where(PhoneNumber.org_id == org_id, PhoneNumber.id == number_id)
            .values(status="released", released_at=clock_of(request).now())
        )


@internal_test_router.post(
    "/orgs",
    operation_id="internal_test_orgs_create",
    status_code=201,
    openapi_extra={
        "x-scope": "public",
        "x-public": "e2e seed (VA_ENV local/test only)",
        **audit_exempt("test-only seed endpoint; the new org's log starts empty"),
    },
)
async def internal_test_orgs_create(body: SeedOrgRequest, request: Request) -> DataResponse[SeedOrgCreated]:
    """Create a funded org with voices, numbers, and a live ``*`` API key (``VA_ENV`` local/test only)."""
    if request.app.state.settings.va_env not in SEED_ENVIRONMENTS:
        raise AppError("NOT_FOUND", SEED_NOT_FOUND_MESSAGE)
    settings = _settings(body)
    numbers = [(_number(index, number), number) for index, number in enumerate(body.numbers)]
    for (e164, _), _ in numbers:
        await _release_stale(request, e164)
    now = clock_of(request).now()
    org_id = new_id()
    minted = mint_api_key(api_key_hasher_of(request), "live")
    async with database_of(request).tenant_session(org_id) as session:
        provisioned = await provisioner_of(request).provision_in(
            session, org_id, name=body.name, owner=None, plan_id=body.plan_id
        )
        provisioned.org.settings = settings
        provisioned.org.created_at = now - BACKDATE
        await session.flush()
        micros = dollars_to_micros(body.dollars)
        if micros > 0:
            await grant_credits(
                session,
                org_id,
                micros,
                idempotency_key=f"test-org:{org_id}",
                description=_CREDITS_DESCRIPTION,
            )
        voices = [
            Voice(id=new_id(), org_id=org_id, name=voice.name, provider_voice_id=voice.provider_voice_id)
            for voice in body.voices
        ]
        rows = [
            PhoneNumber(
                id=new_id(),
                org_id=org_id,
                e164=e164,
                telnyx_phone_number_id=number.telnyx_phone_number_id,
                country_code=country,
                capabilities=list(number.capabilities),
                inbound_config=dict(number.inbound_config),
                status="active",
                purchased_at=now,
            )
            for (e164, country), number in numbers
        ]
        session.add_all([*voices, *rows])
        await ApiKeyRepo(session, org_id).add(
            name=SEED_KEY_NAME,
            prefix=minted.prefix,
            secret_hash=minted.secret_hash,
            pepper_version=minted.pepper_version,
            scopes=[ALL_SCOPES],
            environment="live",
            created_by=None,
        )
        created = SeedOrgCreated(
            org_id=org_id,
            api_key=minted.plaintext,
            voices=[SeedVoiceCreated(id=voice.id, name=voice.name) for voice in voices],
            numbers=[SeedNumberCreated(id=row.id, e164=row.e164) for row in rows],
        )
    return DataResponse(data=created)
