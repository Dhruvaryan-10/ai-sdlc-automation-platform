"""
Phase 17 - artifact structural-contract coverage.

Offline golden-corpus tests for `app/quality/artifact_contract.py` plus the three
generation-service integration points and the Phase 12B error-taxonomy wiring.
No Gemini: services use stub agents injected via `agent=`.

The corpus deliberately uses tiny, representative Markdown fragments - the point
is the *shape* each downstream consumer relies on, not document length.
"""

import pytest

from app.quality.artifact_contract import (
    ArtifactContractError,
    ContractIssue,
    ContractReport,
    check_brd,
    check_hld,
    check_lld,
    check_test_cases,
    check_user_stories,
    enforce,
)
from app.quality.traceability import (
    extract_brd_requirements,
    extract_test_cases,
    extract_user_stories,
)

# --- golden corpus --------------------------------------------------------

VALID_BRD = """# Acme - Business Requirement Document

## 8. Functional Requirements
**FR-1: Account creation**
The system shall let a visitor create an account.

**FR-2: Login**
The system shall authenticate a returning user.

## 9. Non-Functional Requirements
**NFR-1: Availability**
The service shall be available 99.9% of the month.
"""

BRD_NO_IDS = """# Acme - Business Requirement Document

## Executive Summary
The system will be wonderful and do many things for many people.
There are no numbered requirement identifiers anywhere in this document.
"""

BRD_UNTITLED = """# Acme - Business Requirement Document

## Functional Requirements
FR-1. The system shall create accounts.
FR-2. The system shall authenticate users.
"""

BRD_ONE_REQ = """# Acme - Business Requirement Document

## Functional Requirements
**FR-1: The only requirement**
The system shall do exactly one thing.
"""

VALID_US = """# Acme - Draft User Stories

## US-001 - Customer registration
**User Story:** As a customer, I want an account.
**BRD Reference:** FR-1

## US-002 - Customer login
**User Story:** As a customer, I want to sign in.
**BRD Reference:** FR-2
"""

US_NO_HEADINGS = """# Acme - Draft User Stories

The analyst wrote three paragraphs of narrative prose here and never used a
"## US-" heading, so nothing downstream can enumerate the stories.
"""

US_NO_REFS = """# Acme - Draft User Stories

## US-001 - Customer registration
**User Story:** As a customer, I want an account.

## US-002 - Customer login
**User Story:** As a customer, I want to sign in.
"""

US_PARTIAL_REFS = """# Acme - Draft User Stories

## US-001 - Customer registration
**BRD Reference:** FR-1

## US-002 - Customer login
**User Story:** As a customer, I want to sign in.
"""

US_DANGLING = """# Acme - Draft User Stories

## US-001 - Customer registration
**BRD Reference:** FR-1

## US-002 - Something extra
**BRD Reference:** FR-9
"""

VALID_TC = """# Acme - Test Cases

## TC-001 - Register a customer
**Requirement / User Story Reference:** FR-1
**BRD Reference:** FR-1

## TC-002 - Reject a bad email
**Requirement / User Story Reference:** FR-1
**BRD Reference:** FR-1
"""

TC_NONE = """# Acme - Test Cases

No test-case identifiers were emitted; the section is empty prose only.
"""

TC_NO_REFS = """# Acme - Test Cases

## TC-001 - Register a customer
Steps: open page, submit form.

## TC-002 - Reject a bad email
Steps: open page, submit invalid email.
"""


# --- BRD contract -------------------------------------------------------

def test_valid_brd_has_no_issues():
    report = check_brd(VALID_BRD)
    assert report.ok
    assert report.issues == ()


def test_brd_with_zero_requirement_ids_is_blocking():
    report = check_brd(BRD_NO_IDS)
    assert not report.ok
    assert [i.code for i in report.blocking] == ["brd.no_requirements"]


def test_brd_none_is_blocking():
    assert not check_brd(None).ok


def test_brd_untitled_requirements_is_warning_not_blocking():
    report = check_brd(BRD_UNTITLED)
    assert report.ok                               # 2 ids parsed -> not blocking
    assert "brd.untitled_requirements" in {i.code for i in report.warnings}


def test_brd_single_requirement_is_warning_not_blocking():
    report = check_brd(BRD_ONE_REQ)
    assert report.ok
    assert "brd.few_requirements" in {i.code for i in report.warnings}


# --- User Story contract ----------------------------------------------

def test_valid_user_stories_have_no_issues():
    report = check_user_stories(VALID_US)
    assert report.ok
    assert report.issues == ()


def test_user_stories_with_zero_headings_is_blocking():
    report = check_user_stories(US_NO_HEADINGS)
    assert not report.ok
    assert [i.code for i in report.blocking] == ["user_stories.no_headings"]


def test_user_stories_missing_all_brd_references_is_warning():
    report = check_user_stories(US_NO_REFS)
    assert report.ok
    assert "user_stories.no_brd_references" in {i.code for i in report.warnings}


def test_user_stories_partial_brd_references_is_warning():
    report = check_user_stories(US_PARTIAL_REFS)
    assert report.ok
    assert "user_stories.partial_brd_references" in {i.code for i in report.warnings}


def test_user_stories_dangling_reference_is_warning_only_with_brd_context():
    without = check_user_stories(US_DANGLING)
    assert "user_stories.dangling_brd_references" not in {i.code for i in without.warnings}

    with_ctx = check_user_stories(US_DANGLING, brd_text=VALID_BRD)
    assert with_ctx.ok
    assert "user_stories.dangling_brd_references" in {i.code for i in with_ctx.warnings}


# --- Test Case contract ---------------------------------------------

def test_valid_test_cases_have_no_issues():
    report = check_test_cases(VALID_TC)
    assert report.ok
    assert report.issues == ()


def test_test_cases_with_zero_identifiers_is_blocking():
    report = check_test_cases(TC_NONE)
    assert not report.ok
    assert [i.code for i in report.blocking] == ["test_cases.no_identifiers"]


def test_test_cases_without_any_reference_fields_is_warning():
    report = check_test_cases(TC_NO_REFS)
    assert report.ok
    assert "test_cases.no_references" in {i.code for i in report.warnings}


# --- HLD / LLD: no invented invariants this phase --------------------

@pytest.mark.parametrize("text", ["", "# whatever\n\nprose", None])
def test_hld_and_lld_contracts_are_noops(text):
    for report in (check_hld(text), check_lld(text)):
        assert report.ok
        assert report.issues == ()


# --- enforce() --------------------------------------------------------

def test_enforce_raises_on_blocking_and_attaches_report():
    report = check_brd(BRD_NO_IDS)
    with pytest.raises(ArtifactContractError) as ei:
        enforce(report)
    assert ei.value.report is report
    assert "structural contract" in str(ei.value)


def test_enforce_returns_report_unchanged_when_only_warnings():
    report = check_brd(BRD_UNTITLED)
    assert enforce(report) is report


def test_enforce_returns_clean_report_unchanged():
    report = check_brd(VALID_BRD)
    assert enforce(report) is report


# --- immutability / source-of-truth --------------------------------

def test_check_functions_do_not_mutate_input_and_are_idempotent():
    original = VALID_US
    first = check_user_stories(original)
    second = check_user_stories(original)
    assert original == VALID_US
    assert [i.code for i in first.issues] == [i.code for i in second.issues]


def test_report_and_issue_are_frozen():
    with pytest.raises(Exception):
        ContractIssue(code="x", severity="warning", message="m").code = "y"
    with pytest.raises(Exception):
        ContractReport(artifact="brd").artifact = "hld"


@pytest.mark.parametrize("text", [VALID_BRD, BRD_NO_IDS, BRD_UNTITLED, BRD_ONE_REQ])
def test_brd_blocking_decision_tracks_the_extractor(text):
    assert check_brd(text).ok == bool(extract_brd_requirements(text))


@pytest.mark.parametrize("text", [VALID_US, US_NO_HEADINGS, US_NO_REFS])
def test_user_story_blocking_decision_tracks_the_extractor(text):
    assert check_user_stories(text).ok == bool(extract_user_stories(text))


@pytest.mark.parametrize("text", [VALID_TC, TC_NONE, TC_NO_REFS])
def test_test_case_blocking_decision_tracks_the_extractor(text):
    assert check_test_cases(text).ok == bool(extract_test_cases(text))


# --- no raw / secret content in contract errors -------------------

_SECRET = "sk-LEAKED-SECRET-9f3a2b1c0d"

BRD_WITH_SECRET_NO_IDS = f"""# Acme - Business Requirement Document

## Notes
Internal deploy key {_SECRET} was pasted here by mistake and there are no
requirement identifiers at all in this document.
"""


def test_contract_error_message_contains_no_artifact_text_or_secret():
    err = ArtifactContractError(check_brd(BRD_WITH_SECRET_NO_IDS))
    text = str(err)
    assert _SECRET not in text
    assert "pasted here by mistake" not in text          # no artifact prose
    assert "structural contract" in text                 # app-authored wording


def test_error_taxonomy_classifies_contract_failure_safely():
    from app.utils.errors import ErrorCategory, classify

    err = ArtifactContractError(check_brd(BRD_WITH_SECRET_NO_IDS))
    classified = classify(err)
    assert classified.category is ErrorCategory.STATE_INVALID
    assert classified.code == "state.invalid"
    assert _SECRET not in classified.user_message
    assert "structural contract" in classified.user_message


# --- generation-service integration ------------------------------

class _BadBRDAgent:
    """BA agent stub whose BRD has no requirement identifiers at all."""

    def generate_brd(self, clean_sow, metadata):
        return "# BRD\n\n## Summary\nProse only, zero FR/NFR/BR identifiers.\n"

    def refine_brd(self, current_brd, user_feedback, current_version):
        return "# BRD\n\n## Summary\nStill no identifiers after refine.\n"


class _BadStoryAgent:
    def generate_stories(self, brd_text, metadata):
        return "# Draft User Stories\n\nNarrative prose, no '## US-' headings.\n"

    def refine_stories(self, current_stories, user_feedback, current_version):
        return "# Draft User Stories\n\nStill no headings.\n"


def test_brd_generation_blocks_and_persists_nothing(sow_file, sample_metadata):
    from app.agents.business_analyst.service import BusinessAnalystService

    ba = BusinessAnalystService(project_id="proj", agent=_BadBRDAgent())
    with pytest.raises(ArtifactContractError):
        ba.generate_initial_brd(sow_file, sample_metadata)
    assert ba.get_all_versions() == []                   # nothing was written


def test_user_story_generation_blocks_and_persists_nothing(
    stub_ba_agent, sow_file, sample_metadata
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService

    ba = BusinessAnalystService(project_id="proj", agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    us = InitialUserStoryService(
        project_id="proj", ba_service=ba, agent=_BadStoryAgent()
    )
    with pytest.raises(ArtifactContractError):
        us.generate_initial_stories()
    assert us.get_all_versions() == []


def test_existing_brd_and_story_success_paths_still_produce_v1(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService

    ba = BusinessAnalystService(project_id="proj", agent=stub_ba_agent)
    assert ba.generate_initial_brd(sow_file, sample_metadata).version == 1
    ba.choose_final_brd(1)

    us = InitialUserStoryService(project_id="proj", ba_service=ba, agent=stub_us_agent)
    assert us.generate_initial_stories().version == 1


def test_test_case_generation_success_path_and_clean_contract(
    stub_ba_agent, stub_us_agent, stub_tc_agent, sow_file, sample_metadata
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.test_case.service import TestCaseService

    ba = BusinessAnalystService(project_id="proj", agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = InitialUserStoryService(project_id="proj", ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    tc = TestCaseService(project_id="proj", agent=stub_tc_agent)
    version = tc.generate()
    assert version.version == 1
    # the persisted document satisfies its own contract
    assert enforce(check_test_cases(version.content)).ok
