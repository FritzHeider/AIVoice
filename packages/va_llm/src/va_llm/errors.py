"""Gateway errors (Decision 2026-11-01 P03-T13a rule 8).

``error_kind`` is the ``va_llm_errors_total`` label of specs/27 §3.3 (``timeout``/``http``/``parse``).
Messages name the provider id and model and the vendor's message, never the API key.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Final, Literal

import openai

from va_common.errors import AppError
from va_common.http import retry_after_seconds

__all__ = [
    "ErrorKind",
    "LlmConnectionError",
    "LlmError",
    "LlmHttpError",
    "LlmProtocolError",
    "LlmRateLimitedError",
    "LlmTimeoutError",
    "LlmUnavailableError",
    "OrgProviderError",
    "UnavailableReason",
    "map_openai_error",
]

type ErrorKind = Literal["timeout", "http", "parse"]
type UnavailableReason = Literal["breaker_open", "exhausted", "provider_unavailable"]

_HTTP_SERVER: Final = 500
_TOO_MANY_REQUESTS: Final = 429


class LlmError(Exception):
    """A failed LLM request to ``provider_id``/``model``."""

    kind: ClassVar[ErrorKind] = "http"

    def __init__(self, message: str, *, provider_id: str, model: str) -> None:
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id
        self.model = model

    @property
    def error_kind(self) -> ErrorKind:
        return self.kind

    @property
    def breaker_failure(self) -> bool:
        """Whether the circuit breaker counts this as a failure (timeouts, 5xx, connect errors)."""
        return False

    def to_app_error(self) -> AppError:
        """The API rendering for synchronous endpoints (``UPSTREAM_TIMEOUT``, specs/31 §1)."""
        return AppError("UPSTREAM_TIMEOUT", "The language model did not answer in time.")


class LlmTimeoutError(LlmError):
    """No answer within the call type's deadline (or the SDK's own timeout fired first)."""

    kind: ClassVar[ErrorKind] = "timeout"

    @property
    def breaker_failure(self) -> bool:
        return True


class LlmConnectionError(LlmError):
    """The request never got an HTTP answer (connect, reset, protocol error)."""

    @property
    def breaker_failure(self) -> bool:
        return True


class LlmHttpError(LlmError):
    """The provider answered with an error status."""

    def __init__(self, message: str, *, provider_id: str, model: str, status: int) -> None:
        super().__init__(message, provider_id=provider_id, model=model)
        self.status = status

    @property
    def breaker_failure(self) -> bool:
        return self.status >= _HTTP_SERVER


class LlmRateLimitedError(LlmHttpError):
    """``429``; ``retry_after_s`` is the parsed ``Retry-After`` header (``None`` when absent)."""

    def __init__(self, message: str, *, provider_id: str, model: str, retry_after_s: float | None) -> None:
        super().__init__(message, provider_id=provider_id, model=model, status=_TOO_MANY_REQUESTS)
        self.retry_after_s = retry_after_s


class LlmProtocolError(LlmError):
    """A success status whose body is not a usable chat completion."""

    kind: ClassVar[ErrorKind] = "parse"


class OrgProviderError(LlmError):
    """The org's own provider could not be loaded (database, decryption, or an invalid stored row);
    ``cause`` names the underlying exception type, never its message (it may quote the row)."""

    def __init__(self, *, provider_id: str, cause: str) -> None:
        super().__init__(
            f"{provider_id}: provider lookup failed ({cause})", provider_id=provider_id, model=""
        )
        self.cause = cause


class LlmUnavailableError(LlmError):
    """The call type's policy is exhausted (or every target's breaker is open, or the org's provider could
    not be loaded); callers apply their documented
    fallback (specs/10 §2.1). ``last`` is the final request error, ``None`` when nothing was sent."""

    def __init__(
        self, reason: UnavailableReason, *, provider_id: str, model: str, last: LlmError | None
    ) -> None:
        detail = f": {last.message}" if last is not None else ""
        super().__init__(f"LLM unavailable ({reason}){detail}", provider_id=provider_id, model=model)
        self.reason = reason
        self.last = last

    @property
    def error_kind(self) -> ErrorKind:
        return self.last.error_kind if self.last is not None else "http"


def map_openai_error(exc: openai.OpenAIError, *, provider_id: str, model: str, now: datetime) -> LlmError:
    """The :class:`LlmError` for an exception raised by the ``openai`` SDK."""
    where = {"provider_id": provider_id, "model": model}
    match exc:
        case openai.APITimeoutError():
            return LlmTimeoutError(f"{provider_id} {model}: request timed out", **where)
        case openai.APIConnectionError():
            return LlmConnectionError(f"{provider_id} {model}: connection failed: {exc.message}", **where)
        case openai.APIStatusError(status_code=status) if status == _TOO_MANY_REQUESTS:
            retry_after = retry_after_seconds(exc.response.headers.get("retry-after"), now)
            return LlmRateLimitedError(
                f"{provider_id} {model}: {exc.message}", retry_after_s=retry_after, **where
            )
        case openai.APIStatusError(status_code=status):
            return LlmHttpError(f"{provider_id} {model}: {exc.message}", status=status, **where)
        case _:
            return LlmProtocolError(f"{provider_id} {model}: {exc}", **where)
