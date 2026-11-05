"""Tools API (specs/13-tools-and-dynamic-data.md §2, §3).

Rules: specs/13 §2 Decision P08-T01. A definition is valid when :class:`va_domain.calls.tools.InlineTool`
accepts it and :func:`va_tools.validation.check_tool_definition` finds nothing (reserved names, ``https``
outside ``local``/``test``, JSON Schema, JSONPath, secret headers). Updates merge the sent fields over the
stored definition and validate the result as a whole. Secret headers are encrypted before the tenant session
opens (the envelope reads the org's active data key through its own session) and only masked values leave
this module, in responses and in audit rows alike. An update that changes nothing writes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.audit import audit, changed
from va_api.auth.context import RequestContext
from va_api.routers.tools_schemas import ToolCreate, ToolUpdate, ToolView
from va_common.crypto import Envelope
from va_common.errors import AppError
from va_common.ids import PublicPrefix, new_id, to_public
from va_db.models import Tool
from va_db.repositories import ToolReference, ToolRepo
from va_db.session import Database
from va_domain.calls.tools import InlineTool
from va_tools.pathway_executor import tool_ref_id
from va_tools.secrets import decrypt_secret_headers, encrypt_secret_headers, mask_headers
from va_tools.validation import check_tool_definition

__all__ = [
    "DELETED_MESSAGE",
    "INVALID_TOOL_ID_MESSAGE",
    "IN_USE_MESSAGE",
    "ToolDeps",
    "ToolPage",
    "create_tool",
    "delete_tool",
    "get_tool",
    "list_tools",
    "parse_tool_id",
    "update_tool",
]

DELETED_MESSAGE: Final = "Tool deleted."
IN_USE_MESSAGE: Final = (
    "Tool is used by published pathways, personas, or agents; pass force=true to delete it."
)
INVALID_TOOL_ID_MESSAGE: Final = "tool_id must be TL-<uuid> or a uuid."
_SECRET_FIELD: Final = "secret_headers"  # noqa: S105 - a field name


@dataclass(frozen=True, slots=True)
class ToolDeps:
    """What the tools service needs from the container."""

    database: Database
    envelope: Envelope
    allow_http: bool


@dataclass(frozen=True, slots=True)
class ToolPage:
    """One page of tool views."""

    tools: list[ToolView]
    next_cursor: str | None


def parse_tool_id(value: str) -> uuid.UUID:
    """``TL-<uuid>`` or a bare uuid; ``INVALID_INPUT`` (field ``tool_id``) otherwise."""
    tool_id = tool_ref_id(value)
    if tool_id is None:
        raise AppError("INVALID_INPUT", INVALID_TOOL_ID_MESSAGE, field="tool_id")
    return tool_id


def _definition(row: Tool) -> dict[str, Any]:
    """The stored definition in ToolCreate field names (without secret headers)."""
    return {
        "name": row.name,
        "description": row.description,
        "speech": row.speech,
        "url": row.url,
        "method": row.method,
        "headers": dict(row.headers),
        "query": dict(row.query),
        "body": row.body,
        "input_schema": dict(row.input_schema),
        "response": dict(row.response_map),
        "timeout_ms": row.timeout_ms,
        "retries": row.retries,
    }


def _columns(tool: InlineTool) -> dict[str, Any]:
    """The ``tools`` column values of a validated definition."""
    return {
        "name": tool.name,
        "description": tool.description,
        "speech": tool.speech,
        "url": tool.url,
        "method": tool.method,
        "headers": dict(tool.headers),
        "query": dict(tool.query),
        "body": tool.body,
        "input_schema": dict(tool.input_schema),
        "response_map": dict(tool.response),
        "timeout_ms": tool.timeout_ms,
        "retries": tool.retries,
    }


def _view(row: Tool, secret_headers: Mapping[str, str]) -> ToolView:
    return ToolView(
        tool_id=to_public(PublicPrefix.TOOL, row.id),
        **_definition(row),
        secret_headers=mask_headers(secret_headers),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _audited(view: ToolView) -> dict[str, Any]:
    return view.model_dump(mode="json", exclude={"tool_id", "created_at", "updated_at"})


def _validated(raw: Mapping[str, Any]) -> InlineTool:
    """``raw`` as an InlineTool; pydantic errors render like request validation errors (field paths)."""
    try:
        return InlineTool.model_validate(dict(raw))
    except ValidationError as exc:
        errors = [{**error, "loc": ("body", *error["loc"])} for error in exc.errors()]
        raise RequestValidationError(errors) from None


async def _secrets(deps: ToolDeps, org_id: uuid.UUID, row: Tool) -> dict[str, str]:
    return await decrypt_secret_headers(deps.envelope, org_id, row.id, row.secret_headers_enc)


async def create_tool(deps: ToolDeps, ctx: RequestContext, body: ToolCreate) -> ToolView:
    """Validate, encrypt the secret headers, insert, and audit ``tool.create``."""
    definition = InlineTool.model_validate(body.model_dump(exclude={_SECRET_FIELD}))
    check_tool_definition(definition, secret_headers=body.secret_headers, allow_http=deps.allow_http)
    tool_id = new_id()
    blob = await encrypt_secret_headers(deps.envelope, ctx.org_id, tool_id, body.secret_headers)
    async with deps.database.tenant_session(ctx.org_id) as session:
        row = await ToolRepo(session, ctx.org_id).create(
            {**_columns(definition), "secret_headers_enc": blob}, tool_id=tool_id
        )
        view = _view(row, body.secret_headers)
        await audit(session, ctx, "tool.create", "tool", row.id, after=_audited(view))
    return view


async def get_tool(deps: ToolDeps, ctx: RequestContext, tool_id: uuid.UUID) -> ToolView:
    """One live tool of the org."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        row = await ToolRepo(session, ctx.org_id).get(tool_id)
        return _view(row, await _secrets(deps, ctx.org_id, row))


async def list_tools(
    deps: ToolDeps, ctx: RequestContext, *, limit: int, cursor: str | None, search: str | None
) -> ToolPage:
    """The org's live tools, newest first."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        page = await ToolRepo(session, ctx.org_id).page(limit=limit, cursor=cursor, search=search)
        views = [_view(row, await _secrets(deps, ctx.org_id, row)) for row in page.items]
    return ToolPage(tools=views, next_cursor=page.next_cursor)


@dataclass(frozen=True, slots=True)
class _SecretChange:
    """The secret headers after an update and their blob (``blob`` unused when ``sent`` is false)."""

    sent: bool
    headers: dict[str, str]
    blob: bytes | None


async def _secret_change(
    deps: ToolDeps, ctx: RequestContext, tool_id: uuid.UUID, body: ToolUpdate
) -> _SecretChange:
    if _SECRET_FIELD not in body.model_fields_set:
        return _SecretChange(sent=False, headers={}, blob=None)
    headers = dict(body.secret_headers or {})
    blob = await encrypt_secret_headers(deps.envelope, ctx.org_id, tool_id, headers)
    return _SecretChange(sent=True, headers=headers, blob=blob)


async def update_tool(deps: ToolDeps, ctx: RequestContext, tool_id: uuid.UUID, body: ToolUpdate) -> ToolView:
    """Merge the sent fields over the stored definition, validate, store, and audit ``tool.update``."""
    sent = body.model_dump(include=body.model_fields_set - {_SECRET_FIELD})
    change = await _secret_change(deps, ctx, tool_id, body)
    async with deps.database.tenant_session(ctx.org_id) as session:
        repo = ToolRepo(session, ctx.org_id)
        row = await repo.get(tool_id, lock=True)
        stored_secrets = await _secrets(deps, ctx.org_id, row)
        definition = _validated({**_definition(row), **sent})
        secrets_after = change.headers if change.sent else stored_secrets
        check_tool_definition(definition, secret_headers=secrets_after, allow_http=deps.allow_http)
        before = _view(row, stored_secrets)
        values = {key: value for key, value in _columns(definition).items() if getattr(row, key) != value}
        secrets_changed = change.sent and change.headers != stored_secrets
        if not values and not secrets_changed:
            return before
        if secrets_changed:
            values["secret_headers_enc"] = change.blob
        row = await repo.update(row, values)
        after = _view(row, secrets_after)
        await _audit_update(session, ctx, row.id, before, after, secrets_changed=secrets_changed)
    return after


async def _audit_update(  # noqa: PLR0913 - the two views and the secret flag of one change
    session: AsyncSession,
    ctx: RequestContext,
    tool_id: uuid.UUID,
    before: ToolView,
    after: ToolView,
    *,
    secrets_changed: bool,
) -> None:
    old, new = changed(_audited(before), _audited(after))
    if secrets_changed:
        # Masks of short secrets look alike and the audit log redacts the key: the entry still records it.
        old[_SECRET_FIELD], new[_SECRET_FIELD] = before.secret_headers, after.secret_headers
    await audit(session, ctx, "tool.update", "tool", tool_id, before=old, after=new)


def _reference_item(reference: ToolReference) -> dict[str, str]:
    return {"type": reference.type, "id": str(reference.id), "version": reference.version}


async def delete_tool(deps: ToolDeps, ctx: RequestContext, tool_id: uuid.UUID, *, force: bool) -> None:
    """Soft delete; ``CONFLICT`` with ``details.references`` while published resources list it."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        repo = ToolRepo(session, ctx.org_id)
        row = await repo.get(tool_id, lock=True)
        references = await repo.references(row.id)
        if references and not force:
            items = [_reference_item(reference) for reference in references]
            raise AppError("CONFLICT", IN_USE_MESSAGE, details={"references": items})
        await repo.soft_delete(row)
        after = {"name": row.name, "force": force, "references": len(references)}
        await audit(session, ctx, "tool.delete", "tool", row.id, after=after)
