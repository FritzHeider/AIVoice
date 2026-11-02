"""Helpers for the api Send Call input loader tests (`va_api.services.call_inputs`).

The row builders live in `call_input_seed` (va_db test support, shared with the inbound loader tests); this
`Seed` adds the api loader call with a `RequestContext`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from call_input_seed import MODELS, START_NODE, error_of, graph, random_e164, request_of
from call_input_seed import Seed as RowSeed

from va_api.auth.context import RequestContext
from va_api.services.call_inputs import CallInputs, load_call_inputs
from va_common.auth import KeyEnvironment
from va_common.errors import AppError
from va_common.settings import Environment
from va_domain.org import OrgSettings, load_settings

__all__ = [
    "MODELS",
    "START_NODE",
    "Seed",
    "error_of",
    "graph",
    "make_ctx",
    "random_e164",
    "request_of",
]


def make_ctx(
    org_id: uuid.UUID,
    *,
    scopes: Sequence[str] = ("*",),
    environment: KeyEnvironment = "live",
    plan_id: str = "enterprise",
) -> RequestContext:
    return RequestContext(
        request_id="req_test",
        org_id=org_id,
        principal_type="api_key",
        principal_id=uuid.uuid4(),
        role=None,
        scopes=frozenset(scopes),
        environment=environment,
        plan_id=plan_id,
        ip=None,
        user_agent=None,
        acting_parent_org_id=None,
    )


class Seed(RowSeed):
    """An org of its own on the module database, its row builders, and the api loader."""

    async def fails(self, body: Mapping[str, Any], **kwargs: Any) -> tuple[str, str | None, str]:
        """`(code, field, message)` of the `AppError` that `load(body, **kwargs)` raises."""
        try:
            await self.load(body, **kwargs)
        except AppError as exc:
            return error_of(exc)
        msg = "the loader accepted the request"
        raise AssertionError(msg)

    async def load(
        self,
        body: Mapping[str, Any],
        *,
        org: OrgSettings | Mapping[str, Any] | None = None,
        ctx: RequestContext | None = None,
        encrypted_key: str | None = None,
        va_env: Environment = Environment.TEST,
    ) -> CallInputs:
        """Run the loader for `body` (`phone_number` and `task` default) in this org's tenant session."""
        settings = (
            org if isinstance(org, OrgSettings) else load_settings(dict(org or {"default_voice": "Karen"}))
        )
        async with self.database.tenant_session(self.org_id) as session:
            return await load_call_inputs(
                session,
                ctx or make_ctx(self.org_id),
                request_of(body),
                org=settings,
                encrypted_key=encrypted_key,
                models=MODELS,
                va_env=va_env,
            )
