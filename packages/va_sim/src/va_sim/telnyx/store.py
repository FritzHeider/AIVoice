"""FakeTelnyx resource store (specs/30-testing-and-evals.md §3.2): accounts and their resources.

Each API key is an account owning its applications, profiles, numbers, orders, and messages; another account's
ids are invisible (404). Records are stored response-shaped so handlers serve them as they are.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

from va_sim.telnyx.inventory import AvailableNumber, seed_inventory
from va_sim.telnyx.models import TelnyxSimConfig

__all__ = ["DEFAULT_API_KEY", "Account", "Record", "TelnyxStore"]

DEFAULT_API_KEY: Final = "sim-telnyx-key"

type Record = dict[str, Any]


@dataclass(slots=True)
class Account:
    api_key: str
    apps: dict[str, Record] = field(default_factory=dict)
    voice_profiles: dict[str, Record] = field(default_factory=dict)
    messaging_profiles: dict[str, Record] = field(default_factory=dict)
    numbers: dict[str, Record] = field(default_factory=dict)
    orders: dict[str, Record] = field(default_factory=dict)
    messages: dict[str, Record] = field(default_factory=dict)

    def number_by_e164(self, phone_number: str) -> Record | None:
        return next((n for n in self.numbers.values() if n["phone_number"] == phone_number), None)


class TelnyxStore:
    """Accounts, behaviour config, and the shared number inventory."""

    def __init__(self) -> None:
        self.inventory: Final[tuple[AvailableNumber, ...]] = seed_inventory()
        self._inventory_index: Final = {number.phone_number: number for number in self.inventory}
        self.accounts: dict[str, Account] = {}
        self.config = TelnyxSimConfig()
        # Numbers reserved by a pending order or owned by an account (excluded from search, not orderable).
        self.taken: set[str] = set()
        self.reset()

    def reset(self) -> None:
        """Only the built-in account remains, without resources; config back to defaults."""
        self.accounts = {DEFAULT_API_KEY: Account(DEFAULT_API_KEY)}
        self.config = TelnyxSimConfig()
        self.taken.clear()

    def account(self, api_key: str) -> Account:
        """The account of ``api_key``, registered if new."""
        return self.accounts.setdefault(api_key, Account(api_key))

    def available(self, phone_number: str) -> AvailableNumber | None:
        """The inventory entry of ``phone_number`` when it can be ordered."""
        if phone_number in self.taken:
            return None
        return self._inventory_index.get(phone_number)

    def inventory_entry(self, phone_number: str) -> AvailableNumber | None:
        return self._inventory_index.get(phone_number)

    def owner_of(self, phone_number: str) -> tuple[Account, Record] | None:
        """The account owning ``phone_number`` and its ``phone_number`` record."""
        for account in self.accounts.values():
            record = account.number_by_e164(phone_number)
            if record is not None:
                return account, record
        return None

    def all_messages(self) -> Iterator[Record]:
        for account in self.accounts.values():
            yield from account.messages.values()
