"""Send Call input loading for the API (specs/05-calls-api.md §4.1 steps 4-8, Decisions P05-T03c, P05-T12a).

`check_call_permissions` is step 4 (request rules and principal permissions); the shared loader
`va_db.call_inputs.loader.load_call_inputs` runs steps 5-8 inside the caller's `tenant_session`.
`resolve_inputs` then produces the `SendCallConfig` with the loader's warnings first.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_common.errors import AppError
from va_common.settings import Environment
from va_db.call_inputs import loader
from va_db.call_inputs.loader import ByoAccount, CallInputs, app_error, domain_errors, resolve_inputs
from va_domain.call_resolution import PlatformModels
from va_domain.calls import SendCallRequest
from va_domain.org import OrgSettings

__all__ = [
    "ByoAccount",
    "CallInputs",
    "app_error",
    "check_call_permissions",
    "load_call_inputs",
    "resolve_inputs",
]

_AGENT_SCOPES: Final = ("agents:read", "agents:write")
_SIMULATOR_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})
_AGENT_KEY_MESSAGE: Final = "agent_id requires an org-scoped API key (agents:read or agents:write)."
_TEST_CALL_MESSAGE: Final = "test_call requires the pathways:write scope or a developer role."
_TEST_MODE_MESSAGE: Final = "test_mode requires a test environment API key."
_TEST_KEY_MESSAGE: Final = "Test keys cannot place live calls, send SMS, or purchase numbers."


def check_call_permissions(request: SendCallRequest, ctx: RequestContext, *, va_env: Environment) -> None:
    """§4.1 step 4: request rules (`RETRY_NOT_PROVIDED`, `RUNNER_CONFLICT`), then `AGENT_REQUIRES_ORG_KEY`,
    the `test_call` / `test_mode` permissions, and test keys outside local/test (specs/04 §2.2)."""
    with domain_errors():
        request.check_rules()
    if request.agent_id is not None and not any(ctx.has_scope(scope) for scope in _AGENT_SCOPES):
        raise AppError("AGENT_REQUIRES_ORG_KEY", _AGENT_KEY_MESSAGE, field="agent_id")
    if request.test_call and not ctx.has_scope("pathways:write"):
        raise AppError("FORBIDDEN", _TEST_CALL_MESSAGE, field="test_call")
    if request.test_mode and ctx.environment != "test":
        raise AppError("FORBIDDEN", _TEST_MODE_MESSAGE, field="test_mode")
    if ctx.environment == "test" and va_env not in _SIMULATOR_ENVIRONMENTS:
        raise AppError("FORBIDDEN", _TEST_KEY_MESSAGE)


async def load_call_inputs(  # noqa: PLR0913 - keyword-only loader inputs
    session: AsyncSession,
    ctx: RequestContext,
    request: SendCallRequest,
    *,
    org: OrgSettings,
    encrypted_key: str | None,
    models: PlatformModels,
    va_env: Environment,
) -> CallInputs:
    """Run §4.1 steps 4-8 for `request` in `session` (a `tenant_session(ctx.org_id)`).

    `encrypted_key` is the request header (specs/05 §2). Raises the first failing step's `AppError`.
    """
    check_call_permissions(request, ctx, va_env=va_env)
    return await loader.load_call_inputs(
        session,
        ctx.org_id,
        request,
        plan_id=ctx.plan_id,
        org=org,
        encrypted_key=encrypted_key,
        models=models,
    )
