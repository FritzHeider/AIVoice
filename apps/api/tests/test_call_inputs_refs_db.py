"""Send Call input loading against Postgres: voice, tools/KBs, citations/evals/memory/pools, plan features,
BYO `encrypted_key`, destination and caller ID, step order, and the resolved config."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from call_input_rows import Seed, make_ctx, random_e164, request_of
from identity_db import IdentityDb

from va_api.services.call_inputs import resolve_inputs
from va_common.errors import AppError
from va_db.models import (
    CitationSchema,
    Contact,
    EvalSetup,
    EvalSetupVersion,
    KnowledgeBase,
    Memory,
    TelnyxAccount,
    Tool,
)
from va_domain.org import load_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
CUSTOM_RAIL = {"type": "custom", "description": "Never quote prices."}


@pytest.fixture
async def seed(identity: IdentityDb) -> Seed:
    return await Seed.create(identity.database)


@pytest.fixture
async def ready(seed: Seed) -> Seed:
    """An org with a `Karen` voice and one platform-account number."""
    await seed.voice("Karen")
    await seed.number()
    return seed


def _rails(count: int) -> list[dict[str, str]]:
    return [{**CUSTOM_RAIL, "name": f"rail_{index}"} for index in range(count)]


async def test_org_voice_beats_platform_voice_by_name(seed: Seed) -> None:
    await seed.number()
    name = f"Nova{secrets.token_hex(3)}"
    await seed.voice(name.upper(), platform=True)
    own = await seed.voice(name, settings={"stability": 0.2})
    inputs = await seed.load({"voice": name.lower()})
    assert inputs.refs.voice.voice_id == own
    assert inputs.refs.voice.settings.stability == pytest.approx(0.2)


async def test_platform_voice_by_name_and_by_uuid(seed: Seed) -> None:
    await seed.number()
    name = f"Orbit{secrets.token_hex(3)}"
    platform = await seed.voice(name, platform=True)
    assert (await seed.load({"voice": name})).refs.voice.voice_id == platform
    assert (await seed.load({"voice": str(platform)})).refs.voice.voice_id == platform


async def test_unusable_voices_are_not_found(seed: Seed) -> None:
    await seed.number()
    for name, values in (
        ("Gone", {"deleted_at": NOW}),
        ("Cooking", {"clone_status": "processing", "category": "cloned"}),
        ("Broken", {"clone_status": "failed", "category": "cloned"}),
    ):
        await seed.voice(name, **values)
        assert await seed.fails({"voice": name}) == ("VOICE_NOT_FOUND", "voice", f"Voice {name} not found.")
    ready_clone = await seed.voice("Ready", clone_status="ready", category="cloned")
    assert (await seed.load({"voice": "ready"})).refs.voice.voice_id == ready_clone


async def test_org_default_voices(seed: Seed) -> None:
    await seed.number()
    by_id = await seed.voice("Pinned")
    default_name = await seed.voice("Maple")
    assert (await seed.load({}, org={"default_voice_id": str(by_id)})).refs.voice.voice_id == by_id
    assert (await seed.load({}, org={"default_voice": "maple"})).refs.voice.voice_id == default_name
    assert (await seed.fails({}, org={}))[:2] == ("VOICE_NOT_FOUND", "voice")


async def test_another_orgs_voice_reads_as_absent(seed: Seed, identity: IdentityDb) -> None:
    await seed.number()
    other = await Seed.create(identity.database)
    foreign = await other.voice("Secret")
    assert (await seed.fails({"voice": str(foreign)}))[:2] == ("VOICE_NOT_FOUND", "voice")
    assert (await seed.fails({"voice": "Secret"}))[:2] == ("VOICE_NOT_FOUND", "voice")


async def test_tools_and_knowledge_bases_must_exist(ready: Seed, identity: IdentityDb) -> None:
    tool = await ready.row(Tool)
    kb = await ready.row(KnowledgeBase)
    deleted_tool = await ready.row(Tool, deleted_at=NOW)
    other = await Seed.create(identity.database)
    foreign_kb = await other.row(KnowledgeBase)
    inline = {"name": "lookup", "description": "Look up an order.", "url": "https://api.shop.test/orders"}
    inputs = await ready.load({"tools": [inline, f"TL-{tool}", f"KB-{kb}"]})
    assert len(inputs.merged.tools) == 3
    missing_tool = f"TL-{uuid.uuid4()}"
    assert await ready.fails({"tools": [f"TL-{tool}", missing_tool, f"KB-{uuid.uuid4()}"]}) == (
        "TOOL_NOT_FOUND",
        "tools[1]",
        f"Tool {missing_tool} not found.",
    )
    assert (await ready.fails({"tools": [f"TL-{deleted_tool}"]}))[:2] == ("TOOL_NOT_FOUND", "tools[0]")
    assert await ready.fails({"tools": [f"TL-{tool}", f"KB-{foreign_kb}"]}) == (
        "NOT_FOUND",
        "tools[1]",
        f"Knowledge base KB-{foreign_kb} not found.",
    )


async def test_citation_schemas_evals_memory_and_pool_must_exist(ready: Seed) -> None:
    schema = await ready.row(CitationSchema)
    setup = await ready.row(EvalSetup)
    setup_version = await ready.row(EvalSetupVersion)
    contact = await ready.row(Contact)
    memory = await ready.row(Memory, contact_id=contact)
    number_id, _ = await ready.number()
    pool = await ready.pool([(number_id, None)])
    await ready.load(
        {
            "citation_schema_ids": [str(schema)],
            "post_call_evals": {"workbench_setup_id": str(setup)},
            "memory_id": str(memory),
            "dialing_strategy": {"type": "custom_pooling", "pool_id": str(pool)},
        }
    )
    await ready.load({"post_call_evals": {"workbench_setup_version_id": str(setup_version)}})
    missing = uuid.uuid4()
    cases: list[tuple[dict[str, Any], str]] = [
        ({"citation_schema_ids": [str(schema), str(missing)]}, "citation_schema_ids[1]"),
        ({"post_call_evals": {"workbench_setup_id": str(missing)}}, "post_call_evals.workbench_setup_id"),
        (
            {"post_call_evals": {"workbench_setup_version_id": str(missing)}},
            "post_call_evals.workbench_setup_version_id",
        ),
        ({"memory_id": str(missing)}, "memory_id"),
        (
            {"dialing_strategy": {"type": "custom_pooling", "pool_id": str(missing)}},
            "dialing_strategy.pool_id",
        ),
    ]
    for body, field in cases:
        assert (await ready.fails(body))[:2] == ("NOT_FOUND", field)


async def test_org_eval_setup_default_is_checked(ready: Seed) -> None:
    missing = uuid.uuid4()
    org = {"default_voice": "Karen", "post_call_eval_setup_id": str(missing)}
    assert (await ready.fails({}, org=org))[:2] == ("NOT_FOUND", "post_call_evals.workbench_setup_id")


async def test_plan_features(ready: Seed) -> None:
    schema = await ready.row(CitationSchema)
    number_id, _ = await ready.number()
    pool = await ready.pool([(number_id, None)])
    gated: list[tuple[dict[str, Any], str]] = [
        ({"citation_schema_ids": [str(schema)]}, "citation_schema_ids"),
        ({"guard_rails": _rails(1)}, "guard_rails"),
        ({"dialing_strategy": {"type": "local"}}, "dialing_strategy"),
        ({"local_dialing": True}, "dialing_strategy"),
        ({"dialing_strategy": {"type": "custom_pooling", "pool_id": str(pool)}}, "dialing_strategy"),
    ]
    start = make_ctx(ready.org_id, plan_id="start")
    for body, field in gated:
        code, error_field, message = await ready.fails(body, ctx=start)
        assert (code, error_field) == ("FEATURE_NOT_IN_PLAN", field)
        assert message == f"{field} is not available on your plan."
        await ready.load(body)
    build = make_ctx(ready.org_id, plan_id="build")
    await ready.load(
        {"dialing_strategy": {"type": "local"}, "guard_rails": [{"type": "tcpa:opt_out"}]}, ctx=build
    )
    await ready.load({"guard_rails": _rails(5)})
    assert await ready.fails({"guard_rails": _rails(6)}) == (
        "GUARD_RAIL_LIMIT",
        "guard_rails",
        "At most 5 custom guard rails are allowed per call.",
    )


async def test_custom_rails_count_across_layers(ready: Seed) -> None:
    org = {"default_voice": "Karen", "default_guard_rails": _rails(4)}
    extra = [{**CUSTOM_RAIL, "name": f"extra_{index}"} for index in range(2)]
    assert (await ready.fails({"guard_rails": extra}, org=org))[:2] == ("GUARD_RAIL_LIMIT", "guard_rails")


async def _account(seed: Seed, status: str) -> tuple[uuid.UUID, str]:
    handle = f"tlx_{secrets.token_hex(12)}"
    account_id = await seed.row(
        TelnyxAccount, encrypted_key_handle=handle, status=status, call_control_app_id="cc-app"
    )
    return account_id, handle


async def test_byo_account_resolution(ready: Seed, identity: IdentityDb) -> None:
    account_id, handle = await _account(ready, "active")
    _, byo_number = await ready.number(telnyx_account_id=account_id)
    inputs = await ready.load({"from": byo_number}, encrypted_key=handle)
    assert inputs.telnyx_account is not None
    assert (inputs.telnyx_account.account_id, inputs.telnyx_account.call_control_app_id) == (
        account_id,
        "cc-app",
    )
    assert (inputs.refs.from_number, inputs.refs.telnyx_account_id) == (byo_number, account_id)
    invalid = ("INVALID_ENCRYPTED_KEY", None, "Invalid `encrypted_key`.")
    assert await ready.fails({"from": byo_number}, encrypted_key="tlx_unknown") == invalid
    _, deleted = await _account(ready, "deleted")
    assert await ready.fails({}, encrypted_key=deleted) == invalid
    other = await Seed.create(identity.database)
    _, foreign = await _account(other, "active")
    assert await ready.fails({}, encrypted_key=foreign) == invalid
    for status in ("pending", "error"):
        _, unavailable = await _account(ready, status)
        assert await ready.fails({}, encrypted_key=unavailable) == (
            "CARRIER_CONNECTION_ERROR",
            None,
            "Error in creating Telnyx connection.",
        )


async def test_byo_from_rules(ready: Seed) -> None:
    account_id, handle = await _account(ready, "active")
    _, platform_number = await ready.number()
    _, byo_number = await ready.number(telnyx_account_id=account_id)
    assert await ready.fails({}, encrypted_key=handle) == (
        "INVALID_INPUT",
        "from",
        "`from` is required with `encrypted_key`.",
    )
    assert (await ready.fails({"from": platform_number}, encrypted_key=handle))[:2] == (
        "INVALID_INPUT",
        "from",
    )
    assert (await ready.fails({"from": byo_number}))[:2] == ("INVALID_INPUT", "from")


async def test_explicit_from_must_be_an_active_voice_number(ready: Seed, identity: IdentityDb) -> None:
    number_id, e164 = await ready.number()
    inputs = await ready.load({"from": e164})
    assert (inputs.refs.from_number, inputs.refs.phone_number_id) == (e164, number_id)
    other = await Seed.create(identity.database)
    _, foreign = await other.number()
    _, sms_only = await ready.number(capabilities=["sms"])
    _, pending = await ready.number(status="pending")
    _, released = await ready.number(status="released")
    _, deleted = await ready.number(deleted_at=NOW)
    for unusable in (foreign, sms_only, pending, released, deleted, random_e164()):
        assert await ready.fails({"from": unusable}) == (
            "INVALID_INPUT",
            "from",
            "`from` must be an active voice-capable number of this organization.",
        )


async def test_local_presence_prefers_area_code_then_state(seed: Seed) -> None:
    await seed.voice("Karen")
    _, far = await seed.number(random_e164("212"))
    _, same_state = await seed.number(random_e164("650"))
    body: dict[str, Any] = {"phone_number": random_e164("415"), "dialing_strategy": {"type": "local"}}
    assert (await seed.load(body)).refs.from_number == same_state
    _, exact = await seed.number(random_e164("415"))
    assert (await seed.load(body)).refs.from_number == exact
    assert far != exact


async def test_pool_picks_the_least_recently_used_active_member(seed: Seed) -> None:
    await seed.voice("Karen")
    recent_id, recent = await seed.number()
    older_id, older = await seed.number()
    never_id, never = await seed.number()
    inactive_id, _ = await seed.number(status="pending")
    pool = await seed.pool(
        [
            (recent_id, datetime(2026, 10, 30, tzinfo=UTC)),
            (older_id, datetime(2026, 10, 1, tzinfo=UTC)),
            (never_id, None),
            (inactive_id, None),
        ]
    )
    body = {"dialing_strategy": {"type": "custom_pooling", "pool_id": str(pool)}}
    assert (await seed.load(body)).refs.from_number == never
    empty = await seed.pool([(inactive_id, None)])
    fallback = await seed.load({"dialing_strategy": {"type": "custom_pooling", "pool_id": str(empty)}})
    assert fallback.refs.from_number == min(recent, older, never)


async def test_default_caller_id_rules(seed: Seed) -> None:
    await seed.voice("Karen")
    assert await seed.fails({}) == (
        "INVALID_INPUT",
        "from",
        "No caller ID available: pass `from`, set default_from_number, or add a phone number.",
    )
    _, near = await seed.number(random_e164("415"))
    _, default = await seed.number(random_e164("212"))
    destination = random_e164("415")
    assert (await seed.load({"phone_number": destination})).refs.from_number == near
    org = {"default_voice": "Karen", "default_from_number": default}
    assert (await seed.load({"phone_number": destination}, org=org)).refs.from_number == default
    _, sms_only = await seed.number(capabilities=["sms"])
    assert await seed.fails({}, org={"default_voice": "Karen", "default_from_number": sms_only}) == (
        "INVALID_INPUT",
        "default_from_number",
        "The org default_from_number is not an active voice number.",
    )


async def test_destination_parsing(ready: Seed) -> None:
    inputs = await ready.load({"phone_number": "020 7946 0000", "country": "GB"})
    assert (inputs.refs.phone_number, inputs.country) == ("+442079460000", "GB")
    org = {"default_voice": "Karen", "default_country": "GB"}
    assert (await ready.load({"phone_number": "020 7946 0000"}, org=org)).country == "GB"
    assert (await ready.fails({"phone_number": "555"}))[:2] == ("INVALID_PHONE_NUMBER", "phone_number")


async def test_the_earliest_failing_step_wins(seed: Seed) -> None:
    schema = await seed.row(CitationSchema)
    start = make_ctx(seed.org_id, plan_id="start", scopes=("calls:write",))
    assert (await seed.fails({"agent_id": str(uuid.uuid4()), "task": None}, ctx=start))[0] == (
        "AGENT_REQUIRES_ORG_KEY"
    )
    assert (await seed.fails({"voice": "Nobody", "phone_number": "555"}))[0] == "VOICE_NOT_FOUND"
    await seed.voice("Karen")
    tool_and_feature = {"tools": [f"TL-{uuid.uuid4()}"], "citation_schema_ids": [str(schema)]}
    assert (await seed.fails(tool_and_feature, ctx=start))[0] == "TOOL_NOT_FOUND"
    feature_and_key = {"citation_schema_ids": [str(schema)]}
    assert (await seed.fails(feature_and_key, ctx=start, encrypted_key="tlx_x"))[0] == "FEATURE_NOT_IN_PLAN"
    assert (await seed.fails({"phone_number": "555"}, encrypted_key="tlx_x"))[0] == "INVALID_ENCRYPTED_KEY"
    assert (await seed.fails({"phone_number": "555"}))[0] == "INVALID_PHONE_NUMBER"


async def test_resolve_inputs_builds_the_config_with_loader_warnings_first(ready: Seed) -> None:
    pathway_id = await ready.pathway({1: {}, 2: {}}, production=None, draft=2)
    number_id, e164 = await ready.number()
    body = {"pathway_id": str(pathway_id), "task": "ignored", "from": e164, "voice": "karen"}
    inputs = await ready.load(body)
    resolution = resolve_inputs(inputs, request_of(body), NOW)
    config = resolution.config
    assert (config.pathway_id, config.pathway_version) == (pathway_id, 2)
    assert (config.from_number, config.phone_number_id) == (e164, number_id)
    assert config.voice_id == inputs.refs.voice.voice_id
    assert [warning.code for warning in resolution.warnings] == ["PATHWAY_UNPUBLISHED", "TASK_IGNORED"]


async def test_resolve_inputs_maps_rule_failures(ready: Seed) -> None:
    body = {"start_time": "2020-01-01T00:00:00Z"}
    inputs = await ready.load(body)
    with pytest.raises(AppError) as caught:
        resolve_inputs(inputs, request_of(body), NOW)
    assert (caught.value.code, caught.value.field) == ("INVALID_INPUT", "start_time")


async def test_org_settings_layer_errors_are_app_errors(ready: Seed) -> None:
    org = load_settings({"default_voice": "Karen", "call_defaults": {"record": "maybe"}})
    assert (await ready.fails({}, org=org))[:2] == ("INVALID_INPUT", "call_defaults.record")
