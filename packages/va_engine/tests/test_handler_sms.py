"""SMS node handler (specs/11 §4 "SMS" row, §3.2.4, Decision P07-T07)."""

from __future__ import annotations

from typing import Any

import pytest
from graph_helpers import node
from handler_helpers import FakeLines, FakeSms, handler_ctx, spoken, unavailable
from service_helpers import json_log_stream, log_events

from va_engine.handlers import SmsHandler, SmsReport
from va_engine.ports import SmsOutcome, SmsRequest

HANDLER = SmsHandler()


def sms_node(
    *, extra: dict[str, Any] | None = None, sender: str | None = None, **config: Any
) -> dict[str, Any]:
    body = {"message": "Your link: {{link}}", **config}
    if sender is not None:
        body["from"] = sender
    return node("text", node_type="SMS", sms=body, **(extra or {}))


async def test_static_message_goes_to_the_caller() -> None:
    sms = FakeSms()
    ctx = handler_ctx(
        sms_node(speech="I just texted you the link."), sms=sms, variables={"link": "https://l.test"}
    )

    result = await HANDLER.on_enter(ctx)

    assert sms.requests == [
        SmsRequest(to="+14155550100", from_=None, body="Your link: https://l.test", node_id="text")
    ]
    assert await spoken(result.actions) == [("say", "I just texted you the link.", "t3.1", None)]
    assert result.reports == (SmsReport(node_id="text", sent=True, message_id="msg-1"),)
    assert result.reports[0].event_data() == {
        "node_id": "text",
        "source": "sms_node",
        "sent": True,
        "message_id": "msg-1",
        "error": None,
    }
    assert result.problem is None
    assert result.route_fallback is False


async def test_to_and_from_are_rendered_and_normalized() -> None:
    sms = FakeSms()
    raw = sms_node(to="{{mobile}}", sender=" {{agent}} ")
    ctx = handler_ctx(
        raw, sms=sms, variables={"mobile": "+1 (415) 555-0199", "agent": "+14155550000", "link": "x"}
    )

    await HANDLER.on_enter(ctx)

    assert sms.requests[0].to == "+14155550199"
    assert sms.requests[0].from_ == "+14155550000"


async def test_blank_from_means_the_agent_number() -> None:
    sms = FakeSms()
    ctx = handler_ctx(sms_node(sender="{{nothing}}"), sms=sms, variables={"link": "x"})
    await HANDLER.on_enter(ctx)
    assert sms.requests[0].from_ is None


async def test_prompt_message_is_composed() -> None:
    sms, lines = FakeSms(), FakeLines(reply="  Composed text.  ")
    raw = sms_node(messageMode="prompt", messagePrompt="Write the link {{link}} politely.")
    ctx = handler_ctx(raw, sms=sms, lines=lines, variables={"link": "L"})

    await HANDLER.on_enter(ctx)

    assert lines.instructions == ["Write the link L politely."]
    assert sms.requests[0].body == "Composed text."


@pytest.mark.parametrize(
    ("reply", "prompt", "reason"),
    [
        (unavailable(), "Write it.", "llm_unavailable"),
        ("   ", "Write it.", "blank"),
        ("x", "{{gone}}", "blank_instruction"),
    ],
)
async def test_prompt_failure_falls_back_to_the_static_message(reply: Any, prompt: str, reason: str) -> None:
    sms = FakeSms()
    raw = sms_node(messageMode="prompt", messagePrompt=prompt)
    ctx = handler_ctx(raw, sms=sms, lines=FakeLines(reply=reply), variables={"link": "L"})

    stream = json_log_stream()
    await HANDLER.on_enter(ctx)

    assert sms.requests[0].body == "Your link: L"
    fallbacks = [event for event in log_events(stream) if event["event"] == "engine.sms_message_fallback"]
    assert fallbacks[0]["reason"] == reason


@pytest.mark.parametrize(("continue_on_failure", "fallback"), [(True, False), (False, True)])
async def test_blank_message_is_a_problem(continue_on_failure: bool, fallback: bool) -> None:
    sms = FakeSms()
    raw = sms_node(message="{{none}}", continueOnFailure=continue_on_failure)
    result = await HANDLER.on_enter(handler_ctx(raw, sms=sms))
    assert sms.requests == []
    assert result.problem == "sms_message_missing"
    assert result.route_fallback is fallback


async def test_absent_config_has_a_blank_message() -> None:
    result = await HANDLER.on_enter(handler_ctx(node("text", node_type="SMS"), sms=FakeSms()))
    assert result.problem == "sms_message_missing"


@pytest.mark.parametrize(("continue_on_failure", "fallback"), [(True, False), (False, True)])
async def test_send_failure_says_nothing(continue_on_failure: bool, fallback: bool) -> None:
    sms = FakeSms(outcome=SmsOutcome(sent=False, error="carrier_rejected"))
    raw = sms_node(speech="I texted you.", continueOnFailure=continue_on_failure)
    ctx = handler_ctx(raw, sms=sms, variables={"link": "x"})

    result = await HANDLER.on_enter(ctx)

    assert result.actions == ()
    assert result.problem == "sms_failed"
    assert result.route_fallback is fallback
    assert result.reports == (SmsReport(node_id="text", sent=False, error="carrier_rejected"),)


async def test_invalid_destination_is_a_failure_and_never_logged() -> None:
    sms = FakeSms()
    ctx = handler_ctx(sms_node(to="{{mobile}}"), sms=sms, variables={"mobile": "415-555-0199", "link": "x"})

    stream = json_log_stream()
    result = await HANDLER.on_enter(ctx)

    assert sms.requests == []
    assert result.reports == (SmsReport(node_id="text", sent=False, error="invalid_to"),)
    assert "555-0199" not in stream.getvalue()


async def test_sms_channel_sends_the_message_as_the_next_message() -> None:
    sms = FakeSms()
    ctx = handler_ctx(sms_node(speech="I texted you."), channel="sms", sms=sms, variables={"link": "x"})

    result = await HANDLER.on_enter(ctx)

    assert sms.requests == []
    assert await spoken(result.actions) == [("say", "Your link: x", "t3.1", None)]
    assert result.reports == ()


@pytest.mark.parametrize(("continue_on_failure", "fallback"), [(True, False), (False, True)])
async def test_missing_port_is_a_problem(continue_on_failure: bool, fallback: bool) -> None:
    raw = sms_node(continueOnFailure=continue_on_failure)
    result = await HANDLER.on_enter(handler_ctx(raw, variables={"link": "x"}))
    assert result.problem == "sms_unavailable"
    assert result.route_fallback is fallback


async def test_success_without_speech_says_nothing() -> None:
    raw = sms_node(speech="{{none}}", extra={"modelOptions": {"block_interruptions": True}})
    result = await HANDLER.on_enter(handler_ctx(raw, sms=FakeSms(), variables={"link": "x"}))
    assert result.actions == ()


async def test_respond_and_waits_for_user() -> None:
    silent = handler_ctx(sms_node())
    assert (await HANDLER.respond(silent)).actions == ()
    assert HANDLER.waits_for_user(silent.node) is False

    texted = handler_ctx(sms_node(extra={"text": "Anything else?"}))
    assert await spoken((await HANDLER.respond(texted)).actions) == [("say", "Anything else?", "t3.1", None)]
    assert HANDLER.waits_for_user(texted.node) is True
