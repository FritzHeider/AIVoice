"""Text helpers of the FakeLLM structured-call heuristics (specs/30 §3.4 Decision P03-T06b rule 3).

Pure: word splitting, stopwords, overlap scoring, value extraction (integers, numbers, yes/no, names), and the
text sources a heuristic reads from a chat request (latest caller message, caller lines, transcript, system
lines, citation turns).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from va_sim.llm.params import ChatRequest, last_user_text, message_text, system_text

__all__ = [
    "NUMBER_WORDS",
    "STOPWORDS",
    "Turn",
    "caller_text",
    "content_words",
    "first_integer",
    "first_number",
    "first_words",
    "latest_caller",
    "name_value",
    "overlap",
    "system_matches",
    "transcript_text",
    "trimmed",
    "turns",
    "words",
    "yes_no",
]

STOPWORDS: Final = frozenset(
    {
        "a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as", "at", "be",
        "been", "before", "being", "but", "by", "can", "could", "did", "do", "does", "for", "from",
        "had", "has", "have", "he", "her", "here", "him", "his", "how", "i", "i'd", "i'll", "i'm",
        "i've", "if", "in", "into", "is", "it", "it's", "its", "just", "let", "me", "my", "of",
        "on", "or", "our", "she", "should", "so", "some", "than", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "those", "to", "too", "up", "us", "very", "was",
        "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
        "you", "your",
    }
)  # fmt: skip
NUMBER_WORDS: Final[Mapping[str, int]] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}  # fmt: skip
_YES: Final = frozenset({"yes", "yeah", "yep", "sure", "correct", "affirmative", "absolutely"})
_NO: Final = frozenset({"no", "nope", "negative", "nah"})
_WORD: Final = re.compile(r"[a-z0-9']+")
_NUMBER_TOKEN: Final = re.compile(r"-?\d+(?:\.\d+)?|[a-z]+")
_NAME: Final = re.compile(r"(?i)\b(?:my name is|this is|i'm|i am|it's|it is)\s+([^.,!?;\n]+)")
_LATEST: Final = re.compile(r'Latest caller message: "(.*)"')
_CALLER_LINE: Final = re.compile(r"(?im)^\s*(?:caller|user):[ \t]*(.*)$")
_TRANSCRIPT_HEADER: Final = re.compile(r"(?im)^(?:transcript|conversation\b[^\n]*):[ \t]*$")
_TURN_PREFIX: Final = re.compile(r"^\s*\[T\d+\|[^\]]*\]\s*")
_ROLE_PREFIX: Final = re.compile(r"(?i)^\s*(?:user|assistant|human_agent|caller|agent):\s*")
_TURN_LINE: Final = re.compile(r"(?m)^\s*\[T(\d+)\|[^\]]*\][ \t]*(.*)$")
_DIALOGUE_ROLES: Final = frozenset({"user", "assistant"})


@dataclass(frozen=True, slots=True)
class Turn:
    """One ``[T{n}|{start}-{end}|{role}] text`` line of a citations transcript."""

    number: int
    text: str


def words(text: str) -> list[str]:
    """Lowercased words (letters, digits, apostrophes)."""
    return _WORD.findall(text.lower())


def content_words(text: str) -> set[str]:
    """The words of ``text`` that are not stopwords."""
    return {word for word in words(text) if word not in STOPWORDS}


def overlap(left: str, right: str) -> int:
    """How many content words the two texts share."""
    return len(content_words(left) & content_words(right))


def first_integer(text: str) -> int | None:
    """The first integer in ``text``: digits, or a number word one-twenty."""
    for word in words(text):
        if word.isdigit():
            return int(word)
        if word in NUMBER_WORDS:
            return NUMBER_WORDS[word]
    return None


def first_number(text: str) -> int | float | None:
    """The first number in ``text`` (``-2.5``, ``40``, ``twelve``); integral values come back as ``int``."""
    for token in _NUMBER_TOKEN.findall(text.lower()):
        if token in NUMBER_WORDS:
            return NUMBER_WORDS[token]
        if token[-1].isdigit():
            return float(token) if "." in token else int(token)
    return None


def yes_no(text: str) -> bool | None:
    """True or False for the first yes/no word in ``text``, None without one."""
    for word in words(text):
        if word in _YES:
            return True
        if word in _NO:
            return False
    return None


def name_value(text: str) -> str | None:
    """The words after "my name is", "this is", "I'm", "I am", "it's", or "it is", up to punctuation."""
    match = _NAME.search(text)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def trimmed(text: str, limit: int = 80) -> str:
    return text.strip()[:limit].strip()


def first_words(text: str, count: int) -> str:
    return " ".join(text.split()[:count])


def latest_caller(request: ChatRequest) -> str:
    """The quoted ``Latest caller message: "..."`` of the last user message, else that message's text."""
    text = last_user_text(request) or ""
    match = _LATEST.search(text)
    return match.group(1) if match is not None else text


def _user_texts(request: ChatRequest) -> list[str]:
    return [message_text(message) for message in request.messages if message.role == "user"]


def caller_text(request: ChatRequest) -> str:
    """The ``Caller:``/``user:`` lines of the user messages plus the latest caller message, else every user
    message's text."""
    lines: list[str] = []
    for text in _user_texts(request):
        lines.extend(_CALLER_LINE.findall(text))
        latest = _LATEST.search(text)
        if latest is not None:
            lines.append(latest.group(1))
    if not lines:
        lines = _user_texts(request)
    return "\n".join(lines)


def _clean(block: str) -> str:
    lines = (_ROLE_PREFIX.sub("", _TURN_PREFIX.sub("", line)) for line in block.splitlines())
    return "\n".join(line for line in lines if line.strip())


def transcript_text(request: ChatRequest) -> str:
    """The block after a ``Transcript:`` (or ``Conversation (...):``) line of the last user message, else
    every user and assistant message; role prefixes and citation turn ids removed."""
    last = last_user_text(request) or ""
    header = _TRANSCRIPT_HEADER.search(last)
    if header is not None:
        return _clean(last[header.end() :])
    texts = [message_text(message) for message in request.messages if message.role in _DIALOGUE_ROLES]
    return _clean("\n".join(texts))


def system_matches(request: ChatRequest, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """Every match of ``pattern`` in the system and developer messages."""
    return list(pattern.finditer(system_text(request)))


def turns(request: ChatRequest) -> list[Turn]:
    """The citation turn lines of the user messages, in order."""
    found: list[Turn] = []
    for text in _user_texts(request):
        found.extend(Turn(int(number), body.strip()) for number, body in _TURN_LINE.findall(text))
    return found
