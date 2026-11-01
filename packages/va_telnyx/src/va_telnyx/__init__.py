"""VoiceAgents Telnyx client (specs/06-telephony-telnyx.md §2, §3)."""

from va_telnyx.client import PLATFORM_ACCOUNT, TelnyxClient
from va_telnyx.client_state import ClientState, ClientStateError, LegRole, command_id
from va_telnyx.errors import (
    CallAlreadyEnded,
    TelnyxApiError,
    TelnyxError,
    TelnyxErrorItem,
    TelnyxRateLimited,
    TelnyxTransportError,
)
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
from va_telnyx.rate_limit import RateCategory, TelnyxRateLimiter
from va_telnyx.retry import RetryPolicy

__all__ = [
    "PLATFORM_ACCOUNT",
    "AmdConfig",
    "AnswerRequest",
    "CallAlreadyEnded",
    "ClientState",
    "ClientStateError",
    "CommandResult",
    "Conference",
    "CustomHeader",
    "DialRequest",
    "DialResult",
    "LegRole",
    "RateCategory",
    "RetryPolicy",
    "StreamParams",
    "TelnyxApiError",
    "TelnyxClient",
    "TelnyxError",
    "TelnyxErrorItem",
    "TelnyxMetrics",
    "TelnyxRateLimited",
    "TelnyxRateLimiter",
    "TelnyxTransportError",
    "TransferRequest",
    "command_id",
]
