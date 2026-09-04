"""
`sdlc_status(project_id)` — a PURE read-only snapshot of the SDLC pipeline.

Phase 8B-4: the BRD hop, the HLD + Initial-User-Story hop, the LLD hop, and the
QA/Test-Case hop are modelled. This function never writes, never generates,
never finalizes, and never invokes the graph. It exists so the UI / callers can
decide what the next runnable step is without side effects.
"""

from __future__ import annotations

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService

# next_step vocabulary
NEXT_GENERATE_BRD = "generate_brd"                 # no BRD version exists yet
NEXT_APPROVE_BRD = "approve_brd"                   # a BRD exists but none is final
NEXT_GENERATE_HLD = "generate_hld"                 # final BRD, but no HLD version yet
NEXT_APPROVE_HLD = "approve_hld"                   # an HLD exists but none is final
NEXT_GENERATE_LLD = "generate_lld"                 # final HLD, but no LLD version yet
NEXT_APPROVE_LLD = "approve_lld"                   # an LLD exists but none is final
NEXT_GENERATE_TEST_CASES = "generate_test_cases"   # final LLD, but no test-case version yet
NEXT_APPROVE_TEST_CASES = "approve_test_cases"     # test cases exist but none is final
NEXT_NONE = None                                   # test cases are final; no further graph steps


def sdlc_status(
    project_id: str,
    *,
    ba_service: BusinessAnalystService | None = None,
    sa_service: SolutionArchitectService | None = None,
    us_service: InitialUserStoryService | None = None,
    lld_service: LowLevelDesignService | None = None,
    tc_service: TestCaseService | None = None,
) -> dict:
    """Return a plain dict describing BRD / HLD / User-Story / LLD / Test-Case
    state and the next runnable step.

    `*_service` are optional injection points for deterministic tests; when
    omitted, real services are constructed for `project_id` (sharing one
    `BusinessAnalystService` / `SolutionArchitectService` as their upstream
    source; `TestCaseService` takes no such upstream dependency — see
    `app/agents/test_case/service.py`). All getters used here are read-only and
    make no Gemini call.
    """
    ba = ba_service or BusinessAnalystService(project_id=project_id)
    sa = sa_service or SolutionArchitectService(project_id=project_id, ba_service=ba)
    us = us_service or InitialUserStoryService(project_id=project_id, ba_service=ba)
    lld = lld_service or LowLevelDesignService(
        project_id=project_id, sa_service=sa, ba_service=ba
    )
    tc = tc_service or TestCaseService(project_id=project_id)

    brd_versions = ba.get_all_versions()
    brd_final = ba.get_final_brd()
    hld_versions = sa.get_all_versions()
    hld_final = sa.get_final_hld()
    us_versions = us.get_all_versions()
    lld_versions = lld.get_all_versions()
    lld_final = lld.get_final_lld()
    tc_versions = tc.get_all_versions()
    tc_final = tc.get_final()

    brd_latest_version = brd_versions[-1].version if brd_versions else None
    brd_final_version = brd_final.version if brd_final else None
    hld_latest_version = hld_versions[-1].version if hld_versions else None
    hld_final_version = hld_final.version if hld_final else None
    us_latest_version = us_versions[-1].version if us_versions else None
    lld_latest_version = lld_versions[-1].version if lld_versions else None
    lld_final_version = lld_final.version if lld_final else None
    tc_latest_version = tc_versions[-1].version if tc_versions else None
    tc_final_version = tc_final.version if tc_final else None

    brd_exists = brd_latest_version is not None
    hld_exists = hld_latest_version is not None
    us_exists = us_latest_version is not None
    lld_exists = lld_latest_version is not None
    tc_exists = tc_latest_version is not None
    awaiting_brd_approval = brd_exists and brd_final_version is None
    awaiting_hld_approval = (
        brd_final_version is not None and hld_exists and hld_final_version is None
    )
    awaiting_lld_approval = (
        hld_final_version is not None and lld_exists and lld_final_version is None
    )
    awaiting_test_cases_approval = (
        lld_final_version is not None and tc_exists and tc_final_version is None
    )

    if not brd_exists:
        next_step = NEXT_GENERATE_BRD
    elif brd_final_version is None:
        next_step = NEXT_APPROVE_BRD
    elif not hld_exists:
        next_step = NEXT_GENERATE_HLD
    elif hld_final_version is None:
        next_step = NEXT_APPROVE_HLD
    elif not lld_exists:
        next_step = NEXT_GENERATE_LLD
    elif lld_final_version is None:
        next_step = NEXT_APPROVE_LLD
    elif not tc_exists:
        next_step = NEXT_GENERATE_TEST_CASES
    elif tc_final_version is None:
        next_step = NEXT_APPROVE_TEST_CASES
    else:
        next_step = NEXT_NONE

    return {
        "project_id": project_id,
        "brd_exists": brd_exists,
        "brd_latest_version": brd_latest_version,
        "brd_final_version": brd_final_version,
        "awaiting_brd_approval": awaiting_brd_approval,
        "hld_exists": hld_exists,
        "hld_latest_version": hld_latest_version,
        "hld_final_version": hld_final_version,
        "awaiting_hld_approval": awaiting_hld_approval,
        "us_exists": us_exists,
        "us_latest_version": us_latest_version,
        "lld_exists": lld_exists,
        "lld_latest_version": lld_latest_version,
        "lld_final_version": lld_final_version,
        "awaiting_lld_approval": awaiting_lld_approval,
        "tc_exists": tc_exists,
        "tc_latest_version": tc_latest_version,
        "tc_final_version": tc_final_version,
        "awaiting_test_cases_approval": awaiting_test_cases_approval,
        "next_step": next_step,
    }
