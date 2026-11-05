"""``POST /v1/tools/{tool_id}/test``: one real, signed run of a stored tool (specs/13 §2, §5).

Rules: specs/13 §5 Decision P08-T02a. The tool and its decrypted secret headers are read in the tenant
session and the org's active webhook signing secrets in a global session; both close before the request is
sent. The run goes through the same :class:`va_tools.runner.ToolRunner` as calls (argument validation,
rendering, SSRF-guarded sending with retries, ``response_map``), with ``input`` and ``variables`` from the
request and the built-ins of a conversation without a call (empty numbers, UTC). Nothing is stored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
from pydantic import JsonValue

from va_api.auth.context import RequestContext
from va_api.routers.tools_schemas import ToolTestRequest, ToolTestRequestView, ToolTestResult
from va_api.services.tools import ToolDeps
from va_common.time import Clock
from va_db.repositories import NotFoundError, OrgRepo, ToolRepo
from va_db.webhook_secrets import active_signing_secrets
from va_engine.builtins import builtin_values
from va_engine.templating import TemplateScope
from va_tools.http import HttpExecutor
from va_tools.models import ToolSpec
from va_tools.runner import ToolInvocation, ToolRunner
from va_tools.secrets import decrypt_secret_headers
from va_tools.signing import tool_signer

__all__ = ["ToolTestDeps", "run_tool_test", "tool_test_result"]

_TIMEZONE = "UTC"


@dataclass(frozen=True, slots=True)
class ToolTestDeps:
    """The tools service dependencies plus the guarded tools client and the clock."""

    tools: ToolDeps
    http: httpx.AsyncClient
    clock: Clock


def _scope(deps: ToolTestDeps, body: ToolTestRequest) -> TemplateScope:
    builtins = builtin_values(
        now=deps.clock.now(),
        timezone=_TIMEZONE,
        from_number="",
        to_number="",
        call_id="",
        voice_id=None,
        language="en",
        inbound=False,
    )
    return TemplateScope(variables=body.variables, builtins=builtins, timezone=_TIMEZONE)


async def run_tool_test(
    deps: ToolTestDeps, ctx: RequestContext, tool_id: uuid.UUID, body: ToolTestRequest
) -> ToolInvocation:
    """Run the org's tool ``tool_id`` once with ``body`` (``TOOL_NOT_FOUND`` when it is not the org's)."""
    database, envelope = deps.tools.database, deps.tools.envelope
    async with database.tenant_session(ctx.org_id) as session:
        row = await ToolRepo(session, ctx.org_id).get(tool_id)
        secret_headers = await decrypt_secret_headers(envelope, ctx.org_id, row.id, row.secret_headers_enc)
        spec = ToolSpec.from_row(row, secret_headers)
    async with database.global_session() as session:
        org = await OrgRepo(session).get(ctx.org_id)
        if org is None:
            raise NotFoundError("Org")
        signing = await active_signing_secrets(envelope, org, now=deps.clock.now())
    executor = HttpExecutor(
        deps.http,
        clock=deps.clock,
        allow_http=deps.tools.allow_http,
        signer=tool_signer(signing.secrets, deps.clock),
    )
    arguments: dict[str, JsonValue] = dict(body.input)
    return await ToolRunner(executor, clock=deps.clock).run(spec, arguments, _scope(deps, body))


def tool_test_result(invocation: ToolInvocation) -> ToolTestResult:
    """The API body of a test run."""
    request = (
        ToolTestRequestView.model_validate(invocation.request) if invocation.request is not None else None
    )
    return ToolTestResult(
        request=request,
        status_code=invocation.status_code,
        response=invocation.response,
        extracted=invocation.extracted,
        duration_ms=invocation.duration_ms,
        error=None if invocation.outcome == "ok" else invocation.outcome,
        details=list(invocation.details),
        truncated=invocation.truncated,
    )
