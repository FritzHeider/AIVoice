"""Per-org client address allowlists (``OrgSettings.ip_allowlist``, specs/04-auth-tenancy-rbac.md §7).

An org with a non-empty ``ip_allowlist`` accepts API keys and sessions only from addresses inside one of its
networks. The client address is the ASGI peer (``session_auth.client_ip``); behind a load balancer uvicorn
rewrites it from ``X-Forwarded-For`` for the proxies listed in ``FORWARDED_ALLOW_IPS``. An IPv4-mapped IPv6
peer matches as its IPv4 address, and a peer that is not an IP address never matches a non-empty list.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Sequence
from typing import Final

from fastapi import Request

from va_api.auth.context import RequestContext
from va_api.auth.session_auth import client_ip, org_settings_of
from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = ["IP_NOT_ALLOWED_MESSAGE", "enforce_ip_allowlist", "ip_allowed"]

IP_NOT_ALLOWED_MESSAGE: Final = "Requests from this IP address are not allowed for this organization."

_log = get_logger(__name__)


def ip_allowed(ip: str | None, networks: Sequence[str]) -> bool:
    """Whether ``ip`` is inside one of ``networks``; an empty list allows every request, a missing or
    malformed ``ip`` matches none."""
    if not networks:
        return True
    if ip is None:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in ipaddress.ip_network(network) for network in networks)


async def enforce_ip_allowlist(request: Request, ctx: RequestContext) -> None:
    """``FORBIDDEN`` unless the client address passes the allowlist of the org ``ctx`` acts in and, while a
    parent acts on a sub-account, of the parent too. Orgs that no longer exist are skipped; a stored list that
    no longer validates reads as empty (``OrgSettingsReader.get_lenient``)."""
    orgs: list[uuid.UUID] = [ctx.org_id]
    if ctx.acting_parent_org_id is not None and ctx.acting_parent_org_id != ctx.org_id:
        orgs.append(ctx.acting_parent_org_id)
    ip = client_ip(request)
    reader = org_settings_of(request)
    for org_id in orgs:
        settings = await reader.get_lenient(org_id)
        if settings is not None and not ip_allowed(ip, settings.ip_allowlist):
            _log.warning("ip_not_allowed", org_id=str(org_id), ip=ip)
            raise AppError("FORBIDDEN", IP_NOT_ALLOWED_MESSAGE)
