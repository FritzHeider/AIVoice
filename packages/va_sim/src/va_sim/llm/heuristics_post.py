"""Post-call structured-call heuristics (specs/30 §3.4; Decision P03-T06b rule 4): disposition, analysis
schema, answers, sentiment, citations, evaluation grades, and memory updates. Candidates only: the caller
(:mod:`va_sim.llm.heuristics`) conforms them to the tool schema.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from va_sim.llm.heuristics_base import (
    Heuristic,
    analysis_value,
    enum_values,
    summary_text,
)
from va_sim.llm.params import system_text
from va_sim.llm.schema_fill import JsonSchema
from va_sim.llm.text import NUMBER_WORDS, Turn, content_words, name_value, turns, words, yes_no

__all__ = [
    "BUILDERS",
    "answer_questions",
    "classify_disposition",
    "extract_analysis_schema",
    "extract_citations",
    "grade",
    "record_sentiment",
    "update_memory",
]

_DEFINITION_LINE: Final = re.compile(r"(?m)^- ([A-Z][A-Z0-9_]*): (.*)$")
_VOICEMAIL: Final = re.compile(
    r"(?i)leave (?:a|your) message|after the (?:tone|beep)|not available|voicemail"
)
_FRUSTRATION: Final = re.compile(r"(?i)frustrat|annoy|ridiculous|angry")
_POSITIVE: Final = frozenset(
    {"great", "thanks", "thank", "perfect", "love", "happy", "awesome", "good", "wonderful"}
)
_NEGATIVE: Final = frozenset(
    {"bad", "angry", "terrible", "frustrated", "annoyed", "ridiculous", "hate", "awful", "upset"}
)
_FOLLOW_UP: Final = "FOLLOW_UP_REQUIRED"
_DISPOSITION_CONFIDENCE: Final = 0.7
_STRONG: Final = 3
_SCORE_SCALE: Final = 4
_GRADE_SCORE: Final = 80
_MAX_GRADE_DEPTH: Final = 3
_GRADE_RATIONALE: Final = "Simulated evaluation."


def classify_disposition(heuristic: Heuristic) -> dict[str, Any]:
    """The allowed tag whose words and definition overlap the transcript most, else ``FOLLOW_UP_REQUIRED``."""
    tags = [tag for tag in heuristic.enum_of("tag") if isinstance(tag, str)]
    if not tags:
        return {}
    definitions = {
        match.group(1): match.group(2) for match in _DEFINITION_LINE.finditer(system_text(heuristic.request))
    }
    described = [f"{tag.replace('_', ' ')} {definitions.get(tag, '')}" for tag in tags]
    transcript = content_words(heuristic.transcript)
    chosen_index = None
    best = 0
    for index, label in enumerate(described):
        score = len(content_words(label) & transcript)
        if score > best:
            best, chosen_index = score, index
    fallback = _FOLLOW_UP if _FOLLOW_UP in tags else tags[0]
    chosen = tags[chosen_index] if chosen_index is not None else fallback
    return {"tag": chosen, "confidence": _DISPOSITION_CONFIDENCE, "reason": "Simulated disposition."}


def extract_analysis_schema(heuristic: Heuristic) -> dict[str, Any]:
    """Each field by its type from the transcript (emails, phones, dates, names, numbers, yes/no, enums)."""
    return {
        name: analysis_value(heuristic, name, schema, heuristic.transcript)
        for name, schema in heuristic.properties.items()
    }


def _answer(heuristic: Heuristic, index: int, schema: JsonSchema) -> Any:
    options = enum_values(heuristic.fill, schema)
    if "voicemail" in options:
        return "voicemail" if _VOICEMAIL.search(heuristic.transcript) else "human"
    return analysis_value(heuristic, f"answer_{index}", schema, heuristic.transcript)


def answer_questions(heuristic: Heuristic) -> dict[str, Any]:
    """One answer per question position (``prefixItems``, else ``minItems``), each by its item type."""
    fill = heuristic.fill
    answers = fill.resolve(heuristic.properties.get("answers", {}))
    prefix = answers.get("prefixItems")
    prefix = prefix if isinstance(prefix, list) else []
    count = max(len(prefix), int(answers.get("minItems", 0)))
    items = answers.get("items", {})
    return {
        "answers": [
            _answer(heuristic, index, fill.resolve(prefix[index] if index < len(prefix) else items))
            for index in range(count)
        ]
    }


def record_sentiment(heuristic: Heuristic) -> dict[str, Any]:
    """Positive minus negative words of the caller decide ``overall`` and ``score``."""
    said = words(heuristic.caller)
    net = sum(word in _POSITIVE for word in said) - sum(word in _NEGATIVE for word in said)
    if net >= _STRONG:
        overall = "very_positive"
    elif net <= -_STRONG:
        overall = "very_negative"
    else:
        overall = "positive" if net > 0 else ("negative" if net < 0 else "neutral")
    return {
        "overall": overall,
        "score": max(-1.0, min(1.0, net / _SCORE_SCALE)),
        "caller_frustration_detected": _FRUSTRATION.search(heuristic.caller) is not None,
        "notable_moments": [],
    }


def _supports(value: Any, text: str) -> bool:
    """Whether a turn's ``text`` states ``value`` (yes/no words for booleans, number words for numbers)."""
    if isinstance(value, bool):
        return yes_no(text) is not None
    if str(value).lower() in text.lower():
        return True
    return isinstance(value, int) and any(NUMBER_WORDS.get(word) == value for word in words(text))


def _cited_turns(value: Any, transcript_turns: list[Turn]) -> list[int]:
    if value is None:
        return []
    return next(([turn.number] for turn in transcript_turns if _supports(value, turn.text)), [])


def extract_citations(heuristic: Heuristic) -> dict[str, Any]:
    """Per variable a typed value from the transcript and the first turn that contains it."""
    fill = heuristic.fill
    citation = fill.resolve(fill.resolve(heuristic.properties.get("citations", {})).get("items", {}))
    names = heuristic.enum_of("variable_name", citation)
    value_schema = fill.properties(citation).get("value", {})
    transcript_turns = turns(heuristic.request)
    citations = []
    for name in names:
        value = analysis_value(heuristic, str(name), value_schema, heuristic.transcript)
        citations.append(
            {
                "variable_name": name,
                "value": value,
                "turn_numbers": _cited_turns(value, transcript_turns),
                "topics": [],
                "utterance_type": "statement",
            }
        )
    return {"citations": citations}


def _grade_scalar(name: str, kind: str | None) -> Any:
    if kind == "boolean":
        return True
    if kind in {"integer", "number"}:
        return _GRADE_SCORE if "score" in name.lower() else None
    return _GRADE_RATIONALE if kind == "string" else None


def _grade_value(heuristic: Heuristic, name: str, schema: Any, depth: int = 0) -> Any:
    fill = heuristic.fill
    kind = fill.value_type(schema)
    if kind not in {"object", "array"} or depth > _MAX_GRADE_DEPTH:
        return _grade_scalar(name, kind)
    if kind == "object":
        return {
            key: _grade_value(heuristic, key, sub, depth + 1) for key, sub in fill.properties(schema).items()
        }
    if kind == "array":
        resolved = fill.resolve(schema)
        count = max(int(resolved.get("minItems", 0)), 1)
        return [_grade_value(heuristic, name, resolved.get("items", {}), depth + 1) for _ in range(count)]
    return None


def grade(heuristic: Heuristic) -> dict[str, Any]:
    """Pass, score 80 for every score field (criteria included), rationale ``Simulated evaluation.``."""
    return {name: _grade_value(heuristic, name, schema) for name, schema in heuristic.properties.items()}


def update_memory(heuristic: Heuristic) -> dict[str, Any]:
    """A summary of the conversation and the caller's stated name as a fact."""
    name = name_value(heuristic.caller)
    return {
        "summary": summary_text(heuristic.transcript),
        "facts_set": {"name": name} if name is not None else {},
        "facts_delete": [],
        "open_items_add": [],
        "open_items_close": [],
    }


BUILDERS: Final[Mapping[str, Callable[[Heuristic], dict[str, Any]]]] = {
    "classify_disposition": classify_disposition,
    "extract_analysis_schema": extract_analysis_schema,
    "record_analysis": extract_analysis_schema,
    "answer_questions": answer_questions,
    "record_sentiment": record_sentiment,
    "extract_citations": extract_citations,
    "grade": grade,
    "update_memory": update_memory,
}
