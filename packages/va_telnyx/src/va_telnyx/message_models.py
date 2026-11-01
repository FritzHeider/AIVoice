"""Telnyx messaging models (specs/06-telephony-telnyx.md §2.1 ``send_message``; specs/21-messaging-sms.md §2
step 6)."""

from __future__ import annotations

from typing import Annotated, Any, Self

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from va_telnyx.models import RequestModel, ResponseModel

__all__ = ["E164", "Message", "MessageCreate", "MessageRecipient"]

type E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{1,14}$")]
type Url = Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]

MAX_MEDIA_URLS = 10


class MessageCreate(RequestModel):
    """``POST /messages``: ``text`` or ``media_urls`` is required; events go to ``webhook_url`` (never the
    profile's webhooks)."""

    from_: E164 = Field(serialization_alias="from")
    to: E164
    text: Annotated[str, StringConstraints(min_length=1)] | None = None
    media_urls: Annotated[tuple[Url, ...], Field(min_length=1, max_length=MAX_MEDIA_URLS)] | None = None
    subject: str | None = None
    messaging_profile_id: Annotated[str, StringConstraints(min_length=1)]
    webhook_url: Url
    webhook_failover_url: Url | None = None
    use_profile_webhooks: bool = False

    @model_validator(mode="after")
    def _has_content(self) -> Self:
        if self.text is None and self.media_urls is None:
            msg = "text or media_urls is required"
            raise ValueError(msg)
        return self


class MessageRecipient(ResponseModel):
    phone_number: str
    status: str | None = None


class Message(ResponseModel):
    """A Telnyx ``message`` record (``from_`` is the sender's number)."""

    model_config = ConfigDict(extra="ignore", frozen=True, coerce_numbers_to_str=True, populate_by_name=True)

    id: str
    direction: str
    type: str | None = None
    from_: str = Field(validation_alias="from")
    to: tuple[MessageRecipient, ...] = ()
    text: str | None = None
    parts: int | None = None
    encoding: str | None = None
    messaging_profile_id: str | None = None

    @field_validator("from_", mode="before")
    @classmethod
    def _sender_number(cls, value: Any) -> Any:
        return value.get("phone_number") if isinstance(value, dict) else value
