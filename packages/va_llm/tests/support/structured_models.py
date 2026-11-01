"""Pydantic schemas shaped like the platform's forced functions (specs/10 §9, specs/18, specs/12) for the
structured-output tests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

__all__ = ["Alternative", "Answer", "Citation", "Citations", "GuardRail", "GuardRails", "Loop", "Route"]


class Alternative(BaseModel):
    path_id: str
    confidence: int = Field(ge=0, le=10)


class Route(BaseModel):
    """``select_path``."""

    path_id: Literal["edge_book", "edge_hours", "stay"]
    confidence: int = Field(ge=0, le=10)
    alternatives: list[Alternative]
    reason: str = Field(max_length=200)


class Loop(BaseModel):
    """``score_condition``; the validator is a rule JSON Schema cannot express."""

    score: int = Field(ge=0, le=10)
    met: bool
    missing: str = Field(max_length=200)

    @field_validator("missing")
    @classmethod
    def _no_shouting(cls, value: str) -> str:
        if value.isupper():
            msg = "must not be upper case"
            raise ValueError(msg)
        return value


class Answer(BaseModel):
    """``classify_answer``."""

    label: Literal["human", "voicemail", "ivr", "unknown"]
    confidence: float = Field(ge=0, le=1)


class GuardRail(BaseModel):
    name: str
    triggered: bool
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(max_length=200)


class GuardRails(BaseModel):
    """``evaluate_guard_rails``."""

    results: list[GuardRail]


class Citation(BaseModel):
    variable_name: Literal["party_size", "wants_patio"]
    turn_numbers: list[int]


class Citations(BaseModel):
    """``extract_citations``."""

    citations: list[Citation]
