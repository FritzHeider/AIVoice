"""FakeLLM control API: rules (YAML/JSON, listing, deletion), api keys, models, latency config, reset."""

from __future__ import annotations

import orjson
from llm_helpers import add_rules, chat_body, llm_client, llm_state

from va_sim.llm.rules import load_rules
from va_sim.settings import DEFAULT_LLM_MODELS


async def test_rules_register_as_yaml_or_json_and_list_in_match_order() -> None:
    state = llm_state()
    state.llm.rules.add(load_rules("rules:\n  - id: from-file\n").rules, source="/rules/base.yaml")
    async with llm_client(state) as client:
        assert await add_rules(client, "rules:\n  - id: y1\n  - id: y2\n    priority: 3\n") == ["y1", "y2"]
        json_response = await client.post(
            "/sim/llm/rules", json={"rules": [{"id": "j1", "respond": {"content": "hi"}}]}
        )
        assert (json_response.status_code, json_response.json()) == (201, {"ids": ["j1"]})
        listing = (await client.get("/sim/llm/rules")).json()["rules"]
    assert [(rule["id"], rule["source"]) for rule in listing] == [
        ("y2", "runtime"),
        ("from-file", "/rules/base.yaml"),
        ("y1", "runtime"),
        ("j1", "runtime"),
    ]
    assert listing[3]["respond"]["content"] == "hi"
    assert listing[3]["match"]["call_index"] is None


async def test_rule_registration_errors() -> None:
    async with llm_client(llm_state()) as client:
        unsupported = await client.post(
            "/sim/llm/rules", content=b"rules: []", headers={"content-type": "text/plain"}
        )
        yaml_invalid = await client.post(
            "/sim/llm/rules",
            content=b"rules:\n  - id: r\n    match: {last_user_regex: '['}\n",
            headers={"content-type": "application/yaml"},
        )
        not_utf8 = await client.post(
            "/sim/llm/rules", content=b"\xff\xfe", headers={"content-type": "application/yaml"}
        )
        json_invalid = await client.post("/sim/llm/rules", json={"rules": [{"id": "r", "respond": {"x": 1}}]})
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_media_type"
    assert "send the rules as one of" in unsupported.json()["error"]["message"]
    assert yaml_invalid.status_code == 422
    (issue,) = yaml_invalid.json()["error"]["issues"]
    assert (issue["path"], issue["line"], issue["column"]) == ("rules[0].match.last_user_regex", 3, 13)
    assert "invalid regular expression" in issue["message"]
    assert not_utf8.status_code == 422
    assert json_invalid.status_code == 422
    assert json_invalid.json()["error"]["issues"][0]["path"] == "rules[0].respond.x"


async def test_rules_delete_runtime_and_by_id() -> None:
    state = llm_state()
    state.llm.rules.add(load_rules("rules:\n  - id: file-rule\n").rules, source="base.yaml")
    async with llm_client(state) as client:
        await add_rules(client, "rules:\n  - id: a\n  - id: b\n")
        assert (await client.delete("/sim/llm/rules/a")).status_code == 204
        missing = await client.delete("/sim/llm/rules/a")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        assert [rule["id"] for rule in (await client.get("/sim/llm/rules")).json()["rules"]] == [
            "file-rule",
            "b",
        ]
        assert (await client.delete("/sim/llm/rules")).status_code == 204
        assert [rule["id"] for rule in (await client.get("/sim/llm/rules")).json()["rules"]] == ["file-rule"]
        assert (await client.delete("/sim/llm/rules/file-rule")).status_code == 204
        assert (await client.get("/sim/llm/rules")).json() == {"rules": []}


async def test_api_keys_and_models_routes() -> None:
    async with llm_client(llm_state()) as client:
        assert (await client.get("/sim/llm/api-keys")).json() == {"keys": []}
        for key in ("k1", "k1", "sim-llm-key"):
            assert (await client.post("/sim/llm/api-keys", json={"api_key": key})).status_code == 204
        assert (await client.get("/sim/llm/api-keys")).json() == {"keys": ["k1"]}
        assert (await client.post("/sim/llm/api-keys", json={"api_key": "  "})).status_code == 422
        assert (await client.post("/sim/llm/models", json={"id": DEFAULT_LLM_MODELS[0]})).status_code == 204
        assert (await client.post("/sim/llm/models", json={"id": "acme/x"})).status_code == 204
        assert (await client.get("/sim/llm/models")).json() == {"models": [*DEFAULT_LLM_MODELS, "acme/x"]}
        assert (await client.post("/sim/llm/models", json={"name": "x"})).status_code == 422


async def test_latency_config_applies_to_chat() -> None:
    state = llm_state(ttft_ms=150, tokens_per_s=80.0)
    async with llm_client(state) as client:
        patched = await client.patch("/sim/llm/config", json={"ttft_ms": 10, "tokens_per_s": 1000})
        assert patched.json() == {"ttft_ms": 10, "tokens_per_s": 1000.0}
        start = state.clock.virtual_ms()
        await client.post("/v1/chat/completions", json=chat_body("hello there"))
        # 10 ms + 4 x 1 ms.
        assert state.clock.virtual_ms() - start == 14


async def test_reset_restores_llm_state_but_keeps_file_rules() -> None:
    state = llm_state()
    state.llm.rules.add(load_rules("rules:\n  - id: file-rule\n").rules, source="base.yaml")
    async with llm_client(state) as client:
        await add_rules(client, "rules:\n  - id: runtime-rule\n")
        await client.post("/sim/llm/api-keys", json={"api_key": "extra"})
        await client.post("/sim/llm/models", json={"id": "acme/x"})
        await client.put("/sim/llm/fault", json={"kind": "rate_limit"})
        await client.post("/v1/chat/completions", json=chat_body("hi"))
        assert (await client.post("/sim/reset")).status_code == 204
        snapshot = orjson.loads((await client.get("/sim/state")).content)
        rules = (await client.get("/sim/llm/rules")).json()["rules"]
        keys = (await client.get("/sim/llm/api-keys")).json()
        fault = (await client.get("/sim/llm/fault")).json()
        requests = (await client.get("/sim/llm/requests")).json()
    assert snapshot["llm"] == {"requests": 0, "rules": 1, "models": len(DEFAULT_LLM_MODELS), "held": 0}
    assert [rule["id"] for rule in rules] == ["file-rule"]
    assert (keys, fault, requests) == ({"keys": []}, {"fault": None}, {"requests": []})
