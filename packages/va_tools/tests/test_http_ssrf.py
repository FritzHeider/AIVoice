"""The executor's SSRF guard on real sockets (specs/13 §9, Decision P07-T07)."""

from __future__ import annotations

from dns_fakes import MappedResolver
from tools_helpers import call, guarded_executor
from webhook_receiver import Receiver, Reply, served_receiver


async def test_private_destination_allowed_in_local_and_test() -> None:
    receiver = Receiver(Reply(200, headers={"content-type": "application/json"}, body=b'{"status": "ok"}'))
    dns = MappedResolver({"hooks.test": ["127.0.0.1"]})
    async with served_receiver(receiver) as port, guarded_executor(dns, allow_private=True) as executor:
        outcome = await executor.execute(
            call("/lookup", base=f"http://hooks.test:{port}", body={"a": 1}, extract=(("s", "$.status"),))
        )

    assert (outcome.kind, outcome.status, outcome.extracted) == ("ok", 200, {"s": "ok"})
    assert receiver.requests[0].host == "hooks.test"
    assert receiver.requests[0].body == b'{"a":1}'
    assert dns.lookups == ["hooks.test"]


async def test_private_destination_blocked_without_retry() -> None:
    receiver = Receiver()
    dns = MappedResolver({"hooks.test": ["127.0.0.1"]})
    async with served_receiver(receiver) as port, guarded_executor(dns, allow_private=False) as executor:
        outcome = await executor.execute(call("/x", base=f"http://hooks.test:{port}", retries=3))

    assert (outcome.kind, outcome.attempts, outcome.status) == ("blocked", 1, None)
    assert outcome.status_class() == "timeout"
    assert receiver.requests == []


async def test_private_literal_address_blocked() -> None:
    async with guarded_executor(MappedResolver(), allow_private=False) as executor:
        outcome = await executor.execute(call("/x", base="http://10.0.0.1"))
    assert outcome.kind == "blocked"


async def test_any_private_answer_blocks_the_name() -> None:
    receiver = Receiver()
    dns = MappedResolver({"rebind.test": ["8.8.8.8", "127.0.0.1"]})
    async with served_receiver(receiver) as port, guarded_executor(dns, allow_private=False) as executor:
        outcome = await executor.execute(call("/x", base=f"http://rebind.test:{port}"))

    assert outcome.kind == "blocked"
    assert receiver.requests == []


async def test_unresolvable_host_is_a_connection_failure() -> None:
    async with guarded_executor(MappedResolver(), allow_private=True) as executor:
        outcome = await executor.execute(call("/x", base="http://nowhere.test", retries=1))
    assert (outcome.kind, outcome.attempts) == ("connection", 2)
