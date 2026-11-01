"""Forced-tool requests shaped like the platform's (specs/10 §9, specs/18, specs/12, specs/20 §2.5): one
system prompt, user message, and ``parameters`` schema per registered structured function, for the FakeLLM
heuristic tests and the ``openai`` SDK acceptance test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from llm_helpers import MODEL

__all__ = [
    "ANALYSIS_SCHEMA",
    "ANSWERS_SCHEMA",
    "CITATIONS_SCHEMA",
    "DISPOSITION_SCHEMA",
    "DISPOSITION_SYSTEM",
    "GRADE_SCHEMA",
    "GUARD_RAILS_SCHEMA",
    "LOOP_SCHEMA",
    "MEMORY_SCHEMA",
    "ROUTER_SCHEMA",
    "SENTIMENT_SCHEMA",
    "SPEC_CALLS",
    "SpecCall",
    "forced_body",
    "router_system",
    "router_user",
    "transcript_user",
]

ROUTER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path_id", "confidence", "alternatives", "reason"],
    "properties": {
        "path_id": {"type": "string", "enum": ["edge_book", "edge_hours", "stay", "global:human"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path_id", "confidence"],
                "properties": {
                    "path_id": {"type": "string"},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
                },
            },
        },
        "reason": {"type": "string", "maxLength": 200},
    },
}
LOOP_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "met", "missing"],
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "met": {"type": "boolean"},
        "missing": {"type": "string", "maxLength": 200},
    },
}
EXTRACT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["party_size", "caller_name", "wants_patio"],
    "properties": {
        "party_size": {"type": ["integer", "null"]},
        "caller_name": {"type": ["string", "null"]},
        "wants_patio": {"type": ["boolean", "null"]},
    },
}
ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "confidence"],
    "properties": {
        "label": {"type": "string", "enum": ["human", "voicemail", "ivr", "unknown"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
GUARD_RAILS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "triggered", "confidence", "evidence"],
                "properties": {
                    "name": {"type": "string"},
                    "triggered": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string", "maxLength": 200},
                },
            },
        }
    },
}
DISPOSITION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["tag", "confidence", "reason"],
    "properties": {
        "tag": {
            "type": "string",
            "enum": ["INTERESTED", "NOT_INTERESTED", "FOLLOW_UP_REQUIRED", "CALL_BACK_SCHEDULED"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 300},
    },
}
SUMMARY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}
ANALYSIS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["email_address", "appointment_time", "wants_demo", "budget_usd", "callback_phone", "plan"],
    "properties": {
        "email_address": {"type": ["string", "null"], "description": "email"},
        "appointment_time": {"type": ["string", "null"], "description": "YYYY-MM-DD HH:MM:SS"},
        "wants_demo": {"type": ["boolean", "null"], "description": "boolean"},
        "budget_usd": {"type": ["number", "null"], "description": "number"},
        "callback_phone": {"type": ["string", "null"], "description": "phone"},
        "plan": {"type": ["string", "null"], "enum": ["starter", "enterprise", None]},
    },
}
ANSWERS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answers"],
    "properties": {
        "answers": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "prefixItems": [
                {"type": ["boolean", "null"]},
                {"type": ["string", "null"], "enum": ["human", "voicemail", None]},
                {"type": ["number", "null"]},
            ],
            "items": False,
        }
    },
}
SENTIMENT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall", "score", "caller_frustration_detected", "notable_moments"],
    "properties": {
        "overall": {
            "type": "string",
            "enum": ["very_negative", "negative", "neutral", "positive", "very_positive"],
        },
        "score": {"type": "number", "minimum": -1, "maximum": 1},
        "caller_frustration_detected": {"type": "boolean"},
        "notable_moments": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["turn_index", "sentiment", "note"],
                "properties": {
                    "turn_index": {"type": "integer"},
                    "sentiment": {"type": "string"},
                    "note": {"type": "string", "maxLength": 200},
                },
            },
        },
    },
}
CITATIONS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["citations"],
    "properties": {
        "citations": {
            "type": "array",
            "items": {"$ref": "#/$defs/Citation"},
        }
    },
    "$defs": {
        "Citation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["variable_name", "value", "turn_numbers", "topics", "utterance_type"],
            "properties": {
                "variable_name": {"type": "string", "enum": ["party_size", "wants_patio"]},
                "value": {"type": ["integer", "boolean", "null"]},
                "turn_numbers": {"type": "array", "items": {"type": "integer"}},
                "topics": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
                "utterance_type": {
                    "type": "string",
                    "enum": ["statement", "question", "answer", "confirmation", "objection"],
                },
            },
        }
    },
}
GRADE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pass", "rationale"],
    "properties": {"pass": {"type": "boolean"}, "rationale": {"type": "string"}},
}
MEMORY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Updated cumulative summary, max 120 words"},
        "facts_set": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
        "facts_delete": {"type": "array", "items": {"type": "string"}},
        "open_items_add": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "due_at": {"type": ["string", "null"]}},
                "required": ["text"],
            },
        },
        "open_items_close": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "facts_set", "facts_delete", "open_items_add", "open_items_close"],
}

DISPOSITION_SYSTEM: Final = """\
You classify the outcome of a phone call into exactly one disposition tag.
Choose the single tag from the allowed list that best describes the outcome from the business's point of view.
Definitions of the allowed tags:
- INTERESTED: The person expressed interest in the offer or agreed to the next step.
- NOT_INTERESTED: The person declined or showed no interest.
- FOLLOW_UP_REQUIRED: The business must follow up; the conversation is unresolved.
- CALL_BACK_SCHEDULED: A callback at a specific time was agreed.
Rules:
- If nothing fits, choose the closest tag and lower your confidence.
Call the function select_disposition with your answer."""
GUARD_RAILS_SYSTEM: Final = """\
You monitor a live phone conversation for specific situations. For each rule below, decide whether the \
caller's latest message, in the context of the conversation, triggers it.

Rules:
- tcpa_opt_out: The caller asks to stop being called, to be removed from the list, to not be contacted \
again, or says "do not call".
- competitor_mention: The caller mentions a competitor by name.

Be conservative: mark a rule as triggered only when the conversation clearly matches it.
Call evaluate_guard_rails exactly once."""
TRANSCRIPT: Final = """\
assistant: Hi, this is Ava from Bistro. How can I help?
user: Hi, my name is Dana Smith. I'd like a table for four, yes on the patio please.
assistant: Great, what email should I use?
user: dana@mail.test works. Thanks, that's perfect."""
CITATION_TRANSCRIPT: Final = """\
[T0|0-1200|agent] Hi, this is Ava from Bistro.
[T1|1300-4100|caller] We are four people tonight.
[T2|4200-5000|agent] Patio or inside?
[T3|5100-6000|caller] Yes, the patio."""


def router_system(*paths: tuple[str, str]) -> str:
    lines = "\n".join(f"- id: {path_id} | condition: {label}" for path_id, label in paths)
    return (
        "You are the routing component of a phone conversation flow.\n\n"
        'Current step: "Greeting"\n\nPaths from the current step:\n'
        f"{lines}\n- id: stay | condition: None of the paths above apply yet. Remain on the current step.\n\n"
        "Paths available from any step:\n- id: global:human | condition: caller wants a human agent\n\n"
        "Rules:\n- Choose a path only when its condition is clearly met.\nCall select_path exactly once."
    )


ROUTER_PATHS: Final = (
    ("edge_book", "caller wants to book a table"),
    ("edge_hours", "caller asks opening hours"),
)


def router_user(latest: str, *history: str) -> str:
    lines = "".join(f"Caller: {line}\n" for line in history)
    return f'{lines}Known information: {{}}\nLatest caller message: "{latest}"'


def transcript_user(transcript: str = TRANSCRIPT) -> str:
    return f"Call direction: outbound\nDuration: 42 seconds\nTranscript:\n{transcript}"


@dataclass(frozen=True)
class SpecCall:
    """One registered structured function with a representative prompt."""

    name: str
    schema: dict[str, Any]
    system: str
    user: str


SPEC_CALLS: Final = (
    SpecCall(
        "select_path",
        ROUTER_SCHEMA,
        router_system(*ROUTER_PATHS),
        router_user("I want to book a table for tonight"),
    ),
    SpecCall(
        "score_condition",
        LOOP_SCHEMA,
        "You check whether a step of a phone conversation is complete.\n\n"
        "Completion condition: The caller gave the party size\n\nCall score_condition exactly once.",
        router_user("We are four, party size four"),
    ),
    SpecCall(
        "extract_variables",
        EXTRACT_SCHEMA,
        "Extract the following information from the phone conversation.\n\nVariables:\n"
        "- party_size (integer): people\n- caller_name (string): name\n- wants_patio (boolean): patio\n"
        "Call extract_variables exactly once.",
        router_user("Yes please, the patio", "My name is Dana", "Twelve people"),
    ),
    SpecCall(
        "classify_answer",
        ANSWER_SCHEMA,
        "You hear the first words spoken.\n\nTranscript so far: \"You've reached Dana, leave a message after "
        'the tone"\nSeconds since answer: 3\nCall classify_answer exactly once.',
        "Classify.",
    ),
    SpecCall("evaluate_guard_rails", GUARD_RAILS_SCHEMA, GUARD_RAILS_SYSTEM, router_user("Stop calling me.")),
    SpecCall(
        "grade", GRADE_SCHEMA, "Does the reply satisfy this goal? Be strict.", "Agent: Booked for four."
    ),
    SpecCall("summarize", SUMMARY_SCHEMA, "Summarize the call.", transcript_user()),
    SpecCall("classify_disposition", DISPOSITION_SCHEMA, DISPOSITION_SYSTEM, transcript_user()),
    SpecCall("extract_analysis_schema", ANALYSIS_SCHEMA, "You extract structured data.", transcript_user()),
    SpecCall("answer_questions", ANSWERS_SCHEMA, "You answer questions.", transcript_user()),
    SpecCall("record_sentiment", SENTIMENT_SCHEMA, "You assess how the person felt.", transcript_user()),
    SpecCall("record_analysis", ANALYSIS_SCHEMA, "You extract structured data.", transcript_user()),
    SpecCall("extract_citations", CITATIONS_SCHEMA, "You extract facts.", CITATION_TRANSCRIPT),
    SpecCall(
        "update_memory",
        MEMORY_SCHEMA,
        "You maintain long-term memory about a customer.",
        f"Existing memory:\n{{}}\n\nConversation (voice, 2026-10-31):\n{TRANSCRIPT}",
    ),
)


def forced_body(
    name: str, schema: dict[str, Any] | None, system: str, user: str, **extra: Any
) -> dict[str, Any]:
    """A non-streaming chat request forcing ``name`` (``parameters`` omitted when ``schema`` is None)."""
    function: dict[str, Any] = {"name": name}
    if schema is not None:
        function["parameters"] = schema
    return {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "tools": [{"type": "function", "function": function}],
        "tool_choice": {"type": "function", "function": {"name": name}},
        **extra,
    }
