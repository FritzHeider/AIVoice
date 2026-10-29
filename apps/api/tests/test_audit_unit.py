"""``va_api.audit`` without a database: actors from request contexts and changed fields (specs/04 §13)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Final

import pytest

from va_api.audit import actor_of, changed
from va_api.auth.context import PrincipalType, RequestContext

PARENT: Final = uuid.UUID("0192f1d2-0000-7000-8000-000000000001")
CTX: Final = RequestContext(
    request_id="req_audit",
    org_id=uuid.UUID("0192f1d2-0000-7000-8000-000000000002"),
    principal_type="user",
    principal_id=uuid.UUID("0192f1d2-0000-7000-8000-000000000003"),
    role="owner",
    scopes=frozenset({"*"}),
    environment="live",
    plan_id="build",
    ip="203.0.113.24",
    user_agent="Mozilla/5.0",
    acting_parent_org_id=None,
)


@pytest.mark.parametrize(
    ("principal_type", "actor_type", "platform_admin"),
    [("user", "user", False), ("api_key", "api_key", False), ("platform_admin", "user", True)],
)
def test_actor_follows_the_principal(
    principal_type: PrincipalType, actor_type: str, platform_admin: bool
) -> None:
    actor = actor_of(replace(CTX, principal_type=principal_type, acting_parent_org_id=PARENT))
    assert (actor.org_id, actor.actor_type, actor.actor_id) == (CTX.org_id, actor_type, CTX.principal_id)
    assert (actor.ip, actor.user_agent) == ("203.0.113.24", "Mozilla/5.0")
    assert (actor.acting_parent_org_id, actor.platform_admin) == (PARENT, platform_admin)


def test_widgets_write_no_audit_rows() -> None:
    with pytest.raises(ValueError, match="widget principals do not write audit rows"):
        actor_of(replace(CTX, principal_type="widget"))


def test_changed_keeps_only_differing_entries() -> None:
    assert changed({"name": "A", "retention_days": 30}, {"name": "B", "retention_days": 30}) == (
        {"name": "A"},
        {"name": "B"},
    )
    assert changed({"name": "A"}, {"name": "A"}) == ({}, {})
    assert changed({}, {"name": "A"}) == ({"name": None}, {"name": "A"})
