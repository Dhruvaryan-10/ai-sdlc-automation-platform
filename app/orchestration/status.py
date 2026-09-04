"""
`sdlc_status(project_id)` — a PURE read-only snapshot of the SDLC pipeline.

Phase 8B-2: the BRD hop and the HLD + Initial-User-Story hop are modelled. This
function never writes, never generates, never finalizes, and never invokes the
graph. It exists so the UI / callers can decide what the next runnable step is
without side effects.
"""

from __future__ import annotations

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.solution_architect.service import SolutionArchitectService

# next_step vocabulary
NEXT_GENERATE_BRD = "generate_brd"   # no BRD version exists yet
NEXT_APPROVE_BRD = "approve_brd"     # a BRD exists but none is final
NEXT_GENERATE_HLD = "generate_hld"   # final BRD, but no HLD version yet
NEXT_APPROVE_HLD = "approve_hld"     # an HLD exists but none is final
NEXT_NONE = None                     # HLD is final; 8B-2 has no further graph steps


def sdlc_status(
    project_id: str,
    *,
    ba_service: BusinessAnalystService | None = None,
    sa_service: SolutionArchitectService | None = None,
    us_service: InitialUserStoryService | None = None,
) -> dict:
    """Return a plain dict describing BRD / HLD / User-Story state and the next
    runnable step.

    `*_service` are optional injection points for deterministic tests; when
    omitted, real services are constructed for `project_id` (sharing one
    `BusinessAnalystService` as their BRD source). All getters used here are
    read-only and make no Gemini call.
    """
    ba = ba_service or BusinessAnalystService(project_id=project_id)
    sa = sa_service or SolutionArchitectService(project_id=project_id, ba_service=ba)
    us = us_service or InitialUserStoryService(project_id=project_id, ba_service=ba)

    brd_versions = ba.get_all_versions()
    brd_final = ba.get_final_brd()
    hld_versions = sa.get_all_versions()
    hld_final = sa.get_final_hld()
    us_versions = us.get_all_versions()

    brd_latest_version = brd_versions[-1].version if brd_versions else None
    brd_final_version = brd_final.version if brd_final else None
    hld_latest_version = hld_versions[-1].version if hld_versions else None
    hld_final_version = hld_final.version if hld_final else None
    us_latest_version = us_versions[-1].version if us_versions else None

    brd_exists = brd_latest_version is not None
    hld_exists = hld_latest_version is not None
    us_exists = us_latest_version is not None
    awaiting_brd_approval = brd_exists and brd_final_version is None
    awaiting_hld_approval = (
        brd_final_version is not None and hld_exists and hld_final_version is None
    )

    if not brd_exists:
        next_step = NEXT_GENERATE_BRD
    elif brd_final_version is None:
        next_step = NEXT_APPROVE_BRD
    elif not hld_exists:
        next_step = NEXT_GENERATE_HLD
    elif hld_final_version is None:
        next_step = NEXT_APPROVE_HLD
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
        "next_step": next_step,
    }
