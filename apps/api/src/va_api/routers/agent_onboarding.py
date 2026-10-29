"""The public agent onboarding API under ``/v1/agent-onboarding/keys`` (specs/04-auth-tenancy-rbac.md §10).

Both routes are public (``x-scope: public``, no API key): an agent has no key yet. Creating a request is
limited to 5 per client IP and 3 per email address per hour (rejected attempts count) and answers the same
whether or not the address has an account, so it reveals nothing about who uses VoiceAgents; the approval
email goes out after the response. Polling needs the ``poll_secret`` from the create response as
``X-Poll-Secret``; an unknown request and a wrong secret are the same ``NOT_FOUND``. The approved key is in
exactly one poll response, after which the request reads ``delivered``. The approval itself happens in the
dashboard (:mod:`va_api.routers.auth_agent_onboarding`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import BackgroundTasks, Header, Request, Response

from va_api.audit import audit_exempt
from va_api.auth.agent_onboarding import (
    REQUEST_NOT_FOUND_MESSAGE,
    AgentRequest,
    PollStatus,
    check_requested_scopes,
    decode_key_blob,
    key_aad,
    send_agent_request,
)
from va_api.auth.session_auth import (
    agent_requests_of,
    client_ip,
    clock_of,
    email_sender_of,
    envelope_of,
    onboarding_limiter_of,
    settings_of,
)
from va_api.envelopes import envelope_router
from va_api.responses import DataResponse
from va_api.routers.agent_onboarding_schemas import (
    AgentKeyPoll,
    AgentKeyRequestCreate,
    AgentKeyRequestCreated,
)
from va_common.auth import issue_token, sha256_hex, tokens_equal
from va_common.errors import AppError
from va_common.ids import new_id

__all__ = [
    "POLL_SECRET_HEADER",
    "POLL_SECRET_REQUIRED_MESSAGE",
    "agent_onboarding_router",
    "agent_requests_create",
    "agent_requests_poll",
    "poll_url",
]

POLL_SECRET_HEADER: Final = "X-Poll-Secret"  # noqa: S105 - a header name
POLL_SECRET_REQUIRED_MESSAGE: Final = "X-Poll-Secret header required."  # noqa: S105 - error text
_PREFIX: Final = "/v1/agent-onboarding/keys"
_PUBLIC: Final = {"x-scope": "public"}
_CREATE_EXTRA: Final = {**_PUBLIC, **audit_exempt("no org is chosen yet; the request lives only in Redis")}

agent_onboarding_router = envelope_router(prefix=_PREFIX, tags=["API Keys"])


def poll_url(request_id: object) -> str:
    return f"{_PREFIX}/{request_id}"


def _not_found() -> AppError:
    return AppError("NOT_FOUND", REQUEST_NOT_FOUND_MESSAGE)


@agent_onboarding_router.post(
    "", operation_id="agent_onboarding_keys_create", status_code=201, openapi_extra=_CREATE_EXTRA
)
async def agent_requests_create(
    body: AgentKeyRequestCreate, request: Request, background: BackgroundTasks
) -> DataResponse[AgentKeyRequestCreated]:
    """Ask the human at ``email`` to approve a scoped test key; poll ``poll_url`` for the outcome."""
    await onboarding_limiter_of(request).hit(client_ip(request), body.email)
    scopes = check_requested_scopes(body.requested_scopes)
    secret, token = issue_token(), issue_token()
    stored = await agent_requests_of(request).create(
        email=body.email,
        agent_name=body.agent_name,
        purpose=body.purpose,
        scopes=scopes,
        poll_secret_hash=secret.token_hash,
        token_hash=token.token_hash,
        request_id=new_id(),
    )
    background.add_task(
        send_agent_request,
        email_sender_of(request),
        to=body.email,
        request=stored,
        token=token.plaintext,
        dashboard_base_url=settings_of(request).dashboard_base_url,
    )
    created = AgentKeyRequestCreated(
        request_id=stored.request_id,
        status="pending_verification",
        poll_url=poll_url(stored.request_id),
        poll_secret=secret.plaintext,
        expires_at=stored.expires_at,
    )
    return DataResponse(data=created)


def _poll_view(stored: AgentRequest, status: PollStatus, key: str | None) -> AgentKeyPoll:
    return AgentKeyPoll(
        request_id=stored.request_id,
        status=status,
        expires_at=stored.expires_at,
        scopes=list(stored.scopes),
        org_id=stored.org_id,
        key=key,
    )


@agent_onboarding_router.get("/{request_id}", operation_id="agent_onboarding_keys_get", openapi_extra=_PUBLIC)
async def agent_requests_poll(
    request_id: str,
    request: Request,
    response: Response,
    poll_secret: Annotated[str | None, Header(alias=POLL_SECRET_HEADER)] = None,
) -> DataResponse[AgentKeyPoll]:
    """The request's status; the first poll after approval also carries the plaintext ``key``.

    Never cached (``Cache-Control: no-store``).
    """
    response.headers["Cache-Control"] = "no-store"
    if not poll_secret:
        raise AppError("AUTH_FAILURE", POLL_SECRET_REQUIRED_MESSAGE)
    try:
        parsed_id = uuid.UUID(request_id)
    except ValueError as exc:
        raise _not_found() from exc
    store = agent_requests_of(request)
    stored = await store.get(parsed_id)
    if stored is None or not tokens_equal(sha256_hex(poll_secret), stored.poll_secret_hash):
        raise _not_found()
    status = stored.effective_status(clock_of(request).now())
    if status != "approved" or stored.org_id is None:
        return DataResponse(data=_poll_view(stored, status, None))
    taken = await store.take_key(parsed_id)
    if taken is None:
        # A concurrent poll delivered the key, or its 15 minutes in Redis ran out.
        latest = await store.get(parsed_id) or stored
        latest_status = latest.effective_status(clock_of(request).now())
        return DataResponse(
            data=_poll_view(latest, "expired" if latest_status == "approved" else latest_status, None)
        )
    delivered, blob = taken
    key = await envelope_of(request).decrypt_str(stored.org_id, decode_key_blob(blob), key_aad(parsed_id))
    return DataResponse(data=_poll_view(delivered, "approved", key))
