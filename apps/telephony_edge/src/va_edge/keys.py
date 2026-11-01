"""Webhook verification keys selected by the route (specs/06-telephony-telnyx.md §1, §4.1 Decision P05-T01).

``/telnyx/{voice|messaging}`` verifies with the platform ``TELNYX_PUBLIC_KEY``;
``/telnyx/{voice|messaging}/{telnyx_account_id}`` with that BYO account's ``public_key``. The body is
untrusted until verified, so nothing in it ever selects the key. BYO lookups are cached in-process for 5
minutes (the BYO credential cache of specs/06 §1); unknown accounts are cached for 30 seconds so repeated
requests for the same forged id do not reach Postgres every time. Both caches are LRU-bounded
(``MAX_KNOWN`` and ``MAX_UNKNOWN`` entries) and kept apart, so a flood of distinct random account ids
costs one lookup each but neither grows memory without limit nor evicts the keys of real accounts.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from nacl.signing import VerifyKey

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.repositories import TelnyxWebhookKey
from va_telnyx.signatures import load_public_key

__all__ = [
    "KEY_TTL_S",
    "MAX_KNOWN",
    "MAX_UNKNOWN",
    "UNKNOWN_TTL_S",
    "KeyCachePolicy",
    "KeyLookup",
    "UnknownAccountError",
    "WebhookKey",
    "WebhookKeys",
]

KEY_TTL_S: Final = 300.0
UNKNOWN_TTL_S: Final = 30.0
MAX_KNOWN: Final = 10_000
MAX_UNKNOWN: Final = 10_000

type KeyLookup = Callable[[uuid.UUID], Awaitable[TelnyxWebhookKey | None]]

_log = get_logger(__name__)


class UnknownAccountError(Exception):
    """The path names no BYO Telnyx account that may send webhooks (unknown, deleted, malformed id or key)."""


@dataclass(frozen=True, slots=True)
class WebhookKey:
    """The key that must have signed the request; ``org_id`` is set for a BYO account (its owner)."""

    key: VerifyKey
    org_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class KeyCachePolicy:
    """Lifetimes and LRU bounds of the account key caches (known and unknown accounts are cached apart)."""

    ttl_s: float = KEY_TTL_S
    unknown_ttl_s: float = UNKNOWN_TTL_S
    max_known: int = MAX_KNOWN
    max_unknown: int = MAX_UNKNOWN


@dataclass(frozen=True, slots=True)
class _Cached:
    expires_at: float
    key: WebhookKey | None


class _Lru:
    """Least-recently-used map of at most ``limit`` cache entries; expired entries read as absent."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            msg = "a key cache needs room for at least one entry"
            raise ValueError(msg)
        self._limit = limit
        self._items: OrderedDict[uuid.UUID, _Cached] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, account_id: uuid.UUID, now: float) -> _Cached | None:
        entry = self._items.get(account_id)
        if entry is None:
            return None
        if entry.expires_at <= now:
            del self._items[account_id]
            return None
        self._items.move_to_end(account_id)
        return entry

    def put(self, account_id: uuid.UUID, entry: _Cached) -> None:
        self._items[account_id] = entry
        self._items.move_to_end(account_id)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def pop(self, account_id: uuid.UUID) -> None:
        self._items.pop(account_id, None)


class WebhookKeys:
    """Resolves the verification key of a webhook route."""

    def __init__(
        self,
        platform: VerifyKey,
        lookup: KeyLookup,
        *,
        clock: Clock,
        policy: KeyCachePolicy | None = None,
    ) -> None:
        policy = policy if policy is not None else KeyCachePolicy()
        self._platform = WebhookKey(platform)
        self._lookup = lookup
        self._clock = clock
        self._ttl_s = policy.ttl_s
        self._unknown_ttl_s = policy.unknown_ttl_s
        self._known = _Lru(policy.max_known)
        self._unknown = _Lru(policy.max_unknown)

    @property
    def cached(self) -> tuple[int, int]:
        """``(known, unknown)`` entry counts (both bounded)."""
        return len(self._known), len(self._unknown)

    async def resolve(self, account_id: str | None) -> WebhookKey:
        """The platform key for ``None``, else the BYO account's key; ``UnknownAccountError`` otherwise."""
        if account_id is None:
            return self._platform
        try:
            parsed = uuid.UUID(account_id)
        except ValueError as exc:
            msg = "the webhook path does not name a Telnyx account id"
            raise UnknownAccountError(msg) from exc
        now = self._clock.monotonic()
        cached = self._known.get(parsed, now) or self._unknown.get(parsed, now)
        if cached is None:
            key = await self._load(parsed)
            if key is not None:
                cached = _Cached(now + self._ttl_s, key)
                self._unknown.pop(parsed)
                self._known.put(parsed, cached)
            else:
                cached = _Cached(now + self._unknown_ttl_s, None)
                self._known.pop(parsed)
                self._unknown.put(parsed, cached)
        if cached.key is None:
            msg = "the webhook path names no active Telnyx account"
            raise UnknownAccountError(msg)
        return cached.key

    async def _load(self, account_id: uuid.UUID) -> WebhookKey | None:
        found = await self._lookup(account_id)
        if found is None:
            return None
        try:
            key = load_public_key(found.public_key)
        except ValueError:
            _log.error(
                "telnyx.account_key_invalid", telnyx_account_id=str(account_id), org_id=str(found.org_id)
            )
            return None
        return WebhookKey(key, found.org_id)
