"""The custom tools of a task-mode call (specs/13 §5; Decision P08-T02b1 under specs/13 §5).

:func:`load_call_tools` reads the call's stored tools (``config.tool_ids``, live rows of the call's org, in
that order) in one tenant session and decrypts their secret headers only after the session closed; ids that
are absent, deleted or of another org are skipped (logged ``media.tool_missing`` with a count).
:class:`CallToolSource` builds the call's :class:`~va_tools.executor.CallToolExecutor` over those tools merged
with the call's inline tools (:func:`~va_tools.executor.merged_tools`) and the shared tool HTTP client; a call
without tools gets none, and a load failure (database, KMS, a foreign secret blob) is logged
``media.tools_unavailable`` and leaves only the inline tools. Tool requests are signed with the org's active
webhook secrets (specs/13 §5 step 4, :func:`~va_tools.signing.tool_signer`); when they cannot be read
(``media.tool_signing_unavailable``) the requests go out unsigned. The executor also runs the system tools
the runtime built for the call (:mod:`va_media.call_system_tools`), also when the call has no custom tools.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.exc import SQLAlchemyError

from va_common.crypto import CryptoError, Envelope
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.repositories import OrgRepo, ToolRepo
from va_db.session import Database
from va_db.webhook_secrets import active_signing_secrets
from va_domain.call_context import CallContext
from va_tools.executor import CallToolExecutor, SystemTool, ToolEvents, merged_tools
from va_tools.http import HttpExecutor, Signer
from va_tools.models import ToolSpec
from va_tools.runner import ToolRunner
from va_tools.secrets import decrypt_secret_headers
from va_tools.signing import tool_signer

__all__ = ["CallToolSource", "load_call_tools"]

_log = get_logger(__name__)


async def load_call_tools(
    database: Database, envelope: Envelope, org_id: uuid.UUID, tool_ids: Sequence[uuid.UUID]
) -> tuple[ToolSpec, ...]:
    """The org's live tools among ``tool_ids`` with their decrypted secret headers, in ``tool_ids`` order."""
    if not tool_ids:
        return ()
    async with database.tenant_session(org_id) as session:
        rows = await ToolRepo(session, org_id).by_ids(tool_ids)
        stored = [(row, row.secret_headers_enc) for row in rows]
    missing = len(dict.fromkeys(tool_ids)) - len(stored)
    if missing:
        _log.warning("media.tool_missing", count=missing)
    return tuple(
        [
            ToolSpec.from_row(row, await decrypt_secret_headers(envelope, org_id, row.id, blob))
            for row, blob in stored
        ]
    )


@dataclass(frozen=True, slots=True)
class CallToolSource:
    """The per-process pieces of task-mode tools: the database and envelope for stored tools, the shared tool
    HTTP client, and whether tool URLs may be ``http://``/private (``local``/``test`` only)."""

    database: Database
    envelope: Envelope
    tools_http: httpx.AsyncClient
    allow_http: bool
    clock: Clock

    async def executor(
        self, context: CallContext, events: ToolEvents | None, *, system: Sequence[SystemTool] = ()
    ) -> CallToolExecutor | None:
        """The call's tool executor over its custom tools and the ``system`` tools the runtime built
        (``None`` when the call has neither)."""
        config = context.config
        specs: tuple[ToolSpec, ...] = ()
        if config.tool_ids or config.inline_tools:
            try:
                stored = await load_call_tools(self.database, self.envelope, context.org_id, config.tool_ids)
            except (CryptoError, SQLAlchemyError, OSError) as exc:
                _log.warning("media.tools_unavailable", error_type=type(exc).__name__)
                stored = ()
            specs = merged_tools(stored, [ToolSpec.from_inline(tool) for tool in config.inline_tools])
        if not specs and not system:
            return None
        http = HttpExecutor(
            self.tools_http,
            clock=self.clock,
            allow_http=self.allow_http,
            call_id=str(context.call_id),
            signer=await self._signer(context.org_id) if specs else None,
        )
        return CallToolExecutor(
            specs, runner=ToolRunner(http, clock=self.clock), events=events, system=system
        )

    async def _signer(self, org_id: uuid.UUID) -> Signer | None:
        """The signer over the org's active webhook secrets (``None`` when they cannot be read)."""
        try:
            async with self.database.global_session() as session:
                org = await OrgRepo(session).get(org_id)
            if org is None:
                _log.warning("media.tool_signing_unavailable", error_type="OrgMissing")
                return None
            active = await active_signing_secrets(self.envelope, org, now=self.clock.now())
        except (CryptoError, SQLAlchemyError, OSError) as exc:
            _log.warning("media.tool_signing_unavailable", error_type=type(exc).__name__)
            return None
        return tool_signer(active.secrets, self.clock)
