"""
Unit tests for the transient QA structured-output schema
(`app/agents/test_case/schema.py`).

These are pure Pydantic tests — no Gemini, no network, no VersionService. They
pin the behavioural contract the LangChain structured-output pilot relies on:
the schema mirrors the existing QA JSON shape, `priority` / `test_type` stay
plain non-empty strings (no Literal enums yet), and a valid payload round-trips
through `model_dump()` / re-validation unchanged.
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.test_case.schema import TestCaseList, TestCaseModel


def _valid_case(**overrides) -> dict:
    case = {
        "id": "TC-001",
        "title": "Register a new customer account",
        "requirement_or_story_ref": "FR-1",
        "brd_reference": "FR-1",
        "user_story_reference": "US-001",
        "hld_reference": None,
        "lld_reference": None,
        "preconditions": ["The registration page is reachable"],
        "test_data": ["email: user@example.com"],
        "test_steps": ["Open the registration page", "Submit valid details"],
        "expected_result": "The account is created and a confirmation is shown",
        "priority": "High",
        "test_type": "Functional",
        "dependencies": [],
        "notes": "",
    }
    case.update(overrides)
    return case


# 1. valid TestCaseModel ------------------------------------------------------

def test_valid_test_case_model():
    model = TestCaseModel(**_valid_case())
    assert model.id == "TC-001"
    assert model.priority == "High"
    assert model.test_type == "Functional"
    assert model.test_steps == ["Open the registration page", "Submit valid details"]


def test_optional_reference_fields_default_to_none_and_lists_default_empty():
    # requirement_or_story_ref is REQUIRED (R4); the other *_reference fields
    # stay optional because HLD/LLD/User Stories are optional context.
    minimal = {
        "id": "TC-007",
        "title": "Minimal case",
        "requirement_or_story_ref": "FR-9",
        "test_steps": ["Do the thing"],
        "expected_result": "It works",
        "priority": "Low",
        "test_type": "Functional",
    }
    model = TestCaseModel(**minimal)
    assert model.brd_reference is None
    assert model.user_story_reference is None
    assert model.hld_reference is None
    assert model.lld_reference is None
    assert model.preconditions == []
    assert model.test_data == []
    assert model.dependencies == []
    assert model.notes == ""


def test_requirement_or_story_ref_is_required():
    case = _valid_case()
    case.pop("requirement_or_story_ref")
    with pytest.raises(ValidationError):
        TestCaseModel(**case)
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(requirement_or_story_ref=""))


def test_priority_and_test_type_accept_any_non_empty_string_no_literal_enum():
    # The pilot must NOT tighten these into Literal enums yet.
    model = TestCaseModel(**_valid_case(priority="Critical", test_type="Smoke"))
    assert model.priority == "Critical"
    assert model.test_type == "Smoke"


# 2. valid TestCaseList -----------------------------------------------------

def test_valid_test_case_list():
    payload = {"test_cases": [_valid_case(), _valid_case(id="TC-002", title="Second")]}
    parsed = TestCaseList(**payload)
    assert [c.id for c in parsed.test_cases] == ["TC-001", "TC-002"]


# 3. missing required field ------------------------------------------------

@pytest.mark.parametrize("missing", ["id", "title", "requirement_or_story_ref", "test_steps", "expected_result", "priority", "test_type"])
def test_missing_required_field_raises(missing):
    case = _valid_case()
    case.pop(missing)
    with pytest.raises(ValidationError):
        TestCaseModel(**case)


def test_empty_string_required_field_raises():
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(expected_result=""))
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(title=""))


def test_empty_test_steps_list_raises():
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(test_steps=[]))


# 4. invalid test_steps type ---------------------------------------------

def test_test_steps_wrong_type_raises():
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(test_steps="1. not a list"))


def test_test_steps_non_string_items_raise():
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(test_steps=[1, 2, 3]))


# 5. invalid TC id -------------------------------------------------------

@pytest.mark.parametrize("bad_id", ["TC-1", "TC-0001", "tc-001", "TC001", "TX-001", "TC-01A", "", "TC-abc"])
def test_invalid_tc_id_raises(bad_id):
    with pytest.raises(ValidationError):
        TestCaseModel(**_valid_case(id=bad_id))


def test_valid_tc_id_boundaries():
    assert TestCaseModel(**_valid_case(id="TC-000")).id == "TC-000"
    assert TestCaseModel(**_valid_case(id="TC-999")).id == "TC-999"


# 6. empty test_cases list ---------------------------------------------

def test_empty_test_case_list_raises():
    with pytest.raises(ValidationError):
        TestCaseList(test_cases=[])


def test_missing_test_cases_key_raises():
    with pytest.raises(ValidationError):
        TestCaseList()


# 7. JSON / model round trip -----------------------------------------

def test_model_dump_json_round_trip_is_stable():
    original = TestCaseList(
        test_cases=[_valid_case(), _valid_case(id="TC-002", title="Second", priority="Medium")]
    )
    as_json = json.dumps(original.model_dump(), ensure_ascii=False)
    # This is exactly what TestCaseAgent._run() hands back to TestCaseService.
    reloaded = TestCaseList(**json.loads(as_json))
    assert reloaded.model_dump() == original.model_dump()


def test_model_dump_shape_matches_existing_qa_json_contract():
    dumped = TestCaseList(test_cases=[_valid_case()]).model_dump()
    assert set(dumped.keys()) == {"test_cases"}
    case = dumped["test_cases"][0]
    assert set(case.keys()) == {
        "id", "title", "requirement_or_story_ref",
        "brd_reference", "user_story_reference", "hld_reference", "lld_reference",
        "preconditions", "test_data", "test_steps", "expected_result",
        "priority", "test_type", "dependencies", "notes",
    }
