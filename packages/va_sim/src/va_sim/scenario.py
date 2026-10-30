"""Scenario DSL (specs/30-testing-and-evals.md §3.5): pydantic models, YAML loader, and precise errors.

A scenario tells FakeTelnyx how the far end of a call behaves: how it answers, what the caller says and when,
how transfer legs behave, which vendor latency profile applies, and what the e2e harness asserts afterwards.

Errors carry the dotted path of the offending field (``steps[2].say.text``) and the 1-based line and column of
its key in the YAML source, so ``str(ScenarioError)`` reads ``scenarios/x.yaml:14:7: steps[2].say.text: ...``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    Tag,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import ErrorDetails

__all__ = [
    "Answer",
    "Assertions",
    "Dtmf",
    "DtmfStep",
    "ExpectHangup",
    "ExpectHangupStep",
    "Hangup",
    "HangupStep",
    "Match",
    "Say",
    "SayStep",
    "Scenario",
    "ScenarioError",
    "ScenarioIssue",
    "Silence",
    "SilenceStep",
    "Step",
    "TransferTarget",
    "WaitAgentSpeech",
    "WaitAgentSpeechStep",
    "is_wav_path",
    "issues_from_validation",
    "load_scenario",
    "load_scenario_file",
    "load_scenarios",
    "scenario_json_schema",
]

_E164: Final = re.compile(r"^\+[1-9]\d{6,14}$")
# Discriminator tags of the step union; stripped from error paths (they are not keys in the document).
_STEP_TAG_PREFIX: Final = "step:"
_VALIDATOR_PREFIXES: Final = ("Value error, ", "Assertion failed, ")

type CallResult = Literal["answered", "busy", "no_answer", "rejected", "failed"]
type AmdResult = Literal["human_residence", "human_business", "machine", "silence", "fax_detected"]


def _e164(value: str) -> str:
    if not _E164.match(value):
        msg = f"must be an E.164 number such as +14155550101, got {value!r}"
        raise ValueError(msg)
    return value


E164 = Annotated[str, AfterValidator(_e164)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Match(_Model):
    """How FakeTelnyx binds the scenario: an outbound destination or an inbound caller number."""

    to: E164 | None = None
    inbound_from: E164 | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.to is None) == (self.inbound_from is None):
            msg = "set exactly one of `to` (outbound destination) or `inbound_from` (inbound caller)"
            raise ValueError(msg)
        return self


def _check_outcome(result: CallResult, ring_ms: int | None, ring_field: str) -> None:
    if result == "answered" and ring_ms is None:
        msg = f"`{ring_field}` is required when `result` is answered"
        raise ValueError(msg)
    if result != "answered" and ring_ms is not None:
        msg = f"`{ring_field}` is only allowed when `result` is answered (got result {result!r})"
        raise ValueError(msg)


class Answer(_Model):
    """How the destination of an outbound call answers."""

    after_ms: NonNegativeInt | None = None
    result: CallResult = "answered"
    answered_by: AmdResult | None = None
    beep_at_ms: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_outcome(self.result, self.after_ms, "after_ms")
        if self.result != "answered" and self.answered_by is not None:
            msg = "`answered_by` is only allowed when `result` is answered"
            raise ValueError(msg)
        if self.beep_at_ms is not None and self.answered_by != "machine":
            msg = "`beep_at_ms` is only allowed when `answered_by` is machine"
            raise ValueError(msg)
        return self


class WaitAgentSpeech(_Model):
    """Wait until the agent spoke for at least ``min_ms`` and then went quiet for ``then_silence_ms``."""

    min_ms: NonNegativeInt = 0
    then_silence_ms: NonNegativeInt = 700
    timeout_ms: PositiveInt = 10_000


class Say(_Model):
    """Caller utterance; ``barge_in_after_agent_ms`` starts it while the agent is still talking."""

    text: str = Field(min_length=1)
    language: str = Field(default="en", min_length=2)
    barge_in_after_agent_ms: NonNegativeInt | None = None


class Dtmf(_Model):
    """DTMF digits as Telnyx ``dtmf`` events (and in-band tones when ``inband``); ``w`` is a 500 ms pause."""

    digits: str = Field(pattern=r"^[0-9A-D*#w]+$")
    inband: bool = False


class Silence(_Model):
    """Caller stays silent for ``ms``."""

    ms: NonNegativeInt


class ExpectHangup(_Model):
    """The call must end (by ``by``) within ``timeout_ms``."""

    by: Literal["agent", "caller", "any"] = "any"
    timeout_ms: PositiveInt = 10_000


class Hangup(_Model):
    """The caller hangs up."""


class WaitAgentSpeechStep(_Model):
    key: ClassVar[str] = "wait_agent_speech"
    wait_agent_speech: WaitAgentSpeech


class SayStep(_Model):
    key: ClassVar[str] = "say"
    say: Say


class DtmfStep(_Model):
    key: ClassVar[str] = "dtmf"
    dtmf: Dtmf


class SilenceStep(_Model):
    key: ClassVar[str] = "silence"
    silence: Silence


class ExpectHangupStep(_Model):
    key: ClassVar[str] = "expect_hangup"
    expect_hangup: ExpectHangup


class HangupStep(_Model):
    key: ClassVar[str] = "hangup"
    hangup: Hangup


_STEP_MODELS: Final = (SayStep, WaitAgentSpeechStep, DtmfStep, SilenceStep, ExpectHangupStep, HangupStep)
_STEP_KEYS: Final = tuple(model.key for model in _STEP_MODELS)
_STEP_KEY_LIST: Final = ", ".join(_STEP_KEYS)


def _check_step(value: Any) -> Any:
    """Reject anything but a one-key mapping naming a known step, with a message naming the valid keys."""
    if isinstance(value, _STEP_MODELS):
        return value
    if not isinstance(value, Mapping) or len(value) != 1:
        msg = f"a step must have exactly one key, one of: {_STEP_KEY_LIST}"
        raise ValueError(msg)
    [key] = value
    if key not in _STEP_KEYS:
        msg = f"unknown step {key!r}, expected one of: {_STEP_KEY_LIST}"
        raise ValueError(msg)
    return value


def _step_tag(value: Any) -> str:
    key = value.key if isinstance(value, _STEP_MODELS) else next(iter(value))
    return f"{_STEP_TAG_PREFIX}{key}"


Step = Annotated[
    Annotated[SayStep, Tag(f"{_STEP_TAG_PREFIX}say")]
    | Annotated[WaitAgentSpeechStep, Tag(f"{_STEP_TAG_PREFIX}wait_agent_speech")]
    | Annotated[DtmfStep, Tag(f"{_STEP_TAG_PREFIX}dtmf")]
    | Annotated[SilenceStep, Tag(f"{_STEP_TAG_PREFIX}silence")]
    | Annotated[ExpectHangupStep, Tag(f"{_STEP_TAG_PREFIX}expect_hangup")]
    | Annotated[HangupStep, Tag(f"{_STEP_TAG_PREFIX}hangup")],
    Discriminator(_step_tag),
    BeforeValidator(_check_step),
]


class TransferTarget(_Model):
    """Behaviour of transfer legs created from this call."""

    answer_after_ms: NonNegativeInt | None = None
    result: CallResult = "answered"
    steps: tuple[Step, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_outcome(self.result, self.answer_after_ms, "answer_after_ms")
        return self


class Assertions(_Model):
    """Checks the e2e harness evaluates after the call completes; every field is optional."""

    call_status: str | None = None
    answered_by: Literal["human", "machine"] | None = None
    end_reason: str | None = None
    min_turns: NonNegativeInt | None = None
    transcript_contains: tuple[str, ...] = ()
    variables: dict[str, JsonValue] = Field(default_factory=dict)
    max_e2e_ms_p95: PositiveInt | None = None
    webhook_events: tuple[str, ...] = ()


class Scenario(_Model):
    """One scripted far end (specs/30 §3.5)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = ""
    match: Match
    answer: Answer | None = None
    greeting_audio: str | None = None
    llm_rules: tuple[str, ...] = ()
    steps: tuple[Step, ...] = ()
    transfer_target: TransferTarget | None = None
    profile: Literal["fast", "realistic"] = "fast"
    assertions: Assertions = Field(default_factory=Assertions)

    @field_validator("answer")
    @classmethod
    def _platform_answers_inbound(cls, answer: Answer | None, info: ValidationInfo) -> Answer | None:
        match = info.data.get("match")
        if answer is not None and isinstance(match, Match) and match.inbound_from is not None:
            msg = "inbound scenarios must not set `answer`: the platform answers inbound calls"
            raise ValueError(msg)
        return answer

    @property
    def inbound(self) -> bool:
        return self.match.inbound_from is not None


@dataclass(frozen=True, slots=True)
class ScenarioIssue:
    """One problem in a scenario document; ``line``/``column`` are 1-based, or None without a YAML source."""

    path: str
    line: int | None
    column: int | None
    message: str

    def render(self, source: str) -> str:
        where = source if self.line is None else f"{source}:{self.line}:{self.column}"
        return f"{where}: {self.path}: {self.message}" if self.path else f"{where}: {self.message}"


class ScenarioError(ValueError):
    """A scenario document that does not parse or validate; ``issues`` lists every problem found."""

    def __init__(self, source: str, issues: Sequence[ScenarioIssue]) -> None:
        self.source = source
        self.issues = tuple(issues)
        super().__init__(str(self))

    def __str__(self) -> str:
        return "\n".join(issue.render(self.source) for issue in self.issues)


def _path_of(loc: Sequence[int | str]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif not part.startswith(_STEP_TAG_PREFIX):
            path = f"{path}.{part}" if path else part
    return path


def _message_of(error: ErrorDetails) -> str:
    message = error["msg"]
    for prefix in _VALIDATOR_PREFIXES:
        message = message.removeprefix(prefix)
    return message


def _position(mark: yaml.Mark) -> tuple[int, int]:
    return mark.line + 1, mark.column + 1


def _locate(root: yaml.Node, loc: Sequence[int | str]) -> tuple[int, int]:
    """Line/column of the deepest node on ``loc``: a mapping entry's key, or a sequence item."""
    node = root
    position = _position(root.start_mark)
    for part in loc:
        if isinstance(part, str) and part.startswith(_STEP_TAG_PREFIX):
            continue
        child: tuple[yaml.Node, yaml.Node] | None = None
        if isinstance(node, yaml.MappingNode) and isinstance(part, str):
            child = next(((k, v) for k, v in node.value if getattr(k, "value", None) == part), None)
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and 0 <= part < len(node.value):
            item = node.value[part]
            child = (item, item)
        if child is None:
            break
        marker, node = child
        position = _position(marker.start_mark)
    return position


def issues_from_validation(
    error: ValidationError, root: yaml.Node | None = None
) -> tuple[ScenarioIssue, ...]:
    """Pydantic errors → issues; positions come from ``root`` (the composed YAML document) when given."""
    issues = []
    for detail in error.errors(include_url=False):
        line, column = _locate(root, detail["loc"]) if root is not None else (None, None)
        issues.append(ScenarioIssue(_path_of(detail["loc"]), line, column, _message_of(detail)))
    # Top to bottom in the document (pydantic reports in field declaration order); stable for equal positions.
    return tuple(sorted(issues, key=lambda issue: (issue.line or 0, issue.column or 0)))


def _walk_mappings(node: yaml.Node, path: str = "") -> Iterator[tuple[str, yaml.MappingNode]]:
    if isinstance(node, yaml.MappingNode):
        yield path, node
        for key, value in node.value:
            name = str(getattr(key, "value", ""))
            yield from _walk_mappings(value, f"{path}.{name}" if path else name)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            yield from _walk_mappings(item, f"{path}[{index}]")


def _duplicate_keys(root: yaml.Node) -> list[ScenarioIssue]:
    issues = []
    for path, mapping in _walk_mappings(root):
        seen: set[str] = set()
        for key, _ in mapping.value:
            if not isinstance(key, yaml.ScalarNode):
                continue
            if key.value in seen:
                line, column = _position(key.start_mark)
                where = f"{path}.{key.value}" if path else key.value
                issues.append(ScenarioIssue(where, line, column, f"duplicate key {key.value!r}"))
            seen.add(key.value)
    return issues


def _syntax_issue(exc: yaml.YAMLError) -> ScenarioIssue:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        line, column = _position(exc.problem_mark)
        parts = [part for part in (exc.context, exc.problem) if part]
        return ScenarioIssue("", line, column, "invalid YAML: " + ": ".join(parts))
    return ScenarioIssue("", None, None, f"invalid YAML: {exc}")


def _compose(text: str, source: str) -> yaml.Node:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ScenarioError(source, [_syntax_issue(exc)]) from exc
    if root is None:
        raise ScenarioError(source, [ScenarioIssue("", 1, 1, "the scenario document is empty")])
    if not isinstance(root, yaml.MappingNode):
        line, column = _position(root.start_mark)
        kind = "a sequence" if isinstance(root, yaml.SequenceNode) else "a scalar"
        raise ScenarioError(
            source, [ScenarioIssue("", line, column, f"the scenario must be a mapping, got {kind}")]
        )
    duplicates = _duplicate_keys(root)
    if duplicates:
        raise ScenarioError(source, duplicates)
    return root


def is_wav_path(greeting_audio: str) -> bool:
    """Whether a ``greeting_audio`` value names a WAV file (``.wav``, any case) rather than text to speak."""
    return greeting_audio.lower().endswith(".wav")


def _resolve(path: str, base_dir: Path) -> str:
    return path if Path(path).is_absolute() else (base_dir / path).resolve().as_posix()


def _resolve_paths(scenario: Scenario, base_dir: Path) -> Scenario:
    update: dict[str, Any] = {"llm_rules": tuple(_resolve(rule, base_dir) for rule in scenario.llm_rules)}
    greeting = scenario.greeting_audio
    if greeting is not None and is_wav_path(greeting):
        update["greeting_audio"] = _resolve(greeting, base_dir)
    return scenario.model_copy(update=update)


def load_scenario(text: str, *, source: str = "<string>", base_dir: Path | None = None) -> Scenario:
    """Parse and validate one YAML scenario; relative ``llm_rules`` and a relative WAV ``greeting_audio``
    resolve against ``base_dir`` when given.

    Raises :class:`ScenarioError` listing every issue with its path, line, and column.
    """
    root = _compose(text, source)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(source, [_syntax_issue(exc)]) from exc
    try:
        scenario = Scenario.model_validate(data)
    except ValidationError as exc:
        raise ScenarioError(source, issues_from_validation(exc, root)) from exc
    return _resolve_paths(scenario, base_dir) if base_dir is not None else scenario


def load_scenario_file(path: Path) -> Scenario:
    """Load ``path`` (blocking file read: async callers wrap this in ``asyncio.to_thread``)."""
    return load_scenario(path.read_text(encoding="utf-8"), source=str(path), base_dir=path.parent)


def load_scenarios(directory: Path) -> dict[str, Scenario]:
    """Every ``*.yaml``/``*.yml`` scenario in ``directory`` by id; ids must be unique across files."""
    scenarios: dict[str, Scenario] = {}
    origins: dict[str, Path] = {}
    for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
        scenario = load_scenario_file(path)
        if scenario.id in origins:
            message = f"duplicate scenario id {scenario.id!r}, already defined in {origins[scenario.id]}"
            raise ScenarioError(str(path), [ScenarioIssue("id", None, None, message)])
        scenarios[scenario.id] = scenario
        origins[scenario.id] = path
    return scenarios


def scenario_json_schema() -> dict[str, Any]:
    """JSON Schema of the DSL (docs and editor tooling)."""
    return Scenario.model_json_schema()
