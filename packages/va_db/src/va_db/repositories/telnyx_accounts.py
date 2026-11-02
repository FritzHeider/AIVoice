"""BYO Telnyx account webhook keys and encrypted API keys across orgs (specs/03-data-model.md §2; specs/06 §1,
§4.1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select

from va_db.models import TelnyxAccount
from va_db.repositories.base import SystemRepository

__all__ = [
    "DIALING_ACCOUNT_STATUS",
    "WEBHOOK_ACCOUNT_STATUSES",
    "TelnyxAccountLookupRepo",
    "TelnyxApiKeyRow",
    "TelnyxWebhookKey",
]

# A pending account already receives webhooks while its bootstrap runs; error accounts keep their key.
WEBHOOK_ACCOUNT_STATUSES: Final = ("pending", "active", "error")
# Only an active account places calls: a pending bootstrap or an account in error has no usable setup.
DIALING_ACCOUNT_STATUS: Final = "active"


@dataclass(frozen=True, slots=True)
class TelnyxWebhookKey:
    """The Ed25519 public key (base64) that signs a BYO account's webhooks and the owning org."""

    public_key: str
    org_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class TelnyxApiKeyRow:
    """The envelope-encrypted API key of a BYO account and the org whose data key sealed it."""

    org_id: uuid.UUID
    api_key_enc: bytes = field(repr=False)


class TelnyxAccountLookupRepo(SystemRepository):
    """Resolves the webhook key of ``/telnyx/{voice|messaging}/{telnyx_account_id}`` and the encrypted API key
    the worker dials with, before any org context exists (system scope, specs/03 §2)."""

    async def webhook_key(self, account_id: uuid.UUID) -> TelnyxWebhookKey | None:
        """The key of account ``account_id``; ``None`` when unknown or ``deleted``."""
        statement = select(TelnyxAccount.public_key, TelnyxAccount.org_id).where(
            TelnyxAccount.id == account_id, TelnyxAccount.status.in_(WEBHOOK_ACCOUNT_STATUSES)
        )
        row = (await self.session.execute(statement)).first()
        if row is None:
            return None
        public_key, org_id = row
        return TelnyxWebhookKey(public_key=public_key, org_id=org_id)

    async def api_key(self, account_id: uuid.UUID) -> TelnyxApiKeyRow | None:
        """The encrypted API key of account ``account_id``; ``None`` unless the account is ``active``."""
        statement = select(TelnyxAccount.org_id, TelnyxAccount.api_key_enc).where(
            TelnyxAccount.id == account_id, TelnyxAccount.status == DIALING_ACCOUNT_STATUS
        )
        row = (await self.session.execute(statement)).first()
        if row is None:
            return None
        org_id, api_key_enc = row
        return TelnyxApiKeyRow(org_id=org_id, api_key_enc=api_key_enc)
