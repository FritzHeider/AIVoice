"""SMS-capable numbers and BYO Telnyx accounts on a migrated database (va_db and media-worker tests)."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from va_common.ids import new_id
from va_db.models import PhoneNumber, TelnyxAccount
from va_db.session import Database

__all__ = ["add_sms_number", "add_telnyx_account", "random_e164"]


def random_e164() -> str:
    """A fresh 415 number (``phone_numbers.e164`` is unique among active rows across orgs)."""
    return f"+1415{secrets.randbelow(10**7):07d}"


async def add_sms_number(
    database: Database, org_id: uuid.UUID, e164: str | None = None, **values: Any
) -> str:
    """An active voice + SMS number of the org (``values`` override any column); returns its E.164."""
    number = e164 if e164 is not None else random_e164()
    defaults: dict[str, Any] = {"country_code": "US", "capabilities": ["voice", "sms"], "status": "active"}
    async with database.tenant_session(org_id) as session:
        session.add(PhoneNumber(id=new_id(), org_id=org_id, e164=number, **{**defaults, **values}))
    return number


async def add_telnyx_account(
    database: Database, org_id: uuid.UUID, messaging_profile_id: str | None
) -> uuid.UUID:
    """An active BYO Telnyx account of the org with ``messaging_profile_id``."""
    account_id = new_id()
    async with database.tenant_session(org_id) as session:
        session.add(
            TelnyxAccount(
                id=account_id,
                org_id=org_id,
                label="byo",
                api_key_enc=secrets.token_bytes(16),
                public_key=secrets.token_hex(16),
                encrypted_key_handle=secrets.token_hex(16),
                messaging_profile_id=messaging_profile_id,
                status="active",
            )
        )
    return account_id
