"""
Full-SDLC LangGraph — Phase 8B-1 skeleton.

    START -> resolve_state -> ensure_brd -> gate_brd -> END

Only the BRD hop exists. `gate_brd` is a read-only conditional gate: it inspects
whether a *final* BRD exists and routes to END either way (there is no
downstream node yet). It NEVER finalizes anything.

Mirrors app/agents/test_case/graph.py: TypedDict state, thin delegator nodes
bound via closures, exceptions propagate, `StateGraph` compiled per invocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from app.agents.business_analyst.service import BusinessAnalystService
from app.orchestration.state import SDLCState
from app.utils.logger import get_logger

if TYPE_CHECKING:  # ProjectMetadata is only referenced in type hints / caller code
    from app.agents.business_analyst.agent import ProjectMetadata

logger = get_logger(__name__)

_STATUS_AWAITING_APPROVAL = "awaiting_approval"
_STATUS_COMPLETE = "complete"
_AWAITING_BRD_FINAL = "brd_final"
_REQUEST_ENSURE_BRD = "ensure_brd"


# --- nodes (thin delegators to BusinessAnalystService) ----------------------

def _make_resolve_state_node(ba_service: "BusinessAnalystService"):
    """START -> resolve_state: read-only; derive BRD pointers from persistence."""

    def resolve_state(state: SDLCState) -> dict[str, Any]:
        versions = ba_service.get_all_versions()
        final = ba_service.get_final_brd()
        return {
            "brd_latest_version": versions[-1].version if versions else None,
            "brd_final_version": final.version if final else None,
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
    """Conditional edge out of gate_brd. Both routes end the run in 8B-1;
    the split exists so 8B-2 can point "complete" at the next node."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


# --- graph construction ----------------------------------------------------

def build_sdlc_graph(ba_service: "BusinessAnalystService"):
    """Compile the 8B-1 SDLC graph bound to `ba_service`. Cheap; not cached."""
    graph = StateGraph(SDLCState)
    graph.add_node("resolve_state", _make_resolve_state_node(ba_service))
    graph.add_node("ensure_brd", _make_ensure_brd_node(ba_service))
    graph.add_node("gate_brd", _gate_brd_node)

    graph.add_edge(START, "resolve_state")
    graph.add_edge("resolve_state", "ensure_brd")
    graph.add_edge("ensure_brd", "gate_brd")
    graph.add_conditional_edges(
        "gate_brd",
        _route_after_gate_brd,
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
) -> SDLCState:
    """Build the SDLC graph and run a single step. Returns the final SDLCState.

    `ba_service` is an optional injection point (mirrors the Phase 8A pattern of
    passing the service explicitly); when omitted a real
    `BusinessAnalystService(project_id)` is constructed.
    """
    service = ba_service or BusinessAnalystService(project_id=project_id)
    compiled = build_sdlc_graph(service)

    initial: SDLCState = {
        "project_id": project_id,
        "request": request,
        "sow_path": sow_path,
        "metadata": metadata,
        "produced": {},
    }
    logger.info("SDLC 8B-1: run_step project=%s request=%s", project_id, request)
    final_state: SDLCState = compiled.invoke(initial)
    logger.info(
        "SDLC 8B-1: run_step project=%s status=%s awaiting=%s produced=%s",
        project_id, final_state.get("status"), final_state.get("awaiting"),
        final_state.get("produced"),
    )
    return final_state
