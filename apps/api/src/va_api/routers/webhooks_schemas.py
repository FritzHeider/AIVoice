"""Response bodies of ``/v1/webhooks/signing-secret`` (specs/19-events-and-webhooks.md §10 Decision P06-T04a).

The secret itself is shown only in the rotate response (``secret``), ``null`` on an ``Idempotency-Key``
replay; the view carries the last four characters and the issue time, plus the previous secret's last four
characters and expiry while it still signs deliveries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_db.webhook_secrets import RotatedSecret, SecretInfo

__all__ = ["RotatedSigningSecret", "SigningSecretView", "rotated_signing_secret", "signing_secret_view"]

_VIEW_EXAMPLE: Final[dict[str, JsonValue]] = {
    "secret_last4": "Q2Nw",
    "created_at": "2026-11-03T09:30:00Z",
    "previous_secret_last4": "8Bq1",
    "previous_expires_at": "2026-11-04T09:30:00Z",
}
_ROTATED_EXAMPLE: Final[dict[str, JsonValue]] = {
    "secret": "whsec_Ex4mpleS1gn1ngS3cretV4lueOnlyF0rD0csQ2Nw",
    "created_at": "2026-11-03T09:30:00Z",
    "previous_expires_at": "2026-11-04T09:30:00Z",
}


class SigningSecretView(BaseModel):
    """The org's webhook signing secret by its last four characters."""

    model_config = ConfigDict(json_schema_extra={"examples": [_VIEW_EXAMPLE]})

    secret_last4: str = Field(description="Last four characters of the current secret.")
    created_at: datetime = Field(description="When the current secret was issued.")
    previous_secret_last4: str | None = Field(
        default=None,
        description="Last four characters of the previous secret while deliveries are still signed with it.",
    )
    previous_expires_at: datetime | None = Field(
        default=None, description="When the previous secret stops signing deliveries (24 h after a rotation)."
    )


class RotatedSigningSecret(BaseModel):
    """A newly issued signing secret, shown once."""

    model_config = ConfigDict(json_schema_extra={"examples": [_ROTATED_EXAMPLE]})

    secret: str | None = Field(
        description=(
            "The new secret (`whsec_...`); only in this response, `null` on an Idempotency-Key replay."
        )
    )
    created_at: datetime = Field(description="When the new secret was issued.")
    previous_expires_at: datetime = Field(
        description="Until then deliveries carry a second `v1=` signature made with the old secret."
    )


def signing_secret_view(info: SecretInfo) -> SigningSecretView:
    return SigningSecretView(
        secret_last4=info.secret_last4,
        created_at=info.created_at,
        previous_secret_last4=info.previous_secret_last4,
        previous_expires_at=info.previous_expires_at,
    )


def rotated_signing_secret(rotated: RotatedSecret) -> RotatedSigningSecret:
    return RotatedSigningSecret(
        secret=rotated.secret, created_at=rotated.created_at, previous_expires_at=rotated.previous_expires_at
    )
