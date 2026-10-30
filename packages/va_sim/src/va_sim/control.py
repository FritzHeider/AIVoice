"""Simulator control API under ``/sim/*`` (specs/30-testing-and-evals.md §3, §3.2, §3.3).

Every simulator serves ``POST /sim/reset``, ``GET /sim/clock``, and ``GET /sim/state``. FakeElevenLabs and
FakeLLM serve their latency config (``/sim/elevenlabs/config``, ``/sim/llm/config``). FakeTelnyx serves
scenario registration (``/sim/telnyx/scenarios``) and recordings (``/sim/recordings/{id}.wav``). Errors are
``{"error": {"code", "message", "issues"?}}``; scenario issues are ``{path, line, column, message}``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Final

import orjson
from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveFloat, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.scenario import Scenario, ScenarioError, ScenarioIssue, issues_from_validation, load_scenario
from va_sim.settings import ProfileName, SimName
from va_sim.state import SimState

__all__ = ["YAML_MEDIA_TYPES", "control_routes", "invalid_request", "json_body", "sim_error"]

YAML_MEDIA_TYPES: Final = frozenset({"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml"})
_JSON_MEDIA_TYPE: Final = "application/json"
_SCENARIO_SOURCE: Final = "request"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResetBody(_Strict):
    seed: int | None = None


class ProfileBody(_Strict):
    profile: ProfileName


class ElevenLabsConfig(_Strict):
    tts_ttfa_ms: NonNegativeInt | None = None
    stt_final_ms: NonNegativeInt | None = None


class LlmConfig(_Strict):
    ttft_ms: NonNegativeInt | None = None
    tokens_per_s: PositiveFloat | None = None


_CONFIG_MODELS: Final[Mapping[SimName, type[_Strict]]] = {"elevenlabs": ElevenLabsConfig, "llm": LlmConfig}


def sim_error(status: int, code: str, message: str, issues: Sequence[ScenarioIssue] = ()) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if issues:
        error["issues"] = [asdict(issue) for issue in issues]
    return JSONResponse({"error": error}, status_code=status)


def invalid_request(message: str, issues: Sequence[ScenarioIssue]) -> JSONResponse:
    return sim_error(422, "invalid_request", message, issues)


async def json_body[M: BaseModel](request: Request, model: type[M], *, allow_empty: bool) -> M | JSONResponse:
    raw = await request.body()
    if not raw.strip() and allow_empty:
        return model()
    try:
        return model.model_validate(orjson.loads(raw))
    except orjson.JSONDecodeError as exc:
        return invalid_request(
            "the request body is not valid JSON", [ScenarioIssue("", None, None, str(exc))]
        )
    except ValidationError as exc:
        return invalid_request("the request body is invalid", issues_from_validation(exc))


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _common_routes(state: SimState) -> list[BaseRoute]:
    async def reset(request: Request) -> Response:
        body = await json_body(request, ResetBody, allow_empty=True)
        if isinstance(body, JSONResponse):
            return body
        state.reset(body.seed)
        return Response(status_code=204)

    async def clock(_request: Request) -> Response:
        return JSONResponse(
            {
                "mode": state.clock.mode,
                "now": iso_z(state.clock.now()),
                "virtual_ms": state.clock.virtual_ms(),
            }
        )

    async def snapshot(_request: Request) -> Response:
        return JSONResponse(state.snapshot())

    return [
        Route("/sim/reset", reset, methods=["POST"]),
        Route("/sim/clock", clock, methods=["GET"]),
        Route("/sim/state", snapshot, methods=["GET"]),
    ]


def _config_routes(sim: SimName, state: SimState) -> list[BaseRoute]:
    model = _CONFIG_MODELS.get(sim)
    if model is None:
        return []
    fields = tuple(model.model_fields)

    def current() -> JSONResponse:
        latencies = asdict(state.latencies)
        return JSONResponse({name: latencies[name] for name in fields})

    async def read(_request: Request) -> Response:
        return current()

    async def patch(request: Request) -> Response:
        body = await json_body(request, model, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        state.update_latencies(**body.model_dump(exclude_none=True))
        return current()

    async def profile(request: Request) -> Response:
        body = await json_body(request, ProfileBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        state.apply_profile(body.profile)
        return current()

    return [
        Route(f"/sim/{sim}/config", read, methods=["GET"]),
        Route(f"/sim/{sim}/config", patch, methods=["PATCH"]),
        Route(f"/sim/{sim}/config/profile", profile, methods=["POST"]),
    ]


async def _parse_scenario(request: Request) -> Scenario | JSONResponse:
    media_type = _media_type(request)
    raw = await request.body()
    if media_type in YAML_MEDIA_TYPES:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return invalid_request(
                "the scenario is not UTF-8 text", [ScenarioIssue("", None, None, str(exc))]
            )
        try:
            return await asyncio.to_thread(load_scenario, text, source=_SCENARIO_SOURCE)
        except ScenarioError as exc:
            return invalid_request("the scenario is invalid", exc.issues)
    if media_type == _JSON_MEDIA_TYPE:
        return await json_body(request, Scenario, allow_empty=False)
    accepted = ", ".join(sorted({*YAML_MEDIA_TYPES, _JSON_MEDIA_TYPE}))
    return sim_error(415, "unsupported_media_type", f"send the scenario as one of: {accepted}")


def _telnyx_routes(state: SimState) -> list[BaseRoute]:
    async def register(request: Request) -> Response:
        scenario = await _parse_scenario(request)
        if isinstance(scenario, JSONResponse):
            return scenario
        state.register_scenario(scenario)
        return JSONResponse(
            {"id": scenario.id, "match": scenario.match.model_dump(mode="json")}, status_code=201
        )

    async def listing(_request: Request) -> Response:
        scenarios = [state.scenarios[key].model_dump(mode="json") for key in sorted(state.scenarios)]
        return JSONResponse({"scenarios": scenarios})

    async def detail(request: Request) -> Response:
        scenario_id = request.path_params["scenario_id"]
        scenario = state.scenarios.get(scenario_id)
        if scenario is None:
            return sim_error(404, "not_found", f"no scenario with id {scenario_id!r}")
        return JSONResponse(scenario.model_dump(mode="json"))

    async def recording(request: Request) -> Response:
        recording_id = request.path_params["recording_id"]
        wav = state.recordings.get(recording_id)
        if wav is None:
            return sim_error(404, "not_found", f"no recording with id {recording_id!r}")
        return Response(wav, media_type="audio/wav")

    return [
        Route("/sim/telnyx/scenarios", register, methods=["POST"]),
        Route("/sim/telnyx/scenarios", listing, methods=["GET"]),
        Route("/sim/telnyx/scenarios/{scenario_id}", detail, methods=["GET"]),
        Route("/sim/recordings/{recording_id}.wav", recording, methods=["GET"]),
    ]


def control_routes(sim: SimName, state: SimState) -> list[BaseRoute]:
    """The ``/sim/*`` routes simulator ``sim`` serves over the shared ``state``."""
    routes = [*_common_routes(state), *_config_routes(sim, state)]
    if sim == "telnyx":
        routes.extend(_telnyx_routes(state))
    return routes
