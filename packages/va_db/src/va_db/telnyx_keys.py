"""BYO Telnyx API keys (specs/06-telephony-telnyx.md §1; specs/28-security.md §2).

``telnyx_accounts.api_key_enc`` is sealed with the owning org's data key under the AAD
``column_aad("telnyx_accounts", "api_key_enc", <row id>)``, so a blob copied onto another row never decrypts.
:class:`TelnyxKeyResolver` is the ``resolve_key`` of ``va_telnyx.TelnyxClientFactory``: it reads the row in
system scope (the worker has no org context when it builds a client) and decrypts it with the row's org.
"""

from __future__ import annotations

import uuid

from va_common.crypto import Envelope, KmsProvider, column_aad
from va_db.repositories import OrgDataKeyStore, TelnyxAccountLookupRepo
from va_db.session import Database

__all__ = ["TelnyxKeyResolver", "api_key_aad", "seal_api_key"]


def api_key_aad(account_id: uuid.UUID) -> str:
    """The AAD that binds an encrypted BYO API key to its ``telnyx_accounts`` row."""
    return column_aad("telnyx_accounts", "api_key_enc", account_id)


async def seal_api_key(envelope: Envelope, org_id: uuid.UUID, account_id: uuid.UUID, api_key: str) -> bytes:
    """``api_key_enc`` for row ``account_id`` of ``org_id``."""
    return await envelope.encrypt_str(org_id, api_key, api_key_aad(account_id))


class TelnyxKeyResolver:
    """Plaintext API key of an ``active`` BYO account; ``None`` for an unknown or inactive account.

    Decryption failures (KMS unavailable, a tampered blob) propagate, so the caller's retry policy applies.
    """

    def __init__(self, database: Database, envelope: Envelope) -> None:
        self._database = database
        self._envelope = envelope

    @classmethod
    def for_provider(cls, database: Database, kms_provider: KmsProvider) -> TelnyxKeyResolver:
        """A resolver decrypting with org data keys stored in ``database`` under ``kms_provider``."""
        return cls(database, Envelope(kms_provider, OrgDataKeyStore(database)))

    def __repr__(self) -> str:
        return "TelnyxKeyResolver()"

    async def __call__(self, account_id: uuid.UUID) -> str | None:
        async with self._database.system_session() as session:
            row = await TelnyxAccountLookupRepo(session).api_key(account_id)
        if row is None:
            return None
        return await self._envelope.decrypt_str(row.org_id, row.api_key_enc, api_key_aad(account_id))
