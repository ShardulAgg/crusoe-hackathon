import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from paper_library.verification_requirements import (
    MAX_CONDITION_BYTES,
    MAX_CONDITION_DEPTH,
    MAX_SOURCE_QUOTE_CHARS,
    MAX_SOURCE_QUOTES,
    AcceptanceCriterion,
    ExtractedVerificationRequirement,
    VerificationRequirement,
    validate_extracted_requirements,
    validate_requirements,
)


def criterion(**changes):
    return AcceptanceCriterion(
        **{
            "metric": "output_tokens_per_second",
            "operator": "gte",
            "expected": 1000,
            "unit": "tokens/s",
            **changes,
        }
    )


def requirement(**changes):
    return VerificationRequirement(
        **{
            "id": "throughput-check",
            "description": "Verify the stated output throughput.",
            "procedure": ["Submit the pinned benchmark and compare its measured throughput."],
            "conditions": {"precision": "fp16", "concurrency": 32},
            "acceptance_criteria": [criterion()],
            **changes,
        }
    )


def test_structured_conditions_and_typed_criteria_survive_json_round_trip():
    published = requirement(
        description="  Verify throughput.  ",
        procedure=["  Compare the fixed workload.  "],
        conditions={
            "precision": " fp16 ",
            "sampling": {"temperature": 0.0, "seed": 42},
            "batch_sizes": [1, 8, 32],
            "stream": True,
            "optional": None,
        },
        acceptance_criteria=[criterion(metric="  speed  ", unit=" tokens/s ")],
    )

    restored = VerificationRequirement.model_validate_json(published.model_dump_json())
    assert restored == published
    assert restored.description == "Verify throughput."
    assert restored.procedure == ["Compare the fixed workload."]
    assert restored.conditions["precision"] == " fp16 "
    assert restored.conditions["sampling"]["seed"] == 42
    assert restored.conditions["stream"] is True
    assert restored.acceptance_criteria[0].metric == "speed"
    assert restored.acceptance_criteria[0].unit == "tokens/s"


@pytest.mark.parametrize("operator", ["eq", "ne"])
@pytest.mark.parametrize("expected", ["001", "true", " fp16 ", 42, 1.25, True, False])
def test_unordered_comparisons_preserve_exact_scalar_type(operator, expected):
    parsed = criterion(operator=operator, expected=expected)
    assert type(parsed.expected) is type(expected)
    assert parsed.expected == expected
    assert type(AcceptanceCriterion.model_validate_json(parsed.model_dump_json()).expected) is type(
        expected
    )


@pytest.mark.parametrize("operator", ["lt", "lte", "gt", "gte"])
@pytest.mark.parametrize("expected", [0, -1, 1.5])
def test_ordered_comparisons_accept_finite_integer_and_float_values(operator, expected):
    assert criterion(operator=operator, expected=expected).expected == expected


@pytest.mark.parametrize("operator", ["lt", "lte", "gt", "gte"])
@pytest.mark.parametrize("expected", [True, False, "1000"])
def test_ordered_comparisons_reject_booleans_and_strings(operator, expected):
    with pytest.raises(ValidationError, match="Ordered comparisons"):
        criterion(operator=operator, expected=expected)


@pytest.mark.parametrize(
    "changes",
    [
        {"metric": " \n "},
        {"metric": "m" * 201},
        {"operator": "approximately"},
        {"operator": "execute"},
        {"expected": None},
        {"expected": []},
        {"expected": {}},
        {"expected": float("nan")},
        {"expected": float("inf")},
        {"expected": float("-inf")},
        {"expected": Decimal("1.5")},
        {"unit": " "},
        {"unit": "x" * 101},
        {"command": "do something"},
    ],
)
def test_criteria_reject_empty_metrics_unsupported_values_and_execution_fields(changes):
    with pytest.raises(ValidationError):
        criterion(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "UpperCase"},
        {"id": "unsafe command"},
        {"id": "../outside"},
        {"id": "x" * 101},
        {"description": " \n "},
        {"description": "x" * 2001},
        {"procedure": []},
        {"procedure": [" "]},
        {"procedure": ["x" * 2001]},
        {"procedure": ["step"] * 31},
        {"conditions": {}},
        {"conditions": {f"key-{index}": index for index in range(101)}},
        {"acceptance_criteria": []},
        {"acceptance_criteria": [criterion()] * 31},
        {"command": "arbitrary execution"},
    ],
)
def test_requirement_items_must_publish_all_required_parts(changes):
    with pytest.raises(ValidationError):
        requirement(**changes)


@pytest.mark.parametrize(
    "conditions",
    [
        [],
        {1: "numeric key"},
        {" ": "blank key"},
        {"x" * 201: "long key"},
        {"nested": {"": "blank nested key"}},
        {"nested": [{False: "non-string nested key"}]},
        {"value": float("nan")},
        {"nested": [1, {"value": float("inf")}]},
        {"value": Decimal("1.5")},
        {"value": b"binary"},
        {"value": (1, 2)},
        {"value": {1, 2}},
        {"value": object()},
    ],
)
def test_conditions_accept_only_exact_finite_json(conditions):
    with pytest.raises(ValidationError):
        requirement(conditions=conditions)


def test_conditions_enforce_depth_before_python_recursion_or_cycles():
    nested = "scalar"
    for _ in range(MAX_CONDITION_DEPTH - 1):
        nested = {"level": nested}
    assert requirement(conditions={"root": nested}).conditions["root"] == nested
    with pytest.raises(ValidationError, match="container levels"):
        requirement(conditions={"extra-level": {"root": nested}})

    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValidationError, match="container levels"):
        requirement(conditions=cyclic)


def test_conditions_size_is_measured_as_encoded_json_bytes():
    overhead = len(json.dumps({"data": ""}, separators=(",", ":")).encode())
    boundary = {"data": "x" * (MAX_CONDITION_BYTES - overhead)}
    assert requirement(conditions=boundary).conditions == boundary
    with pytest.raises(ValidationError, match="32 KiB"):
        requirement(conditions={"data": boundary["data"] + "x"})
    with pytest.raises(ValidationError, match="32 KiB"):
        requirement(conditions={"data": "界" * (MAX_CONDITION_BYTES // 2)})


def test_requirement_list_preserves_order_and_rejects_duplicate_ids():
    published = [requirement(id="speed"), requirement(id="accuracy")]
    assert validate_requirements(published) == published
    with pytest.raises(ValueError, match="unique"):
        validate_requirements([requirement(), requirement()])
    assert validate_requirements([]) == []


def test_manifest_rejects_empty_requirement_items():
    with pytest.raises(ValidationError):
        validate_requirements([{}])


def test_manifest_count_and_total_encoded_size_are_bounded():
    assert (
        len(validate_requirements([requirement(id=f"check-{index}") for index in range(50)])) == 50
    )
    with pytest.raises(ValueError, match="at most 50"):
        validate_requirements([requirement(id=f"check-{index}") for index in range(51)])

    published = [
        requirement(id=f"check-{index}", conditions={"data": "x" * 32000}) for index in range(9)
    ]
    with pytest.raises(ValueError, match="256 KiB"):
        validate_requirements(published)


def test_manifest_helper_revalidates_mutated_models():
    published = requirement()
    published.acceptance_criteria[0].expected = None
    with pytest.raises(ValidationError):
        validate_requirements([published])


def test_manifest_does_not_coerce_arbitrary_iterables_to_a_list():
    with pytest.raises(ValueError, match="must be a list"):
        validate_requirements((requirement(),))


def extracted(**changes):
    return ExtractedVerificationRequirement(
        **{
            **requirement().model_dump(),
            "source_quotes": ["The throughput is at least 1000 tokens/s at concurrency 32."],
            **changes,
        }
    )


def test_extracted_requirements_preserve_verbatim_quotes_and_ground_in_paper():
    quote = "  The throughput is at least 1000 tokens/s.\n"
    parsed = extracted(source_quotes=[quote, "concurrency 32"])
    paper = "Abstract.\n" + quote + "We use concurrency 32 for the experiment."

    assert parsed.source_quotes[0] == quote
    assert validate_extracted_requirements([parsed], paper_text=paper) == [parsed]
    restored = ExtractedVerificationRequirement.model_validate_json(parsed.model_dump_json())
    assert restored.source_quotes == [quote, "concurrency 32"]
    assert restored.acceptance_criteria[0].expected == 1000
    assert restored.conditions == {"precision": "fp16", "concurrency": 32}


@pytest.mark.parametrize(
    "quotes",
    [
        [],
        [""],
        [" \n\t "],
        ["x" * (MAX_SOURCE_QUOTE_CHARS + 1)],
        ["A real excerpt"] * (MAX_SOURCE_QUOTES + 1),
        [42],
    ],
)
def test_extraction_requires_bounded_nonblank_source_quotes(quotes):
    with pytest.raises(ValidationError):
        extracted(source_quotes=quotes)


def test_extraction_cannot_omit_provenance_even_when_legacy_shape_is_valid():
    legacy = requirement().model_dump()
    assert VerificationRequirement.model_validate(legacy) == requirement()
    with pytest.raises(ValidationError):
        ExtractedVerificationRequirement.model_validate(legacy)
    with pytest.raises(ValidationError):
        validate_extracted_requirements([legacy])


@pytest.mark.parametrize(
    "quotes,paper",
    [
        (["Measured throughput is 1000."], "Measured throughput is 999."),
        (["Throughput"], "throughput"),
        (["  throughput  "], "throughput"),
        (["café"], "cafe\u0301"),
        (["A real excerpt", "An invented excerpt"], "A real excerpt only."),
    ],
)
def test_every_quote_must_appear_exactly_without_case_or_whitespace_normalization(quotes, paper):
    with pytest.raises(ValueError, match="appear verbatim"):
        validate_extracted_requirements([extracted(source_quotes=quotes)], paper_text=paper)


def test_quote_grounding_can_be_deferred_for_stored_extractions():
    parsed = extracted()
    assert validate_extracted_requirements([parsed]) == [parsed]
    assert validate_extracted_requirements([]) == []
    with pytest.raises(ValueError, match="Paper text must be a string"):
        validate_extracted_requirements([parsed], paper_text=123)


def test_extracted_ids_are_unique_and_collection_count_is_bounded():
    with pytest.raises(ValueError, match="unique"):
        validate_extracted_requirements([extracted(), extracted()])
    with pytest.raises(ValueError, match="at most 50"):
        validate_extracted_requirements([extracted(id=f"check-{index}") for index in range(51)])


def test_extracted_total_size_includes_source_quotes():
    quote = "x" * 3000
    parsed = [
        extracted(id=f"check-{index}", source_quotes=[quote] * MAX_SOURCE_QUOTES)
        for index in range(3)
    ]
    with pytest.raises(ValueError, match="256 KiB"):
        validate_extracted_requirements(parsed, paper_text=quote)
    assert validate_requirements([requirement(id=f"check-{index}") for index in range(3)])


def test_mutated_extraction_is_revalidated_before_use():
    parsed = extracted()
    parsed.source_quotes = []
    with pytest.raises(ValidationError):
        validate_extracted_requirements([parsed])

    parsed = extracted()
    parsed.conditions["bad"] = float("nan")
    with pytest.raises(ValidationError):
        validate_extracted_requirements([parsed])
