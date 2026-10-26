"""CORS policy for the dashboard origin (specs/28-security.md §5)."""

from __future__ import annotations

from api_helpers import make_client, sample_app

DASHBOARD = "http://localhost:3000"
ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-CSRF-Token",
    "X-VA-Org-Id",
    "Last-Event-ID",
)


async def test_preflight_from_dashboard_allowed() -> None:
    async with make_client(sample_app()) as client:
        response = await client.options(
            "/v1/health",
            headers={
                "Origin": DASHBOARD,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": ", ".join(ALLOWED_HEADERS),
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DASHBOARD
    assert response.headers["access-control-allow-credentials"] == "true"
    methods = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
    allowed = {h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")}
    assert {h.lower() for h in ALLOWED_HEADERS} <= allowed


async def test_other_origin_not_allowed() -> None:
    async with make_client(sample_app()) as client:
        preflight = await client.options(
            "/v1/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
        )
        simple = await client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in simple.headers


async def test_preflight_rejects_unlisted_header() -> None:
    async with make_client(sample_app()) as client:
        response = await client.options(
            "/v1/health",
            headers={
                "Origin": DASHBOARD,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Not-Allowed",
            },
        )
    assert response.status_code == 400


async def test_simple_request_exposes_request_id() -> None:
    async with make_client(sample_app()) as client:
        response = await client.get("/v1/health", headers={"Origin": DASHBOARD})
    assert response.headers["access-control-allow-origin"] == DASHBOARD
    exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
    assert exposed == {"x-request-id", "retry-after"}


async def test_dashboard_origin_from_settings() -> None:
    origin = "https://app.voiceagents.ai"
    async with make_client(sample_app(dashboard_base_url=origin)) as client:
        response = await client.get("/v1/health", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin
