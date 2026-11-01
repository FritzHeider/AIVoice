"""A failing org-provider lookup inside ``complete`` (specs/10 §1 Decision P03-T13a rule 6): dialogue and
analysis answer from the platform fallback model, the other call types raise
``LlmUnavailableError("provider_unavailable")`` without a request."""

from __future__ import annotations

import secrets
import uuid
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
import respx
from crypto_fakes import InMemoryDataKeyStore
from llm_client_helpers import COMPLETIONS, ORG_COMPLETIONS, completion_body, gateway_rig, sent_json, user
from opentelemetry.trace import StatusCode
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.crypto import Envelope, LocalKmsProvider
from va_common.time import FrozenClock
from va_llm import CallType, LlmProvider, LlmUnavailableError, OrgProviderError, OrgProviderStore
from va_llm.metrics import DURATION_METRIC, REQUESTS_METRIC

OK = httpx.Response(200, json=completion_body())


class BrokenOrgProviders:
    """An org-provider source whose lookup always fails (the store's wrapped DB/KMS/row errors)."""

    def __init__(self) -> None:
        self.lookups = 0

    async def get(self, org_id: uuid.UUID) -> LlmProvider | None:
        self.lookups += 1
        raise OrgProviderError(provider_id=f"org:{org_id}", cause="OperationalError")


@pytest.mark.parametrize("call_type", ["dialogue", "analysis"])
async def test_dialogue_and_analysis_answer_from_the_platform_fallback(
    llm_api: respx.MockRouter, call_type: CallType
) -> None:
    source = BrokenOrgProviders()
    async with gateway_rig(org_providers=source) as rig:
        org_route = llm_api.post(ORG_COMPLETIONS).mock(return_value=OK)
        platform_route = llm_api.post(COMPLETIONS).mock(return_value=OK)
        stream = json_log_stream()
        completion = await rig.gateway.complete(call_type, org_id=rig.org_id, messages=[user()])
        assert (completion.provider_id, completion.model, completion.fell_back, completion.attempts) == (
            "platform",
            "fb-model",
            True,
            1,
        )
        assert org_route.call_count == 0
        assert [sent_json(call.request)["model"] for call in platform_route.calls] == ["fb-model"]
        assert source.lookups == 1
        [event] = [log for log in log_events(stream) if log["event"] == "llm.org_provider_unavailable"]
        assert (event["org_id"], event["error"], event["call_type"]) == (
            str(rig.org_id),
            "OperationalError",
            call_type,
        )
        [record] = rig.usage.records
        assert (record.provider_id, record.model) == ("platform", "fb-model")
        [span] = rig.spans()
        assert span.attributes is not None
        assert (span.attributes["llm.provider"], span.attributes["llm.fallback"]) == ("platform", True)
        assert span.status.status_code is StatusCode.UNSET


async def test_dialogue_fallback_failure_is_unavailable(llm_api: respx.MockRouter) -> None:
    async with gateway_rig(org_providers=BrokenOrgProviders()) as rig:
        route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(503))
        with pytest.raises(LlmUnavailableError) as raised:
            await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()])
        assert (raised.value.reason, raised.value.model) == ("exhausted", "fb-model")
        assert route.call_count == 1


@pytest.mark.parametrize("call_type", ["router", "loop_condition", "extraction", "voicemail", "guard_rails"])
async def test_other_call_types_raise_provider_unavailable_without_a_request(
    llm_api: respx.MockRouter, call_type: CallType
) -> None:
    async with gateway_rig(org_providers=BrokenOrgProviders()) as rig:
        route = llm_api.post(COMPLETIONS).mock(return_value=OK)
        with pytest.raises(LlmUnavailableError) as raised:
            await rig.gateway.complete(call_type, org_id=rig.org_id, messages=[user()])
        error = raised.value
        assert (error.reason, error.last, error.provider_id) == (
            "provider_unavailable",
            None,
            f"org:{rig.org_id}",
        )
        assert isinstance(error.__cause__, OrgProviderError)
        assert route.call_count == 0
        assert rig.metric(f"{DURATION_METRIC}_count", call_type=call_type) == 1
        attempts = [
            sample
            for family in rig.registry.collect()
            for sample in family.samples
            if sample.name == REQUESTS_METRIC
        ]
        assert attempts == []
        [span] = rig.spans()
        assert span.attributes is not None
        assert span.attributes["llm.provider"] == f"org:{rig.org_id}"
        assert (span.status.status_code, span.status.description) == (StatusCode.ERROR, "LlmUnavailableError")
        assert "llm.attempts" not in span.attributes


class UnreachableDatabase:
    """Tenant sessions that fail like a database that is down (driver error or refused connection)."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.opened: list[uuid.UUID] = []

    def tenant_session(self, org_id: uuid.UUID) -> AbstractAsyncContextManager[AsyncSession]:
        self.opened.append(org_id)
        raise self.error


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (OperationalError("SELECT 1", {}, OSError("connection refused")), "OperationalError"),
        (ConnectionRefusedError("refused"), "ConnectionRefusedError"),
    ],
)
async def test_an_unreachable_database_is_a_lookup_error_and_not_cached(error: Exception, cause: str) -> None:
    database = UnreachableDatabase(error)
    envelope = Envelope(LocalKmsProvider.from_hex(secrets.token_hex(32)), InMemoryDataKeyStore())
    store = OrgProviderStore(database, envelope, clock=FrozenClock())
    org_id = uuid.uuid4()
    for _ in range(2):
        with pytest.raises(OrgProviderError) as raised:
            await store.get(org_id)
        assert (raised.value.provider_id, raised.value.cause) == (f"org:{org_id}", cause)
        assert raised.value.__cause__ is error
    assert database.opened == [org_id, org_id]
