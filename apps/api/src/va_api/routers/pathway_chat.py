"""The pathway test chat under ``/v1/pathway/chat`` (specs/12-pathways-api.md §7; legacy bodies).

Rules: specs/12 §7 Decision P07-T10. :data:`va_api.main.ROUTERS` lists this router before the pathways
router, so ``/v1/pathway/chat/…`` is never taken by a ``/v1/pathway/{pathway_id}`` route. Every route needs
``pathways:write`` and, for session principals, an editing role. Nothing persists beyond the Redis chat
document, so the routes write no audit rows.
"""

from __future__ import annotations

import uuid

from fastapi import Request

from va_api.audit import audit_exempt
from va_api.auth.session_auth import clock_of, database_of, org_settings_of, redis_of
from va_api.envelopes import legacy_router
from va_api.routers.pathway_chat_schemas import (
    ChatCreate,
    ChatCreated,
    ChatDeleted,
    ChatMessage,
    ChatReply,
    chat_reply,
)
from va_api.routers.pathways import EDITOR, WRITE_SCOPE, WritePathways, allow_http
from va_api.services.pathway_chat import (
    DELETED_MESSAGE,
    ChatDeps,
    ChatStart,
    create_chat,
    delete_chat,
    send_message,
)
from va_api.services.pathways import parse_version_selector

__all__ = ["pathway_chat_router", "pathways_chat_create", "pathways_chat_reset", "pathways_chat_send"]

pathway_chat_router = legacy_router(prefix="/v1/pathway/chat", tags=["Pathways"])

_EXEMPT = audit_exempt("ephemeral test chat; nothing persists beyond Redis")


def chat_deps(request: Request) -> ChatDeps:
    """The chat's dependencies from the app container."""
    container = request.app.state.container
    return ChatDeps(
        redis=redis_of(request),
        database=database_of(request),
        gateway=container.llm_gateway,
        tools_http=container.tools_http,
        org_settings=org_settings_of(request),
        clock=clock_of(request),
        allow_http=allow_http(request),
    )


@pathway_chat_router.post(
    "/create",
    operation_id="pathways_chat_create",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **_EXEMPT},
)
async def pathways_chat_create(body: ChatCreate, request: Request, ctx: WritePathways) -> ChatCreated:
    """Start a text simulation of a pathway version; the start node's opening comes back at once."""
    start = ChatStart(
        pathway_id=body.pathway_id,
        version=parse_version_selector(str(body.version)),
        request_data=body.request_data,
        start_node_id=body.start_node_id,
        simulate_from=body.simulate_from,
        simulate_to=body.simulate_to,
    )
    turn = await create_chat(chat_deps(request), ctx, start)
    return chat_reply(ChatCreated, turn)


@pathway_chat_router.post(
    "/{chat_id}",
    operation_id="pathways_chat_send",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **_EXEMPT},
)
async def pathways_chat_send(
    chat_id: uuid.UUID, body: ChatMessage, request: Request, ctx: WritePathways
) -> ChatReply:
    """Send the caller's message; with ``branch_from_turn`` the chat forks into a new ``chat_id``."""
    turn = await send_message(chat_deps(request), ctx, chat_id, body.message, body.branch_from_turn)
    return chat_reply(ChatReply, turn)


@pathway_chat_router.delete(
    "/{chat_id}",
    operation_id="pathways_chat_reset",
    dependencies=EDITOR,
    openapi_extra={**WRITE_SCOPE, **_EXEMPT},
)
async def pathways_chat_reset(chat_id: uuid.UUID, request: Request, ctx: WritePathways) -> ChatDeleted:
    """Delete the chat (reset: start a new one with ``/create``)."""
    await delete_chat(chat_deps(request), ctx, chat_id)
    return ChatDeleted(message=DELETED_MESSAGE)
