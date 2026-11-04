"""A fake Telnyx messaging client factory for the call SMS tests (voicemail SMS, pathway SMS nodes)."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from va_telnyx import Message as TelnyxMessage
from va_telnyx import MessageCreate, UnknownTelnyxAccount

__all__ = ["FakeTelnyx"]


@dataclass
class FakeTelnyx:
    """Client factory + client: records sends; ``fail`` raises from ``send_message``, ``unknown`` from
    ``for_account``."""

    sent: list[MessageCreate] = field(default_factory=list)
    accounts: list[uuid.UUID | None] = field(default_factory=list)
    status: str | None = "queued"
    parts: int | None = 2
    unknown: bool = False
    fail: Exception | None = None

    async def for_account(self, telnyx_account_id: uuid.UUID | None) -> FakeTelnyx:
        if self.unknown:
            raise UnknownTelnyxAccount(telnyx_account_id or uuid.uuid4())
        self.accounts.append(telnyx_account_id)
        return self

    async def send_message(self, message: MessageCreate) -> TelnyxMessage:
        if self.fail is not None:
            raise self.fail
        self.sent.append(message)
        recipient: dict[str, Any] = {"phone_number": message.to}
        if self.status is not None:
            recipient["status"] = self.status
        return TelnyxMessage.model_validate(
            {
                "id": f"msg-{secrets.token_hex(8)}",
                "direction": "outbound",
                "from": {"phone_number": message.from_},
                "to": [recipient],
                "parts": self.parts,
            }
        )
