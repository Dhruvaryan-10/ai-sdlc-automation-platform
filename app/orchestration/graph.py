"""
Full-SDLC LangGraph — Phase 8B-3.

    START
      -> resolve_state
      -> ensure_brd
      -> gate_brd
           |-- awaiting_approval --> END        (awaiting = "brd_final")
           '-- complete          --> ensure_hld
                                       -> ensure_user_stories
                                       -> gate_hld
                                            |-- awaiting_approval --> END   (awaiting = "hld_final")
                                            '-- complete          --> ensure_lld
                                                                        -> gate_lld
                                                                             |-- awaiting_approval --> END  (awaiting = "lld_final")
                                                                             '-- complete          --> END

The HLD, Initial-User-Story and LLD hops run *sequentially* (not a true parallel
fan-out): LangGraph 1.2.11 raises InvalidUpdateError if two concurrent branch
nodes write the same state key (`produced`) in one super-step.

Mirrors app/agents/test_case/graph.py: TypedDict state, thin delegator nodes
bound via closures, exceptions propagate, `StateGraph` compiled per invocation.
Nodes NEVER finalize anything (no choose_final_* / mark_final / unlock_final).
`VersionService` (JSON) remains the only persistence authority. No checkpointer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.orchestration.state import SDLCState
from app.utils.logger import get_logger

if TYPE_CHECKING:  # ProjectMetadata is only referenced in type hints / caller code
    from app.agents.business_analyst.agent import ProjectMetadata

logger = get_logger(__name__)

_STATUS_AWAITING_APPROVAL = "awaiting_approval"
_STATUS_COMPLETE = "complete"
_AWAITING_BRD_FINAL = "brd_final"
_AWAITING_HLD_FINAL = "hld_final"
_AWAITING_LLD_FINAL = "lld_final"
_REQUEST_ENSURE_BRD = "ensure_brd"


# --- nodes (thin delegators to the existing services) ----------------------

def _make_resolve_state_node(
    ba_service: "BusinessAnalystService",
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
):
    """START -> resolve_state: read-only; derive BRD/HLD/US/LLD pointers from persistence.

    `sa_service` / `us_service` / `lld_service` default to real services for the
    same project, wired to the SAME `ba_service` (and `sa_service`) — so
    `_make_resolve_state_node(ba_service)` works standalone in tests.
    """
    sa_service = sa_service or SolutionArchitectService(
        project_id=ba_service.project_id, ba_service=ba_service
    )
    us_service = us_service or InitialUserStoryService(
        project_id=ba_service.project_id, ba_service=ba_service
    )
    lld_service = lld_service or LowLevelDesignService(
        project_id=ba_service.project_id, sa_service=sa_service, ba_service=ba_service
    )

    def resolve_state(state: SDLCState) -> dict[str, Any]:
        brd_versions = ba_service.get_all_versions()
        brd_final = ba_service.get_final_brd()
        hld_versions = sa_service.get_all_versions()
        hld_final = sa_service.get_final_hld()
        us_versions = us_service.get_all_versions()
        lld_versions = lld_service.get_all_versions()
        lld_final = lld_service.get_final_lld()
        return {
            "brd_latest_version": brd_versions[-1].version if brd_versions else None,
            "brd_final_version": brd_final.version if brd_final else None,
            "hld_latest_version": hld_versions[-1].version if hld_versions else None,
            "hld_final_version": hld_final.version if hld_final else None,
            "us_latest_version": us_versions[-1].version if us_versions else None,
            "lld_latest_version": lld_versions[-1].version if lld_versions else None,
            "lld_final_version": lld_final.version if lld_final else None,
        }

    return resolve_state


def _make_ensure_brd_node(ba_service: "BusinessAnalystService"):
    """resolve_state -> ensure_brd: generate BRD v1 only when none exists yet."""

    def ensure_brd(state: SDLCState) -> dict[str, Any]:
        if state.get("brd_latest_version") is not None:
            return {}  # a BRD version already exists -> do nothing
        if state.get("request") != _REQUEST_ENSURE_BRD:
            return {}  # not asked to generate

        sow_path = state.get("sow_path")
        metadata = state.get("metadata")
        if not sow_path or metadata is None:
            raise ValueError(
                "ensure_brd needs both sow_path and metadata to generate the "
                "first BRD when none exists"
            )

        version = ba_service.generate_initial_brd(sow_path, metadata)
        produced = dict(state.get("produced") or {})
        produced["brd"] = version.version
        return {"produced": produced, "brd_latest_version": version.version}

    return ensure_brd


def _gate_brd_node(state: SDLCState) -> dict[str, Any]:
    """ensure_brd -> gate_brd: read-only. Reports whether a final BRD exists.

    MUST NOT call choose_final_brd / mark_final / touch persistence.
    """
    if state.get("brd_final_version") is None:
        return {"status": _STATUS_AWAITING_APPROVAL, "awaiting": _AWAITING_BRD_FINAL}
    return {"status": _STATUS_COMPLETE, "awaiting": None}


def _route_after_gate_brd(state: SDLCState) -> str:
    """Conditional edge out of gate_brd:
    awaiting_approval -> END; complete -> the HLD hop (ensure_hld)."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


def _make_ensure_hld_node(sa_service: "SolutionArchitectService"):
    """gate_brd(complete) -> ensure_hld: generate HLD v1 only when none exists yet.

    Reached only after a final BRD exists (gate_brd routes here). Delegates to the
    existing SolutionArchitectService; never finalizes.
    """

    def ensure_hld(state: SDLCState) -> dict[str, Any]:
        if state.get("hld_latest_version") is not None:
            return {}  # an HLD version already exists -> do nothing

        version = sa_service.generate_initial_hld()
        produced = dict(state.get("produced") or {})
        produced["hld"] = version.version
        return {"produced": produced, "hld_latest_version": version.version}

    return ensure_hld


def _make_ensure_user_stories_node(us_service: "InitialUserStoryService"):
    """ensure_hld -> ensure_user_stories: generate draft user stories v1 only when
    none exist yet. Soft downstream context — NO approval gate. Never finalizes.
    """

    def ensure_user_stories(state: SDLCState) -> dict[str, Any]:
        if state.get("us_latest_version") is not None:
            return {}  # a user-story version already exists -> do nothing

        version = us_service.generate_initial_stories()
        produced = dict(state.get("produced") or {})
        produced["us"] = version.version
        return {"produced": produced, "us_latest_version": version.version}

    return ensure_user_stories


def _gate_hld_node(state: SDLCState) -> dict[str, Any]:
    """ensure_user_stories -> gate_hld: read-only. Reports whether a final HLD exists.

    MUST NOT call choose_final_hld / mark_final / unlock_final / touch persistence.
    """
    if state.get("hld_final_version") is None:
        return {"status": _STATUS_AWAITING_APPROVAL, "awaiting": _AWAITING_HLD_FINAL}
    return {"status": _STATUS_COMPLETE, "awaiting": None}


def _route_after_gate_hld(state: SDLCState) -> str:
    """Conditional edge out of gate_hld:
    awaiting_approval -> END; complete -> the LLD hop (ensure_lld)."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


def _make_ensure_lld_node(lld_service: "LowLevelDesignService"):
    """gate_hld(complete) -> ensure_lld: generate LLD v1 only when none exists yet.

    Reached only after a final HLD exists (gate_hld routes here). Delegates to the
    existing LowLevelDesignService; never finalizes. `generate_initial_lld()` is
    NOT itself idempotent, so the `lld_latest_version` guard here is required.
    """

    def ensure_lld(state: SDLCState) -> dict[str, Any]:
        if state.get("lld_latest_version") is not None:
            return {}  # an LLD version already exists -> do nothing

        version = lld_service.generate_initial_lld()
        produced = dict(state.get("produced") or {})
        produced["lld"] = version.version
        return {"produced": produced, "lld_latest_version": version.version}

    return ensure_lld


def _gate_lld_node(state: SDLCState) -> dict[str, Any]:
    """ensure_lld -> gate_lld: read-only. Reports whether a final LLD exists.

    MUST NOT call choose_final_lld / mark_final / unlock_final / touch persistence.
    """
    if state.get("lld_final_version") is None:
        return {"status": _STATUS_AWAITING_APPROVAL, "awaiting": _AWAITING_LLD_FINAL}
    return {"status": _STATUS_COMPLETE, "awaiting": None}


def _route_after_gate_lld(state: SDLCState) -> str:
    """Conditional edge out of gate_lld. Both routes end the run in 8B-3;
    the split exists so 8B-4 can point "complete" at the next node."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


# --- graph construction ----------------------------------------------------

def build_sdlc_graph(
    ba_service: "BusinessAnalystService",
    *,
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
):
    """Compile the 8B-3 SDLC graph.

    `sa_service` / `us_service` / `lld_service` are optional injection points
    (mirrors `ba_service` on `run_step`). When omitted they are constructed for
    the same project and wired to the SAME `ba_service` (and `sa_service`)
    instances so every hop shares one BRD/HLD source. Cheap to build; not cached.
    """
    sa = sa_service or SolutionArchitectService(
        project_id=ba_service.project_id, ba_service=ba_service
    )
    us = us_service or InitialUserStoryService(
        project_id=ba_service.project_id, ba_service=ba_service
    )
    lld = lld_service or LowLevelDesignService(
        project_id=ba_service.project_id, sa_service=sa, ba_service=ba_service
    )

    graph = StateGraph(SDLCState)
    graph.add_node("resolve_state", _make_resolve_state_node(ba_service, sa, us, lld))
    graph.add_node("ensure_brd", _make_ensure_brd_node(ba_service))
    graph.add_node("gate_brd", _gate_brd_node)
    graph.add_node("ensure_hld", _make_ensure_hld_node(sa))
    graph.add_node("ensure_user_stories", _make_ensure_user_stories_node(us))
    graph.add_node("gate_hld", _gate_hld_node)
    graph.add_node("ensure_lld", _make_ensure_lld_node(lld))
    graph.add_node("gate_lld", _gate_lld_node)

    graph.add_edge(START, "resolve_state")
    graph.add_edge("resolve_state", "ensure_brd")
    graph.add_edge("ensure_brd", "gate_brd")
    graph.add_conditional_edges(
        "gate_brd",
        _route_after_gate_brd,
        {_STATUS_AWAITING_APPROVAL: END, _STATUS_COMPLETE: "ensure_hld"},
    )
    graph.add_edge("ensure_hld", "ensure_user_stories")
    graph.add_edge("ensure_user_stories", "gate_hld")
    graph.add_conditional_edges(
        "gate_hld",
        _route_after_gate_hld,
        {_STATUS_AWAITING_APPROVAL: END, _STATUS_COMPLETE: "ensure_lld"},
    )
    graph.add_edge("ensure_lld", "gate_lld")
    graph.add_conditional_edges(
        "gate_lld",
        _route_after_gate_lld,
        {_STATUS_AWAITING_APPROVAL: END, _STATUS_COMPLETE: END},
    )
    return graph.compile()


def run_step(
    project_id: str,
    request: str = _REQUEST_ENSURE_BRD,
    *,
    sow_path: str | None = None,
    metadata: "ProjectMetadata | None" = None,
    ba_service: "BusinessAnalystService | None" = None,
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
) -> SDLCState:
    """Build the SDLC graph and run a single step. Returns the final SDLCState.

    `ba_service` / `sa_service` / `us_service` / `lld_service` are optional
    injection points (mirrors the Phase 8A pattern of passing the service
    explicitly); when omitted, real services are constructed for `project_id`,
    sharing one `BusinessAnalystService` (and `SolutionArchitectService`) as
    their upstream source.
    """
    service = ba_service or BusinessAnalystService(project_id=project_id)
    compiled = build_sdlc_graph(
        service,
        sa_service=sa_service,
        us_service=us_service,
        lld_service=lld_service,
    )

    initial: SDLCState = {
        "project_id": project_id,
        "request": request,
        "sow_path": sow_path,
        "metadata": metadata,
        "produced": {},
    }
    logger.info("SDLC 8B-3: run_step project=%s request=%s", project_id, request)
    final_state: SDLCState = compiled.invoke(initial)
    logger.info(
        "SDLC 8B-3: run_step project=%s status=%s awaiting=%s produced=%s",
        project_id, final_state.get("status"), final_state.get("awaiting"),
        final_state.get("produced"),
    )
    return final_state
