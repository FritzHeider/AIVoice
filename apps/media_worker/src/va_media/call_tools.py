"""The custom tools of a call (specs/13 §5; Decision P08-T02b1 under specs/13 §5, Decision P08-T02c2 under
specs/13 §4).

:func:`load_call_tools` reads stored tools (live rows of the call's org, in the order asked) in one tenant
session and decrypts their secret headers only after the session closed; ids that are absent, deleted or of
another org are skipped (logged ``media.tool_missing`` with a count).

Task mode: :meth:`CallToolSource.executor` builds the call's :class:`~va_tools.executor.CallToolExecutor`
over the tools of ``config.tool_ids`` merged with the call's inline tools
(:func:`~va_tools.executor.merged_tools`) and the shared tool HTTP client; a call without tools gets none,
and a load failure (database, KMS, a foreign secret blob) is logged ``media.tools_unavailable`` and leaves
only the inline tools. Tool requests are signed with the org's active webhook secrets (specs/13 §5 step 4,
:func:`~va_tools.signing.tool_signer`); when they cannot be read (``media.tool_signing_unavailable``) the
requests go out unsigned. The executor also runs the system tools the runtime built for the call
(:mod:`va_media.call_system_tools`), also when the call has no custom tools.

Pathway calls: :meth:`CallToolSource.pathway_tools` builds a :class:`PathwayCallTools` over the call's
:class:`~va_tools.pathway_executor.PathwayCallExecutor` (always: reply nodes may hold inline tools, and the
executor runs the call's system tools). :meth:`PathwayCallTools.ensure` loads the stored tools a graph's
node ``tools`` and ``globalConfig.globalTools`` name, for the pinned graph at call start and, through
:meth:`PathwayCallTools.loader`, for every graph the call switches to (Transfer Pathway, pathway forwarding).
An id whose lookup ran is not looked up again in the call. A load that failed is logged
``media.tools_unavailable`` and leaves that graph's inline tools; it learned nothing about the ids, so the
next load of the graph looks again. Requests are signed like task-mode ones, with a signer read when the
first graph with tool refs is seen; a read that failed is repeated at the next load of a graph with tool
refs (an org that is gone is not asked for again). A load at a graph switch runs inside the caller's turn,
so it is given :data:`SWITCH_LOAD_TIMEOUT_S` for the tools and the signing secrets together; running out of
time counts as a failure of what was not finished.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import httpx
from sqlalchemy.exc import SQLAlchemyError

from va_common.crypto import CryptoError, Envelope
from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_db.repositories import OrgRepo, ToolRepo
from va_db.session import Database
from va_db.webhook_secrets import active_signing_secrets
from va_domain.call_context import CallContext
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_engine.tool_executor import ToolRef
from va_tools.executor import CallToolExecutor, SystemTool, ToolEvents, merged_tools
from va_tools.http import HttpExecutor, Signer
from va_tools.models import ToolSpec
from va_tools.pathway_executor import PathwayCallExecutor, graph_tool_refs, tool_ref_id
from va_tools.runner import ToolRunner
from va_tools.secrets import decrypt_secret_headers
from va_tools.signing import tool_signer

__all__ = ["SWITCH_LOAD_TIMEOUT_S", "CallToolSource", "PathwayCallTools", "SigningRead", "load_call_tools"]

SWITCH_LOAD_TIMEOUT_S: Final = 2.0
"""What a graph switch may spend on the new graph's stored tools and the signing secrets (call clock)."""

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SigningRead:
    """What reading an org's signing secrets gave: the ``signer`` (``None``: requests go out unsigned) and
    whether the answer is ``final`` (the read worked, or the org is gone) or a later read may give another
    (the read failed)."""

    signer: Signer | None
    final: bool


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
            signer=await self.signer(context.org_id) if specs else None,
        )
        return CallToolExecutor(
            specs, runner=ToolRunner(http, clock=self.clock), events=events, system=system
        )

    async def pathway_tools(
        self,
        context: CallContext,
        graph: PathwayGraph,
        events: ToolEvents | None,
        *,
        system: Sequence[SystemTool] = (),
    ) -> PathwayCallTools:
        """The tools of a pathway call on its pinned ``graph`` (that graph's stored tools already loaded)
        and the ``system`` tools the runtime built."""
        signer = _CallSigner()
        http = HttpExecutor(
            self.tools_http,
            clock=self.clock,
            allow_http=self.allow_http,
            call_id=str(context.call_id),
            signer=signer,
        )
        executor = PathwayCallExecutor(
            runner=ToolRunner(http, clock=self.clock), events=events, system=system
        )
        tools = PathwayCallTools(self, context.org_id, executor, signer)
        await tools.ensure(graph)
        return tools

    async def signer(self, org_id: uuid.UUID) -> Signer | None:
        """The signer over the org's active webhook secrets (``None`` when they cannot be read)."""
        return (await self.read_signer(org_id)).signer

    async def read_signer(self, org_id: uuid.UUID) -> SigningRead:
        """The signer over the org's active webhook secrets, and whether asking again could change it."""
        try:
            async with self.database.global_session() as session:
                org = await OrgRepo(session).get(org_id)
            if org is None:
                _log.warning("media.tool_signing_unavailable", error_type="OrgMissing")
                return SigningRead(None, final=True)
            active = await active_signing_secrets(self.envelope, org, now=self.clock.now())
        except (CryptoError, SQLAlchemyError, OSError) as exc:
            _log.warning("media.tool_signing_unavailable", error_type=type(exc).__name__)
            return SigningRead(None, final=False)
        return SigningRead(tool_signer(active.secrets, self.clock), final=True)


class _CallSigner:
    """The call's request signer, set once the first tools are loaded (unset: requests go out unsigned)."""

    def __init__(self) -> None:
        self.inner: Signer | None = None

    def __call__(self, body: bytes) -> Mapping[str, str]:
        return {} if self.inner is None else self.inner(body)


class PathwayCallTools:
    """The tools of one pathway call: its executor, and the loading of the stored tools each graph the call
    runs on references."""

    def __init__(
        self, source: CallToolSource, org_id: uuid.UUID, executor: PathwayCallExecutor, signer: _CallSigner
    ) -> None:
        self.executor = executor
        self._source = source
        self._org_id = org_id
        self._signer = signer
        self._tried: set[uuid.UUID] = set()
        self._signing_read = False

    async def ensure(self, graph: PathwayGraph, *, timeout_s: float | None = None) -> None:
        """Make the stored tools ``graph`` references resolvable and, until it worked once, read the org's
        signing secrets; never raises. An id whose lookup ran is not looked up again; a lookup that failed,
        ran out of time (``timeout_s`` on the call clock, for the tools and the secrets together) or was
        cancelled (the turn that switched graphs was cut) learned nothing: the next load of the graph looks
        again, and until then the graph has its inline tools. The same holds for the signing secrets."""
        refs = graph_tool_refs(graph)
        if not refs:
            return
        clock = self._source.clock
        deadline = None if timeout_s is None else clock.monotonic() + timeout_s
        if not await within(self._load(refs), clock, timeout_s):
            _log.warning("media.tools_unavailable", error_type="TimeoutError")
            self.executor.add((), refs)
            return
        if self._signing_read:
            return
        left = None if deadline is None else deadline - clock.monotonic()
        if not await within(self._read_signing(), clock, left):
            _log.warning("media.tool_signing_unavailable", error_type="TimeoutError")

    async def _load(self, refs: Sequence[ToolRef]) -> None:
        """Look up the stored tools among ``refs`` not looked up yet and hand them, with the refs, to the
        executor (state changes only after the await, so a cancelled load leaves no trace)."""
        ids = [tool_ref_id(ref) for ref in refs if isinstance(ref, str)]
        wanted = [
            tool_id
            for tool_id in dict.fromkeys(ids)
            if tool_id is not None and tool_id not in self._tried and not self.executor.knows(tool_id)
        ]
        source = self._source
        try:
            stored = await load_call_tools(source.database, source.envelope, self._org_id, wanted)
        except (CryptoError, SQLAlchemyError, OSError) as exc:
            _log.warning("media.tools_unavailable", error_type=type(exc).__name__)
            stored = ()
        else:
            self._tried.update(wanted)
        self.executor.add(stored, refs)

    async def _read_signing(self) -> None:
        read = await self._source.read_signer(self._org_id)
        self._signer.inner = read.signer
        self._signing_read = read.final

    def loader(self, inner: PathwayLoader) -> PathwayLoader:
        """``inner`` whose every loaded graph has its stored tools loaded before the engine runs on it."""
        return _ToolLoadingLoader(inner, self)


class _ToolLoadingLoader:
    """A :class:`~va_engine.navigation.PathwayLoader` that loads a graph, then its tools within
    :data:`SWITCH_LOAD_TIMEOUT_S` (the inner loader's errors pass through untouched)."""

    def __init__(self, inner: PathwayLoader, tools: PathwayCallTools) -> None:
        self._inner = inner
        self._tools = tools

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        active = await self._inner.load(pathway_id, version)
        await self._tools.ensure(active.graph, timeout_s=SWITCH_LOAD_TIMEOUT_S)
        return active
