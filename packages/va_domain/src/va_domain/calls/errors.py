"""Send Call rule failures raised by the pure request checks (specs/05-calls-api.md §4.1 step 4)."""

from __future__ import annotations

__all__ = ["CallRequestError"]


class CallRequestError(Exception):
    """A parsed request that breaks a Send Call rule; the API maps it to `AppError(code, message, field)`."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def __repr__(self) -> str:
        return f"CallRequestError(code={self.code!r}, message={self.message!r}, field={self.field!r})"
