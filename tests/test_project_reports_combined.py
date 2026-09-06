"""
Phase 11A / Item 4 — `build_project_reports_for_project()` combined single-pass
entry point, and the `traceability_report=` reuse parameter on
`build_project_quality_report_for_project()`.

The whole point is that NO reported value changes: the combined call just stops
the traceability matrix + extraction (and, un-injected, a second/third set of
never-used Gemini agents) from being computed twice. These tests build a real
project with the stub agents and assert byte-for-byte report equality against
the two original standalone builders.

Deterministic; no Gemini, no network.
"""

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.quality.project_quality_report import (
    build_project_quality_report_for_project,
    build_project_reports_for_project,
)
from app.quality.traceability import build_project_traceability_report

PID = "p11a_combined"


def _build_full_project(stub_ba_agent, stub_sa_agent, stub_us_agent,
                        stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)

    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)

    tc = TestCaseService(project_id=PID, agent=stub_tc_agent)
    tc.generate()
    return ba, sa, us, lld, tc


def test_combined_report_equals_the_two_standalone_builders(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    _build_full_project(stub_ba_agent, stub_sa_agent, stub_us_agent,
                        stub_lld_agent, stub_tc_agent, sow_file, sample_metadata)

    combined = build_project_reports_for_project(PID)
    quality_standalone = build_project_quality_report_for_project(PID)
    traceability_standalone = build_project_traceability_report(PID)

    assert set(combined) == {"traceability", "quality"}
    assert combined["quality"] == quality_standalone
    assert combined["traceability"] == traceability_standalone


def test_precomputed_traceability_param_does_not_change_quality_output(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    _build_full_project(stub_ba_agent, stub_sa_agent, stub_us_agent,
                        stub_lld_agent, stub_tc_agent, sow_file, sample_metadata)

    trace = build_project_traceability_report(PID)
    without_param = build_project_quality_report_for_project(PID)
    with_param = build_project_quality_report_for_project(PID, traceability_report=trace)

    assert with_param == without_param
    # the grounding findings in particular come straight from the traceability
    # report's ungrounded_references either way
    assert with_param["grounding_findings"] == without_param["grounding_findings"]


def test_combined_report_on_a_brd_only_project(
    stub_ba_agent, sow_file, sample_metadata,
):
    ba = BusinessAnalystService(project_id="p11a_brdonly", agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    combined = build_project_reports_for_project("p11a_brdonly")
    assert combined["quality"] == build_project_quality_report_for_project("p11a_brdonly")
    assert combined["traceability"] == build_project_traceability_report("p11a_brdonly")
    # nothing downstream exists yet
    astat = combined["quality"]["artifact_status"]
    assert astat["brd"]["exists"] is True
    assert astat["test_cases"]["exists"] is False


def test_combined_report_on_an_empty_project():
    combined = build_project_reports_for_project("p11a_empty")
    assert combined["quality"] == build_project_quality_report_for_project("p11a_empty")
    assert combined["traceability"] == build_project_traceability_report("p11a_empty")
    assert combined["quality"]["artifact_status"]["brd"]["exists"] is False


def test_combined_report_accepts_injected_services(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    ba, sa, us, lld, tc = _build_full_project(
        stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
        sow_file, sample_metadata,
    )
    injected = build_project_reports_for_project(
        PID, ba_service=ba, sa_service=sa, us_service=us,
        lld_service=lld, tc_service=tc,
    )
    plain = build_project_reports_for_project(PID)
    assert injected == plain
