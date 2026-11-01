"""Per-model cost of goods (specs/10-llm-gateway.md §11; Decision P03-T13a rule 9).

``MODEL_COSTS`` holds provisional built-in prices in micro-dollars per 1 000 tokens; ``LLM_MODEL_COSTS_JSON``
(``{"model": {"in_per_1k_micros": n, "out_per_1k_micros": n}}``) is merged over them at startup.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from va_common.logging import get_logger

__all__ = ["MODEL_COSTS", "CostTable", "ModelCost", "load_costs"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ModelCost:
    in_per_1k_micros: int
    out_per_1k_micros: int


MODEL_COSTS: Final[Mapping[str, ModelCost]] = MappingProxyType(
    {
        "zai-org/GLM-5.3-Flash": ModelCost(50, 200),
        "zai-org/GLM-5.3": ModelCost(600, 2200),
        "moonshotai/Kimi-K2.6": ModelCost(600, 2500),
        "Qwen/Qwen3-Embedding-8B": ModelCost(20, 0),
    }
)


class _CostOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    in_per_1k_micros: int = Field(ge=0)
    out_per_1k_micros: int = Field(ge=0)


def load_costs(overrides_json: str, base: Mapping[str, ModelCost] = MODEL_COSTS) -> Mapping[str, ModelCost]:
    """``base`` with the ``LLM_MODEL_COSTS_JSON`` entries merged over it; ``ValueError`` for invalid JSON or
    an entry that is not ``{"in_per_1k_micros": int >= 0, "out_per_1k_micros": int >= 0}``."""
    try:
        document = orjson.loads(overrides_json or "{}")
    except orjson.JSONDecodeError as exc:
        msg = f"LLM_MODEL_COSTS_JSON is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(document, dict):
        msg = "LLM_MODEL_COSTS_JSON must be a JSON object keyed by model"
        raise ValueError(msg)  # noqa: TRY004 - a configuration error, reported like the other invalid values
    merged = dict(base)
    for model, entry in document.items():
        try:
            parsed = _CostOverride.model_validate(entry)
        except ValidationError as exc:
            msg = f"LLM_MODEL_COSTS_JSON entry {model!r} is invalid: {exc.errors()[0]['msg']}"
            raise ValueError(msg) from exc
        merged[model] = ModelCost(parsed.in_per_1k_micros, parsed.out_per_1k_micros)
    return MappingProxyType(merged)


class CostTable:
    """Costs of token counts; an unknown model costs 0 and is logged ``llm.unpriced_model`` once."""

    def __init__(self, costs: Mapping[str, ModelCost] = MODEL_COSTS) -> None:
        self._costs = costs
        self._unpriced: set[str] = set()

    def cost_micros(self, model: str, prompt_tokens: int, completion_tokens: int) -> tuple[int, int]:
        """``(cost_in, cost_out)`` in micro-dollars, each rounded up."""
        cost = self._costs.get(model)
        if cost is None:
            if model not in self._unpriced:
                self._unpriced.add(model)
                _log.warning("llm.unpriced_model", model=model)
            return 0, 0
        return (
            math.ceil(prompt_tokens * cost.in_per_1k_micros / 1000),
            math.ceil(completion_tokens * cost.out_per_1k_micros / 1000),
        )
