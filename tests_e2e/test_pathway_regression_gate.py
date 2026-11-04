"""``pathway_regression_gate`` (specs/12 §4 step 2; Decision P07-T11e3): a pathway whose suite opts into a
blocking regression run on publish. The broken draft routes "order food" to the goodbye node, so its one unit
test fails in the publish request and the publish answers 409 ``CONFLICT`` with ``details.run_id``; after the
draft is fixed the same publish runs the suite again, passes, and pins production. Decisions run through the
api container's real LLM gateway against FakeLLM (it routes by edge label words)."""

from __future__ import annotations

from typing import Any, Final

import pytest
from e2e_harness import Harness
from e2e_pathways import PathwayApi, ok, ordering_graph

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

ORDER_TEST: Final[dict[str, Any]] = {
    "name": "Ordering routes to Take Order",
    "node_id": "greet",
    "user_message": "I want to order food please",
    "expected": {"route_to_node_id": "order"},
    "simulated_variations": 0,
}
CONFLICT: Final = 409


async def production(pathways: PathwayApi, pathway_id: str) -> int | None:
    versions = ok(await pathways.versions(pathway_id))["versions"]
    pinned = [item["version_number"] for item in versions if item["is_production"]]
    return pinned[0] if pinned else None


async def test_pathway_regression_gate(harness: Harness) -> None:
    org = await harness.create_org()
    pathways = PathwayApi(harness.http, harness.urls.api, org)
    pathway_id = str(ok(await pathways.create("Bella Pasta gate", ordering_graph("bye")))["pathway_id"])
    ok(await pathways.add_unit_test(pathway_id, ORDER_TEST))
    settings = ok(
        await pathways.put_regression(pathway_id, {"run_on_publish": True, "block_publish_on_failure": True})
    )
    assert settings["regression"]["block_publish_on_failure"] is True

    blocked = await pathways.publish(pathway_id)
    assert blocked.status_code == CONFLICT, blocked.text
    (error,) = blocked.json()["errors"]
    assert (error["error"], error["details"]["outcome"]) == ("CONFLICT", "blocked")
    run = ok(await pathways.run(pathway_id, error["details"]["run_id"]))["run"]
    assert (run["kind"], run["status"], run["publish"]["outcome"]) == ("regression", "failed", "blocked")
    (result,) = run["results"]
    assert (result["status"], result["passed"]) == ("failed", False)
    assert await production(pathways, pathway_id) is None

    ok(await pathways.update_graph(pathway_id, ordering_graph("order")))
    published = ok(await pathways.publish(pathway_id))
    assert (published["status"], published["published_version"], published["environment"]) == (
        "success",
        1,
        "production",
    )
    passed = ok(await pathways.run(pathway_id, published["run_id"]))["run"]
    assert (passed["status"], passed["publish"]["outcome"]) == ("passed", "published")
    assert await production(pathways, pathway_id) == 1
