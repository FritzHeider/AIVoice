"""VoiceAgents Telnyx client (specs/06-telephony-telnyx.md §1, §2, §3, §4, §5, §8)."""

from va_telnyx.client import PLATFORM_ACCOUNT, TelnyxClient
from va_telnyx.client_state import ClientState, ClientStateError, LegRole, command_id
from va_telnyx.errors import (
    CallAlreadyEnded,
    TelnyxApiError,
    TelnyxDownloadTooLarge,
    TelnyxError,
    TelnyxErrorItem,
    TelnyxRateLimited,
    TelnyxTransportError,
)
from va_telnyx.factory import KeyResolver, TelnyxClientFactory, UnknownTelnyxAccount
from va_telnyx.media import InboundMediaMessage, MediaFrame, MediaProtocolError, parse_media_message
from va_telnyx.message_events import MessageEventPayload
from va_telnyx.message_models import Message, MessageCreate, MessageRecipient
from va_telnyx.messaging import MAX_DOWNLOAD_BYTES
from va_telnyx.metrics import TelnyxMetrics
from va_telnyx.models import (
    AmdConfig,
    AnswerRequest,
    CommandResult,
    Conference,
    CustomHeader,
    DialRequest,
    DialResult,
    StreamParams,
    TransferRequest,
)
from va_telnyx.number_models import (
    AvailableNumber,
    NumberOrder,
    NumberSearch,
    OrderedNumber,
    PhoneNumber,
    PhoneNumberMessaging,
)
from va_telnyx.numbers import number_tags
from va_telnyx.provisioning_models import Balance, CallControlApp, MessagingProfile, OutboundVoiceProfile
from va_telnyx.rate_limit import RateCategory, TelnyxRateLimiter
from va_telnyx.retry import RetryPolicy
from va_telnyx.signatures import WebhookVerificationError, load_public_key, verify_media_ws, verify_webhook
from va_telnyx.webhooks import (
    EventPayload,
    UnknownEventPayload,
    WebhookEnvelope,
    WebhookEvent,
    WebhookParseError,
    parse_envelope,
    parse_payload,
)

__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "PLATFORM_ACCOUNT",
    "AmdConfig",
    "AnswerRequest",
    "AvailableNumber",
    "Balance",
    "CallAlreadyEnded",
    "CallControlApp",
    "ClientState",
    "ClientStateError",
    "CommandResult",
    "Conference",
    "CustomHeader",
    "DialRequest",
    "DialResult",
    "EventPayload",
    "InboundMediaMessage",
    "KeyResolver",
    "LegRole",
    "MediaFrame",
    "MediaProtocolError",
    "Message",
    "MessageCreate",
    "MessageEventPayload",
    "MessageRecipient",
    "MessagingProfile",
    "NumberOrder",
    "NumberSearch",
    "OrderedNumber",
    "OutboundVoiceProfile",
    "PhoneNumber",
    "PhoneNumberMessaging",
    "RateCategory",
    "RetryPolicy",
    "StreamParams",
    "TelnyxApiError",
    "TelnyxClient",
    "TelnyxClientFactory",
    "TelnyxDownloadTooLarge",
    "TelnyxError",
    "TelnyxErrorItem",
    "TelnyxMetrics",
    "TelnyxRateLimited",
    "TelnyxRateLimiter",
    "TelnyxTransportError",
    "TransferRequest",
    "UnknownEventPayload",
    "UnknownTelnyxAccount",
    "WebhookEnvelope",
    "WebhookEvent",
    "WebhookParseError",
    "WebhookVerificationError",
    "command_id",
    "load_public_key",
    "number_tags",
    "parse_envelope",
    "parse_media_message",
    "parse_payload",
    "verify_media_ws",
    "verify_webhook",
]
