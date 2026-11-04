"""Request and response bodies of the pathway regression settings API (specs/12-pathways-api.md §9; legacy).

Rules: specs/12 §9 Decision P07-T11e1.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_api.routers.pathway_tests_schemas import ScenarioBody
from va_domain.pathway_tests import MAX_REGRESSION_SCENARIOS, RegressionSettings

__all__ = ["RegressionBody", "RegressionSaved", "RegressionView"]

_SETTINGS: Final[dict[str, JsonValue]] = {
    "run_on_publish": True,
    "block_publish_on_failure": True,
    "include_simulations": False,
    "scenarios": [],
}
_HASH: Final = "4f1c2a9e0b7d3c5a8e6f1b2d4c9a7e3f5b8d1c6a2e9f4b7d0c3a5e8f1b2d4c6a"


class RegressionBody(RegressionSettings):
    """The regression suite settings: ``run_on_publish`` (a publish runs the suite),
    ``block_publish_on_failure`` (a failing suite keeps the version unpublished), ``include_simulations`` (the
    suite also plays ``scenarios``, one conversation each). Omitted keys take their defaults."""

    model_config = ConfigDict(json_schema_extra={"examples": [_SETTINGS]})

    scenarios: tuple[ScenarioBody, ...] = Field(default=(), max_length=MAX_REGRESSION_SCENARIOS)


class RegressionView(BaseModel):
    """``GET /v1/pathway/{pathway_id}/regression``."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "regression": _SETTINGS}]}
    )

    status: Literal["success"] = "success"
    regression: RegressionSettings


class RegressionSaved(RegressionView):
    """``PUT /v1/pathway/{pathway_id}/regression``: the stored settings and the draft holding them."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "success", "regression": _SETTINGS, "draft_version": 5, "graph_hash": _HASH}
            ]
        }
    )

    draft_version: int
    graph_hash: str
