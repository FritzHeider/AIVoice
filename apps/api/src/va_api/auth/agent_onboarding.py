"""Agent onboarding requests in Redis (specs/04-auth-tenancy-rbac.md §10; specs/03 §11).

An autonomous agent asks for a scoped ``test`` key for a human's email address; the human approves or denies
through the emailed dashboard link; the agent polls with the ``poll_secret`` it was given. Nothing of the flow
is in Postgres until approval mints the key, so the request lives in Redis:

- ``agentreq:{request_id}`` (24 h) — the :class:`AgentRequest` JSON. Only hashes of the email, the poll
  secret, and the link token are stored.
- ``agentreq:token:{token_hash}`` (1 h, the link lifetime) — the request id the link addresses.
- ``agentkey:{request_id}`` (15 min) — the minted key, envelope-encrypted under the approving org's data key,
  base64. It is deleted in the same transaction that marks the request ``delivered``, so exactly one poll
  carries the plaintext.
- ``agentonb:ip:{ip}`` / ``agentonb:email:{sha256(email)}`` — fixed one-hour windows of 5 and 3 creations.

Every status change is a compare-and-set (``WATCH``/``MULTI``) on ``agentreq:{request_id}``, so concurrent
approvals, or an approval racing a denial, have exactly one winner. Redis being unavailable answers
``SERVICE_UNAVAILABLE``: the flow has no state anywhere else.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final, Literal, Self, get_args

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from va_api.auth.login_throttle import seconds_phrase
from va_api.auth.scopes import SCOPES
from va_api.auth.verification import dashboard_link, deliver
from va_api.auth.window_counter import hit_window
from va_common import json
from va_common.auth import sha256_hex
from va_common.email import EmailSender, agent_key_request
from va_common.errors import AppError
from va_common.time import Clock

__all__ = [
    "AGENT_KEY_LIFETIME",
    "APPROVAL_WINDOW",
    "EMAIL_LIMIT",
    "EXCLUDED_SCOPES",
    "IP_LIMIT",
    "KEY_TTL",
    "LINK_INVALID_MESSAGE",
    "REQUEST_NOT_FOUND_MESSAGE",
    "REQUEST_TTL_S",
    "STORE_UNAVAILABLE_MESSAGE",
    "WINDOW_S",
    "AgentRequest",
    "AgentRequestStore",
    "OnboardingLimiter",
    "PollStatus",
    "StoredStatus",
    "agent_email_key",
    "agent_ip_key",
    "agent_key_key",
    "agent_request_key",
    "agent_token_key",
    "approval_link",
    "check_requested_scopes",
    "decode_key_blob",
    "email_hash",
    "encode_key_blob",
    "key_aad",
    "onboarding_limited_message",
    "send_agent_request",
]

APPROVAL_WINDOW: Final = timedelta(hours=1)
REQUEST_TTL_S: Final = 86_400
KEY_TTL: Final = timedelta(minutes=15)
AGENT_KEY_LIFETIME: Final = timedelta(days=30)
IP_LIMIT: Final = 5
EMAIL_LIMIT: Final = 3
WINDOW_S: Final = 3600
# Billing and key management stay with humans: an agent key never pays or mints keys (specs/04 §10 step 1).
EXCLUDED_SCOPES: Final = frozenset({"*", "billing:read", "billing:write", "api_keys:write"})

REQUEST_NOT_FOUND_MESSAGE: Final = "Agent key request not found."
LINK_INVALID_MESSAGE: Final = "Agent key request link is invalid or has expired."
STORE_UNAVAILABLE_MESSAGE: Final = "Agent onboarding is temporarily unavailable; try again shortly."

# ``approving`` is the claim an approval holds while it mints the key; polls report it as pending.
type StoredStatus = Literal["pending_verification", "approving", "approved", "delivered", "denied"]
type PollStatus = Literal["pending_verification", "approved", "delivered", "denied", "expired"]

_STORED_STATUSES: Final[frozenset[str]] = frozenset(get_args(StoredStatus.__value__))
_UNKNOWN_IP: Final = "unknown"


def agent_request_key(request_id: uuid.UUID) -> str:
    return f"agentreq:{request_id}"


def agent_token_key(token_hash: str) -> str:
    return f"agentreq:token:{token_hash}"


def agent_key_key(request_id: uuid.UUID) -> str:
    return f"agentkey:{request_id}"


def agent_ip_key(ip: str | None) -> str:
    return f"agentonb:ip:{ip or _UNKNOWN_IP}"


def email_hash(email: str) -> str:
    """``sha256_hex`` of the case-folded address: the request stores and compares only this."""
    return sha256_hex(email.strip().casefold())


def agent_email_key(email: str) -> str:
    return f"agentonb:email:{email_hash(email)}"


def key_aad(request_id: uuid.UUID) -> str:
    """The envelope AAD binding an encrypted key to its request."""
    return f"agentkey:{request_id}"


def encode_key_blob(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def decode_key_blob(raw: str | bytes) -> bytes:
    return base64.b64decode(raw, validate=True)


def approval_link(dashboard_base_url: str, token: str) -> str:
    """``{DASHBOARD_BASE_URL}/agent-onboarding/{token}`` (the dashboard page of specs/25)."""
    return dashboard_link(dashboard_base_url, "agent-onboarding", token)


def onboarding_limited_message(retry_after_s: int) -> str:
    return f"Too many agent key requests; try again in {seconds_phrase(retry_after_s)}."


def check_requested_scopes(scopes: Iterable[str]) -> list[str]:
    """``scopes`` deduplicated and sorted; unknown or excluded scopes → ``INVALID_INPUT`` on
    ``requested_scopes``."""
    unique = set(scopes)
    for scope in sorted(unique):
        if scope not in SCOPES:
            raise AppError("INVALID_INPUT", f"Unknown scope '{scope}'.", field="requested_scopes")
        if scope in EXCLUDED_SCOPES:
            message = f"Scope '{scope}' cannot be requested by an agent."
            raise AppError("INVALID_INPUT", message, field="requested_scopes")
    return sorted(unique)


def _optional_uuid(value: object) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value is not None else None


def _optional_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """One agent key request; the hashes never appear in ``repr``."""

    request_id: uuid.UUID
    email_hash: str = field(repr=False)
    agent_name: str
    purpose: str
    scopes: tuple[str, ...]
    poll_secret_hash: str = field(repr=False)
    token_hash: str = field(repr=False)
    status: StoredStatus
    created_at: datetime
    expires_at: datetime
    approver_user_id: uuid.UUID | None = None
    org_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    key_expires_at: datetime | None = None

    def effective_status(self, now: datetime) -> PollStatus:
        """What a poll reports: a pending request past its approval deadline, or an approved one whose key was
        not fetched within 15 minutes, is ``expired``; an approval in progress still reads as pending."""
        match self.status:
            case "pending_verification":
                return "expired" if now >= self.expires_at else "pending_verification"
            case "approving":
                return "pending_verification"
            case "approved":
                fresh = self.key_expires_at is not None and now < self.key_expires_at
                return "approved" if fresh else "expired"
            case "delivered" | "denied":
                return self.status

    def approvable(self, now: datetime) -> bool:
        """Whether the link may still approve or deny: pending and before the deadline."""
        return self.status == "pending_verification" and now < self.expires_at

    def to_json(self) -> str:
        return json.dumps_str(
            {
                "request_id": str(self.request_id),
                "email_hash": self.email_hash,
                "agent_name": self.agent_name,
                "purpose": self.purpose,
                "scopes": list(self.scopes),
                "poll_secret_hash": self.poll_secret_hash,
                "token_hash": self.token_hash,
                "status": self.status,
                "created_at": self.created_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "approver_user_id": str(self.approver_user_id) if self.approver_user_id else None,
                "org_id": str(self.org_id) if self.org_id else None,
                "key_id": str(self.key_id) if self.key_id else None,
                "key_expires_at": _optional_iso(self.key_expires_at),
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        """Inverse of :meth:`to_json`; ``ValueError``/``KeyError``/``TypeError`` on a malformed value."""
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = "agent request is not a JSON object"
            raise TypeError(msg)
        status = data["status"]
        scopes = data["scopes"]
        if status not in _STORED_STATUSES or not isinstance(scopes, list):
            msg = "agent request has an unknown status or malformed scopes"
            raise ValueError(msg)
        texts = {
            name: data[name]
            for name in ("email_hash", "agent_name", "purpose", "poll_secret_hash", "token_hash")
        }
        if not all(isinstance(value, str) for value in texts.values()):
            msg = "agent request text fields must be strings"
            raise TypeError(msg)
        key_expires_at = data.get("key_expires_at")
        return cls(
            request_id=uuid.UUID(data["request_id"]),
            scopes=tuple(str(scope) for scope in scopes),
            status=status,
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            approver_user_id=_optional_uuid(data.get("approver_user_id")),
            org_id=_optional_uuid(data.get("org_id")),
            key_id=_optional_uuid(data.get("key_id")),
            key_expires_at=datetime.fromisoformat(key_expires_at) if key_expires_at else None,
            **texts,
        )


def _unavailable() -> AppError:
    return AppError("SERVICE_UNAVAILABLE", STORE_UNAVAILABLE_MESSAGE)


def _parse(raw: str | bytes | None) -> AgentRequest | None:
    if raw is None:
        return None
    try:
        return AgentRequest.from_json(raw)
    except (KeyError, TypeError, ValueError):
        return None


class AgentRequestStore:
    """The Redis state of agent key requests (module docstring)."""

    def __init__(self, redis: Redis, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    async def create(  # noqa: PLR0913 - the request's fields
        self,
        *,
        email: str,
        agent_name: str,
        purpose: str,
        scopes: Sequence[str],
        poll_secret_hash: str,
        token_hash: str,
        request_id: uuid.UUID,
    ) -> AgentRequest:
        """Store a new pending request and its link index; the approval deadline is one hour away."""
        now = self._clock.now()
        request = AgentRequest(
            request_id=request_id,
            email_hash=email_hash(email),
            agent_name=agent_name,
            purpose=purpose,
            scopes=tuple(scopes),
            poll_secret_hash=poll_secret_hash,
            token_hash=token_hash,
            status="pending_verification",
            created_at=now,
            expires_at=now + APPROVAL_WINDOW,
        )
        link_ttl = int(APPROVAL_WINDOW.total_seconds())
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(agent_request_key(request_id), request.to_json(), ex=REQUEST_TTL_S)
                pipe.set(agent_token_key(token_hash), str(request_id), ex=link_ttl)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        return request

    async def get(self, request_id: uuid.UUID) -> AgentRequest | None:
        """The stored request; ``None`` when unknown, gone after 24 h, or malformed."""
        try:
            raw = await self._redis.get(agent_request_key(request_id))
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        return _parse(raw)

    async def by_token(self, token: str) -> AgentRequest | None:
        """The request an approval link addresses; ``None`` once the link is used or past its hour."""
        try:
            raw_id = await self._redis.get(agent_token_key(sha256_hex(token)))
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        if raw_id is None:
            return None
        try:
            request_id = uuid.UUID(raw_id.decode() if isinstance(raw_id, bytes) else raw_id)
        except ValueError:
            return None
        return await self.get(request_id)

    async def transition(
        self,
        request_id: uuid.UUID,
        from_status: StoredStatus,
        update: Callable[[AgentRequest], AgentRequest],
    ) -> AgentRequest | None:
        """Replace the request with ``update(request)`` if its status is still ``from_status``
        (compare-and-set, TTL kept); ``None`` when another change won the race or the request is gone."""
        key = agent_request_key(request_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current = _parse(await pipe.get(key))
                if current is None or current.status != from_status:
                    return None
                updated = update(current)
                pipe.multi()  # type: ignore[no-untyped-call]  # redis-py leaves Pipeline.multi unannotated
                pipe.set(key, updated.to_json(), keepttl=True)
                await pipe.execute()
        except WatchError:
            return None
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        return updated

    async def put_key(self, request_id: uuid.UUID, encoded_blob: str) -> None:
        """Store the encrypted key for 15 minutes."""
        ttl = int(KEY_TTL.total_seconds())
        try:
            await self._redis.set(agent_key_key(request_id), encoded_blob, ex=ttl)
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc

    async def take_key(self, request_id: uuid.UUID) -> tuple[AgentRequest, str] | None:
        """Atomically mark an ``approved`` request ``delivered`` and remove its stored key, returning both;
        ``None`` when the request is not ``approved``, its key is gone, or a concurrent poll took it."""
        request_key, blob_key = agent_request_key(request_id), agent_key_key(request_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(request_key, blob_key)
                current = _parse(await pipe.get(request_key))
                blob = await pipe.get(blob_key)
                if current is None or current.status != "approved" or blob is None:
                    return None
                delivered = replace(current, status="delivered")
                pipe.multi()  # type: ignore[no-untyped-call]  # redis-py leaves Pipeline.multi unannotated
                pipe.set(request_key, delivered.to_json(), keepttl=True)
                pipe.delete(blob_key)
                await pipe.execute()
        except WatchError:
            return None
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        return delivered, blob.decode("ascii") if isinstance(blob, bytes) else str(blob)

    async def drop_token(self, token_hash: str) -> None:
        """Delete the link index so the link stops working."""
        try:
            await self._redis.delete(agent_token_key(token_hash))
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc


class OnboardingLimiter:
    """5 creations per client IP and 3 per email address in fixed one-hour windows; fails closed."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, ip: str | None, email: str) -> None:
        """Count one creation, the IP first; ``RATE_LIMITED`` with ``Retry-After`` beyond either limit."""
        for key, limit in ((agent_ip_key(ip), IP_LIMIT), (agent_email_key(email), EMAIL_LIMIT)):
            await hit_window(
                self._redis,
                key,
                limit=limit,
                window_s=WINDOW_S,
                message=onboarding_limited_message,
                unavailable_event=None,
            )


async def send_agent_request(
    sender: EmailSender, *, to: str, request: AgentRequest, token: str, dashboard_base_url: str
) -> None:
    """Send the ``agent_key_request`` template (never the poll secret); failures are logged as
    ``agent_request_email_failed``."""
    message = agent_key_request(
        agent_name=request.agent_name,
        purpose=request.purpose,
        scopes=request.scopes,
        link=approval_link(dashboard_base_url, token),
        to=to,
    )
    await deliver(sender, message, failure_event="agent_request_email_failed")
