"""``InboundRouter`` on Postgres with FakeTelnyx (specs/06-telephony-telnyx.md §7, Decision P05-T12b).

Every refusal sends one idempotent command (``reject-{ccid}`` / ``fallback-{ccid}``), stores its reason in
``telnyx_events.process_error``, and settles the event (attempts exhausted, never reclaimed); failures before
the command leave a normal error that the reprocessor retries.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from compliance_rows import add_dnc
from edge_helpers import envelope, stored_event
from routing_helpers import (
    CALLER,
    FALLBACK,
    RecordingAcceptor,
    RoutingRig,
    failing_telnyx,
    random_number,
    routing_rig,
)
from structlog.testing import capture_logs

from va_billing import keys
from va_db.repositories import TelnyxEventRepo
from va_db.session import Database
from va_edge.processor import MAX_ATTEMPTS

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


async def claimable(rig: RoutingRig, event_id: Any) -> bool:
    async with rig.database.system_session() as session:
        rows = await TelnyxEventRepo(session).claim_retries(
            now=datetime.now(UTC) + timedelta(minutes=5),
            stale_after=timedelta(seconds=30),
            retry_after=timedelta(seconds=5),
            max_attempts=MAX_ATTEMPTS,
        )
    return event_id in {row.id for row in rows}


def assert_settled(row: Any, reason: str) -> None:
    assert (row.process_error, row.process_attempts) == (reason, MAX_ATTEMPTS)
    assert row.processed_at is not None


async def test_unknown_number_is_rejected_through_the_platform_account(database: Database) -> None:
    async with routing_rig(database) as rig:
        # Owned by the sim (so the call rings) but unknown to the platform database.
        to = random_number()
        record_id = rig.state.ids.uuid()
        rig.state.telnyx.accounts["sim-telnyx-key"].numbers[record_id] = {
            "id": record_id,
            "phone_number": to,
            "connection_id": rig.app_ids["sim-telnyx-key"],
            "messaging_profile_id": None,
        }
        ccid, event = await rig.ring(to)
        row = await rig.route(event)
        assert [(c.action, c.body) for c in rig.commands(ccid)] == [
            ("reject", {"cause": "CALL_REJECTED", "command_id": f"reject-{ccid}"})
        ]
        assert rig.commands(ccid)[0].authorization == "Bearer sim-telnyx-key"
        assert_settled(row, "unknown_number")
        assert not await claimable(rig, event.id)
        leg = await rig.leg(ccid)
        assert (leg["hangup_cause"], leg["answered_at"]) == ("call_rejected", None)
        assert rig.counted("rejected") == 1


async def test_suspended_org_with_a_fallback_transfers_the_ringing_call(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status="suspended")
        to = await rig.number(org, {"fallback_number": FALLBACK})
        ccid, event = await rig.ring(to)
        row = await rig.route(event)
        [command] = rig.commands(ccid)
        assert command.action == "transfer"
        assert command.body == {
            "to": FALLBACK,
            "from": to,
            "timeout_secs": 30,
            "time_limit_secs": 14_400,
            "early_media": True,
            "command_id": f"fallback-{ccid}",
            "answering_machine_detection": "disabled",
        }
        assert_settled(row, "org_suspended")
        leg = await rig.leg(ccid)
        assert [t["to"] for t in leg["transfers"]] == [FALLBACK]
        assert rig.counted("fallback") == 1
        assert rig.acceptor.calls == []


async def test_suspended_org_without_a_fallback_is_rejected(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status="suspended")
        ccid, event = await rig.ring(await rig.number(org, {"prompt": "Hello"}))
        row = await rig.route(event)
        assert [c.action for c in rig.commands(ccid)] == ["reject"]
        assert_settled(row, "org_suspended")


async def test_low_credits_are_refused(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(balance=10_000)
        ccid, event = await rig.ring(await rig.number(org))
        assert_settled(await rig.route(event), "insufficient_credits")
        assert rig.commands(ccid)[0].body["cause"] == "CALL_REJECTED"


async def test_credit_limit_counts_towards_the_minimum_attempt(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(balance=0, credit_limit=15_000)
        ccid, event = await rig.ring(await rig.number(org))
        row = await rig.route(event)
        assert (row.process_error, rig.commands(ccid)) == (None, [])
        assert len(rig.acceptor.calls) == 1


async def test_dnc_callers_are_rejected_only_when_the_number_blocks_them(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        await add_dnc(database, org, CALLER)
        open_number = await rig.number(org)
        strict = await rig.number(org, {"block_dnc_callers": True, "fallback_number": FALLBACK})
        ccid, event = await rig.ring(open_number)
        assert (await rig.route(event)).process_error is None
        assert rig.commands(ccid) == []
        ccid, event = await rig.ring(strict)
        assert_settled(await rig.route(event), "caller_dnc")
        assert [c.action for c in rig.commands(ccid)] == ["reject"]


async def test_platform_dnc_rows_block_callers_too(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        caller = random_number()
        await add_dnc(database, None, caller)
        ccid, event = await rig.ring(await rig.number(org, {"block_dnc_callers": True}), caller)
        assert_settled(await rig.route(event), "caller_dnc")
        assert [c.action for c in rig.commands(ccid)] == ["reject"]


async def test_caller_lists_match_the_national_form_of_the_caller(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        to = await rig.number(org, {"blocked_callers": [CALLER], "fallback_number": FALLBACK})
        # A national form of the caller is normalized with the called number's region (a hand-made event:
        # FakeTelnyx only sends E.164; the reject then finds no such leg and the event still settles).
        ccid = f"v3:{uuid.uuid4().hex}"
        payload = {"call_control_id": ccid, "direction": "incoming", "from": "(415) 555-0123", "to": to}
        national = await stored_event(database, envelope("call.initiated", payload=payload))
        assert_settled(await rig.route(national), "caller_blocked")
        assert [c.action for c in rig.commands(ccid)] == ["reject"]
        allowed = await rig.number(org, {"allowed_callers": ["+14155550999"]})
        ccid, event = await rig.ring(allowed)
        assert_settled(await rig.route(event), "caller_not_allowed")


async def test_maintenance_with_a_fallback_transfers(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        config = {"fallback_number": FALLBACK, "maintenance": {"enabled": True}}
        ccid, event = await rig.ring(await rig.number(org, config))
        assert_settled(await rig.route(event), "maintenance")
        assert [c.action for c in rig.commands(ccid)] == ["transfer"]


async def test_maintenance_message_without_a_fallback_is_accepted(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        config = {"maintenance": {"enabled": True, "message": "Closed for the holiday."}}
        ccid, event = await rig.ring(await rig.number(org, config))
        assert (await rig.route(event)).process_error is None
        [call] = rig.acceptor.calls
        assert call.maintenance_message is True
        assert rig.commands(ccid) == []


async def test_invalid_config_uses_a_valid_raw_fallback(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        to = await rig.number(org, {"fallback_number": FALLBACK, "test_call": True})
        ccid, event = await rig.ring(to)
        with capture_logs() as logs:
            assert_settled(await rig.route(event), "invalid_inbound_config")
        assert [c.action for c in rig.commands(ccid)] == ["transfer"]
        [invalid] = [log for log in logs if log["event"] == "telnyx.inbound_config_invalid"]
        assert invalid["field"] is None


async def test_accepted_call_holds_a_slot_under_a_fresh_call_id(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org()
        to = await rig.number(org, {"prompt": "Greet the caller.", "max_duration": 12})
        ccid, event = await rig.ring(to)
        row = await rig.route(event)
        assert (row.process_error, row.process_attempts) == (None, 1)
        [call] = rig.acceptor.calls
        assert (call.number.e164, call.number.org_id, call.caller) == (to, org, CALLER)
        assert (call.config.prompt, call.maintenance_message, call.payload.call_control_id) == (
            "Greet the caller.",
            False,
            ccid,
        )
        key = keys.concurrency_key(org)
        assert await rig.redis.zcard(key) == 1
        expiry_ms = await rig.redis.zscore(key, str(call.call_id))
        # max_duration 12 min + the 5 min grace, from now.
        assert expiry_ms is not None
        assert time.time() * 1000 + 16 * 60_000 < expiry_ms < time.time() * 1000 + 18 * 60_000
        assert rig.commands(ccid) == []
        assert rig.counted("answered") == 1


@pytest.mark.parametrize(("fallback", "action"), [(True, "transfer"), (False, "reject")])
async def test_concurrency_cap(database: Database, fallback: bool, action: str) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(limits={"concurrency": 1})
        far = (time.time() + 3600) * 1000
        await rig.redis.zadd(keys.concurrency_key(org), {"busy-call": far})
        config: dict[str, Any] = {"fallback_number": FALLBACK} if fallback else {}
        ccid, event = await rig.ring(await rig.number(org, config))
        assert_settled(await rig.route(event), "concurrency_cap")
        [command] = rig.commands(ccid)
        assert command.action == action
        if not fallback:
            assert command.body["cause"] == "USER_BUSY"
            assert (await rig.leg(ccid))["hangup_cause"] == "user_busy"
        assert rig.acceptor.calls == []


async def test_failing_acceptor_releases_the_slot_and_falls_back(database: Database) -> None:
    acceptor = RecordingAcceptor(error=RuntimeError("context build failed"))
    async with routing_rig(database, acceptor=acceptor) as rig:
        org = await rig.org()
        ccid, event = await rig.ring(await rig.number(org, {"fallback_number": FALLBACK}))
        with capture_logs() as logs:
            assert_settled(await rig.route(event), "accept_failed")
        assert len(acceptor.calls) == 1
        assert await rig.limiter.active_calls(org) == 0
        assert [c.action for c in rig.commands(ccid)] == ["transfer"]
        [failed] = [log for log in logs if log["event"] == "telnyx.inbound_accept_failed"]
        assert failed["error_type"] == "RuntimeError"


async def test_router_without_an_acceptor_refuses_with_accept_unavailable(database: Database) -> None:
    async with routing_rig(database, with_acceptor=False) as rig:
        org = await rig.org()
        ccid, event = await rig.ring(await rig.number(org))
        assert_settled(await rig.route(event), "accept_unavailable")
        assert await rig.limiter.active_calls(org) == 0
        assert rig.commands(ccid)[0].body["cause"] == "CALL_REJECTED"


async def test_byo_number_commands_use_the_byo_key(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status="suspended")
        account = await rig.byo_account(org)
        ccid, event = await rig.ring(await rig.number(org, account=account))
        assert_settled(await rig.route(event), "org_suspended")
        [command] = rig.commands(ccid)
        assert command.authorization == f"Bearer {account[1]}"


async def test_a_caller_who_hung_up_first_still_settles(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status="suspended")
        ccid, event = await rig.ring(await rig.number(org))
        response = await rig.raw.post(f"/sim/telnyx/calls/{ccid}/hangup", json={})
        assert response.status_code < 300, response.text
        with capture_logs() as logs:
            assert_settled(await rig.route(event), "org_suspended")
        assert "telnyx.inbound_caller_gone" in [log["event"] for log in logs]


async def test_telnyx_failure_before_the_command_is_retried(database: Database) -> None:
    async with routing_rig(database) as rig:
        org = await rig.org(status="suspended")
        ccid, event = await rig.ring(await rig.number(org))
        row = await rig.route(event, router=rig.router_on(failing_telnyx(rig.redis)))
        assert row.process_error is not None
        assert row.process_error.startswith("TelnyxApiError")
        assert row.process_attempts == 1
        assert await claimable(rig, event.id)
        # The retry succeeds through the sim and settles the event.
        retried = await rig.route(await rig.stored(event))
        assert_settled(retried, "org_suspended")
        assert [c.action for c in rig.commands(ccid)] == ["reject"]


async def test_an_event_without_a_called_number_is_an_unknown_number(database: Database) -> None:
    async with routing_rig(database) as rig:
        ccid = f"v3:{uuid.uuid4().hex}"
        payload = {"call_control_id": ccid, "direction": "incoming", "from": CALLER}
        event = await stored_event(database, envelope("call.initiated", payload=payload))
        assert_settled(await rig.route(event), "unknown_number")
        assert [c.action for c in rig.commands(ccid)] == ["reject"]
