"""
Phase 9A — Traceability & Quality Report (backend only).

Deterministic tests only: pure-function unit tests on hand-built Markdown
fixtures (covering flat/grouped-decimal FR ids, missing optional fields, and
more than one heading-dash style), plus an end-to-end test of
`build_project_traceability_report` using the REAL BA/SA/US/LLD/QA services
wired to the existing stub agents from `tests/conftest.py` (same convention
every other test file in this repo already uses). No Gemini calls anywhere.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.quality.traceability import (
    Requirement,
    TestCaseRecord,
    UserStoryRecord,
    build_project_traceability_report,
    extract_brd_requirements,
    extract_test_cases,
    extract_user_stories,
    is_reference_grounded,
    requirement_to_stories,
    requirement_to_test_cases,
    story_to_test_cases,
)
from tests.conftest import (
    STUB_BRD,
    STUB_HLD,
    STUB_LLD,
    STUB_TEST_CASES,
    STUB_USER_STORIES,
    StubBAAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

PID = "quality9a"


# --- 1. extraction: BRD requirements ----------------------------------------

def test_extract_brd_requirements_flat_ids():
    brd = "## 8. Functional Requirements\n**FR-1: Login**\ntext\n**FR-2: Logout**\ntext\n"
    reqs = extract_brd_requirements(brd)
    assert [r.id for r in reqs] == ["FR-1", "FR-2"]
    assert reqs[0] == Requirement(id="FR-1", kind="FR", title="Login")


def test_extract_brd_requirements_grouped_decimal_ids():
    brd = (
        "## 8. Functional Requirements\n"
        "### 8.1. Catalog\n"
        "*   **FR-1.1: Product CRUD Operations**  \n    text\n"
        "*   **FR-1.2: Product Variants**  \n    text\n"
    )
    reqs = extract_brd_requirements(brd)
    assert [r.id for r in reqs] == ["FR-1.1", "FR-1.2"]
    assert reqs[0].title == "Product CRUD Operations"


def test_extract_brd_requirements_nfr_ids():
    brd = "## 9. Non-Functional Requirements\n**NFR-1: Performance**\ntext\n"
    reqs = extract_brd_requirements(brd)
    assert reqs == [Requirement(id="NFR-1", kind="NFR", title="Performance")]


def test_extract_brd_requirements_br_ids():
    brd = "## 10. Business Rules\n*   **BR-1: Inventory Reservation**  \n    text\n"
    reqs = extract_brd_requirements(brd)
    assert reqs == [Requirement(id="BR-1", kind="BR", title="Inventory Reservation")]


def test_extract_brd_requirements_plain_non_bold_ids_are_still_found():
    """Matches the actual STUB_BRD style: 'FR-1. The system shall...' - no bold."""
    reqs = extract_brd_requirements(STUB_BRD)
    assert reqs == [Requirement(id="FR-1", kind="FR", title=None)]


def test_extract_brd_requirements_empty_or_none():
    assert extract_brd_requirements(None) == []
    assert extract_brd_requirements("") == []
    assert extract_brd_requirements("no requirements mentioned here") == []


def test_extract_brd_requirements_deduplicates_and_keeps_first_title():
    brd = "**FR-1: Real Title**\ntext\nSee FR-1 again later.\n"
    reqs = extract_brd_requirements(brd)
    assert reqs == [Requirement(id="FR-1", kind="FR", title="Real Title")]


# --- 1. extraction: User Stories --------------------------------------------

def test_extract_user_stories_basic_and_brd_reference():
    us = (
        "## US-001 — Manage Products\n\n"
        "**BRD Reference:** FR-1.1\n\n"
        "## US-002 — Configure Variants\n\n"
        "**BRD Reference:** FR-1.2, BR-1\n"
    )
    stories = extract_user_stories(us)
    assert stories == [
        UserStoryRecord(id="US-001", title="Manage Products", brd_references=["FR-1.1"]),
        UserStoryRecord(id="US-002", title="Configure Variants", brd_references=["FR-1.2", "BR-1"]),
    ]


def test_extract_user_stories_tolerates_double_hyphen_and_missing_dash():
    us = (
        "## US-001 -- Double Hyphen Title\n\n**BRD Reference:** FR-1\n\n"
        "## US-002\nno dash, no title\n"
    )
    stories = extract_user_stories(us)
    assert stories[0].title == "Double Hyphen Title"
    assert stories[1].id == "US-002"
    assert stories[1].title is None


def test_extract_user_stories_missing_brd_reference_line():
    us = "## US-001 — No Reference\n\n**User Story:**\nAs a user...\n"
    stories = extract_user_stories(us)
    assert stories == [UserStoryRecord(id="US-001", title="No Reference", brd_references=[])]


def test_extract_user_stories_lenient_separator_between_multiple_refs():
    """Not assuming comma is the only separator - any FR/NFR/BR token on the
    line is picked up regardless of how it's joined to the others."""
    us = "## US-001 — Title\n\n**BRD Reference:** FR-1 and BR-2; also NFR-3\n"
    stories = extract_user_stories(us)
    assert stories[0].brd_references == ["FR-1", "BR-2", "NFR-3"]


def test_extract_user_stories_empty_or_none():
    assert extract_user_stories(None) == []
    assert extract_user_stories("") == []
    assert extract_user_stories("# Draft User Stories\nno stories yet\n") == []


# --- 1. extraction: Test Cases ----------------------------------------------

def test_extract_test_cases_all_fields():
    tc = (
        "## TC-001 — Create Product\n\n"
        "**Requirement / User Story Reference:** FR-1.1\n"
        "**BRD Reference:** FR-1.1\n"
        "**User Story Reference:** US-001\n"
        "**HLD Reference:** Section 8.0\n"
        "**LLD Reference:** Section 8.2\n"
    )
    cases = extract_test_cases(tc)
    assert cases == [
        TestCaseRecord(
            id="TC-001",
            requirement_or_story_ref="FR-1.1",
            brd_reference="FR-1.1",
            user_story_reference="US-001",
            hld_reference="Section 8.0",
            lld_reference="Section 8.2",
        )
    ]


def test_extract_test_cases_missing_optional_fields_are_none():
    """Mirrors TestCaseService._render_markdown: a falsy field is OMITTED
    entirely, never written as an empty line."""
    tc = "## TC-001 — Minimal\n\n**Requirement / User Story Reference:** US-001\n"
    cases = extract_test_cases(tc)
    assert cases == [
        TestCaseRecord(
            id="TC-001",
            requirement_or_story_ref="US-001",
            brd_reference=None,
            user_story_reference=None,
            hld_reference=None,
            lld_reference=None,
        )
    ]


def test_extract_test_cases_multiple_cases_in_order():
    tc = (
        "## TC-001 — First\n\n**BRD Reference:** FR-1\n\n"
        "## TC-002 — Second\n\n**BRD Reference:** FR-2\n"
    )
    cases = extract_test_cases(tc)
    assert [c.id for c in cases] == ["TC-001", "TC-002"]
    assert [c.brd_reference for c in cases] == ["FR-1", "FR-2"]


def test_extract_test_cases_empty_or_none():
    assert extract_test_cases(None) == []
    assert extract_test_cases("") == []
    assert extract_test_cases("# Test Cases\nnothing here\n") == []


# --- 2. traceability matrix --------------------------------------------------

def test_requirement_to_story_mapping():
    reqs = [Requirement(id="FR-1", kind="FR"), Requirement(id="FR-2", kind="FR")]
    stories = [
        UserStoryRecord(id="US-001", title=None, brd_references=["FR-1"]),
        UserStoryRecord(id="US-002", title=None, brd_references=["FR-1"]),
    ]
    mapping = requirement_to_stories(reqs, stories)
    assert mapping == {"FR-1": ["US-001", "US-002"], "FR-2": []}


def test_story_to_test_case_mapping():
    stories = [UserStoryRecord(id="US-001", title=None), UserStoryRecord(id="US-002", title=None)]
    cases = [
        TestCaseRecord("TC-001", None, None, "US-001", None, None),
        TestCaseRecord("TC-002", None, None, "US-001", None, None),
    ]
    mapping = story_to_test_cases(stories, cases)
    assert mapping == {"US-001": ["TC-001", "TC-002"], "US-002": []}


def test_requirement_to_test_case_mapping_via_brd_reference_and_direct_ref():
    reqs = [Requirement(id="FR-1", kind="FR"), Requirement(id="FR-2", kind="FR")]
    cases = [
        TestCaseRecord("TC-001", None, "FR-1", None, None, None),          # via brd_reference
        TestCaseRecord("TC-002", "FR-2", None, None, None, None),          # via requirement_or_story_ref
    ]
    mapping = requirement_to_test_cases(reqs, cases)
    assert mapping == {"FR-1": ["TC-001"], "FR-2": ["TC-002"]}


def test_missing_mappings_show_up_as_uncovered():
    reqs = [Requirement(id="FR-1", kind="FR"), Requirement(id="FR-2", kind="FR")]
    stories = [UserStoryRecord(id="US-001", title=None, brd_references=["FR-1"])]
    mapping = requirement_to_stories(reqs, stories)
    uncovered = [rid for rid, sids in mapping.items() if not sids]
    assert uncovered == ["FR-2"]


# --- 3. improved reference grounding -----------------------------------------

_LLD_SNIPPET = "## 8. Validation Rules\nAll inputs are validated server-side.\n"


def test_is_reference_grounded_recognizes_decimal_subpoint_against_top_level_section():
    assert is_reference_grounded("Section 8.2", _LLD_SNIPPET) is True
    assert is_reference_grounded("Section 8.0", _LLD_SNIPPET) is True
    assert is_reference_grounded("Section 3.2.1", "## 3. Something\ntext\n") is True


def test_is_reference_grounded_rejects_genuinely_invalid_section():
    assert is_reference_grounded("Section 99.9", _LLD_SNIPPET) is False


def test_is_reference_grounded_exact_substring_still_works_like_the_original():
    assert is_reference_grounded("RegistrationService", "class RegistrationService: ...") is True
    assert is_reference_grounded("NoSuchClass", "class RegistrationService: ...") is False


def test_is_reference_grounded_empty_reference_is_trivially_grounded():
    assert is_reference_grounded(None, _LLD_SNIPPET) is True
    assert is_reference_grounded("", _LLD_SNIPPET) is True
    assert is_reference_grounded("   ", _LLD_SNIPPET) is True


def test_is_reference_grounded_no_document_returns_false():
    assert is_reference_grounded("Section 8.2", None) is False
    assert is_reference_grounded("Section 8.2", "") is False


# --- 4. project-level report --------------------------------------------------

def _generate_full_pipeline(pid, ba_agent, sa_agent, us_agent, lld_agent, tc_agent, sow_file, sample_metadata):
    """Generate v1 of every artifact via the REAL services + given stub agents.
    Mirrors the setup convention used throughout the existing test suite."""
    ba = BusinessAnalystService(project_id=pid, agent=ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=sa_agent)
    sa.generate_initial_hld()

    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=us_agent)
    us.generate_initial_stories()

    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=lld_agent)
    sa.choose_final_hld(1)
    lld.generate_initial_lld()

    tc = TestCaseService(project_id=pid, agent=tc_agent)
    tc.generate()

    return ba, sa, us, lld, tc


def test_build_project_traceability_report_full_pipeline(sow_file, sample_metadata):
    ba, sa, us, lld, tc = _generate_full_pipeline(
        PID, StubBAAgent(), StubSAAgent(), StubUserStoryAgent(), StubLLDAgent(), StubTestCaseAgent(),
        sow_file, sample_metadata,
    )

    report = build_project_traceability_report(
        PID, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )

    assert report["project_id"] == PID
    assert report["brd"] == {"exists": True, "version_used": 1, "version_kind": "final"}
    assert report["user_stories"]["exists"] is True
    assert report["test_cases"]["exists"] is True

    assert report["requirement_ids"] == [{"id": "FR-1", "kind": "FR", "title": None}]
    assert report["user_story_ids"] == [{"id": "US-001", "title": "Customer Registration"}]
    assert report["test_case_ids"] == ["TC-001", "TC-002"]

    assert report["requirement_to_user_stories"] == {"FR-1": ["US-001"]}
    assert report["user_story_to_test_cases"] == {"US-001": ["TC-001", "TC-002"]}
    assert report["requirement_to_test_cases"] == {"FR-1": ["TC-001", "TC-002"]}

    assert report["requirement_to_story_coverage"] == {
        "total": 1, "covered": 1, "coverage_pct": 100.0, "uncovered_ids": [],
    }
    assert report["story_to_test_coverage"] == {
        "total": 1, "covered": 1, "coverage_pct": 100.0, "uncovered_ids": [],
    }

    pop = report["reference_field_population"]
    assert pop["brd_reference"] == {"populated": 2, "total": 2}
    assert pop["user_story_reference"] == {"populated": 2, "total": 2}
    assert pop["hld_reference"] == {"populated": 0, "total": 2}   # STUB_TEST_CASES leaves these None
    assert pop["lld_reference"] == {"populated": 0, "total": 2}

    assert report["ungrounded_references"] == []  # every populated ref in the stub data is grounded


def test_build_project_traceability_report_missing_artifacts_is_graceful(stub_ba_agent):
    """An empty project (no BRD at all) must not raise - every section degrades
    to its empty/None form."""
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    report = build_project_traceability_report(PID, ba_service=ba)

    assert report["brd"] == {"exists": False, "version_used": None, "version_kind": None}
    assert report["requirement_ids"] == []
    assert report["user_story_ids"] == []
    assert report["test_case_ids"] == []
    assert report["requirement_to_user_stories"] == {}
    assert report["requirement_to_story_coverage"] == {
        "total": 0, "covered": 0, "coverage_pct": 0.0, "uncovered_ids": [],
    }
    assert report["ungrounded_references"] == []


def test_build_project_traceability_report_prefers_final_over_latest_version(sow_file, sample_metadata):
    """Version-selection behaviour: when a final version exists, its content is
    used - NOT whatever the latest draft happens to be."""
    stub_ba = StubBAAgent()
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba)
    ba.generate_initial_brd(sow_file, sample_metadata)   # v1
    ba.save_manual_edit(  # v2, draft only - created BEFORE finalizing anything
        "# Test Project — Business Requirement Document\n\nFR-9. A totally different, unfinalized draft.\n"
    )
    ba.choose_final_brd(1)  # v1 (not the newer v2) is the one marked final

    report = build_project_traceability_report(PID, ba_service=ba)
    assert report["brd"] == {"exists": True, "version_used": 1, "version_kind": "final"}
    assert [r["id"] for r in report["requirement_ids"]] == ["FR-1"]  # from v1 (STUB_BRD), not v2's FR-9


def test_build_project_traceability_report_falls_back_to_latest_when_no_final(sow_file, sample_metadata):
    stub_ba = StubBAAgent()
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba)
    ba.generate_initial_brd(sow_file, sample_metadata)  # v1, not final

    report = build_project_traceability_report(PID, ba_service=ba)
    assert report["brd"] == {"exists": True, "version_used": 1, "version_kind": "latest"}


def test_build_project_traceability_report_is_deterministic_across_repeated_calls(sow_file, sample_metadata):
    ba, sa, us, lld, tc = _generate_full_pipeline(
        PID, StubBAAgent(), StubSAAgent(), StubUserStoryAgent(), StubLLDAgent(), StubTestCaseAgent(),
        sow_file, sample_metadata,
    )

    kwargs = dict(ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc)
    r1 = build_project_traceability_report(PID, **kwargs)
    r2 = build_project_traceability_report(PID, **kwargs)
    r3 = build_project_traceability_report(PID, **kwargs)

    assert r1 == r2 == r3
    # round-trips through JSON identically too (no non-serializable/nondeterministic values)
    assert json.loads(json.dumps(r1)) == r1


def test_build_project_traceability_report_never_generates_or_finalizes(monkeypatch, sow_file, sample_metadata):
    """Read-only guarantee: the report must never call any generate_*/refine_*/
    choose_final_*/mark_final/unlock_final* method on any of the five services."""
    ba, sa, us, lld, tc = _generate_full_pipeline(
        PID, StubBAAgent(), StubSAAgent(), StubUserStoryAgent(), StubLLDAgent(), StubTestCaseAgent(),
        sow_file, sample_metadata,
    )

    forbidden_methods = {
        BusinessAnalystService: ["generate_initial_brd", "choose_final_brd", "unlock_final_brd", "save_manual_edit", "refine_with_ai"],
        SolutionArchitectService: ["generate_initial_hld", "choose_final_hld", "unlock_final_hld", "refine_with_ai"],
        InitialUserStoryService: ["generate_initial_stories", "choose_final_stories", "unlock_final_stories", "refine_with_ai"],
        LowLevelDesignService: ["generate_initial_lld", "choose_final_lld", "unlock_final_lld", "refine_with_ai"],
        TestCaseService: ["generate", "regenerate", "refine_with_ai", "choose_final", "unlock_final"],
    }
    calls: list = []
    for cls, methods in forbidden_methods.items():
        for name in methods:
            monkeypatch.setattr(
                cls, name,
                lambda self, *a, __n=name, **kw: calls.append(__n) or (_ for _ in ()).throw(
                    AssertionError(f"traceability report must not call {__n}")
                ),
            )

    build_project_traceability_report(
        PID, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )
    assert calls == []


def test_build_project_traceability_report_default_services_need_no_stub_and_no_generation():
    """Constructing the report with NO service injection (real, non-stub agents
    behind each default service) must still work for an empty project, proving
    the report itself never triggers agent construction to actually GENERATE
    anything (so no Gemini call ever happens even though a real agent object
    - never invoked - exists behind each default service)."""
    report = build_project_traceability_report("quality9a-empty-default")
    assert report["brd"] == {"exists": False, "version_used": None, "version_kind": None}
    assert report["requirement_ids"] == []
