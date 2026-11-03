"""A fake ``HostResolver`` (``va_common.net``) for tests that must never touch real DNS."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["MappedResolver"]


@dataclass
class MappedResolver:
    """Answers ``answers[host]``, else ``default``; with no ``default`` an unknown host fails like NXDOMAIN
    (``OSError``). Records every lookup."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    default: list[str] | None = None
    lookups: list[str] = field(default_factory=list)

    async def __call__(self, host: str) -> list[str]:
        self.lookups.append(host)
        found = self.answers.get(host, self.default)
        if found is None:
            raise OSError(host)
        return list(found)
