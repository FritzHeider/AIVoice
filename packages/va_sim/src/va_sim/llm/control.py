"""FakeLLM control routes (specs/30 §3.4 Control API; Decision P03-T06a rules 10, 11).

``GET``/``DELETE /sim/llm/requests``; ``GET``/``POST``/``DELETE /sim/llm/rules``; ``DELETE
/sim/llm/rules/{rule_id}``; ``PUT``/``GET``/``DELETE /sim/llm/fault``; ``GET``/``POST /sim/llm/api-keys``;
``GET``/``POST /sim/llm/models``. Errors use the shared ``{"error": ...}`` control format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt, StringConstraints, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import fault_route, json_body, sim_error, yaml_or_json_body
from va_sim.llm.rules import RUNTIME_SOURCE, RuleFile, load_rules
from va_sim.llm.store import FaultKind

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["ApiKeyBody", "LlmFaultBody", "ModelBody", "llm_control_routes"]

type _NonEmpty = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LlmFaultBody(_Strict):
    kind: FaultKind
    times: PositiveInt = 1
    status: Literal[500, 502, 503] | None = None
    retry_after_s: NonNegativeInt | None = None
    after_tokens: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _fields_fit_kind(self) -> Self:
        if self.status is not None and self.kind != "server_error":
            msg = "status applies to the server_error fault only"
            raise ValueError(msg)
        if self.retry_after_s is not None and self.kind != "rate_limit":
            msg = "retry_after_s applies to the rate_limit fault only"
            raise ValueError(msg)
        if self.after_tokens is not None and self.kind != "timeout":
            msg = "after_tokens applies to the timeout fault only"
            raise ValueError(msg)
        return self


class ApiKeyBody(_Strict):
    api_key: _NonEmpty


class ModelBody(_Strict):
    id: _NonEmpty


def _load_request_rules(text: str) -> RuleFile:
    return load_rules(text, source="request")


def _rules_routes(state: SimState) -> list[BaseRoute]:
    book = state.llm.rules

    async def listing(_request: Request) -> Response:
        return JSONResponse({"rules": [entry.to_json() for entry in book.ordered()]})

    async def register(request: Request) -> Response:
        document = await yaml_or_json_body(request, RuleFile, _load_request_rules, what="rules")
        if isinstance(document, JSONResponse):
            return document
        ids = book.add(document.rules, source=RUNTIME_SOURCE)
        return JSONResponse({"ids": ids}, status_code=201)

    async def clear(_request: Request) -> Response:
        book.clear_runtime()
        return Response(status_code=204)

    async def remove(request: Request) -> Response:
        rule_id = request.path_params["rule_id"]
        if not book.remove(rule_id):
            return sim_error(404, "not_found", f"no rule with id {rule_id!r}")
        return Response(status_code=204)

    return [
        Route("/sim/llm/rules", listing, methods=["GET"]),
        Route("/sim/llm/rules", register, methods=["POST"]),
        Route("/sim/llm/rules", clear, methods=["DELETE"]),
        Route("/sim/llm/rules/{rule_id}", remove, methods=["DELETE"]),
    ]


def llm_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.llm

    async def requests(request: Request) -> Response:
        if request.method == "DELETE":
            store.requests.clear()
            return Response(status_code=204)
        return JSONResponse({"requests": [record.to_json() for record in store.requests]})

    def arm(body: LlmFaultBody) -> None:
        store.arm_fault(
            body.kind,
            times=body.times,
            status=body.status,
            retry_after_s=body.retry_after_s,
            after_tokens=body.after_tokens,
        )

    def clear_fault() -> None:
        store.fault = None

    async def api_keys(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse({"keys": list(store.api_keys)})
        body = await json_body(request, ApiKeyBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.add_key(body.api_key)
        return Response(status_code=204)

    async def models(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse({"models": list(store.models)})
        body = await json_body(request, ModelBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.add_model(body.id)
        return Response(status_code=204)

    return [
        Route("/sim/llm/requests", requests, methods=["GET", "DELETE"]),
        *_rules_routes(state),
        fault_route(
            "/sim/llm/fault",
            LlmFaultBody,
            armed=lambda: None if store.fault is None else store.fault.to_json(),
            arm=arm,
            clear=clear_fault,
        ),
        Route("/sim/llm/api-keys", api_keys, methods=["GET", "POST"]),
        Route("/sim/llm/models", models, methods=["GET", "POST"]),
    ]
