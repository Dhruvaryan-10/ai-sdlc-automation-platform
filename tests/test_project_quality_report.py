"""
Phase 9A-4 — Project Quality Report (backend only).

Deterministic tests only: pure-function unit tests on hand-built
`Requirement`/`UserStoryRecord`/`TestCaseRecord` fixtures and hand-built
`artifact_status` dicts, plus one end-to-end test of
`build_project_quality_report_for_project` using the REAL BA/SA/US/LLD/QA
services wired to the existing stub agents from `tests/conftest.py` (same
convention `tests/test_quality_traceability.py` already uses). No Gemini
calls anywhere. `app/quality/traceability.py` and its own tests are not
touched by anything here.
"""

import copy
import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.quality.project_quality_report import (
    build_project_quality_report,
    build_project_quality_report_for_project,
)
from app.quality.traceability import (
    Requirement,
    TestCaseRecord,
    UserStoryRecord,
    build_traceability_matrix,
    summarize_traceability_matrix,
)
from tests.conftest import (
    StubBAAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

PID = "pqr9a4"


def _generate_full_pipeline(pid, ba_agent, sa_agent, us_agent, lld_agent, tc_agent, sow_file, sample_metadata):
    """Generate v1 of every artifact via the REAL services + given stub agents.
    Mirrors the exact setup convention `tests/test_quality_traceability.py`
    already uses. BRD + HLD finalized; User Stories, LLD, and Test Cases are
    left as drafts (no gate for US; LLD/TC never explicitly finalized here) -
    a real mix of "final" and "draft_available" artifact states."""
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


def _empty_status() -> dict:
    return {
        "brd": {"exists": False, "latest_version": None, "final_version": None},
        "hld": {"exists": False, "latest_version": None, "final_version": None},
        "user_stories": {"exists": False, "latest_version": None, "final_version": None},
        "lld": {"exists": False, "latest_version": None, "final_version": None},
        "test_cases": {"exists": False, "latest_version": None, "final_version": None},
    }


# --- 1. empty project ---------------------------------------------------

def test_empty_project_all_metrics_zero_and_no_crash():
    report = build_project_quality_report([], [], [], _empty_status())

    assert report["artifact_status"] == _empty_status()
    assert report["requirement_coverage"]["total"] == 0
    assert report["requirement_coverage"]["coverage_pct"] == 0.0
    assert report["user_story_coverage"] == {
        "total": 0, "covered": 0, "coverage_pct": 0.0, "uncovered_ids": [],
    }
    for field in ("requirement_or_story_ref", "brd_reference", "user_story_reference",
                  "hld_reference", "lld_reference"):
        assert report["test_case_reference_population"][field]["populated"] == 0
        assert report["test_case_reference_population"][field]["total"] == 0
    assert report["grounding_findings"] == {"total": 0, "entries": []}
    assert report["orphan_references"] == {"total": 0, "entries": []}


# --- 2. BRD-only project --------------------------------------------------

def test_brd_only_project_requirement_coverage_reflects_no_stories():
    reqs = [Requirement(id="FR-1", kind="FR"), Requirement(id="FR-2", kind="FR")]
    status = dict(_empty_status(), brd={"exists": True, "latest_version": 1, "final_version": 1})

    report = build_project_quality_report(reqs, [], [], status)

    assert report["artifact_status"]["brd"] == {"exists": True, "latest_version": 1, "final_version": 1}
    assert report["requirement_coverage"]["total"] == 2
    assert report["requirement_coverage"]["covered"] == 0
    assert report["requirement_coverage"]["uncovered_ids"] == ["FR-1", "FR-2"]
    assert report["user_story_coverage"]["total"] == 0


# --- 3/6/7/8/9/10. full hand-built fixture -----------------------------

def _full_fixture():
    reqs = [
        Requirement(id="FR-1", kind="FR", title="Login"),
        Requirement(id="BR-1", kind="BR", title="Inventory"),
        Requirement(id="NFR-1", kind="NFR", title=None),
    ]
    stories = [
        UserStoryRecord(id="US-001", title=None, brd_references=["FR-1"]),
        UserStoryRecord(id="US-002", title=None, brd_references=["BR-1"]),
        UserStoryRecord(id="US-003", title=None, brd_references=["FR-99"]),  # orphan ref
    ]
    cases = [
        TestCaseRecord("TC-001", None, "FR-1", "US-001", None, None),               # direct + via US-001
        TestCaseRecord("TC-002", None, None, "US-002", "Section 8.1", None),        # via-story only; HLD ref
        TestCaseRecord("TC-003", "FR-77", None, None, None, "Section 99"),          # orphan ref + no US
    ]
    status = {
        "brd": {"exists": True, "latest_version": 1, "final_version": 1},
        "hld": {"exists": True, "latest_version": 1, "final_version": 1},
        "user_stories": {"exists": True, "latest_version": 1, "final_version": None},
        "lld": {"exists": True, "latest_version": 1, "final_version": None},
        "test_cases": {"exists": True, "latest_version": 1, "final_version": None},
    }
    return reqs, stories, cases, status


def test_full_fixture_project_expected_report_shape():
    reqs, stories, cases, status = _full_fixture()
    ungrounded = [{"test_case_id": "TC-003", "field": "lld_reference", "value": "Section 99"}]

    report = build_project_quality_report(reqs, stories, cases, status, ungrounded_references=ungrounded)

    assert report["artifact_status"] == status

    # 6. requirement_coverage must exactly match summarize_traceability_matrix()
    expected_matrix_summary = summarize_traceability_matrix(build_traceability_matrix(reqs, stories, cases))
    assert report["requirement_coverage"] == expected_matrix_summary
    assert report["requirement_coverage"]["covered"] == 2   # FR-1, BR-1 (NFR-1 uncovered)
    assert report["requirement_coverage"]["uncovered_ids"] == ["NFR-1"]

    # 7. story coverage: covered and uncovered stories
    assert report["user_story_coverage"] == {
        "total": 3, "covered": 2, "coverage_pct": 66.7, "uncovered_ids": ["US-003"],
    }

    # 8. reference population: required vs optional, missing optional not a failure
    pop = report["test_case_reference_population"]
    assert pop["requirement_or_story_ref"] == {"populated": 1, "total": 3, "required": True}
    assert pop["brd_reference"] == {"populated": 1, "total": 3, "required": False}
    assert pop["user_story_reference"] == {"populated": 2, "total": 3, "required": False}
    assert pop["hld_reference"] == {"populated": 1, "total": 3, "required": False}
    assert pop["lld_reference"] == {"populated": 1, "total": 3, "required": False}

    # 9. grounding findings surfaced unchanged
    assert report["grounding_findings"] == {"total": 1, "entries": ungrounded}

    # 10. orphan findings surfaced, and do NOT alter coverage numbers above
    assert report["orphan_references"]["total"] == 2
    assert {"source_id": "US-003", "field": "brd_references", "value": "FR-99"} in report["orphan_references"]["entries"]
    assert {"source_id": "TC-003", "field": "requirement_or_story_ref", "value": "FR-77"} in report["orphan_references"]["entries"]
    # coverage numbers above already reflect the orphan-free, correct mappings -
    # requirement_coverage/user_story_coverage are unaffected by orphan presence.


# --- 4. artifact status: missing / draft / final / latest > final -------

@pytest.mark.parametrize("status_value", [
    {"exists": False, "latest_version": None, "final_version": None},
    {"exists": True, "latest_version": 1, "final_version": None},
    {"exists": True, "latest_version": 1, "final_version": 1},
    {"exists": True, "latest_version": 3, "final_version": 1},
])
def test_artifact_status_passed_through_unchanged_for_every_state(status_value):
    status = dict(_empty_status(), brd=status_value)
    report = build_project_quality_report([], [], [], status)
    assert report["artifact_status"]["brd"] == status_value  # facts only, no derived "defect" label


# --- 5. User Stories always report final_version None (project-level) ----

def test_project_level_user_stories_final_version_always_none(sow_file, sample_metadata):
    ba, sa, us, lld, tc = _generate_full_pipeline(
        PID, StubBAAgent(), StubSAAgent(), StubUserStoryAgent(), StubLLDAgent(), StubTestCaseAgent(),
        sow_file, sample_metadata,
    )
    report = build_project_quality_report_for_project(
        PID, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )
    assert report["artifact_status"]["user_stories"]["exists"] is True
    assert report["artifact_status"]["user_stories"]["latest_version"] == 1
    assert report["artifact_status"]["user_stories"]["final_version"] is None


# --- 11. JSON determinism -------------------------------------------------

def test_report_is_json_serializable_and_round_trips():
    reqs, stories, cases, status = _full_fixture()
    report = build_project_quality_report(reqs, stories, cases, status)
    assert json.loads(json.dumps(report)) == report


# --- 12. input immutability ------------------------------------------------

def test_pure_function_does_not_mutate_its_inputs():
    reqs, stories, cases, status = _full_fixture()
    reqs_snapshot = copy.deepcopy(reqs)
    stories_snapshot = copy.deepcopy(stories)
    cases_snapshot = copy.deepcopy(cases)
    status_snapshot = copy.deepcopy(status)
    ungrounded = [{"test_case_id": "TC-003", "field": "lld_reference", "value": "Section 99"}]
    ungrounded_snapshot = copy.deepcopy(ungrounded)

    build_project_quality_report(reqs, stories, cases, status, ungrounded_references=ungrounded)

    assert reqs == reqs_snapshot
    assert stories == stories_snapshot
    assert cases == cases_snapshot
    assert status == status_snapshot
    assert ungrounded == ungrounded_snapshot


# --- 13. no generation / finalization (project-level) ----------------------

def test_project_level_never_generates_or_finalizes(monkeypatch, sow_file, sample_metadata):
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
                    AssertionError(f"quality report must not call {__n}")
                ),
            )

    build_project_quality_report_for_project(
        PID, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )
    assert calls == []


# --- 14. no Gemini / network -----------------------------------------------

def test_project_level_default_services_need_no_stub_and_no_generation():
    """Constructing the report with NO service injection (real, non-stub
    agents behind each default service) must still work for an empty project,
    proving the report itself never triggers agent construction to actually
    GENERATE anything - so no Gemini call ever happens even though a real
    agent object (never invoked) exists behind each default service."""
    report = build_project_quality_report_for_project("pqr9a4-empty-default")
    assert report["artifact_status"]["brd"] == {"exists": False, "latest_version": None, "final_version": None}
    assert report["requirement_coverage"]["total"] == 0


# --- 15. full stub pipeline integration -------------------------------

def test_full_stub_pipeline_integration_report_shape(sow_file, sample_metadata):
    ba, sa, us, lld, tc = _generate_full_pipeline(
        PID, StubBAAgent(), StubSAAgent(), StubUserStoryAgent(), StubLLDAgent(), StubTestCaseAgent(),
        sow_file, sample_metadata,
    )

    report = build_project_quality_report_for_project(
        PID, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )

    assert report["artifact_status"] == {
        "brd": {"exists": True, "latest_version": 1, "final_version": 1},
        "hld": {"exists": True, "latest_version": 1, "final_version": 1},
        "user_stories": {"exists": True, "latest_version": 1, "final_version": None},
        "lld": {"exists": True, "latest_version": 1, "final_version": None},
        "test_cases": {"exists": True, "latest_version": 1, "final_version": None},
    }
    assert report["requirement_coverage"] == {
        "total": 1, "covered": 1, "coverage_pct": 100.0, "uncovered_ids": [],
        "by_kind": {
            "FR": {"total": 1, "covered": 1, "coverage_pct": 100.0, "uncovered_ids": []},
            "NFR": {"total": 0, "covered": 0, "coverage_pct": 0.0, "uncovered_ids": []},
            "BR": {"total": 0, "covered": 0, "coverage_pct": 0.0, "uncovered_ids": []},
        },
    }
    assert report["user_story_coverage"] == {
        "total": 1, "covered": 1, "coverage_pct": 100.0, "uncovered_ids": [],
    }
    pop = report["test_case_reference_population"]
    assert pop["brd_reference"] == {"populated": 2, "total": 2, "required": False}
    assert pop["hld_reference"] == {"populated": 0, "total": 2, "required": False}  # STUB_TEST_CASES leaves this None
    assert report["grounding_findings"] == {"total": 0, "entries": []}
    assert report["orphan_references"] == {"total": 0, "entries": []}
    assert json.loads(json.dumps(report)) == report
