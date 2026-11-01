"""Destination number normalization (specs/05-calls-api.md §3.2): any dialable form -> E.164."""

from __future__ import annotations

from typing import Final

import phonenumbers

__all__ = ["DEFAULT_REGION", "InvalidPhoneNumberError", "normalize_e164"]

DEFAULT_REGION: Final = "US"


class InvalidPhoneNumberError(ValueError):
    """The value is not a valid, dialable phone number (mapped to `INVALID_PHONE_NUMBER` by the API)."""


def normalize_e164(value: str, region: str = DEFAULT_REGION) -> str:
    """Parse ``value`` (spaces, dashes, dots and parentheses allowed) and return its E.164 form.

    ``region`` is the ISO 3166-1 alpha-2 country used when the value has no ``+`` country code; values
    starting with ``+`` ignore it. The number must be both possible and valid for its region.
    """
    text = value.strip()
    if not text:
        msg = "phone number is empty"
        raise InvalidPhoneNumberError(msg)
    if any(char.isalpha() for char in text):
        # phonenumbers would translate vanity letters (1-800-FLOWERS); a destination must be digits.
        msg = f"phone number {text!r} contains letters"
        raise InvalidPhoneNumberError(msg)
    try:
        parsed = phonenumbers.parse(text, region.upper())
    except phonenumbers.NumberParseException as error:
        msg = f"phone number {text!r} cannot be parsed"
        raise InvalidPhoneNumberError(msg) from error
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        msg = f"phone number {text!r} is not a valid dialable number"
        raise InvalidPhoneNumberError(msg)
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
