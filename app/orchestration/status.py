"""
`sdlc_status(project_id)` — a PURE read-only snapshot of the SDLC pipeline.

Phase 8B-1: only the BRD hop is modelled. This function never writes, never
generates, never finalizes, and never invokes the graph. It exists so the UI /
callers can decide what the next runnable step is without side effects.
"""

from __future__ import annotations

from app.agents.business_analyst.service import BusinessAnalystService

# next_step values (8B-1 vocabulary)
NEXT_GENERATE_BRD = "generate_brd"   # no BRD version exists yet
NEXT_APPROVE_BRD = "approve_brd"     # a BRD exists but none is final
NEXT_NONE = None                     # BRD is final; 8B-1 has no further graph steps


def sdlc_status(
    project_id: str,
    *,
    ba_service: BusinessAnalystService | None = None,
) -> dict:
    """Return a plain dict describing the BRD state and the next runnable step.

    `ba_service` is an optional injection point for deterministic tests; when
    omitted a real `BusinessAnalystService(project_id)` is constructed (its
    getters are read-only and make no Gemini call).
    """
    service = ba_service or BusinessAnalystService(project_id=project_id)

    versions = service.get_all_versions()
    final = service.get_final_brd()

    brd_latest_version = versions[-1].version if versions else None
    brd_final_version = final.version if final else None
    brd_exists = brd_latest_version is not None
    awaiting_brd_approval = brd_exists and brd_final_version is None

    if not brd_exists:
        next_step = NEXT_GENERATE_BRD
    elif brd_final_version is None:
        next_step = NEXT_APPROVE_BRD
    else:
        next_step = NEXT_NONE

    return {
        "project_id": project_id,
        "brd_exists": brd_exists,
        "brd_latest_version": brd_latest_version,
        "brd_final_version": brd_final_version,
        "awaiting_brd_approval": awaiting_brd_approval,
        "next_step": next_step,
    }
