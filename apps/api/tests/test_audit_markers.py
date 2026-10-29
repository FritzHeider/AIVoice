"""Every mutating route declares its audit actions or why it writes none (specs/04 §13 decision, P02-T11b).

The check walks every router of the app, so routes added by later phases are covered without editing this
module: a route whose method is not GET/HEAD/OPTIONS carries exactly one of ``x-audit`` (the §13 actions it
may write) and ``x-audit-exempt`` (a reason). A GET that changes state may carry ``x-audit`` too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from va_api.audit import AUDIT_EXEMPT_KEY, AUDIT_KEY, audit_exempt, audited
from va_api.main import create_app
from va_db.audit import AUDIT_ACTIONS

READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT: Final = "exempt"

# The routes of phase 02 and what they log; later phases add routes without editing this table.
PHASE_02: Final[dict[str, list[str] | str]] = {
    "auth_signup": EXEMPT,
    "auth_verify_email": EXEMPT,
    "auth_resend_verification": EXEMPT,
    "auth_login": ["auth.login", "auth.login_failed"],
    "auth_logout": ["auth.logout"],
    "auth_logout_all": ["auth.logout"],
    "auth_password_forgot": EXEMPT,
    "auth_password_reset": ["auth.password_reset"],
    "auth_password_change": ["auth.password_changed", "auth.login_failed"],
    "auth_mfa_setup": EXEMPT,
    "auth_mfa_enable": ["auth.mfa_enabled"],
    "auth_mfa_verify": EXEMPT,
    "auth_mfa_disable": ["auth.mfa_disabled"],
    "auth_sso_callback": ["auth.sso_login"],
    "auth_invitation_accept": ["invitation.accept"],
    "auth_switch_org": EXEMPT,
    "org_update": ["org.update"],
    "org_members_update": ["member.role_change"],
    "org_members_remove": ["member.remove"],
    "org_transfer_ownership": ["org.ownership_transfer"],
    "org_invitations_create": ["member.invite"],
    "org_invitations_revoke": ["invitation.revoke"],
    "org_sso_put": ["org.sso_update"],
    "org_sso_delete": ["org.sso_update"],
    "orgs_create": ["subaccount.create", "billing.credit_transfer"],
    "orgs_suspend": ["subaccount.suspend"],
    "orgs_reactivate": ["subaccount.reactivate"],
    "api_keys_create": ["api_key.create"],
    "api_keys_update": ["api_key.update"],
    "api_keys_revoke": ["api_key.revoke"],
    "api_keys_roll": ["api_key.roll"],
}


def mutating(route: APIRoute) -> bool:
    return bool((route.methods or set()) - READ_METHODS)


def marker_problems(routes: Iterable[APIRoute]) -> list[str]:
    """One message per route whose audit markers break the rule; empty when every route complies."""
    problems: list[str] = []
    for route in routes:
        extra = route.openapi_extra or {}
        actions = extra.get(AUDIT_KEY)
        reason = extra.get(AUDIT_EXEMPT_KEY)
        name = f"{sorted(route.methods or ())} {route.path}"
        if actions is None and reason is None:
            if mutating(route):
                problems.append(f"{name}: no {AUDIT_KEY} or {AUDIT_EXEMPT_KEY}")
            continue
        if actions is not None and reason is not None:
            problems.append(f"{name}: both {AUDIT_KEY} and {AUDIT_EXEMPT_KEY}")
        if actions is not None and (
            not isinstance(actions, list)
            or not actions
            or len(set(actions)) != len(actions)
            or not set(actions) <= AUDIT_ACTIONS
        ):
            problems.append(f"{name}: {AUDIT_KEY} must list distinct §13 actions, got {actions!r}")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            problems.append(f"{name}: {AUDIT_EXEMPT_KEY} needs a reason")
    return problems


def app_routes(app: FastAPI) -> list[APIRoute]:
    return [route for router in app.state.routers for route in router.routes if isinstance(route, APIRoute)]


@pytest.fixture(scope="module")
def routes() -> list[APIRoute]:
    return app_routes(create_app())


def test_every_route_declares_its_audit(routes: list[APIRoute]) -> None:
    assert marker_problems(routes) == []


def test_phase_02_routes_log_the_planned_actions(routes: list[APIRoute]) -> None:
    declared: dict[str, list[str] | str] = {}
    for route in routes:
        extra = route.openapi_extra or {}
        if AUDIT_KEY in extra:
            declared[route.operation_id or route.name] = extra[AUDIT_KEY]
        elif AUDIT_EXEMPT_KEY in extra:
            declared[route.operation_id or route.name] = EXEMPT
    assert {name: declared.get(name) for name in PHASE_02} == PHASE_02


def test_exempt_reasons_are_written_out(routes: list[APIRoute]) -> None:
    reasons = {
        route.operation_id: (route.openapi_extra or {}).get(AUDIT_EXEMPT_KEY)
        for route in routes
        if route.operation_id in {name for name, value in PHASE_02.items() if value == EXEMPT}
    }
    assert reasons == {
        "auth_signup": "creates the org and its owner; the org's log starts empty",
        "auth_verify_email": "email verification is not a §13 action",
        "auth_resend_verification": "sends an email only",
        "auth_password_forgot": "sends an email only; no account change",
        "auth_mfa_setup": "pending secret; MFA changes are logged by enable",
        "auth_mfa_verify": "second factor of a logged login",
        "auth_switch_org": "session preference only",
    }


def test_the_session_flow_marker_is_kept(routes: list[APIRoute]) -> None:
    internal = [route for route in routes if route.path.startswith("/internal/auth")]
    assert internal
    assert all((route.openapi_extra or {}).get("x-public") == "session-flow" for route in internal)


async def _endpoint() -> dict[str, str]:
    return {}


def _route(method: str, path: str, extra: Mapping[str, object] | None = None) -> APIRoute:
    router = APIRouter()
    router.add_api_route(path, _endpoint, methods=[method], openapi_extra=dict(extra) if extra else None)
    route = router.routes[0]
    assert isinstance(route, APIRoute)
    return route


def test_the_check_reports_an_unmarked_mutating_route() -> None:
    routes = [
        _route("POST", "/things"),
        _route("GET", "/things"),
        _route("DELETE", "/things/{id}", audited("tool.delete")),
        _route("PATCH", "/things/{id}", audit_exempt("cache only")),
    ]
    assert marker_problems(routes) == ["['POST'] /things: no x-audit or x-audit-exempt"]


@pytest.mark.parametrize(
    ("extra", "problem"),
    [
        ({**audited("tool.create"), **audit_exempt("both")}, "both x-audit and x-audit-exempt"),
        ({AUDIT_KEY: []}, "x-audit must list distinct §13 actions, got []"),
        ({AUDIT_KEY: ["tool.create", "tool.create"]}, "x-audit must list distinct"),
        ({AUDIT_KEY: ["tool.explode"]}, "x-audit must list distinct"),
        ({AUDIT_KEY: "tool.create"}, "x-audit must list distinct"),
        ({AUDIT_EXEMPT_KEY: "  "}, "x-audit-exempt needs a reason"),
    ],
)
def test_the_check_reports_malformed_markers(extra: dict[str, object], problem: str) -> None:
    for method in ("PUT", "GET"):
        problems = marker_problems([_route(method, "/things", extra)])
        assert any(problem in item for item in problems), problems


def test_audited_builds_the_marker() -> None:
    assert audited("api_key.create") == {AUDIT_KEY: ["api_key.create"]}
    assert audited("tool.create", "tool.create", "tool.update") == {AUDIT_KEY: ["tool.create", "tool.update"]}


@pytest.mark.parametrize("actions", [(), ("tool.explode",), ("tool.create", "Tool.Create")])
def test_audited_rejects_unknown_or_missing_actions(actions: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="action"):
        audited(*actions)


def test_audit_exempt_needs_a_reason() -> None:
    assert audit_exempt("sends an email only") == {AUDIT_EXEMPT_KEY: "sends an email only"}
    for reason in ("", "   "):
        with pytest.raises(ValueError, match="reason"):
            audit_exempt(reason)
