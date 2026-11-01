"""Send Call input loading (specs/05-calls-api.md §4.1 steps 4-8, Decision P05-T03c).

`load_call_inputs` runs inside the caller's `tenant_session` and checks, in spec order, the runner rules and
call permissions, the existence of every referenced resource, plan features, the BYO `encrypted_key`, and
the destination / caller ID. The first failure is raised as `AppError`. `resolve_inputs` then produces the
`SendCallConfig` with the loader's warnings first.

The org settings are read by the caller (`OrgSettingsReader`) before the tenant session opens: the reader
opens a session of its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_api.services.call_refs import (
    check_plan_features,
    check_references,
    check_tools,
    load_byo_account,
    load_voice,
)
from va_api.services.call_runners import load_runner
from va_api.services.caller_id import select_caller_id
from va_common.errors import AppError
from va_common.settings import Environment
from va_domain.call_resolution import (
    CallResolution,
    MergedParameters,
    PlatformModels,
    ResolutionWarning,
    ResolvedRefs,
    RunnerInputs,
    merge_layers,
    resolve_call_config,
)
from va_domain.calls import CallRequestError, SendCallRequest
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


@dataclass(frozen=True, slots=True)
class ByoAccount:
    """The BYO Telnyx account dialing the call (never its key)."""

    account_id: UUID
    call_control_app_id: str | None


@dataclass(frozen=True, slots=True)
class CallInputs:
    """Everything `resolve_call_config` needs, plus what later dispatch steps read.

    `warnings` are the loader's own (pathway/persona version fallbacks); `country` is the ISO region of the
    destination for the international policy (`None` for non-geographic numbers).
    """

    org: OrgSettings
    runner: RunnerInputs
    merged: MergedParameters
    refs: ResolvedRefs
    telnyx_account: ByoAccount | None
    country: str | None
    warnings: tuple[ResolutionWarning, ...] = ()


def app_error(error: CallRequestError) -> AppError:
    """The API error of a va_domain rule failure (same code, message, and field)."""
    return AppError(error.code, error.message, field=error.field)


@contextmanager
def _domain_errors() -> Iterator[None]:
    try:
        yield
    except CallRequestError as exc:
        raise app_error(exc) from exc


def check_call_permissions(request: SendCallRequest, ctx: RequestContext, *, va_env: Environment) -> None:
    """§4.1 step 4: request rules (`RETRY_NOT_PROVIDED`, `RUNNER_CONFLICT`), then `AGENT_REQUIRES_ORG_KEY`,
    the `test_call` / `test_mode` permissions, and test keys outside local/test (specs/04 §2.2)."""
    with _domain_errors():
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
    org_id = ctx.org_id
    with _domain_errors():
        loaded = await load_runner(session, org_id, request)
        merged = merge_layers(org, request, loaded.runner)
    voice = await load_voice(session, org_id, merged, org)
    await check_tools(session, org_id, merged.tools)
    await check_references(session, org_id, merged)
    await check_plan_features(session, ctx.plan_id, merged)
    account = await load_byo_account(session, org_id, encrypted_key)
    account_id = None if account is None else account.id
    selected = await select_caller_id(
        session, org_id, phone_number=request.phone_number, merged=merged, org=org, account_id=account_id
    )
    refs = ResolvedRefs(
        phone_number=selected.destination.e164,
        from_number=selected.caller.e164,
        phone_number_id=selected.caller.phone_number_id,
        telnyx_account_id=account_id,
        voice=voice,
        models=models,
    )
    return CallInputs(
        org=org,
        runner=loaded.runner,
        merged=merged,
        refs=refs,
        telnyx_account=None if account is None else ByoAccount(account.id, account.call_control_app_id),
        country=selected.destination.region,
        warnings=loaded.warnings,
    )


def resolve_inputs(inputs: CallInputs, request: SendCallRequest, now: datetime) -> CallResolution:
    """The resolved `SendCallConfig`; warnings are the loader's, then the resolution's (`INVALID_INPUT` and
    other rule failures come back as `AppError`)."""
    with _domain_errors():
        resolution = resolve_call_config(inputs.org, request, inputs.runner, inputs.refs, now)
    return CallResolution(config=resolution.config, warnings=(*inputs.warnings, *resolution.warnings))
