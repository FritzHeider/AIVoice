"""FakeLLM rules engine (specs/30 §3.4 rules engine; Decision P03-T06a rule 9).

Rules come from YAML files (``SIM_LLM_RULES``, kept across resets) and from ``POST /sim/llm/rules`` (runtime
rules, dropped by reset). The first matching rule wins, ordered by ``priority`` (descending) and then by
registration order. A ``call_index`` rule fires only on the n-th (0-based) request that met its other
conditions, counted per (rule id, ``X-VA-Call-Id``); every rule whose other conditions hold is counted, even
when an earlier rule wins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    ValidationError,
    field_validator,
)

from va_sim.scenario import ScenarioError, compose_document, issues_from_validation

__all__ = [
    "RUNTIME_SOURCE",
    "InjectedError",
    "LoadedRule",
    "MatchContext",
    "Rule",
    "RuleBook",
    "RuleFile",
    "RuleMatch",
    "RuleResponse",
    "RuleToolCall",
    "RulesError",
    "load_rule_file",
    "load_rules",
]

RUNTIME_SOURCE: Final = "runtime"
_MIN_ERROR_STATUS: Final = 400
_MAX_ERROR_STATUS: Final = 599


class RulesError(ScenarioError):
    """A rule document that does not parse or validate; ``issues`` carry path, line, column, message."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleMatch(_Strict):
    system_contains: tuple[str, ...] = ()
    last_user_regex: str | None = None
    tool_name: str | None = None
    model: str | None = None
    call_index: NonNegativeInt | None = None
    call_id: str | None = None

    @field_validator("last_user_regex")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                msg = f"invalid regular expression: {exc}"
                raise ValueError(msg) from exc
        return value


class RuleToolCall(_Strict):
    name: Annotated[str, Field(min_length=1)]
    # An object is sent as JSON; a string is sent verbatim (lets rules inject malformed arguments).
    arguments: dict[str, JsonValue] | str = Field(default_factory=dict)


class InjectedError(_Strict):
    status: Annotated[int, Field(ge=_MIN_ERROR_STATUS, le=_MAX_ERROR_STATUS)]
    body: JsonValue = None
    headers: dict[str, str] = Field(default_factory=dict)


class RuleResponse(_Strict):
    content: str | None = None
    tool_calls: tuple[RuleToolCall, ...] = ()
    ttft_ms: NonNegativeInt | None = None
    delay_ms: NonNegativeInt = 0
    error: InjectedError | None = None


class Rule(_Strict):
    id: Annotated[str, Field(min_length=1)]
    priority: int = 0
    match: RuleMatch = RuleMatch()
    respond: RuleResponse = RuleResponse()


class RuleFile(_Strict):
    rules: tuple[Rule, ...]

    @field_validator("rules")
    @classmethod
    def _unique_ids(cls, rules: tuple[Rule, ...]) -> tuple[Rule, ...]:
        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                msg = f"duplicate rule id {rule.id!r}"
                raise ValueError(msg)
            seen.add(rule.id)
        return rules


def load_rules(text: str, *, source: str = "<string>") -> RuleFile:
    """Parse and validate one YAML rule document; raises :class:`RulesError` listing every issue."""
    try:
        root, data = compose_document(text, source=source, what="rules")
    except ScenarioError as exc:
        raise RulesError(source, exc.issues) from exc
    try:
        return RuleFile.model_validate(data)
    except ValidationError as exc:
        raise RulesError(source, issues_from_validation(exc, root)) from exc


def load_rule_file(path: Path) -> RuleFile:
    """Load ``path`` (blocking file read: async callers wrap this in ``asyncio.to_thread``)."""
    return load_rules(path.read_text(encoding="utf-8"), source=str(path))


@dataclass(frozen=True, slots=True)
class MatchContext:
    """What a request offers the matcher."""

    model: str
    system_text: str
    last_user_text: str | None
    forced_tool: str | None
    tool_names: tuple[str, ...]
    call_id: str | None


@dataclass(frozen=True, slots=True)
class LoadedRule:
    rule: Rule
    source: str
    pattern: re.Pattern[str] | None

    def conditions_hold(self, context: MatchContext) -> bool:
        """Every ``match`` condition except ``call_index``."""
        match = self.rule.match
        checks = (
            match.model is None or match.model == context.model,
            match.call_id is None or match.call_id == context.call_id,
            all(needle in context.system_text for needle in match.system_contains),
            self.pattern is None
            or (
                context.last_user_text is not None and self.pattern.search(context.last_user_text) is not None
            ),
            match.tool_name is None or self._tool_matches(match.tool_name, context),
        )
        return all(checks)

    @staticmethod
    def _tool_matches(name: str, context: MatchContext) -> bool:
        """The forced function when ``tool_choice`` names one, else any offered tool."""
        if context.forced_tool is not None:
            return name == context.forced_tool
        return name in context.tool_names

    def to_json(self) -> dict[str, JsonValue]:
        return {**self.rule.model_dump(mode="json"), "source": self.source}


class RuleBook:
    """Loaded rules in registration order plus the ``call_index`` counters."""

    def __init__(self) -> None:
        self._rules: list[LoadedRule] = []
        self._counters: dict[tuple[str, str], int] = {}

    def add(self, rules: Iterable[Rule], *, source: str) -> list[str]:
        """Register ``rules``; a rule whose id is taken replaces the earlier one (at the new position)."""
        ids = []
        for rule in rules:
            self._rules = [entry for entry in self._rules if entry.rule.id != rule.id]
            pattern = (
                re.compile(rule.match.last_user_regex) if rule.match.last_user_regex is not None else None
            )
            self._rules.append(LoadedRule(rule, source, pattern))
            ids.append(rule.id)
        return ids

    def remove(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [entry for entry in self._rules if entry.rule.id != rule_id]
        return len(self._rules) != before

    def clear_runtime(self) -> None:
        self._rules = [entry for entry in self._rules if entry.source != RUNTIME_SOURCE]

    def reset(self) -> None:
        """Drop runtime rules and every ``call_index`` counter; file rules stay."""
        self.clear_runtime()
        self._counters.clear()

    def ordered(self) -> Sequence[LoadedRule]:
        """Match order: ``priority`` descending, then registration order (``sorted`` is stable)."""
        return sorted(self._rules, key=lambda entry: -entry.rule.priority)

    def match(self, context: MatchContext) -> Rule | None:
        """The winning rule for ``context``; bumps the counter of every rule whose conditions hold."""
        winner: Rule | None = None
        for entry in self.ordered():
            if not entry.conditions_hold(context):
                continue
            key = (entry.rule.id, context.call_id or "")
            index = self._counters.get(key, 0)
            self._counters[key] = index + 1
            wanted = entry.rule.match.call_index
            if winner is None and (wanted is None or wanted == index):
                winner = entry.rule
        return winner

    def __len__(self) -> int:
        return len(self._rules)
