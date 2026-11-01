"""Built-in structured-call heuristics (specs/30 §3.4; Decision P03-T06b rules 1-4).

A forced (or ``required``) tool call that no rule answers gets arguments from the heuristic registered for the
tool name: the in-call classifiers live here (``select_path``, ``score_condition``, ``extract_variables``,
``classify_answer``, ``evaluate_guard_rails``), the post-call ones in :mod:`va_sim.llm.heuristics_post`. Every
candidate passes through :meth:`SchemaFill.conform`, so the arguments always validate against the request's
tool ``parameters``; a name without a heuristic gets the pure schema default. The simulator keeps its own copy
of the specs/10 §3 registered names (no code is shared with ``va_llm``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from functools import cached_property
from typing import Any, Final

from pydantic import JsonValue

from va_sim.llm import heuristics_post
from va_sim.llm.heuristics_base import Heuristic, summary_text, typed_value
from va_sim.llm.params import ChatRequest, system_text
from va_sim.llm.schema_fill import JsonSchema, SchemaFill
from va_sim.llm.text import content_words, overlap, system_matches, transcript_text

__all__ = ["STRUCTURED_TOOL_NAMES", "TOOL_ALIASES", "structured_arguments", "summary_reply"]

STRUCTURED_TOOL_NAMES: Final = frozenset(
    {
        "select_path",
        "score_condition",
        "extract_variables",
        "classify_answer",
        "evaluate_guard_rails",
        "grade",
        "summarize",
        "classify_disposition",
        "extract_analysis_schema",
        "answer_questions",
        "record_sentiment",
        "record_analysis",
        "extract_citations",
        "update_memory",
    }
)
TOOL_ALIASES: Final[Mapping[str, str]] = {
    "select_disposition": "classify_disposition",
    "record_citations": "extract_citations",
}
SUMMARY_SYSTEM_MARKER: Final = "You write concise, factual summaries of phone calls"

_PATH_LINE: Final = re.compile(r"(?m)^- id: (\S+) \| condition: (.*?)(?: \| description: (.*))?$")
_CONDITION_LINE: Final = re.compile(r"(?m)^Completion condition: (.*)$")
_TRANSCRIPT_SO_FAR: Final = re.compile(r'Transcript so far: "(.*)"')
_RULES_BLOCK: Final = re.compile(r"(?ms)^Rules:[ \t]*\n(.*?)(?:\n[ \t]*\n|\Z)")
_RULE_LINE: Final = re.compile(r"(?m)^- ([A-Za-z0-9_:.-]+): (.*)$")
_VOICEMAIL: Final = re.compile(
    r"(?i)leave (?:a|your) message|after the (?:tone|beep)|not available|voicemail|you've reached|mailbox"
)
_IVR: Final = re.compile(r"(?i)press \d|please (?:say|enter)|your call is important")
_OPT_OUT: Final = re.compile(r"(?i)stop calling|do not call|don't call|remove me|unsubscribe")
_STAY: Final = "stay"
_ROUTE_CONFIDENCE: Final = 8
_FALLBACK_CONFIDENCE: Final = 5
_MAX_CONFIDENCE: Final = 10
_LIKELY: Final = 8
_UNLIKELY: Final = 2
_MET_SCORE: Final = 7
_SURE: Final = 0.9
_UNSURE: Final = 0.5
_TRIGGERED: Final = 0.95
_QUIET: Final = 0.05


def _path_labels(heuristic: Heuristic) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in system_matches(heuristic.request, _PATH_LINE):
        labels[match.group(1)] = " ".join(part for part in match.group(2, 3) if part)
    return labels


def _fallback_path(options: list[str], labels: Mapping[str, str]) -> str:
    if _STAY in options:
        return _STAY
    for option in options:
        if "fallback" in f"{option} {labels.get(option, '')}".lower():
            return option
    return "none" if "none" in options else options[0]


def _path_scores(options: list[str], labels: Mapping[str, str], latest: str) -> dict[str, int]:
    """Overlap of each option's label (its id when unlabelled) with ``latest``; ``stay`` is unscored."""
    return {
        option: overlap(labels.get(option, option.replace("_", " ")), latest)
        for option in options
        if option != _STAY
    }


def _alternatives(scores: Mapping[str, int], chosen: str) -> list[dict[str, Any]]:
    return [
        {"path_id": option, "confidence": min(_MAX_CONFIDENCE, 2 * score)}
        for option, score in scores.items()
        if option != chosen and score > 0
    ]


def select_path(heuristic: Heuristic) -> dict[str, Any]:
    """Route by content-word overlap of each path label with the latest caller message."""
    options = [option for option in heuristic.enum_of("path_id") if isinstance(option, str)]
    if not options:
        return {}
    labels = _path_labels(heuristic)
    scores = _path_scores(options, labels, heuristic.latest)
    best = max(scores.values(), default=0)
    if best > 0:
        chosen = next(option for option in options if scores.get(option) == best)
    else:
        chosen = _fallback_path(options, labels)
    confidence = _ROUTE_CONFIDENCE if best > 0 else _FALLBACK_CONFIDENCE
    return {
        "path_id": chosen,
        "confidence": confidence,
        "alternatives": _alternatives(scores, chosen),
        "reason": f"Simulated routing: {best} shared words.",
    }


def score_condition(heuristic: Heuristic) -> dict[str, Any]:
    """8 when a word of the completion condition appears in what the caller said, else 2."""
    conditions = system_matches(heuristic.request, _CONDITION_LINE)
    keywords = content_words(" ".join(match.group(1) for match in conditions))
    score = _LIKELY if keywords & content_words(heuristic.caller) else _UNLIKELY
    met = score >= _MET_SCORE
    return {"score": score, "met": met, "missing": "" if met else "Simulated: condition not detected."}


def extract_variables(heuristic: Heuristic) -> dict[str, Any]:
    """Each variable by its type from the caller's words."""
    return {
        name: typed_value(heuristic.fill.value_type(schema), heuristic.caller, heuristic.latest)
        for name, schema in heuristic.properties.items()
    }


def _answer_label(text: str) -> str:
    if _VOICEMAIL.search(text):
        return "voicemail"
    if _IVR.search(text):
        return "ivr"
    return "human" if text.strip() else "unknown"


def classify_answer(heuristic: Heuristic) -> dict[str, Any]:
    """human / voicemail / ivr / unknown from the transcript so far; boolean fields mean "is voicemail"."""
    found = system_matches(heuristic.request, _TRANSCRIPT_SO_FAR)
    text = found[0].group(1) if found else heuristic.latest
    label = _answer_label(text)
    candidate: dict[str, Any] = {"label": label, "confidence": _UNSURE if label == "unknown" else _SURE}
    for name, schema in heuristic.properties.items():
        if heuristic.fill.value_type(schema) == "boolean":
            candidate[name] = label == "voicemail"
    return candidate


def _guard_rule_names(heuristic: Heuristic) -> list[str]:
    names: list[str] = []
    for block in system_matches(heuristic.request, _RULES_BLOCK):
        names.extend(match.group(1) for match in _RULE_LINE.finditer(block.group(1)))
    return names


def evaluate_guard_rails(heuristic: Heuristic) -> dict[str, Any]:
    """Opt-out rules trigger on opt-out phrases in the latest caller message; custom rules never trigger."""
    opt_out = _OPT_OUT.search(heuristic.latest)
    results = []
    for name in _guard_rule_names(heuristic):
        triggered = opt_out is not None and "opt_out" in name
        results.append(
            {
                "name": name,
                "triggered": triggered,
                "confidence": _TRIGGERED if triggered else _QUIET,
                "evidence": opt_out.group(0) if triggered and opt_out is not None else "",
            }
        )
    return {"results": results}


def summarize(heuristic: Heuristic) -> dict[str, Any]:
    """Every string field is ``Summary: `` + the first 30 words of the transcript."""
    text = summary_text(heuristic.transcript)
    return {
        name: text
        for name, schema in heuristic.properties.items()
        if heuristic.fill.value_type(schema) == "string"
    }


_BUILDERS: Final[Mapping[str, Callable[[Heuristic], dict[str, Any]]]] = {
    "select_path": select_path,
    "score_condition": score_condition,
    "extract_variables": extract_variables,
    "classify_answer": classify_answer,
    "evaluate_guard_rails": evaluate_guard_rails,
    "summarize": summarize,
    **heuristics_post.BUILDERS,
}


class _Arguments:
    """Heuristic arguments for one tool of one request."""

    def __init__(self, name: str, parameters: JsonSchema, request: ChatRequest) -> None:
        self.name = TOOL_ALIASES.get(name, name)
        self.parameters = parameters
        self.request = request

    @cached_property
    def heuristic(self) -> Heuristic:
        return Heuristic(SchemaFill(self.parameters), self.request)

    def build(self) -> dict[str, JsonValue]:
        builder = _BUILDERS.get(self.name)
        candidate: Any = builder(self.heuristic) if builder is not None else None
        known = self.heuristic.properties
        if isinstance(candidate, dict) and known:
            candidate = {key: value for key, value in candidate.items() if key in known}
        fill = self.heuristic.fill
        result = (
            fill.conform(self.parameters, candidate)
            if candidate is not None
            else fill.default(self.parameters)
        )
        return result if isinstance(result, dict) else {}


def structured_arguments(
    name: str, parameters: Mapping[str, Any] | None, request: ChatRequest
) -> dict[str, JsonValue]:
    """Schema-valid arguments for a forced call of ``name`` (``{}`` when the tool declares no parameters)."""
    if parameters is None:
        return {}
    return _Arguments(name, parameters, request).build()


def summary_reply(request: ChatRequest) -> str | None:
    """The plain-text summary of a specs/18 §3 summary request (no tools, the summary system prompt)."""
    if request.tools or SUMMARY_SYSTEM_MARKER not in system_text(request):
        return None
    return summary_text(transcript_text(request))
