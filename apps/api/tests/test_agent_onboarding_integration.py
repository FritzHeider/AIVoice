"""The agent onboarding flow against a real database (specs/04-auth-tenancy-rbac.md §10).

An agent creates a request, the human signs in and approves or denies it through the emailed link, and the
agent polls with its ``X-Poll-Secret``. The agent's calls go through a second client without the human's
cookies.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, NamedTuple

import fakeredis
import httpx
import pytest
from identity_db import IdentityDb, unique_email
from peer import PeerSwitch
from session_helpers import (
    VERIFIED_AT,
    Member,
    SessionContainer,
    add_member,
    audit_rows,
    enrol_mfa,
    error_of,
    link_token,
    login,
    make_member,
    session_app,
    session_client,
    set_org,
    signed_in,
    write_headers,
)
from sqlalchemy import select, update

from va_api.auth.agent_onboarding import (
    AGENT_KEY_LIFETIME,
    APPROVAL_WINDOW,
    KEY_TTL,
    LINK_INVALID_MESSAGE,
    REQUEST_NOT_FOUND_MESSAGE,
    AgentRequestStore,
    OnboardingLimiter,
    agent_key_key,
)
from va_api.auth.ip_allowlist import IP_NOT_ALLOWED_MESSAGE
from va_api.auth.session_auth import MFA_REQUIRED_MESSAGE
from va_api.routers.agent_onboarding import POLL_SECRET_REQUIRED_MESSAGE
from va_api.routers.auth_agent_onboarding import (
    NOT_ELIGIBLE_MESSAGE,
    OTHER_EMAIL_MESSAGE,
    SCOPES_NOT_REQUESTED_MESSAGE,
    UNVERIFIED_EMAIL_MESSAGE,
)
from va_common.auth import looks_like_token
from va_db.models import ApiKey, MemberRole, User

KEYS = "/v1/agent-onboarding/keys"
SCOPES = ["calls:read", "org:read", "pathways:write"]


def offline_redis() -> fakeredis.FakeAsyncRedis:
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeAsyncRedis(server=server)


def message_of(response: httpx.Response) -> str:
    message: str = response.json()["errors"][0]["message"]
    return message


def page(token: str, action: str = "") -> str:
    return f"/internal/auth/agent-onboarding/{token}{'/' + action if action else ''}"


@pytest.fixture
async def agent(container: SessionContainer) -> AsyncIterator[httpx.AsyncClient]:
    async with session_client(session_app(container)) as client:
        yield client


async def verify(identity: IdentityDb, user: User) -> None:
    async with identity.database.system_session() as session:
        await session.execute(update(User).where(User.id == user.id).values(email_verified_at=VERIFIED_AT))


async def human(
    client: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer, role: MemberRole = "owner"
) -> Member:
    """A signed-in member with a verified email."""
    member = await signed_in(client, identity, container, role)
    await verify(identity, member.user)
    return member


class Flow(NamedTuple):
    """A signed-in, verified owner, the agent's request for the owner's address, and its link token."""

    owner: Member
    data: dict[str, Any]
    token: str


@pytest.fixture
async def flow(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> Flow:
    owner = await human(client, identity, container)
    data, token = await requested(agent, container, owner.user.email)
    return Flow(owner, data, token)


async def request_key(
    agent: httpx.AsyncClient, email: str, scopes: list[str] | None = None, **extra: Any
) -> httpx.Response:
    body = {"email": email, "agent_name": "Claude Code", "requested_scopes": scopes or SCOPES, **extra}
    return await agent.post(KEYS, json=body)


async def requested(
    agent: httpx.AsyncClient, container: SessionContainer, email: str, scopes: list[str] | None = None
) -> tuple[dict[str, Any], str]:
    """A created request's ``data`` and the token of its emailed link."""
    response = await request_key(agent, email, scopes, purpose="Build the reminder pathway.")
    assert response.status_code == 201, response.text
    return response.json()["data"], link_token(container.outbox, email, "agent_key_request")


async def poll(agent: httpx.AsyncClient, data: dict[str, Any], secret: str | None = None) -> httpx.Response:
    headers = {"X-Poll-Secret": secret if secret is not None else data["poll_secret"]}
    return await agent.get(data["poll_url"], headers=headers)


async def approve(
    client: httpx.AsyncClient, token: str, org_id: uuid.UUID, scopes: list[str] | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"org_id": str(org_id)}
    if scopes is not None:
        body["scopes"] = scopes
    return await client.post(page(token, "approve"), json=body, headers=write_headers(client))


async def agent_keys(identity: IdentityDb, org_id: uuid.UUID) -> list[ApiKey]:
    async with identity.database.system_session() as session:
        rows = await session.scalars(
            select(ApiKey).where(ApiKey.org_id == org_id, ApiKey.name.like("agent:%"))
        )
        return list(rows)


async def test_create_returns_pending_request_and_emails_the_link(
    agent: httpx.AsyncClient, container: SessionContainer
) -> None:
    email = unique_email("jane")
    response = await request_key(agent, email, ["pathways:write", "calls:read", "calls:read"], purpose="Demo")
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "pending_verification"
    assert data["poll_url"] == f"{KEYS}/{data['request_id']}"
    assert uuid.UUID(data["request_id"]).version == 7
    assert looks_like_token(data["poll_secret"])
    expected = (container.clock.now() + APPROVAL_WINDOW).isoformat().replace("+00:00", "Z")
    assert data["expires_at"] == expected
    [message] = container.outbox.to(email)
    assert message.template == "agent_key_request"
    assert "calls:read, pathways:write" in message.text
    assert data["poll_secret"] not in message.text
    assert data["poll_secret"] not in message.html
    token = link_token(container.outbox, email, "agent_key_request")
    assert looks_like_token(token)
    pending = await poll(agent, data)
    assert pending.status_code == 200
    assert pending.headers["cache-control"] == "no-store"
    assert pending.json()["data"] == {
        "request_id": data["request_id"],
        "status": "pending_verification",
        "expires_at": expected,
        "scopes": ["calls:read", "pathways:write"],
        "org_id": None,
        "key": None,
    }


async def test_create_answers_the_same_for_known_and_unknown_emails(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    member = await make_member(identity, container.password_hasher)
    known = await request_key(agent, member.user.email)
    unknown = await request_key(agent, unique_email("nobody"))
    assert known.status_code == unknown.status_code == 201
    assert set(known.json()["data"]) == set(unknown.json()["data"])


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"requested_scopes": ["billing:read"]}, "requested_scopes"),
        ({"requested_scopes": ["*"]}, "requested_scopes"),
        ({"requested_scopes": ["api_keys:write"]}, "requested_scopes"),
        ({"requested_scopes": ["calls:fly"]}, "requested_scopes"),
        ({"requested_scopes": []}, "requested_scopes"),
        ({"agent_name": "  "}, "agent_name"),
        ({"agent_name": "x" * 101}, "agent_name"),
        ({"purpose": "x" * 501}, "purpose"),
        ({"email": "not-an-email"}, "email"),
    ],
)
async def test_create_rejects_invalid_input(
    agent: httpx.AsyncClient, body: dict[str, Any], field: str
) -> None:
    payload = {"email": unique_email("jane"), "agent_name": "Claude Code", "requested_scopes": SCOPES, **body}
    response = await agent.post(KEYS, json=payload)
    assert error_of(response) == (400, "INVALID_INPUT", field)


async def test_full_flow_approve_poll_once_then_delivered(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await human(client, identity, container)
    viewer_org = await make_member(identity, container.password_hasher)
    await add_member(identity, container.password_hasher, viewer_org.org.id, "viewer")
    data, token = await requested(agent, container, owner.user.email)

    details = await client.get(page(token))
    assert details.status_code == 200, details.text
    assert details.json()["data"] == {
        "request_id": data["request_id"],
        "agent_name": "Claude Code",
        "purpose": "Build the reminder pathway.",
        "requested_scopes": SCOPES,
        "expires_at": data["expires_at"],
        "orgs": [{"org_id": str(owner.org.id), "name": owner.org.name, "role": "owner"}],
    }

    approved = await approve(client, token, owner.org.id, ["org:read", "calls:read"])
    assert approved.status_code == 200, approved.text
    result = approved.json()["data"]
    expires_at = container.clock.now() + AGENT_KEY_LIFETIME
    assert result["status"] == "approved"
    assert result["org_id"] == str(owner.org.id)
    assert result["scopes"] == ["calls:read", "org:read"]
    assert result["expires_at"] == expires_at.isoformat().replace("+00:00", "Z")
    assert "key" not in result

    [row] = await agent_keys(identity, owner.org.id)
    assert str(row.id) == result["key_id"]
    assert (row.name, row.environment, row.scopes) == (
        "agent:Claude Code",
        "test",
        ["calls:read", "org:read"],
    )
    assert (row.created_by, row.expires_at) == (owner.user.id, expires_at)
    [audit] = await audit_rows(identity, owner.org.id, "api_key.create")
    assert (audit.actor_type, audit.actor_id, audit.resource_id) == ("user", owner.user.id, str(row.id))
    assert audit.changes["after"]["agent_name"] == "Claude Code"
    assert audit.changes["after"]["prefix"] == row.prefix

    first = await poll(agent, data)
    assert first.status_code == 200
    delivered = first.json()["data"]
    assert (delivered["status"], delivered["org_id"]) == ("approved", str(owner.org.id))
    assert delivered["scopes"] == ["calls:read", "org:read"]
    assert delivered["key"].startswith("va_test_")
    assert await container.redis.exists(agent_key_key(uuid.UUID(data["request_id"]))) == 0
    me = await agent.get("/v1/me", headers={"Authorization": f"Bearer {delivered['key']}"})
    assert me.status_code == 200, me.text

    second = await poll(agent, data)
    assert (second.json()["data"]["status"], second.json()["data"]["key"]) == ("delivered", None)
    reused = await approve(client, token, owner.org.id)
    assert error_of(reused) == (400, "TOKEN_EXPIRED", "token")
    assert message_of(reused) == LINK_INVALID_MESSAGE


async def test_concurrent_polls_deliver_the_key_once(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, flow: Flow
) -> None:
    owner, data, token = flow
    assert (await approve(client, token, owner.org.id)).status_code == 200
    responses = await asyncio.gather(*(poll(agent, data) for _ in range(4)))
    bodies = [response.json()["data"] for response in responses]
    assert [body["key"] is not None for body in bodies].count(True) == 1
    assert {body["status"] for body in bodies if body["key"] is None} == {"delivered"}


async def test_concurrent_approvals_have_one_winner(
    client: httpx.AsyncClient, identity: IdentityDb, flow: Flow
) -> None:
    owner, _, token = flow
    responses = await asyncio.gather(
        approve(client, token, owner.org.id), approve(client, token, owner.org.id)
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 400]
    loser = next(response for response in responses if response.status_code == 400)
    assert error_of(loser) == (400, "TOKEN_EXPIRED", "token")
    assert len(await agent_keys(identity, owner.org.id)) == 1


async def test_deny_then_poll_reads_denied(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, flow: Flow
) -> None:
    owner, data, token = flow
    denied = await client.post(page(token, "deny"), headers=write_headers(client))
    assert denied.status_code == 200, denied.text
    assert denied.json()["data"] == {"request_id": data["request_id"], "status": "denied"}
    assert (await poll(agent, data)).json()["data"]["status"] == "denied"
    assert error_of(await approve(client, token, owner.org.id)) == (400, "TOKEN_EXPIRED", "token")
    assert error_of(await client.get(page(token))) == (400, "TOKEN_EXPIRED", "token")
    assert await agent_keys(identity, owner.org.id) == []


async def test_request_expires_after_an_hour(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, container: SessionContainer, flow: Flow
) -> None:
    owner, data, token = flow
    container.clock.advance(APPROVAL_WINDOW.total_seconds() + 1)
    assert (await poll(agent, data)).json()["data"]["status"] == "expired"
    assert error_of(await client.get(page(token))) == (400, "TOKEN_EXPIRED", "token")
    assert error_of(await approve(client, token, owner.org.id)) == (400, "TOKEN_EXPIRED", "token")
    assert error_of(await client.post(page(token, "deny"), headers=write_headers(client))) == (
        400,
        "TOKEN_EXPIRED",
        "token",
    )


@pytest.mark.parametrize("how", ["clock", "redis"])
async def test_unfetched_key_expires(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, container: SessionContainer, flow: Flow, how: str
) -> None:
    owner, data, token = flow
    assert (await approve(client, token, owner.org.id)).status_code == 200
    if how == "clock":
        container.clock.advance(KEY_TTL.total_seconds())
    else:
        await container.redis.delete(agent_key_key(uuid.UUID(data["request_id"])))
    expired = (await poll(agent, data)).json()["data"]
    assert (expired["status"], expired["key"]) == ("expired", None)


async def test_poll_secret_rules(agent: httpx.AsyncClient, container: SessionContainer) -> None:
    data, _ = await requested(agent, container, unique_email("jane"))
    missing = await agent.get(data["poll_url"])
    assert error_of(missing) == (401, "AUTH_FAILURE", None)
    assert message_of(missing) == POLL_SECRET_REQUIRED_MESSAGE
    wrong = await poll(agent, data, secret="wrong-secret")  # noqa: S106 - a deliberately wrong value
    unknown = await agent.get(f"{KEYS}/{uuid.uuid4()}", headers={"X-Poll-Secret": data["poll_secret"]})
    malformed = await agent.get(f"{KEYS}/not-a-uuid", headers={"X-Poll-Secret": data["poll_secret"]})
    for response in (wrong, unknown, malformed):
        assert error_of(response) == (404, "NOT_FOUND", None)
        assert message_of(response) == REQUEST_NOT_FOUND_MESSAGE


async def test_approver_must_own_the_mailbox(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    await human(client, identity, container)
    _, token = await requested(agent, container, unique_email("someone-else"))
    response = await client.get(page(token))
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == OTHER_EMAIL_MESSAGE


async def test_approver_needs_a_verified_email(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await signed_in(client, identity, container)
    _, token = await requested(agent, container, owner.user.email.upper())
    response = await approve(client, token, owner.org.id)
    assert error_of(response) == (403, "EMAIL_NOT_VERIFIED", None)
    assert message_of(response) == UNVERIFIED_EMAIL_MESSAGE


async def test_approval_needs_a_session_and_csrf(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, flow: Flow
) -> None:
    owner, _, token = flow
    signed_out = await agent.get(page(token))
    assert error_of(signed_out)[:2] == (401, "AUTH_FAILURE")
    no_csrf = await client.post(page(token, "approve"), json={"org_id": str(owner.org.id)})
    assert error_of(no_csrf)[:2] == (403, "FORBIDDEN")
    malformed = await client.get(page("short"))
    assert error_of(malformed) == (400, "TOKEN_EXPIRED", "token")


@pytest.mark.parametrize("role", ["viewer", "billing", "analyst"])
async def test_approval_into_an_ineligible_org_is_forbidden(
    client: httpx.AsyncClient,
    agent: httpx.AsyncClient,
    identity: IdentityDb,
    container: SessionContainer,
    role: MemberRole,
) -> None:
    member = await human(client, identity, container, role)
    stranger = await make_member(identity, container.password_hasher)
    _, token = await requested(agent, container, member.user.email)
    assert (await client.get(page(token))).json()["data"]["orgs"] == []
    for org_id in (member.org.id, stranger.org.id):
        response = await approve(client, token, org_id)
        assert error_of(response) == (403, "FORBIDDEN", None)
        assert message_of(response) == NOT_ELIGIBLE_MESSAGE


async def test_developer_grants_only_developer_scopes(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    developer = await human(client, identity, container, "developer")
    data, token = await requested(agent, container, developer.user.email, ["calls:read", "org:write"])
    forbidden = await approve(client, token, developer.org.id)
    assert error_of(forbidden) == (403, "FORBIDDEN", None)
    assert "org:write" in message_of(forbidden)
    assert (await poll(agent, data)).json()["data"]["status"] == "pending_verification"
    allowed = await approve(client, token, developer.org.id, ["calls:read"])
    assert allowed.status_code == 200, allowed.text


async def test_scopes_must_be_a_subset_of_the_request(client: httpx.AsyncClient, flow: Flow) -> None:
    owner, _, token = flow
    response = await approve(client, token, owner.org.id, ["calls:read", "calls:write"])
    assert error_of(response) == (400, "INVALID_INPUT", "scopes")
    assert message_of(response) == SCOPES_NOT_REQUESTED_MESSAGE


async def test_org_mfa_requirement_applies(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await human(client, identity, container)
    await set_org(identity, owner.org.id, settings={"mfa_required": True})
    _, token = await requested(agent, container, owner.user.email)
    response = await approve(client, token, owner.org.id)
    assert error_of(response)[:2] == (401, "MFA_REQUIRED")
    assert message_of(response) == MFA_REQUIRED_MESSAGE


async def test_user_mfa_must_be_passed(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await human(client, identity, container)
    await enrol_mfa(client, container)
    async with session_client(session_app(container)) as fresh:
        await login(fresh, owner.user.email)
        _, token = await requested(agent, container, owner.user.email)
        response = await fresh.get(page(token))
    assert error_of(response)[:2] == (401, "MFA_REQUIRED")


async def test_org_ip_allowlist_applies(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, identity: IdentityDb, container: SessionContainer
) -> None:
    owner = await human(client, identity, container)
    await set_org(identity, owner.org.id, settings={"ip_allowlist": ["203.0.113.0/24"]})
    _, token = await requested(agent, container, owner.user.email)
    response = await approve(client, token, owner.org.id)
    assert error_of(response) == (403, "FORBIDDEN", None)
    assert message_of(response) == IP_NOT_ALLOWED_MESSAGE
    assert await agent_keys(identity, owner.org.id) == []


async def test_failed_mint_hands_the_claim_back(
    client: httpx.AsyncClient,
    agent: httpx.AsyncClient,
    container: SessionContainer,
    flow: Flow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, data, token = flow

    async def broken(*_args: object, **_kwargs: object) -> bytes:
        raise ConnectionError

    monkeypatch.setattr(container.envelope, "encrypt_str", broken)
    assert (await approve(client, token, owner.org.id)).status_code == 500
    monkeypatch.undo()
    assert (await poll(agent, data)).json()["data"]["status"] == "pending_verification"
    assert (await approve(client, token, owner.org.id)).status_code == 200


async def test_ip_limit(agent: httpx.AsyncClient) -> None:
    for _ in range(5):
        assert (await request_key(agent, unique_email("jane"))).status_code == 201
    limited = await request_key(agent, unique_email("jane"))
    assert error_of(limited) == (429, "RATE_LIMITED", None)
    assert 0 < int(limited.headers["retry-after"]) <= 3600


async def test_email_limit_across_addresses(container: SessionContainer) -> None:
    peer = PeerSwitch(session_app(container))
    email = unique_email("jane")
    async with session_client(peer) as agent:
        for index in range(3):
            peer.host = f"198.51.100.{index + 1}"
            assert (await request_key(agent, email)).status_code == 201
        peer.host = "198.51.100.9"
        limited = await request_key(agent, email.upper())
        assert error_of(limited) == (429, "RATE_LIMITED", None)
        assert (await request_key(agent, unique_email("other"))).status_code == 201


async def test_redis_down_fails_closed(
    agent: httpx.AsyncClient, container: SessionContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    offline = offline_redis()
    monkeypatch.setattr(container, "onboarding_limiter", OnboardingLimiter(offline))
    assert error_of(await request_key(agent, unique_email("jane"))) == (503, "SERVICE_UNAVAILABLE", None)
    monkeypatch.setattr(container, "onboarding_limiter", OnboardingLimiter(container.redis))
    monkeypatch.setattr(container, "agent_requests", AgentRequestStore(offline, container.clock))
    assert error_of(await request_key(agent, unique_email("jane"))) == (503, "SERVICE_UNAVAILABLE", None)
    headers = {"X-Poll-Secret": "secret"}
    assert error_of(await agent.get(f"{KEYS}/{uuid.uuid4()}", headers=headers)) == (
        503,
        "SERVICE_UNAVAILABLE",
        None,
    )


async def test_request_lifetime_in_redis(agent: httpx.AsyncClient, container: SessionContainer) -> None:
    data, _ = await requested(agent, container, unique_email("jane"))
    ttl = await container.redis.ttl(f"agentreq:{data['request_id']}")
    assert timedelta(hours=23) < timedelta(seconds=ttl) <= timedelta(hours=24)
