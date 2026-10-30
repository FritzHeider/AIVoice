"""Seeded identifiers for simulator resources (specs/30-testing-and-evals.md §3: simulators are
deterministic).

Every id a simulator hands out comes from the shared state's RNG, so two runs with the same seed and the same
requests produce the same ids. ``SimState.reset`` reseeds the RNG, which restarts the id sequence.
"""

from __future__ import annotations

import base64
import random
import uuid

__all__ = ["SimIds"]


class SimIds:
    """UUIDs, url-safe tokens, and numeric ids drawn from ``rng``."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def uuid(self) -> str:
        """A version-4 UUID string."""
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    def token(self, n: int = 16) -> str:
        """``n`` random bytes as unpadded url-safe base64."""
        return base64.urlsafe_b64encode(self._rng.randbytes(n)).rstrip(b"=").decode("ascii")

    def digits(self, n: int = 19) -> str:
        """An ``n``-digit decimal id without a leading zero (Telnyx numeric resource ids are 19 digits)."""
        if n < 1:
            msg = f"a numeric id needs at least one digit, got {n}"
            raise ValueError(msg)
        return str(self._rng.randrange(10 ** (n - 1), 10**n))
