"""Pathway test chat (specs/12 §7; Decision P07-T10): create, multi-turn routing, real webhook calls,
simulated side effects, branching, reset, and refusals. Decisions and replies go through the real gateway
against FakeLLM (it routes by edge label words)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
import pytest
from chat_helpers import CHAT, GREETING, HOOK_HOST, Chats, ChatStack, WallClock, chat_stack, restaurant
from graph_helpers import node, start
from identity_db import IdentityDb
from pathway_helpers import Api
from service_helpers import json_log_stream, log_events
from session_helpers import SessionContainer, error_of, make_member, store_api_key
from webhook_receiver import Receiver, Reply, served_receiver

from va_api.services import pathway_chat
from va_api.services.pathway_chat import CHAT_TTL_S, chat_key, lock_key

pytestmark = pytest.mark.integration


@pytest.fixture
def container(identity: IdentityDb) -> SessionContainer:
    """The chat runs decisions and the turn cap on the container clock: wall clock here."""
    return SessionContainer(identity=identity, clock=WallClock())


@pytest.fixture
async def stack(container: SessionContainer) -> AsyncIterator[ChatStack]:
    async with chat_stack(container) as running:
        yield running


@pytest.fixture
async def receiver(container: SessionContainer) -> AsyncIterator[tuple[Receiver, int]]:
    hooks = Receiver(Reply(200, headers={"content-type": "application/json"}, body=b'{"eta": 25}'))
    container.host_resolver.answers[HOOK_HOST] = ["127.0.0.1"]
    async with served_receiver(hooks) as port:
        yield hooks, port


async def org_keys(stack: ChatStack, identity: IdentityDb, container: SessionContainer) -> tuple[Chats, Api]:
    member = await make_member(identity, container.password_hasher)
    key, _ = await store_api_key(container, member.org.id)
    return Chats(stack.client, key), Api(stack.client, member.org.id, key)


@pytest.fixture
async def org(stack: ChatStack, identity: IdentityDb, container: SessionContainer) -> tuple[Chats, Api]:
    return await org_keys(stack, identity, container)


@pytest.fixture
async def pathway(org: tuple[Chats, Api], receiver: tuple[Receiver, int]) -> str:
    _, api = org
    return await api.created(**restaurant(receiver[1]))


async def stored(container: SessionContainer, key: str) -> bytes:
    raw = await container.redis.get(key)
    assert isinstance(raw, bytes)
    return raw


def route_items(reply: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
    return [(item["from"], item["to"], item["label"]) for item in reply["logs"] if item["type"] == "route"]


async def test_create_returns_the_opening_and_stores_the_session(
    org: tuple[Chats, Api], pathway: str, container: SessionContainer
) -> None:
    chats, _ = org
    body = await chats.created(pathway, request_data={"first_name": "Mike"})
    assert body["status"] == "success"
    assert body["assistant_messages"] == [GREETING]
    assert (body["current_node_id"], body["ended"], body["actions"], body["logs"]) == ("greet", False, [], [])
    chat_id = uuid.UUID(body["chat_id"])
    ttl = await container.redis.ttl(chat_key(chat_id))
    assert 0 < ttl <= CHAT_TTL_S
    document = orjson.loads(await stored(container, chat_key(chat_id)))
    assert document["request_data"] == {"first_name": "Mike"}
    assert (document["simulate_from"], document["simulate_to"]) == ("+14155550123", "+14155550100")
    assert list(document["snapshots"]) == ["0"]


async def test_multi_turn_chat_reproduces_the_expected_routes(
    org: tuple[Chats, Api], pathway: str, stack: ChatStack
) -> None:
    chats, _ = org
    chat_id = (await chats.created(pathway, request_data={"first_name": "Mike"}))["chat_id"]
    ordering = await chats.said(chat_id, "I want to order some food")
    assert route_items(ordering) == [("greet", "order", "Caller wants to order food")]
    assert ordering["assistant_messages"] == ["What would you like to order, Mike?"]
    assert (ordering["chat_id"], ordering["current_node_id"], ordering["ended"]) == (chat_id, "order", False)
    goodbye = await chats.said(chat_id, "Thanks, goodbye")
    assert route_items(goodbye) == [("order", "bye", "Caller says goodbye")]
    assert goodbye["assistant_messages"] == ["Goodbye!"]
    assert goodbye["actions"] == [{"type": "end_call", "reason": "agent_end_call", "ended_by": "agent"}]
    assert goodbye["ended"] is True
    again = await chats.send(chat_id, "Hello?")
    assert error_of(again) == (409, "CONFLICT", None)
    assert again.json()["errors"][0]["message"] == "This chat has ended."
    forced = [request.get("tool_choice") for request in await stack.requests()]
    assert {"type": "function", "function": {"name": "select_path"}} in forced


async def test_webhook_node_calls_the_endpoint_for_real_with_masked_logs(
    org: tuple[Chats, Api], pathway: str, receiver: tuple[Receiver, int]
) -> None:
    chats, _ = org
    hooks, port = receiver
    chat_id = (await chats.created(pathway))["chat_id"]
    reply = await chats.said(chat_id, "What is my delivery status?")
    [received] = hooks.requests
    assert received.path == "/status"
    assert received.headers["authorization"] == "Bearer chat-test-secret"
    assert received.headers["x-va-call-id"] == chat_id
    assert orjson.loads(received.body) == {"caller": "+14155550123"}
    assert reply["assistant_messages"] == ["Let me check that for you.", "It arrives in 25 minutes."]
    assert reply["variables"]["eta"] == 25
    kinds = [item["type"] for item in reply["logs"]]
    assert kinds == ["route", "tool", "webhook"]
    tool = reply["logs"][1]
    assert tool["request_headers"] == {"Authorization": "***", "X-Order": "none"}
    assert (tool["method"], tool["url"], tool["status"], tool["outcome"]) == (
        "POST",
        f"http://{HOOK_HOST}:{port}/status",
        200,
        "ok",
    )
    assert (tool["request_body"], tool["response_body"]) == ('{"caller":"+14155550123"}', '{"eta":25}')
    webhook = reply["logs"][2]
    assert (webhook["node_id"], webhook["status"], webhook["outcome"]) == ("hook", 200, "ok")
    assert "chat-test-secret" not in orjson.dumps(reply).decode()


async def test_transfer_is_simulated_and_ends_the_chat(org: tuple[Chats, Api], pathway: str) -> None:
    chats, _ = org
    chat_id = (await chats.created(pathway))["chat_id"]
    reply = await chats.said(chat_id, "Can I talk to the manager please")
    assert route_items(reply) == [("greet", "manager", "Caller asks for the manager")]
    assert reply["assistant_messages"] == ["Connecting you now."]
    assert reply["actions"] == [
        {"type": "transfer", "mode": "cold", "target": "+14155550100", "caller_id_mode": "agent_number"}
    ]
    assert reply["ended"] is True
    refused = await chats.send(chat_id, "Hello?")
    assert error_of(refused) == (409, "CONFLICT", None)


async def test_sms_node_is_simulated(org: tuple[Chats, Api], pathway: str) -> None:
    chats, _ = org
    chat_id = (await chats.created(pathway, simulate_from="(415) 555-0177"))["chat_id"]
    reply = await chats.said(chat_id, "Please send me a text message")
    assert reply["actions"] == [
        {
            "type": "sms",
            "node_id": "sms",
            "to": "+14155550177",
            "from": None,
            "body": "Your Bella Pasta order is confirmed.",
            "message_id": "sim-1",
        }
    ]
    assert reply["assistant_messages"] == ["I just texted you."]
    assert {"type": "sms", "node_id": "sms", "sent": True} in reply["logs"]
    assert reply["ended"] is False


async def test_branching_forks_a_new_chat_and_leaves_the_original(
    org: tuple[Chats, Api], pathway: str, container: SessionContainer
) -> None:
    chats, _ = org
    chat_id = (await chats.created(pathway))["chat_id"]
    await chats.said(chat_id, "I want to order some food")
    await chats.said(chat_id, "A large pizza")
    branch = await chats.said(chat_id, "Can I talk to the manager please", branch_from_turn=0)
    assert branch["chat_id"] != chat_id
    assert route_items(branch) == [("greet", "manager", "Caller asks for the manager")]
    branched = orjson.loads(await stored(container, chat_key(uuid.UUID(branch["chat_id"]))))
    assert sorted(branched["snapshots"], key=int) == ["0", "1"]
    users = [turn["content"] for turn in branched["state"]["history"] if turn["role"] == "user"]
    assert users == ["Can I talk to the manager please"]
    original = orjson.loads(await stored(container, chat_key(uuid.UUID(chat_id))))
    assert sorted(original["snapshots"], key=int) == ["0", "1", "2"]
    assert original["state"]["current_node_id"] == "order"
    following = await chats.said(chat_id, "Thanks, goodbye")
    assert route_items(following) == [("order", "bye", "Caller says goodbye")]
    ended_branch = await chats.said(chat_id, "I want to order some food", branch_from_turn=1)
    assert ended_branch["current_node_id"] == "order"
    missing = await chats.send(chat_id, "Hi", branch_from_turn=9)
    assert error_of(missing) == (400, "INVALID_INPUT", "branch_from_turn")


async def test_start_node_override_and_a_streamed_opening(org: tuple[Chats, Api], stack: ChatStack) -> None:
    chats, api = org
    nodes = [start("greet", text="Hello."), node("order", text="What would you like?")]
    pathway = await api.created(nodes=nodes)
    body = await chats.created(pathway, start_node_id="order")
    assert (body["assistant_messages"], body["current_node_id"]) == (["What would you like?"], "order")
    bad = await chats.create(pathway, start_node_id="nope")
    assert error_of(bad) == (400, "INVALID_INPUT", "start_node_id")
    prompted = await api.created(nodes=[start("greet", prompt="Greet the caller of Bella Pasta.")])
    streamed = await chats.created(prompted)
    [opening] = streamed["assistant_messages"]
    assert opening
    dialogue = [request for request in await stack.requests() if not request.get("tool_choice")]
    assert "Greet the caller of Bella Pasta." in dialogue[-1]["messages"][0]["content"]


async def test_create_refusals(
    org: tuple[Chats, Api], pathway: str, stack: ChatStack, identity: IdentityDb, container: SessionContainer
) -> None:
    chats, _ = org
    bad_phone = await chats.create(pathway, simulate_from="12")
    assert error_of(bad_phone) == (400, "INVALID_INPUT", "simulate_from")
    unpinned = await chats.create(pathway, version="production")
    assert error_of(unpinned) == (404, "PATHWAY_VERSION_NOT_FOUND", "version")
    huge = await chats.create(pathway, request_data={"blob": "x" * 70_000})
    assert error_of(huge) == (400, "INVALID_INPUT", "request_data")
    unknown = await chats.create(str(uuid.uuid4()))
    assert error_of(unknown)[:2] == (404, "NOT_FOUND")
    other, _ = await org_keys(stack, identity, container)
    foreign = await other.create(pathway)
    assert error_of(foreign)[:2] == (404, "NOT_FOUND")


async def test_chats_of_other_orgs_reset_and_busy_locks(
    org: tuple[Chats, Api], pathway: str, stack: ChatStack, identity: IdentityDb, container: SessionContainer
) -> None:
    chats, _ = org
    chat_id = (await chats.created(pathway))["chat_id"]
    other, _ = await org_keys(stack, identity, container)
    assert error_of(await other.send(chat_id, "Hi"))[:2] == (404, "NOT_FOUND")
    assert error_of(await other.delete(chat_id))[:2] == (404, "NOT_FOUND")
    await container.redis.set(lock_key(uuid.UUID(chat_id)), "held", ex=60)
    busy = await chats.send(chat_id, "Hi")
    assert error_of(busy) == (409, "CONFLICT", None)
    assert busy.json()["errors"][0]["message"] == "This chat is answering another message."
    await container.redis.delete(lock_key(uuid.UUID(chat_id)))
    blank = await chats.send(chat_id, "   ")
    assert error_of(blank) == (400, "INVALID_INPUT", "message")
    deleted = await chats.delete(chat_id)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "success", "message": "Chat deleted."}
    assert await container.redis.exists(chat_key(uuid.UUID(chat_id))) == 0
    assert error_of(await chats.send(chat_id, "Hi"))[:2] == (404, "NOT_FOUND")


async def test_concurrent_sends_to_one_chat_answer_one_and_refuse_the_other(
    org: tuple[Chats, Api], pathway: str, receiver: tuple[Receiver, int]
) -> None:
    chats, _ = org
    hooks, _ = receiver
    hooks.script("/status", Reply(200, hold=True, body=b"{}"))
    chat_id = (await chats.created(pathway))["chat_id"]
    first = asyncio.create_task(chats.send(chat_id, "What is my delivery status?"))
    await hooks.wait_for(lambda: len(hooks.requests) == 1)
    second = await chats.send(chat_id, "Hello?")
    assert error_of(second) == (409, "CONFLICT", None)
    hooks.release()
    answered = await first
    assert answered.status_code == 200, answered.text


async def test_chat_routes_are_not_taken_by_pathway_routes(stack: ChatStack, org: tuple[Chats, Api]) -> None:
    chats, _ = org
    response = await stack.client.post(f"{CHAT}/create", json={}, headers=chats.headers)
    assert error_of(response) == (400, "INVALID_INPUT", "pathway_id")


async def test_a_turn_over_the_cap_returns_what_it_produced(
    org: tuple[Chats, Api], pathway: str, receiver: tuple[Receiver, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    chats, _ = org
    hooks, _ = receiver
    hooks.script("/status", Reply(200, hold=True, body=b"{}"))
    monkeypatch.setattr(pathway_chat, "TURN_TIMEOUT_S", 0.5)
    chat_id = (await chats.created(pathway))["chat_id"]
    stream = json_log_stream()
    reply = await chats.said(chat_id, "What is my delivery status?")
    assert reply["assistant_messages"] == ["Let me check that for you."]
    assert (reply["current_node_id"], reply["ended"]) == ("hook", False)
    assert [
        event["chat_id"] for event in log_events(stream) if event["event"] == "pathway_chat.turn_timeout"
    ] == [chat_id]
    hooks.release()
