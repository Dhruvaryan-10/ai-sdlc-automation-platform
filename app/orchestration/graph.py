"""
Full-SDLC LangGraph — Phase 8B-5.

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
                                                                             '-- complete          --> ensure_test_cases
                                                                                                          -> gate_test_cases
                                                                                                               |-- awaiting_approval --> END  (awaiting = "tc_final")
                                                                                                               '-- complete          --> END

The HLD, Initial-User-Story, LLD and QA/Test-Case hops run *sequentially* (not a
true parallel fan-out): LangGraph 1.2.11 raises InvalidUpdateError if two
concurrent branch nodes write the same state key (`produced`) in one super-step.

QA/Test-Case integration (8B-4) delegates DIRECTLY to the existing
`TestCaseService.generate()` — the SAME public method the UI calls. It does NOT
nest or invoke the Phase 8A QA LangGraph pilot (`app/agents/test_case/graph.py`
`build_qa_graph` / `run_qa`), which remains an untouched, orchestration-agnostic
internal implementation detail of `TestCaseService` (see its `use_graph` flag).
`TestCaseService`'s own hard prerequisite is only a final BRD; this orchestration
graph intentionally imposes a *stricter* gate (final LLD) because test cases are
the last artifact in this pipeline — it does not change what `TestCaseService`
itself requires or allows when called directly/outside the graph.

Mirrors app/agents/test_case/graph.py: TypedDict state, thin delegator nodes
bound via closures, exceptions propagate, `StateGraph` compiled per invocation.
Nodes NEVER finalize anything (no choose_final* / mark_final / unlock_final).
`VersionService` (JSON) remains the only persistence authority. No checkpointer.

Phase 8B-5 adds `refine_user_stories_step()` — an EXPLICIT, human-requested User
Story Refinement action. It is deliberately NOT wired into the compiled graph
above (no new node, no conditional routing, no new `run_step(request=...)`
value): `UserStoryRefinementService.refine()` is intentionally non-idempotent
(each explicit call creates the next version, v1 -> v2 -> v3 -> ...), which is
fundamentally incompatible with the `ensure_*` nodes' "generate exactly once,
guarded by a *_latest_version check" pattern used everywhere else in this graph.
Keeping it a plain function outside `build_sdlc_graph()` guarantees, by
construction, that a normal `run_step()` invocation can never trigger it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.agents.user_story_refinement.service import UserStoryRefinementService
from app.orchestration.state import SDLCState
from app.utils.logger import get_logger

if TYPE_CHECKING:  # referenced only in type hints / caller code
    from app.agents.business_analyst.agent import ProjectMetadata
    from app.services.version_service import BRDVersion

logger = get_logger(__name__)

_STATUS_AWAITING_APPROVAL = "awaiting_approval"
_STATUS_COMPLETE = "complete"
_AWAITING_BRD_FINAL = "brd_final"
_AWAITING_HLD_FINAL = "hld_final"
_AWAITING_LLD_FINAL = "lld_final"
_AWAITING_TC_FINAL = "tc_final"
_REQUEST_ENSURE_BRD = "ensure_brd"


# --- nodes (thin delegators to the existing services) ----------------------

def _make_resolve_state_node(
    ba_service: "BusinessAnalystService",
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
    tc_service: "TestCaseService | None" = None,
):
    """START -> resolve_state: read-only; derive BRD/HLD/US/LLD/TC pointers from persistence.

    `sa_service` / `us_service` / `lld_service` default to real services for the
    same project, wired to the SAME `ba_service` (and `sa_service`) — so
    `_make_resolve_state_node(ba_service)` works standalone in tests.
    `tc_service` defaults to a plain `TestCaseService(project_id=...)` — it takes
    no other service as a constructor dependency (see `TestCaseService.__init__`).
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
    tc_service = tc_service or TestCaseService(project_id=ba_service.project_id)

    def resolve_state(state: SDLCState) -> dict[str, Any]:
        brd_versions = ba_service.get_all_versions()
        brd_final = ba_service.get_final_brd()
        hld_versions = sa_service.get_all_versions()
        hld_final = sa_service.get_final_hld()
        us_versions = us_service.get_all_versions()
        lld_versions = lld_service.get_all_versions()
        lld_final = lld_service.get_final_lld()
        tc_versions = tc_service.get_all_versions()
        tc_final = tc_service.get_final()
        return {
            "brd_latest_version": brd_versions[-1].version if brd_versions else None,
            "brd_final_version": brd_final.version if brd_final else None,
            "hld_latest_version": hld_versions[-1].version if hld_versions else None,
            "hld_final_version": hld_final.version if hld_final else None,
            "us_latest_version": us_versions[-1].version if us_versions else None,
            "lld_latest_version": lld_versions[-1].version if lld_versions else None,
            "lld_final_version": lld_final.version if lld_final else None,
            "tc_latest_version": tc_versions[-1].version if tc_versions else None,
            "tc_final_version": tc_final.version if tc_final else None,
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
    """Conditional edge out of gate_lld:
    awaiting_approval -> END; complete -> the QA/Test-Case hop (ensure_test_cases)."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


def _make_ensure_test_cases_node(tc_service: "TestCaseService"):
    """gate_lld(complete) -> ensure_test_cases: generate test cases only when none exist yet.

    Reached only after a final LLD exists (gate_lld routes here) — a deliberately
    STRICTER gate than `TestCaseService` itself imposes (it only hard-requires a
    final BRD; HLD/LLD/User Stories are optional context — see
    `TestCaseService._require_final_brd` / `_gather_optional`). This orchestration
    graph chooses to wait for a final LLD because test cases are the last artifact
    in the modelled pipeline; calling `TestCaseService.generate()` directly
    (outside the graph, e.g. from the UI) is unaffected by this stricter gate.

    Delegates to the EXISTING `TestCaseService.generate()` — the same public
    method the UI calls — never the Phase 8A QA LangGraph pilot
    (`app/agents/test_case/graph.py`). Never finalizes. `generate()` is NOT
    itself idempotent, so the `tc_latest_version` guard here is required (same
    shape as `_make_ensure_lld_node`).
    """

    def ensure_test_cases(state: SDLCState) -> dict[str, Any]:
        if state.get("tc_latest_version") is not None:
            return {}  # a test-case version already exists -> do nothing

        version = tc_service.generate()
        produced = dict(state.get("produced") or {})
        produced["tc"] = version.version
        return {"produced": produced, "tc_latest_version": version.version}

    return ensure_test_cases


def _gate_test_cases_node(state: SDLCState) -> dict[str, Any]:
    """ensure_test_cases -> gate_test_cases: read-only. Reports whether a final
    (approved) test-case version exists.

    MUST NOT call choose_final / mark_final / unlock_final / touch persistence.
    """
    if state.get("tc_final_version") is None:
        return {"status": _STATUS_AWAITING_APPROVAL, "awaiting": _AWAITING_TC_FINAL}
    return {"status": _STATUS_COMPLETE, "awaiting": None}


def _route_after_gate_test_cases(state: SDLCState) -> str:
    """Conditional edge out of gate_test_cases. Both routes end the run — test
    cases are the last artifact in the current pipeline."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


# --- graph construction ----------------------------------------------------

def build_sdlc_graph(
    ba_service: "BusinessAnalystService",
    *,
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
    tc_service: "TestCaseService | None" = None,
):
    """Compile the 8B-4 SDLC graph.

    `sa_service` / `us_service` / `lld_service` / `tc_service` are optional
    injection points (mirrors `ba_service` on `run_step`). When omitted they are
    constructed for the same project; `sa`/`us`/`lld` are wired to the SAME
    `ba_service` (and `sa_service`) instances so every hop shares one BRD/HLD
    source. `TestCaseService` takes no other service as a constructor dependency
    (it reads BRD/HLD/LLD/User-Story context via its own `VersionService`
    instances — see `app/agents/test_case/service.py`), so `tc` is constructed
    from `project_id` alone. Cheap to build; not cached.
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
    tc = tc_service or TestCaseService(project_id=ba_service.project_id)

    graph = StateGraph(SDLCState)
    graph.add_node("resolve_state", _make_resolve_state_node(ba_service, sa, us, lld, tc))
    graph.add_node("ensure_brd", _make_ensure_brd_node(ba_service))
    graph.add_node("gate_brd", _gate_brd_node)
    graph.add_node("ensure_hld", _make_ensure_hld_node(sa))
    graph.add_node("ensure_user_stories", _make_ensure_user_stories_node(us))
    graph.add_node("gate_hld", _gate_hld_node)
    graph.add_node("ensure_lld", _make_ensure_lld_node(lld))
    graph.add_node("gate_lld", _gate_lld_node)
    graph.add_node("ensure_test_cases", _make_ensure_test_cases_node(tc))
    graph.add_node("gate_test_cases", _gate_test_cases_node)

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
        {_STATUS_AWAITING_APPROVAL: END, _STATUS_COMPLETE: "ensure_test_cases"},
    )
    graph.add_edge("ensure_test_cases", "gate_test_cases")
    graph.add_conditional_edges(
        "gate_test_cases",
        _route_after_gate_test_cases,
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
    tc_service: "TestCaseService | None" = None,
) -> SDLCState:
    """Build the SDLC graph and run a single step. Returns the final SDLCState.

    `ba_service` / `sa_service` / `us_service` / `lld_service` / `tc_service` are
    optional injection points (mirrors the Phase 8A pattern of passing the
    service explicitly); when omitted, real services are constructed for
    `project_id`, sharing one `BusinessAnalystService` (and
    `SolutionArchitectService`) as their upstream source.
    """
    service = ba_service or BusinessAnalystService(project_id=project_id)
    compiled = build_sdlc_graph(
        service,
        sa_service=sa_service,
        us_service=us_service,
        lld_service=lld_service,
        tc_service=tc_service,
    )

    initial: SDLCState = {
        "project_id": project_id,
        "request": request,
        "sow_path": sow_path,
        "metadata": metadata,
        "produced": {},
    }
    logger.info("SDLC 8B-4: run_step project=%s request=%s", project_id, request)
    final_state: SDLCState = compiled.invoke(initial)
    logger.info(
        "SDLC 8B-4: run_step project=%s status=%s awaiting=%s produced=%s",
        project_id, final_state.get("status"), final_state.get("awaiting"),
        final_state.get("produced"),
    )
    return final_state


# --- 8B-5: explicit, human-requested User Story Refinement action ----------
#
# NOT a graph node. NOT reachable from `run_step()` / `build_sdlc_graph()` under
# any `request` value. A separate, plain orchestration-level entry point that
# does nothing but delegate to the EXISTING `UserStoryRefinementService.refine()`
# — the same method the Step 6 Streamlit UI already calls directly. See the
# module docstring above for why this is intentionally NOT a graph node.


def refine_user_stories_step(
    project_id: str,
    *,
    us_service: "UserStoryRefinementService | None" = None,
) -> "BRDVersion":
    """Explicitly trigger one User Story Refinement pass. Returns the new version.

    `us_service` is an optional injection point (mirrors the pattern used by
    `run_step` / `build_sdlc_graph`); when omitted, a plain
    `UserStoryRefinementService(project_id=project_id)` is constructed — this
    service takes no other service as a constructor dependency.

    This function contains NO refinement business logic of its own: it does not
    check prerequisites, compute version numbers, stamp source/provenance, derive
    metadata, touch persistence, or check/change lock state. All of that remains
    entirely owned by `UserStoryRefinementService.refine()`; any exception it
    raises (`NoFinalBRDError`, `NoInitialUserStoriesError`,
    `RefinementLockedError`, or an agent error) propagates unchanged.

    Deliberately NOT idempotent: `UserStoryRefinementService.refine()` is itself
    non-idempotent by design (repeated calls intentionally create v2, v3, v4,
    ...), so — unlike every `ensure_*` node in this module — this function MUST
    NOT guard on `us_latest_version` or any other "already exists" check.

    NEVER finalizes: calls `refine()` only. Never `choose_final_stories()`,
    `mark_final()`, or `unlock_final_stories()` — finalization stays a human
    action via the existing Step 4 UI / `InitialUserStoryService`, unchanged.
    """
    service = us_service or UserStoryRefinementService(project_id=project_id)
    logger.info("SDLC 8B-5: refine_user_stories_step project=%s", project_id)
    version = service.refine()
    logger.info(
        "SDLC 8B-5: refine_user_stories_step project=%s produced v%d",
        project_id, version.version,
    )
    return version
