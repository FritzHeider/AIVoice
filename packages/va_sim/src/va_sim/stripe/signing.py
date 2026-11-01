"""``Stripe-Signature`` header (specs/30 §4.1; Decision P03-T07a rule 10).

``t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>``; the key is the UTF-8 bytes of the whole
``whsec_...`` string, exactly as Stripe and its SDKs compute it.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["signature_header"]


def signature_header(secret: str, timestamp: int, payload: bytes) -> str:
    signed = str(timestamp).encode("ascii") + b"." + payload
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
