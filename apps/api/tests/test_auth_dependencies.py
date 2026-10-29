"""``get_principal`` and ``require_scope`` over HTTP (specs/04 §2.2, §2.4; specs/31 §1)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from auth_helpers import AUTH_ENVELOPE_PREFIX, AUTH_LEGACY_PREFIX, AuthContainer, auth_app
from fastapi.routing import APIRoute
from service_helpers import json_log_stream, log_events, make_client

from va_api.auth import SCOPES, require_scope
from va_api.auth.dependencies import REQUIRED_SCOPE_ATTR
from va_api.auth.scopes import ALL_SCOPES

SPEC_04 = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"
PREFIXES = [AUTH_LEGACY_PREFIX, AUTH_ENVELOPE_PREFIX]


async def _request(
    container: AuthContainer, method: str, path: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    async with make_client(auth_app(container)) as client:
        return await client.request(method, path, headers=headers or {})


def _error(response: httpx.Response) -> dict[str, object]:
    body = response.json()
    assert isinstance(body["errors"], list)
    item: dict[str, object] = body["errors"][0]
    return item


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("scopes", [(ALL_SCOPES,), ("calls:read",), ("calls:read", "calls:write")])
async def test_require_scope_allows_wildcard_and_exact(prefix: str, scopes: tuple[str, ...]) -> None:
    container = AuthContainer()
    plaintext, record = container.store.issue(scopes=scopes)
    response = await _request(container, "GET", f"{prefix}/read", {"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200
    assert response.json() == {"org_id": str(record.org_id), "environment": "live"}


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_require_scope_denies_with_forbidden(prefix: str) -> None:
    container = AuthContainer()
    plaintext, _ = container.store.issue(scopes=("calls:read", "batches:write"))
    response = await _request(container, "POST", f"{prefix}/write", {"Authorization": plaintext})
    assert response.status_code == 403
    assert _error(response) == {"error": "FORBIDDEN", "message": "Missing required scope 'calls:write'."}
    if prefix == AUTH_LEGACY_PREFIX:
        assert response.json()["status"] == "error"
        assert response.json()["message"] == "Missing required scope 'calls:write'."
    else:
        assert response.json()["data"] is None
    assert response.headers["x-request-id"] == response.json()["request_id"]


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_missing_authorization_is_auth_failure(prefix: str) -> None:
    response = await _request(AuthContainer(), "GET", f"{prefix}/read")
    assert response.status_code == 401
    assert _error(response) == {"error": "AUTH_FAILURE", "message": "Invalid API key."}


async def test_x_api_key_header_is_ignored() -> None:
    container = AuthContainer()
    plaintext, _ = container.store.issue()
    response = await _request(container, "GET", f"{AUTH_LEGACY_PREFIX}/read", {"x-api-key": plaintext})
    assert response.status_code == 401
    assert _error(response)["error"] == "AUTH_FAILURE"
    assert container.store.lookups == []


async def test_suspended_org_write_is_org_suspended_over_http() -> None:
    container = AuthContainer()
    plaintext, _ = container.store.issue(org_status="suspended")
    headers = {"Authorization": plaintext}
    assert (await _request(container, "GET", f"{AUTH_ENVELOPE_PREFIX}/read", headers)).status_code == 200
    response = await _request(container, "POST", f"{AUTH_ENVELOPE_PREFIX}/write", headers)
    assert response.status_code == 403
    assert _error(response)["error"] == "ORG_SUSPENDED"


def test_require_scope_rejects_unknown_scope_name() -> None:
    with pytest.raises(ValueError, match="unknown scope: 'calls:delete'"):
        require_scope("calls:delete")


def test_require_scope_dependency_is_discoverable() -> None:
    app = auth_app(AuthContainer())
    found: dict[str, list[str]] = {}
    routes = [route for router in app.state.routers for route in router.routes]  # app.routes nests routers
    for route in routes:
        if isinstance(route, APIRoute) and route.path.startswith(AUTH_LEGACY_PREFIX):
            calls = [dep.call for dep in route.dependant.dependencies]
            found[route.path] = [
                getattr(call, REQUIRED_SCOPE_ATTR) for call in calls if hasattr(call, REQUIRED_SCOPE_ATTR)
            ]
    assert found == {
        f"{AUTH_LEGACY_PREFIX}/read": ["calls:read"],
        f"{AUTH_LEGACY_PREFIX}/write": ["calls:write"],
    }


def _spec_scopes() -> set[str]:
    """Every scope of the specs/04 §2.3 table, ``a`` / ``b`` rows expanded."""
    text = SPEC_04.read_text(encoding="utf-8")
    section = text[text.index("### 2.3 Scopes") : text.index("### 2.4")]
    scopes: set[str] = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            first_cell = line.split("|")[1]
            scopes.update(re.findall(r"`([^`]+)`", first_cell))
    return scopes


def test_scopes_match_spec_table() -> None:
    assert _spec_scopes() == SCOPES
    assert len(SCOPES) == 40


async def test_principal_stored_on_request_state_and_org_bound_in_logs() -> None:
    container = AuthContainer()
    plaintext, record = container.store.issue()
    stream = json_log_stream()
    response = await _request(container, "GET", f"{AUTH_LEGACY_PREFIX}/read", {"Authorization": plaintext})
    assert response.json()["org_id"] == str(record.org_id)
    (access,) = [event for event in log_events(stream) if event["event"] == "http.request"]
    assert access["org_id"] == str(record.org_id)
    assert plaintext not in stream.getvalue()


async def test_touch_scheduled_as_background_task() -> None:
    container = AuthContainer()
    plaintext, record = container.store.issue()
    headers = {"Authorization": f"Bearer {plaintext}"}
    async with make_client(auth_app(container)) as client:
        for _ in range(3):
            assert (await client.get(f"{AUTH_LEGACY_PREFIX}/read", headers=headers)).status_code == 200
    assert container.store.touches == [(record.id, container.clock.now())]


async def test_failed_auth_schedules_no_touch() -> None:
    container = AuthContainer()
    container.store.issue()
    await _request(container, "GET", f"{AUTH_LEGACY_PREFIX}/read", {"Authorization": "va_live_" + "x" * 32})
    assert container.store.touches == []
