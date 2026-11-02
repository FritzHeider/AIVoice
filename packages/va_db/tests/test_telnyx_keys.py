"""BYO Telnyx API keys: sealed per row, resolved in system scope (specs/06 §1; specs/28 §2)."""

from __future__ import annotations

import secrets
import uuid

import pytest
from identity_db import IdentityDb

from va_common.crypto import DecryptionError
from va_common.ids import new_id
from va_db.models import TelnyxAccount
from va_db.models.telephony import TelnyxAccountStatus
from va_db.telnyx_keys import TelnyxKeyResolver, api_key_aad, seal_api_key

pytestmark = pytest.mark.integration


async def _org(db: IdentityDb) -> uuid.UUID:
    provisioned = await db.provisioner.provision(name="BYO Org", owner=None)
    return provisioned.org.id


async def _account(
    db: IdentityDb,
    org_id: uuid.UUID,
    api_key: str,
    *,
    status: TelnyxAccountStatus = "active",
    sealed_for: uuid.UUID | None = None,
) -> uuid.UUID:
    """A ``telnyx_accounts`` row whose key is sealed for its own id (or for ``sealed_for``)."""
    account_id = new_id()
    blob = await seal_api_key(db.envelope, org_id, sealed_for or account_id, api_key)
    async with db.database.tenant_session(org_id) as session:
        session.add(
            TelnyxAccount(
                id=account_id,
                org_id=org_id,
                label="byo",
                api_key_enc=blob,
                public_key="cHVibGlj",
                encrypted_key_handle=f"ek_{secrets.token_hex(8)}",
                status=status,
            )
        )
    return account_id


def _key() -> str:
    return f"KEY{secrets.token_hex(12)}"


async def test_a_sealed_key_round_trips_per_org(identity_db: IdentityDb) -> None:
    first_org, second_org = await _org(identity_db), await _org(identity_db)
    first_key, second_key = _key(), _key()
    first = await _account(identity_db, first_org, first_key)
    second = await _account(identity_db, second_org, second_key)
    resolver = TelnyxKeyResolver(identity_db.database, identity_db.envelope)
    assert await resolver(first) == first_key
    assert await resolver(second) == second_key
    assert first_key not in repr(resolver)


async def test_the_stored_blob_is_not_the_plaintext(identity_db: IdentityDb) -> None:
    org_id = await _org(identity_db)
    api_key = _key()
    account_id = new_id()
    blob = await seal_api_key(identity_db.envelope, org_id, account_id, api_key)
    assert api_key.encode() not in blob
    assert await identity_db.envelope.decrypt_str(org_id, blob, api_key_aad(account_id)) == api_key


async def test_an_unknown_account_resolves_to_none(identity_db: IdentityDb) -> None:
    resolver = TelnyxKeyResolver(identity_db.database, identity_db.envelope)
    assert await resolver(uuid.uuid4()) is None


@pytest.mark.parametrize("status", ["pending", "error", "deleted"])
async def test_an_inactive_account_resolves_to_none(
    identity_db: IdentityDb, status: TelnyxAccountStatus
) -> None:
    org_id = await _org(identity_db)
    account_id = await _account(identity_db, org_id, _key(), status=status)
    assert await TelnyxKeyResolver(identity_db.database, identity_db.envelope)(account_id) is None


async def test_a_key_sealed_for_another_row_does_not_decrypt(identity_db: IdentityDb) -> None:
    org_id = await _org(identity_db)
    account_id = await _account(identity_db, org_id, _key(), sealed_for=uuid.uuid4())
    with pytest.raises(DecryptionError):
        await TelnyxKeyResolver(identity_db.database, identity_db.envelope)(account_id)
