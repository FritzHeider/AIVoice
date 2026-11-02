"""Call input loading shared by Send Call and inbound calls (specs/05-calls-api.md §4.1 steps 5-8, Decisions
P05-T03c and P05-T12a; specs/16 §4).

Both loaders run inside the caller's `tenant_session(org_id)` and raise the first failing step's `AppError`:
runner, merged parameters, voice, tools, references, plan features. Send Call then loads the BYO account of
`encrypted_key` and selects the caller ID; an inbound call takes both from the called number instead.
`resolve_inputs` produces the `SendCallConfig` with the loader's warnings first.

The org settings are read by the caller (`OrgSettingsReader`) before the tenant session opens: the reader
opens a session of its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_db.call_inputs.caller_id import inbound_destination, select_caller_id
from va_db.call_inputs.refs import (
    check_plan_features,
    check_references,
    check_tools,
    load_byo_account,
    load_number_account,
    load_voice,
)
from va_db.call_inputs.runners import LoadedRunner, load_runner
from va_domain.call_resolution import (
    CallResolution,
    MergedParameters,
    PlatformModels,
    ResolutionWarning,
    ResolvedRefs,
    ResolvedVoice,
    RunnerInputs,
    merge_layers,
    resolve_call_config,
)
from va_domain.calls import CallRequestError, SendCallRequest
from va_domain.org import OrgSettings

__all__ = [
    "ByoAccount",
    "CallInputs",
    "InboundNumber",
    "app_error",
    "domain_errors",
    "load_call_inputs",
    "load_inbound_inputs",
    "resolve_inputs",
]


@dataclass(frozen=True, slots=True)
class ByoAccount:
    """The BYO Telnyx account carrying the call (never its key)."""

    account_id: uuid.UUID
    call_control_app_id: str | None


@dataclass(frozen=True, slots=True)
class InboundNumber:
    """The org number an inbound call reached (`telnyx_account_id` set for a BYO number)."""

    phone_number_id: uuid.UUID
    e164: str
    telnyx_account_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class CallInputs:
    """Everything `resolve_call_config` needs, plus what later dispatch steps read.

    `warnings` are the loader's own (pathway/persona version fallbacks); `country` is the ISO region of the
    destination for the international policy (`None` for non-geographic numbers and anonymous callers).
    """

    org: OrgSettings
    runner: RunnerInputs
    merged: MergedParameters
    refs: ResolvedRefs
    telnyx_account: ByoAccount | None
    country: str | None
    warnings: tuple[ResolutionWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class _Common:
    loaded: LoadedRunner
    merged: MergedParameters
    voice: ResolvedVoice


def app_error(error: CallRequestError) -> AppError:
    """The API error of a va_domain rule failure (same code, message, and field)."""
    return AppError(error.code, error.message, field=error.field)


@contextmanager
def domain_errors() -> Iterator[None]:
    """Re-raise a `CallRequestError` raised inside the block as its `AppError`."""
    try:
        yield
    except CallRequestError as exc:
        raise app_error(exc) from exc


async def _load_common(
    session: AsyncSession, org_id: uuid.UUID, request: SendCallRequest, *, plan_id: str, org: OrgSettings
) -> _Common:
    """§4.1 steps 5-7 without the BYO key: runner, merge, voice, tools, references, plan features."""
    with domain_errors():
        loaded = await load_runner(session, org_id, request)
        merged = merge_layers(org, request, loaded.runner)
    voice = await load_voice(session, org_id, merged, org)
    await check_tools(session, org_id, merged.tools)
    await check_references(session, org_id, merged)
    await check_plan_features(session, plan_id, merged)
    return _Common(loaded=loaded, merged=merged, voice=voice)


async def load_call_inputs(  # noqa: PLR0913 - keyword-only loader inputs
    session: AsyncSession,
    org_id: uuid.UUID,
    request: SendCallRequest,
    *,
    plan_id: str,
    org: OrgSettings,
    encrypted_key: str | None,
    models: PlatformModels,
) -> CallInputs:
    """Send Call §4.1 steps 5-8 for `request` in `session` (a `tenant_session(org_id)`).

    `encrypted_key` is the request header (specs/05 §2). The request rules and permissions (step 4) are the
    caller's. Raises the first failing step's `AppError`.
    """
    common = await _load_common(session, org_id, request, plan_id=plan_id, org=org)
    account = await load_byo_account(session, org_id, encrypted_key)
    account_id = None if account is None else account.id
    selected = await select_caller_id(
        session,
        org_id,
        phone_number=request.phone_number,
        merged=common.merged,
        org=org,
        account_id=account_id,
    )
    refs = ResolvedRefs(
        phone_number=selected.destination.e164,
        from_number=selected.caller.e164,
        phone_number_id=selected.caller.phone_number_id,
        telnyx_account_id=account_id,
        voice=common.voice,
        models=models,
    )
    return CallInputs(
        org=org,
        runner=common.loaded.runner,
        merged=common.merged,
        refs=refs,
        telnyx_account=None if account is None else ByoAccount(account.id, account.call_control_app_id),
        country=selected.destination.region,
        warnings=common.loaded.warnings,
    )


async def load_inbound_inputs(  # noqa: PLR0913 - keyword-only loader inputs
    session: AsyncSession,
    org_id: uuid.UUID,
    request: SendCallRequest,
    *,
    plan_id: str,
    org: OrgSettings,
    number: InboundNumber,
    models: PlatformModels,
) -> CallInputs:
    """The inputs of an inbound call to `number` (specs/16 §4, specs/06 §7 step 8) in `session`.

    `request` comes from `va_domain.call_resolution.inbound_request` (`phone_number` = the caller). The
    caller ID is the called number; a BYO number dials through its own Telnyx account.
    """
    common = await _load_common(session, org_id, request, plan_id=plan_id, org=org)
    telnyx_account = None
    if number.telnyx_account_id is not None:
        account = await load_number_account(session, org_id, number.telnyx_account_id)
        telnyx_account = ByoAccount(account.id, account.call_control_app_id)
    destination = inbound_destination(request.phone_number, number.e164)
    refs = ResolvedRefs(
        phone_number=destination.e164,
        from_number=number.e164,
        phone_number_id=number.phone_number_id,
        telnyx_account_id=number.telnyx_account_id,
        voice=common.voice,
        models=models,
    )
    return CallInputs(
        org=org,
        runner=common.loaded.runner,
        merged=common.merged,
        refs=refs,
        telnyx_account=telnyx_account,
        country=destination.region,
        warnings=common.loaded.warnings,
    )


def resolve_inputs(inputs: CallInputs, request: SendCallRequest, now: datetime) -> CallResolution:
    """The resolved `SendCallConfig`; warnings are the loader's, then the resolution's (`INVALID_INPUT` and
    other rule failures come back as `AppError`)."""
    with domain_errors():
        resolution = resolve_call_config(inputs.org, request, inputs.runner, inputs.refs, now)
    return CallResolution(config=resolution.config, warnings=(*inputs.warnings, *resolution.warnings))
