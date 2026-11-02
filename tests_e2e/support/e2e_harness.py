"""The e2e harness: drives the compose ``e2e`` stack through the public API and the simulators' control APIs
(specs/30 §3.5 harness; Decision P05-T17b under specs/30 §5).

One :class:`Harness` per test. It never calls ``/sim/reset`` (that would drop the platform Call Control
application the stack was bootstrapped with); isolation comes from a per-test ``run_token``: scenario ids are
``<name>-<token>``, every scenario is bound to a fresh random number, and LLM rule files get ``{{run}}`` (rule
ids) and ``{{marker}}`` (the scenario marker ``[[SCENARIO:<name>-<token>]]`` the call's prompt carries)
substituted. :meth:`Harness.aclose` deletes the rules it registered and releases the simulator numbers it
ordered (the scenario API has no delete; unique ids and numbers keep old scenarios inert).

Waits poll an external system on the wall clock with a bounded deadline and fail with the last observed value.
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import time
import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx
import phonenumbers
import yaml
from e2e_assertions import evaluate

from va_domain.org import OrgSettings
from va_sim.elevenlabs.voice_catalog import KAREN_VOICE_ID
from va_sim.scenario import Scenario, load_scenario
from va_telnyx.bootstrap import platform_resource_name

__all__ = [
    "SCENARIOS_DIR",
    "SIM_TELNYX_KEY",
    "E2eOrg",
    "FinishedCall",
    "Harness",
    "RegisteredScenario",
    "StackUrls",
    "WaitTimeoutError",
    "compose_container",
    "docker",
    "iso_ms",
    "prepare_rules",
    "prepare_scenario",
    "random_e164",
    "scenario_marker",
    "wav_channels",
]

SCENARIOS_DIR: Final = Path(__file__).resolve().parents[1] / "scenarios"
SIM_TELNYX_KEY: Final = "sim-telnyx-key"
SEED_PATH: Final = "/internal/test/orgs"
HTTP_TIMEOUT_S: Final = 30.0
POLL_S: Final = 0.25
DEFAULT_WAIT_S: Final = 30.0
COMPLETE_WAIT_S: Final = 180.0
ORDER_WAIT_S: Final = 20.0
DOCKER_TIMEOUT_S: Final = 60.0
RESTART_WAIT_S: Final = 120.0
COMPOSE_PROJECT: Final = os.environ.get("E2E_COMPOSE_PROJECT", "voiceagents")
MEDIA_SERVICE: Final = "media"
VA_ENV: Final = os.environ.get("E2E_VA_ENV", "local")
_RUN_FIELD: Final = "{{run}}"
_MARKER_FIELD: Final = "{{marker}}"
# NANP area codes used for random numbers: destinations in San Francisco (a state with quiet-hour rules),
# caller IDs in 628 (same metro area).
DESTINATION_NPA: Final = "415"
CALLER_ID_NPA: Final = "628"


class WaitTimeoutError(AssertionError):
    """A harness wait ran out of time; the message carries the last observed value."""


def random_e164(npa: str = DESTINATION_NPA) -> str:
    """A random valid ``+1`` number in area code ``npa`` (exchange 200-999)."""
    while True:
        exchange = 200 + secrets.randbelow(800)
        candidate = f"+1{npa}{exchange}{secrets.randbelow(10_000):04d}"
        if phonenumbers.is_valid_number(phonenumbers.parse(candidate)):
            return candidate


def scenario_marker(scenario_id: str) -> str:
    """The prompt marker FakeLLM rules match on."""
    return f"[[SCENARIO:{scenario_id}]]"


def prepare_scenario(text: str, *, name: str, run: str, number: str) -> Scenario:
    """The scenario of ``text`` with id ``<name>-<run>`` and its ``match`` number replaced by ``number``.

    ``text`` is validated first as written (positioned errors), then again after the rewrite.
    """
    load_scenario(text, source=f"{name}.yaml", base_dir=SCENARIOS_DIR)
    document: dict[str, Any] = yaml.safe_load(text)
    document["id"] = f"{name}-{run}"
    match = document["match"]
    key = "inbound_from" if match.get("inbound_from") is not None else "to"
    document["match"] = {key: number}
    return Scenario.model_validate(document)


def prepare_rules(text: str, *, run: str, marker: str) -> str:
    """A FakeLLM rule file with ``{{run}}`` → ``run`` and ``{{marker}}`` → ``marker``."""
    return text.replace(_RUN_FIELD, run).replace(_MARKER_FIELD, marker)


def iso_ms(at: str) -> int:
    """A simulator ISO-8601 timestamp (``Z`` or an offset) as epoch milliseconds."""
    moment = datetime.fromisoformat(at)
    if moment.tzinfo is None:
        message = f"timestamp {at!r} has no time zone"
        raise ValueError(message)
    return round(moment.timestamp() * 1000)


def wav_channels(wav: bytes) -> tuple[int, int]:
    """``(channels, frames)`` of a PCM WAV file."""
    with wave.open(io.BytesIO(wav)) as reader:
        return reader.getnchannels(), reader.getnframes()


@dataclass(frozen=True, slots=True)
class StackUrls:
    """Host URLs of the stack (``E2E_*_URL`` environment overrides)."""

    api: str
    telnyx: str
    elevenlabs: str
    llm: str
    edge: str
    media: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> StackUrls:
        def url(name: str, default: str) -> str:
            return environ.get(name, default).rstrip("/")

        return cls(
            api=url("E2E_API_URL", "http://localhost:8000"),
            telnyx=url("E2E_SIM_URL", "http://localhost:9100"),
            elevenlabs=url("E2E_ELEVENLABS_URL", "http://localhost:9200"),
            llm=url("E2E_LLM_URL", "http://localhost:9300"),
            edge=url("E2E_EDGE_URL", "http://localhost:8100"),
            media=url("E2E_MEDIA_URL", "http://localhost:8200"),
        )

    def probes(self) -> list[str]:
        """The URLs that must answer 200 before any flow runs."""
        return [
            f"{self.api}/readyz",
            f"{self.edge}/readyz",
            f"{self.media}/readyz",
            f"{self.telnyx}/sim/state",
        ]


@dataclass(frozen=True, slots=True)
class E2eOrg:
    """A seeded org: id, live ``*`` key, caller-ID number, and voice name."""

    org_id: str
    api_key: str = field(repr=False)
    number: str
    voice: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass(frozen=True, slots=True)
class RegisteredScenario:
    """A scenario registered with FakeTelnyx under a run-unique id and number."""

    scenario: Scenario
    rule_ids: tuple[str, ...]

    @property
    def id(self) -> str:
        return self.scenario.id

    @property
    def marker(self) -> str:
        return scenario_marker(self.scenario.id)

    @property
    def number(self) -> str:
        match = self.scenario.match
        number = match.inbound_from if match.inbound_from is not None else match.to
        if number is None:
            message = "a registered scenario always has a match number"
            raise AssertionError(message)
        return number


@dataclass(frozen=True, slots=True)
class FinishedCall:
    """A completed call: its Call Object, event log, and FakeTelnyx timeline."""

    call_id: str
    call: dict[str, Any]
    events: list[dict[str, Any]]
    timeline: dict[str, Any]

    def failures(self, scenario: RegisteredScenario) -> list[str]:
        """The scenario's failed ``assertions`` (empty when all hold)."""
        return evaluate(scenario.scenario.assertions, self.call, self.events, self.timeline)


async def _poll[T](
    read: Callable[[], Awaitable[T]], done: Callable[[T], bool], *, timeout_s: float, what: str
) -> T:
    deadline = time.monotonic() + timeout_s
    while True:
        value = await read()
        if done(value):
            return value
        if time.monotonic() >= deadline:
            message = f"timed out after {timeout_s} s waiting for {what}; last value: {value!r}"
            raise WaitTimeoutError(message)
        await asyncio.sleep(POLL_S)


async def docker(*args: str) -> str:
    """Run ``docker *args`` (bounded by :data:`DOCKER_TIMEOUT_S`); its stdout (AssertionError on failure)."""
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(DOCKER_TIMEOUT_S):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        message = f"docker {' '.join(args)} exited {process.returncode}: {stderr.decode(errors='replace')}"
        raise AssertionError(message)
    return stdout.decode()


async def compose_container(service: str, *, running_only: bool) -> str:
    """The id of the compose ``service``'s single container (found by the compose labels)."""
    listing = "-q" if running_only else "-aq"
    ids = (
        await docker(
            "ps",
            listing,
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        )
    ).split()
    if len(ids) != 1:
        message = f"expected one {'running ' if running_only else ''}{service} container, found {ids}"
        raise AssertionError(message)
    return ids[0]


def _ok(response: httpx.Response, *statuses: int) -> httpx.Response:
    wanted = statuses or (200,)
    if response.status_code not in wanted:
        message = (
            f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text}"
        )
        raise AssertionError(message)
    return response


class Harness:
    """Per-test access to the stack; create with :meth:`open` (an async context manager)."""

    def __init__(self, urls: StackUrls, http: httpx.AsyncClient, token: str) -> None:
        self.urls = urls
        self.http = http
        self.run_token = token
        self._rule_ids: list[str] = []
        self._sim_numbers: list[str] = []
        self._tts_baseline = 0

    @classmethod
    def open(cls, urls: StackUrls) -> _HarnessScope:
        return _HarnessScope(urls)

    async def aclose(self) -> None:
        """Delete this run's LLM rules and release its simulator numbers (best effort, reports failures)."""
        problems: list[str] = []
        for rule_id in self._rule_ids:
            response = await self.http.delete(f"{self.urls.llm}/sim/llm/rules/{rule_id}")
            if response.status_code not in {204, 404}:
                problems.append(f"rule {rule_id}: {response.status_code}")
        for number_id in self._sim_numbers:
            response = await self.http.delete(
                f"{self.urls.telnyx}/v2/phone_numbers/{number_id}", headers=_telnyx()
            )
            if response.status_code not in {200, 404}:
                problems.append(f"number {number_id}: {response.status_code}")
        if problems:
            message = f"harness cleanup failed: {problems}"
            raise AssertionError(message)

    # --- seeding -------------------------------------------------------------------------------------------

    async def create_org(
        self, *, numbers: Sequence[Mapping[str, Any]] = (), settings: Mapping[str, Any] | None = None
    ) -> E2eOrg:
        """A funded org with the default voice (FakeElevenLabs' Karen), a caller ID, and ``numbers``."""
        voice = OrgSettings().default_voice
        caller_id = random_e164(CALLER_ID_NPA)
        body = {
            "name": f"E2E {self.run_token}",
            "settings": dict(settings or {}),
            "voices": [{"name": voice, "provider_voice_id": KAREN_VOICE_ID}],
            "numbers": [{"e164": caller_id}, *numbers],
        }
        data = _ok(await self.http.post(f"{self.urls.api}{SEED_PATH}", json=body), 201).json()["data"]
        return E2eOrg(org_id=data["org_id"], api_key=data["api_key"], number=caller_id, voice=voice)

    async def load_scenario(self, name: str, *, number: str | None = None) -> RegisteredScenario:
        """Register ``scenarios/<name>.yaml`` (run-unique id and number) and its LLM rule files; apply its
        latency profile to the vendor simulators; remember the TTS log position for :meth:`tts_texts`."""
        text = (SCENARIOS_DIR / f"{name}.yaml").read_text(encoding="utf-8")
        scenario = prepare_scenario(text, name=name, run=self.run_token, number=number or random_e164())
        marker = scenario_marker(scenario.id)
        rule_ids: list[str] = []
        for rule_file in scenario.llm_rules:
            rule_text = (SCENARIOS_DIR / rule_file).read_text(encoding="utf-8")
            rules = prepare_rules(rule_text, run=self.run_token, marker=marker)
            response = await self.http.post(
                f"{self.urls.llm}/sim/llm/rules", content=rules, headers={"Content-Type": "application/yaml"}
            )
            ids = _ok(response, 201).json()["ids"]
            rule_ids.extend(ids)
            self._rule_ids.extend(ids)
        document = scenario.model_dump(mode="json", exclude={"llm_rules"}, exclude_none=True)
        _ok(await self.http.post(f"{self.urls.telnyx}/sim/telnyx/scenarios", json=document), 201)
        profile = {"profile": scenario.profile}
        _ok(await self.http.post(f"{self.urls.llm}/sim/llm/config/profile", json=profile))
        self._tts_baseline = len(await self._tts_records())
        return RegisteredScenario(scenario=scenario, rule_ids=tuple(rule_ids))

    # --- calls ---------------------------------------------------------------------------------------------

    async def send_call(self, org: E2eOrg, body: Mapping[str, Any]) -> str:
        """``POST /v1/calls``; the new call id."""
        response = _ok(
            await self.http.post(f"{self.urls.api}/v1/calls", json=dict(body), headers=org.headers)
        )
        call_id: str = response.json()["call_id"]
        return call_id

    async def send_task_call(
        self, org: E2eOrg, scenario: RegisteredScenario, task: str, **parameters: Any
    ) -> str:
        """A task call from the org's number to the scenario's number; ``task`` gets the scenario marker."""
        body = {
            "phone_number": scenario.number,
            "from": org.number,
            "task": f"{task} {scenario.marker}",
            "voice": org.voice,
            **parameters,
        }
        return await self.send_call(org, body)

    async def finished(
        self, org: E2eOrg, call_id: str, *, ccid: str | None = None, number: str | None = None
    ) -> FinishedCall:
        """Wait for post-call completion, then read the event log and the timeline of the leg ``ccid`` (or of
        the newest simulator call of ``number``)."""
        call = await self.wait_complete(org, call_id)
        events = await self.events(org, call_id)
        if ccid is None:
            if number is None:
                message = "finished() needs the leg's ccid or the scenario number"
                raise ValueError(message)
            ccid = (await self.sim_call(number))["call_control_id"]
        return FinishedCall(call_id, call, events, await self.timeline(ccid))

    async def finished_leg(
        self, org: E2eOrg, call_id: str, number: str
    ) -> tuple[FinishedCall, dict[str, Any]]:
        """:meth:`finished` for the newest simulator call of ``number``, plus that leg's call summary."""
        ccid = (await self.sim_call(number))["call_control_id"]
        finished = await self.finished(org, call_id, ccid=ccid)
        return finished, await self.sim_summary(ccid)

    async def call_object(self, org: E2eOrg, call_id: str) -> dict[str, Any]:
        response = await self.http.get(f"{self.urls.api}/v1/calls/{call_id}", headers=org.headers)
        body: dict[str, Any] = _ok(response).json()
        return body

    async def wait_complete(
        self, org: E2eOrg, call_id: str, *, timeout_s: float = COMPLETE_WAIT_S
    ) -> dict[str, Any]:
        """The Call Object once post-call processing set ``queue_status`` to ``complete``."""
        return await self.wait_call(
            org,
            call_id,
            lambda call: call.get("queue_status") == "complete",
            what="queue_status=complete",
            timeout_s=timeout_s,
        )

    async def wait_call(
        self,
        org: E2eOrg,
        call_id: str,
        done: Callable[[dict[str, Any]], bool],
        *,
        what: str,
        timeout_s: float = DEFAULT_WAIT_S,
    ) -> dict[str, Any]:
        """The Call Object once ``done`` holds."""
        return await _poll(
            lambda: self.call_object(org, call_id), done, timeout_s=timeout_s, what=f"call {call_id} {what}"
        )

    async def stop_call(self, org: E2eOrg, call_id: str) -> None:
        _ok(await self.http.post(f"{self.urls.api}/v1/calls/{call_id}/stop", headers=org.headers))

    async def only_call(self, org: E2eOrg, *, timeout_s: float = DEFAULT_WAIT_S) -> str:
        """The id of the org's single call (``GET /v1/calls``), waiting until it exists."""

        async def calls() -> list[dict[str, Any]]:
            response = await self.http.get(f"{self.urls.api}/v1/calls", headers=org.headers)
            found: list[dict[str, Any]] = _ok(response).json()["calls"]
            return found

        [call] = await _poll(calls, lambda found: len(found) == 1, timeout_s=timeout_s, what="the org's call")
        call_id: str = call["call_id"]
        return call_id

    async def events(self, org: E2eOrg, call_id: str) -> list[dict[str, Any]]:
        """Every entry of the call's event log, following ``next_after_seq``."""
        collected: list[dict[str, Any]] = []
        after = 0
        while True:
            response = await self.http.get(
                f"{self.urls.api}/v1/calls/{call_id}/events",
                params={"after_seq": after, "limit": 200},
                headers=org.headers,
            )
            page = _ok(response).json()
            collected.extend(page["events"])
            if page["next_after_seq"] is None:
                return collected
            after = page["next_after_seq"]

    async def recording_wav(self, url: str) -> bytes:
        """Download a presigned recording URL from the host.

        Inside the stack the object store is ``minio:9000``, which the host cannot resolve; SigV4 signs the
        ``Host`` header, so the request goes to ``localhost`` with the original ``Host`` header kept.
        """
        parts = urlsplit(url)
        local = urlunsplit(parts._replace(netloc=f"localhost:{parts.port or 80}"))
        response = await self.http.get(local, headers={"Host": parts.netloc})
        return _ok(response).content

    # --- containers ----------------------------------------------------------------------------------------

    async def kill_media(self) -> int:
        """SIGKILL the media worker container; the sim clock read right after ``docker kill`` returned."""
        container = await compose_container(MEDIA_SERVICE, running_only=True)
        await docker("kill", "--signal", "KILL", container)
        return await self.sim_clock_ms()

    async def restart_media(self, *, timeout_s: float = RESTART_WAIT_S) -> None:
        """Start the media worker container again (a running one is fine) and wait for ``/readyz`` 200."""
        container = await compose_container(MEDIA_SERVICE, running_only=False)
        await docker("start", container)

        async def ready() -> int:
            try:
                return (await self.http.get(f"{self.urls.media}/readyz")).status_code
            except httpx.TransportError:
                return 0

        await _poll(ready, lambda status: status == 200, timeout_s=timeout_s, what="media /readyz")

    # --- simulators ----------------------------------------------------------------------------------------

    async def sim_calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = _ok(await self.http.get(f"{self.urls.telnyx}/sim/telnyx/calls")).json()[
            "calls"
        ]
        return calls

    async def sim_call(self, number: str, *, timeout_s: float = DEFAULT_WAIT_S) -> dict[str, Any]:
        """The newest FakeTelnyx call to or from ``number`` (waits until one exists)."""

        async def newest() -> dict[str, Any] | None:
            matching = [c for c in await self.sim_calls() if number in {c["to"], c["from"]}]
            return matching[-1] if matching else None

        found = await _poll(
            newest, lambda call: call is not None, timeout_s=timeout_s, what=f"a sim call of {number}"
        )
        if found is None:
            message = "unreachable: _poll returned a missing call"
            raise AssertionError(message)
        return found

    async def sim_summary(self, ccid: str) -> dict[str, Any]:
        summary: dict[str, Any] = _ok(
            await self.http.get(f"{self.urls.telnyx}/sim/telnyx/calls/{ccid}")
        ).json()
        return summary

    async def remote_hangup(self, ccid: str) -> None:
        """The far end of the FakeTelnyx leg ``ccid`` hangs up (``POST /sim/telnyx/calls/{ccid}/hangup``)."""
        response = await self.http.post(f"{self.urls.telnyx}/sim/telnyx/calls/{ccid}/hangup", json={})
        _ok(response, 202)

    async def wait_sim(
        self,
        ccid: str,
        done: Callable[[dict[str, Any]], bool],
        *,
        what: str,
        timeout_s: float = DEFAULT_WAIT_S,
    ) -> dict[str, Any]:
        """The call summary once ``done`` holds."""
        return await _poll(lambda: self.sim_summary(ccid), done, timeout_s=timeout_s, what=what)

    async def timeline(self, ccid: str) -> dict[str, Any]:
        response = await self.http.get(f"{self.urls.telnyx}/sim/telnyx/calls/{ccid}/timeline")
        timeline: dict[str, Any] = _ok(response).json()
        return timeline

    async def sim_clock_ms(self) -> int:
        virtual_ms: int = _ok(await self.http.get(f"{self.urls.telnyx}/sim/clock")).json()["virtual_ms"]
        return virtual_ms

    async def wait_until_sim_ms(self, virtual_ms: int, *, timeout_s: float = DEFAULT_WAIT_S) -> int:
        """Wait until the simulator clock reaches ``virtual_ms``; the reading that did."""
        return await _poll(
            self.sim_clock_ms,
            lambda now: now >= virtual_ms,
            timeout_s=timeout_s,
            what=f"sim clock {virtual_ms} ms",
        )

    async def _tts_records(self) -> list[dict[str, Any]]:
        response = await self.http.get(f"{self.urls.elevenlabs}/sim/elevenlabs/tts/requests")
        records: list[dict[str, Any]] = _ok(response).json()["requests"]
        return records

    async def tts_texts(self) -> list[str]:
        """Non-empty texts FakeElevenLabs synthesized since the scenario was loaded, in order."""
        records = (await self._tts_records())[self._tts_baseline :]
        texts = [str(record.get("generated_text") or "").strip() for record in records]
        return [text for text in texts if text]

    async def llm_requests(self, call_id: str) -> list[dict[str, Any]]:
        """FakeLLM requests that carried ``X-VA-Call-Id: call_id``."""
        response = await self.http.get(f"{self.urls.llm}/sim/llm/requests")
        requests: list[dict[str, Any]] = _ok(response).json()["requests"]
        return [request for request in requests if request.get("call_id") == call_id]

    async def order_inbound_number(self) -> tuple[str, str]:
        """Order an available FakeTelnyx number onto the platform Call Control application; ``(e164, id)``.

        The number is released again in :meth:`aclose`.
        """
        app_id = await self._platform_app_id()
        search = await self.http.get(
            f"{self.urls.telnyx}/v2/available_phone_numbers",
            params={"filter[country_code]": "US", "filter[limit]": 1},
            headers=_telnyx(),
        )
        [available] = _ok(search).json()["data"]
        e164: str = available["phone_number"]
        order_body = {"phone_numbers": [{"phone_number": e164}], "connection_id": app_id}
        order = _ok(
            await self.http.post(f"{self.urls.telnyx}/v2/number_orders", json=order_body, headers=_telnyx())
        ).json()["data"]

        async def order_status() -> str:
            response = await self.http.get(
                f"{self.urls.telnyx}/v2/number_orders/{order['id']}", headers=_telnyx()
            )
            status: str = _ok(response).json()["data"]["status"]
            return status

        status = await _poll(
            order_status, lambda s: s != "pending", timeout_s=ORDER_WAIT_S, what="the number order"
        )
        if status != "success":
            message = f"number order for {e164} ended {status}"
            raise AssertionError(message)
        numbers = await self.http.get(
            f"{self.urls.telnyx}/v2/phone_numbers", params={"filter[phone_number]": e164}, headers=_telnyx()
        )
        [number] = _ok(numbers).json()["data"]
        number_id: str = number["id"]
        self._sim_numbers.append(number_id)
        return e164, number_id

    async def _platform_app_id(self) -> str:
        name = platform_resource_name(VA_ENV)
        response = await self.http.get(
            f"{self.urls.telnyx}/v2/call_control_applications",
            params={"filter[application_name][contains]": name},
            headers=_telnyx(),
        )
        apps = [app for app in _ok(response).json()["data"] if app["application_name"] == name]
        if len(apps) != 1:
            message = (
                f"expected one Call Control application named {name!r}, found {len(apps)} (run make e2e)"
            )
            raise AssertionError(message)
        app_id: str = apps[0]["id"]
        return app_id

    async def inbound(self, to: str, from_: str, scenario_id: str) -> str:
        """``POST /sim/telnyx/inbound``: the caller ``from_`` rings ``to``; the new leg's call control id."""
        body = {"to": to, "from": from_, "scenario": scenario_id}
        response = await self.http.post(f"{self.urls.telnyx}/sim/telnyx/inbound", json=body)
        ccid: str = _ok(response, 202).json()["call_control_id"]
        return ccid


def _telnyx() -> dict[str, str]:
    return {"Authorization": f"Bearer {SIM_TELNYX_KEY}"}


class _HarnessScope:
    def __init__(self, urls: StackUrls) -> None:
        self._urls = urls
        self._http: httpx.AsyncClient | None = None
        self._harness: Harness | None = None

    async def __aenter__(self) -> Harness:
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        self._harness = Harness(self._urls, self._http, secrets.token_hex(4))
        return self._harness

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            if self._harness is not None:
                await self._harness.aclose()
        finally:
            if self._http is not None:
                await self._http.aclose()
