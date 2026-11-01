"""BYO Telnyx account webhook keys across orgs (specs/03-data-model.md §2; specs/06 §1, §4.1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select

from va_db.models import TelnyxAccount
from va_db.repositories.base import SystemRepository

__all__ = ["WEBHOOK_ACCOUNT_STATUSES", "TelnyxAccountLookupRepo", "TelnyxWebhookKey"]

# A pending account already receives webhooks while its bootstrap runs; error accounts keep their key.
WEBHOOK_ACCOUNT_STATUSES: Final = ("pending", "active", "error")


@dataclass(frozen=True, slots=True)
class TelnyxWebhookKey:
    """The Ed25519 public key (base64) that signs a BYO account's webhooks and the owning org."""

    public_key: str
    org_id: uuid.UUID


class TelnyxAccountLookupRepo(SystemRepository):
    """Resolves the webhook key of ``/telnyx/{voice|messaging}/{telnyx_account_id}`` before any org context
    exists (system scope, specs/03 §2)."""

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
