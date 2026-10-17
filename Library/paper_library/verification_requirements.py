"""Structured paper requirements with verbatim provenance; all procedures remain prose."""

import json
import math
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_CONDITION_BYTES = 32 * 1024
MAX_CONDITION_DEPTH = 10
MAX_REQUIREMENTS = 50
MAX_REQUIREMENTS_BYTES = 256 * 1024
MAX_SOURCE_QUOTES = 30
MAX_SOURCE_QUOTE_CHARS = 4000

RequirementId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]
RequirementText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
Metric = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
SourceQuote = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOURCE_QUOTE_CHARS)]


def _json_bytes(value) -> bytes:
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("Requirements must contain valid finite JSON data") from exc


def _validate_json(value, depth=1):
    kind = type(value)
    if kind is dict or kind is list:
        if depth > MAX_CONDITION_DEPTH:
            raise ValueError(f"Conditions must not exceed {MAX_CONDITION_DEPTH} container levels")
        if kind is dict:
            for key, child in value.items():
                if type(key) is not str or not key.strip() or len(key) > 200:
                    raise ValueError("Condition keys must be nonblank strings of at most 200 chars")
                _validate_json(child, depth + 1)
        else:
            for child in value:
                _validate_json(child, depth + 1)
    elif kind is float:
        if not math.isfinite(value):
            raise ValueError("Conditions must not contain non-finite numbers")
    elif kind not in (str, int, bool, type(None)):
        raise ValueError("Conditions must contain only JSON objects, arrays, and scalar values")


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    metric: Metric
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte"]
    expected: str | int | float | bool
    unit: Unit | None = None

    @field_validator("expected", mode="before")
    @classmethod
    def finite_expected_value(cls, value):
        if type(value) not in (str, int, float, bool):
            raise ValueError("Expected values must be JSON strings, integers, floats, or booleans")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Expected numeric values must be finite")
        return value

    @model_validator(mode="after")
    def numeric_ordered_comparison(self):
        if self.operator in {"lt", "lte", "gt", "gte"} and type(self.expected) not in (
            int,
            float,
        ):
            raise ValueError("Ordered comparisons require a numeric expected value, excluding bool")
        return self


class VerificationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    id: RequirementId
    description: RequirementText
    procedure: list[RequirementText] = Field(min_length=1, max_length=30)
    conditions: dict[str, Any] = Field(min_length=1, max_length=100)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=30)

    @field_validator("conditions", mode="before")
    @classmethod
    def exact_json_conditions(cls, value):
        if type(value) is not dict:
            raise ValueError("Conditions must be a JSON object")
        _validate_json(value)
        if len(_json_bytes(value)) > MAX_CONDITION_BYTES:
            raise ValueError("Conditions must not exceed 32 KiB of encoded JSON")
        return value


class ExtractedVerificationRequirement(VerificationRequirement):
    source_quotes: list[SourceQuote] = Field(min_length=1, max_length=MAX_SOURCE_QUOTES)

    @field_validator("source_quotes")
    @classmethod
    def nonblank_verbatim_quotes(cls, value):
        if any(not quote.strip() for quote in value):
            raise ValueError("Source quotes must be nonblank verbatim excerpts")
        # Preserve whitespace exactly so grounding never relies on normalized text.
        return value


def _validate_requirement_list(requirements, model):
    if type(requirements) is not list:
        raise ValueError("Verification requirements must be a list")
    if len(requirements) > MAX_REQUIREMENTS:
        raise ValueError(f"A list may contain at most {MAX_REQUIREMENTS} verification requirements")
    validated = [model.model_validate(item) for item in requirements]
    if len({item.id for item in validated}) != len(validated):
        raise ValueError("Verification requirement IDs must be unique")
    content = _json_bytes([item.model_dump(mode="json") for item in validated])
    if len(content) > MAX_REQUIREMENTS_BYTES:
        raise ValueError("Verification requirements must not exceed 256 KiB of encoded JSON")
    return validated


def validate_requirements(
    requirements: list[VerificationRequirement],
) -> list[VerificationRequirement]:
    """Validate legacy stored structured requirements without requiring provenance."""
    return _validate_requirement_list(requirements, VerificationRequirement)


def validate_extracted_requirements(
    requirements: list[ExtractedVerificationRequirement],
    paper_text: str | None = None,
) -> list[ExtractedVerificationRequirement]:
    """Validate extraction and, when text is supplied, ground every excerpt exactly in it."""
    validated = _validate_requirement_list(requirements, ExtractedVerificationRequirement)
    if paper_text is not None:
        if type(paper_text) is not str:
            raise ValueError("Paper text must be a string when checking source quotes")
        for requirement in validated:
            if any(quote not in paper_text for quote in requirement.source_quotes):
                raise ValueError("Every source quote must appear verbatim in the paper text")
    return validated
