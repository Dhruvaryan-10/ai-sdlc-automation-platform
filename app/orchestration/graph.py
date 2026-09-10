"""
Full-SDLC LangGraph — Phase 8B-7 (Closure Report hop) + Phase 11B (HLD ∥ US fan-out).

    START
      -> resolve_state
      -> ensure_brd
      -> gate_brd
           |-- awaiting_approval --> END        (awaiting = "brd_final")
           '-- complete          --> [ ensure_hld , ensure_user_stories ]   (Phase 11B: concurrent fan-out)
                                          \\           /
                                           '--> gate_hld                    (fan-in: runs once, after BOTH)
                                            |-- awaiting_approval --> END   (awaiting = "hld_final")
                                            '-- complete          --> ensure_lld
                                                                        -> gate_lld
                                                                             |-- awaiting_approval --> END  (awaiting = "lld_final")
                                                                             '-- complete          --> ensure_test_cases
                                                                                                          -> gate_test_cases
                                                                                                               |-- awaiting_approval --> END  (awaiting = "tc_final")
                                                                                                               '-- complete          --> ensure_closure_report
                                                                                                                                            -> gate_closure_report
                                                                                                                                                 |-- awaiting_approval --> END  (awaiting = "closure_final")
                                                                                                                                                 '-- complete          --> END

The Closure Report hop (8B-7) delegates DIRECTLY to `ClosureReportService.generate()`
— the same public method the UI calls. That service consumes the existing
Traceability and Project Quality reports (read-only `app.quality.*`) plus the
persisted artifacts; those reports are NOT graph nodes (they are pure functions,
not generators with an approval lifecycle). The graph never finalizes the closure
report — `gate_closure_report` only reports whether a human has approved one, and
the run stops there until they do. Re-invoking `run_step` after a closure report
already exists regenerates nothing (the `closure_latest_version` guard).

Phase 11B: `ensure_hld` and `ensure_user_stories` now fan out CONCURRENTLY from
`gate_brd` (both are independent branches off the finalized BRD, write disjoint
`produced` keys "hld"/"us", and persist to isolated version streams). They
fan back in at `gate_hld`, which — via its two incoming plain edges — LangGraph
runs exactly once, after BOTH branches complete (no explicit join node). The
`produced` key carries a reducer (`SDLCState._merge_produced`) so the two
same-step writes merge instead of raising InvalidUpdateError. The LLD and
QA/Test-Case hops remain sequential. Approval gates are unchanged.

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

import functools
import time
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.closure_report.service import ClosureReportService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.agents.user_story_refinement.service import UserStoryRefinementService
from app.observability import pipeline_progress as _progress
from app.orchestration.state import SDLCState
from app.utils.logger import get_logger
from app.utils.run_context import current_run_id, new_run_id, run_context

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
_AWAITING_CLOSURE_FINAL = "closure_final"
_REQUEST_ENSURE_BRD = "ensure_brd"

# Phase 11B: the two independent branches off the finalized BRD. `gate_brd` on
# `complete` fans out to BOTH concurrently; both fan back in at `gate_hld`.
_HLD_US_FANOUT = ["ensure_hld", "ensure_user_stories"]

# Phase 13A: best-effort mapping from a propagated agent-error type to the SDLC
# stage that raised it, so a `run_step` failure line names the failing stage.
# (The per-call `event=llm_call outcome=error` telemetry record already carries
# the exact stage; this is a convenience for the run-level line.)
_EXC_TYPE_TO_STAGE = {
    "BusinessAnalystAgentError": "brd",
    "SolutionArchitectAgentError": "hld",
    "InitialUserStoryAgentError": "user_stories",
    "LLDAgentError": "lld",
    "UserStoryRefinementAgentError": "user_story_refinement",
    "TestCaseAgentError": "test_cases",
    "ClosureReportAgentError": "closure_report",
}


def _stage_from_exception(exc: BaseException) -> str:
    for cls in type(exc).__mro__:
        stage = _EXC_TYPE_TO_STAGE.get(cls.__name__)
        if stage is not None:
            return stage
    return "-"


# --- Phase 15B: optional structured progress channel ----------------------
#
# `run_step(on_event=...)` / `build_sdlc_graph(on_event=...)` accept an OPTIONAL
# callback. When it is None (the default) NOTHING below runs and the compiled
# graph + `run_step` behave EXACTLY as before — no topology change, no state
# change, no telemetry change, no retry change, no exception change, no
# persistence change. When a callback is supplied it receives bounded
# `pipeline_progress.PipelineEvent` objects (never a prompt, model output, API
# key, or exception message).


def _emit(on_event, **fields: Any) -> None:
    """Best-effort structured progress event. No-op when `on_event` is None; a
    callback that raises is swallowed (progress must never break a run — same
    rule as `app.utils.metrics`)."""
    if on_event is None:
        return
    try:
        on_event(_progress.PipelineEvent(**fields))
    except Exception:  # pragma: no cover - defensive; progress must not break a run
        pass


def _instrument(node_fn, *, stage: str, on_event):
    """Wrap an `ensure_*` node so it emits stage start/complete/failed progress
    events. Returns `node_fn` UNCHANGED when `on_event` is None, so the compiled
    graph is byte-identical in the default path. When a callback is supplied the
    wrapper is fully transparent: the node's return value is passed through
    untouched and the ORIGINAL exception is re-raised unchanged (so
    `_stage_from_exception` still classifies it and retries / persistence /
    `event=llm_call` telemetry are unaffected)."""
    if on_event is None:
        return node_fn

    @functools.wraps(node_fn)
    def instrumented(state):
        _emit(on_event, phase=_progress.PHASE_STAGE_STARTED, stage=stage,
              run_id=current_run_id())
        t0 = time.perf_counter()
        try:
            result = node_fn(state)
        except BaseException:
            _emit(on_event, phase=_progress.PHASE_STAGE_FAILED, stage=stage,
                  run_id=current_run_id(), outcome=_progress.OUTCOME_ERROR,
                  elapsed_ms=round((time.perf_counter() - t0) * 1000))
            raise
        _emit(on_event, phase=_progress.PHASE_STAGE_COMPLETED, stage=stage,
              run_id=current_run_id(), outcome=_progress.OUTCOME_SUCCESS,
              elapsed_ms=round((time.perf_counter() - t0) * 1000))
        return result

    return instrumented


# --- nodes (thin delegators to the existing services) ----------------------

def _make_resolve_state_node(
    ba_service: "BusinessAnalystService",
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
    tc_service: "TestCaseService | None" = None,
    closure_service: "ClosureReportService | None" = None,
):
    """START -> resolve_state: read-only; derive BRD/HLD/US/LLD/TC/Closure pointers.

    `sa_service` / `us_service` / `lld_service` default to real services for the
    same project, wired to the SAME `ba_service` (and `sa_service`) — so
    `_make_resolve_state_node(ba_service)` works standalone in tests.
    `tc_service` / `closure_service` default to plain
    `TestCaseService(project_id=...)` / `ClosureReportService(project_id=...)` —
    neither takes another service as a constructor dependency.
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
    closure_service = closure_service or ClosureReportService(
        project_id=ba_service.project_id
    )

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
        closure_versions = closure_service.get_all_versions()
        closure_final = closure_service.get_final()
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
            "closure_latest_version": (
                closure_versions[-1].version if closure_versions else None
            ),
            "closure_final_version": closure_final.version if closure_final else None,
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


def _route_after_gate_brd(state: SDLCState):
    """Conditional edge out of gate_brd. Phase 11B:
    awaiting_approval -> END (BRD approval gate, unchanged);
    complete          -> FAN OUT to BOTH `ensure_hld` and `ensure_user_stories`
                         (they run concurrently and fan back in at gate_hld).
    Returns a list of node names for the fan-out — LangGraph 1.2.11 does not
    accept a list *value* inside a path_map dict, so the router itself returns
    the list and the path_map arg is the flat set of possible destinations."""
    if state.get("status") == _STATUS_COMPLETE:
        return list(_HLD_US_FANOUT)
    return END


def _make_ensure_hld_node(sa_service: "SolutionArchitectService"):
    """gate_brd(complete) -> ensure_hld: generate HLD v1 only when none exists yet.

    Reached only after a final BRD exists (gate_brd routes here). Phase 11B: runs
    CONCURRENTLY with `ensure_user_stories` (independent branch off the finalized
    BRD, own `hld` stream). Delegates to the existing SolutionArchitectService;
    never finalizes.
    """

    def ensure_hld(state: SDLCState) -> dict[str, Any]:
        if state.get("hld_latest_version") is not None:
            return {}  # an HLD version already exists -> do nothing

        version = sa_service.generate_initial_hld()
        # Delta-only update (Phase 11B): the `produced` reducer merges this with
        # the concurrent `ensure_user_stories` write in the same super-step.
        return {"produced": {"hld": version.version}, "hld_latest_version": version.version}

    return ensure_hld


def _make_ensure_user_stories_node(us_service: "InitialUserStoryService"):
    """gate_brd(complete) -> ensure_user_stories: generate draft user stories v1
    only when none exist yet. Phase 11B: runs CONCURRENTLY with `ensure_hld`
    (independent branch off the finalized BRD, own `user_stories` stream). Soft
    downstream context — NO approval gate. Never finalizes.
    """

    def ensure_user_stories(state: SDLCState) -> dict[str, Any]:
        if state.get("us_latest_version") is not None:
            return {}  # a user-story version already exists -> do nothing

        version = us_service.generate_initial_stories()
        # Delta-only update (Phase 11B): the `produced` reducer merges this with
        # the concurrent `ensure_hld` write in the same super-step.
        return {"produced": {"us": version.version}, "us_latest_version": version.version}

    return ensure_user_stories


def _gate_hld_node(state: SDLCState) -> dict[str, Any]:
    """[ensure_hld, ensure_user_stories] -> gate_hld: read-only fan-in. Reports
    whether a final HLD exists. Its two incoming plain edges make LangGraph run
    this node ONCE, after BOTH branches complete (Phase 11B — no explicit join
    node). Only the HLD is gated (User Stories have no approval gate).

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
    """Conditional edge out of gate_test_cases:
    awaiting_approval -> END; complete -> the Closure Report hop (ensure_closure_report)."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


def _make_ensure_closure_report_node(closure_service: "ClosureReportService"):
    """gate_test_cases(complete) -> ensure_closure_report: generate the closure
    report only when none exists yet.

    Reached only after a final (approved) test-case version exists
    (gate_test_cases routes here) — so `ClosureReportService`'s own hard
    prerequisite (a final BRD) is always already satisfied and its
    `NoFinalBRDError` cannot fire on this path. Delegates to the EXISTING
    `ClosureReportService.generate()` — the same public method the UI calls.
    Never finalizes. `generate()` is NOT itself idempotent, so the
    `closure_latest_version` guard here is required (same shape as
    `_make_ensure_test_cases_node`).
    """

    def ensure_closure_report(state: SDLCState) -> dict[str, Any]:
        if state.get("closure_latest_version") is not None:
            return {}  # a closure-report version already exists -> do nothing

        version = closure_service.generate()
        produced = dict(state.get("produced") or {})
        produced["closure"] = version.version
        return {"produced": produced, "closure_latest_version": version.version}

    return ensure_closure_report


def _gate_closure_report_node(state: SDLCState) -> dict[str, Any]:
    """ensure_closure_report -> gate_closure_report: read-only. Reports whether a
    final (approved) closure-report version exists.

    MUST NOT call choose_final / mark_final / unlock_final / touch persistence.
    """
    if state.get("closure_final_version") is None:
        return {"status": _STATUS_AWAITING_APPROVAL, "awaiting": _AWAITING_CLOSURE_FINAL}
    return {"status": _STATUS_COMPLETE, "awaiting": None}


def _route_after_gate_closure_report(state: SDLCState) -> str:
    """Conditional edge out of gate_closure_report. Both routes end the run — the
    closure report is the last artifact in the pipeline, and the graph never
    finalizes it (closure approval stays a human action)."""
    return state.get("status", _STATUS_AWAITING_APPROVAL)


# --- graph construction ----------------------------------------------------

def build_sdlc_graph(
    ba_service: "BusinessAnalystService",
    *,
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
    tc_service: "TestCaseService | None" = None,
    closure_service: "ClosureReportService | None" = None,
    on_event: "_progress.OnEvent | None" = None,
):
    """Compile the full SDLC graph (8B-7: BRD -> HLD/US -> LLD -> Test Cases -> Closure Report).

    `sa_service` / `us_service` / `lld_service` / `tc_service` / `closure_service`
    are optional injection points (mirrors `ba_service` on `run_step`). When
    omitted they are constructed for the same project; `sa`/`us`/`lld` are wired
    to the SAME `ba_service` (and `sa_service`) instances so every hop shares one
    BRD/HLD source. `TestCaseService` and `ClosureReportService` take no other
    service as a constructor dependency (each reads the other streams via its own
    `VersionService` / `app.quality.*`), so `tc` / `closure` are constructed from
    `project_id` alone. Cheap to build; not cached.

    Phase 15B: `on_event` is an OPTIONAL structured-progress callback. When None
    (the default) the six `ensure_*` nodes are registered exactly as before and
    the compiled graph is byte-identical; only the node CALLABLES are wrapped
    (transparently) when a callback is supplied — the node set, every edge, and
    every router are unchanged either way.
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
    closure = closure_service or ClosureReportService(project_id=ba_service.project_id)

    graph = StateGraph(SDLCState)
    graph.add_node(
        "resolve_state",
        _make_resolve_state_node(ba_service, sa, us, lld, tc, closure),
    )
    graph.add_node(
        "ensure_brd",
        _instrument(_make_ensure_brd_node(ba_service),
                    stage=_progress.STAGE_BRD, on_event=on_event),
    )
    graph.add_node("gate_brd", _gate_brd_node)
    graph.add_node(
        "ensure_hld",
        _instrument(_make_ensure_hld_node(sa),
                    stage=_progress.STAGE_HLD, on_event=on_event),
    )
    graph.add_node(
        "ensure_user_stories",
        _instrument(_make_ensure_user_stories_node(us),
                    stage=_progress.STAGE_USER_STORIES, on_event=on_event),
    )
    graph.add_node("gate_hld", _gate_hld_node)
    graph.add_node(
        "ensure_lld",
        _instrument(_make_ensure_lld_node(lld),
                    stage=_progress.STAGE_LLD, on_event=on_event),
    )
    graph.add_node("gate_lld", _gate_lld_node)
    graph.add_node(
        "ensure_test_cases",
        _instrument(_make_ensure_test_cases_node(tc),
                    stage=_progress.STAGE_TEST_CASES, on_event=on_event),
    )
    graph.add_node("gate_test_cases", _gate_test_cases_node)
    graph.add_node(
        "ensure_closure_report",
        _instrument(_make_ensure_closure_report_node(closure),
                    stage=_progress.STAGE_CLOSURE_REPORT, on_event=on_event),
    )
    graph.add_node("gate_closure_report", _gate_closure_report_node)

    graph.add_edge(START, "resolve_state")
    graph.add_edge("resolve_state", "ensure_brd")
    graph.add_edge("ensure_brd", "gate_brd")
    # Phase 11B: gate_brd(complete) fans out to BOTH branches concurrently.
    # `_route_after_gate_brd` returns the list of node names (LangGraph 1.2.11
    # rejects a list *value* in a path_map dict); the path_map arg here is the
    # flat set of possible destinations.
    graph.add_conditional_edges(
        "gate_brd",
        _route_after_gate_brd,
        [*_HLD_US_FANOUT, END],
    )
    # Fan-in: both branches feed gate_hld, which LangGraph runs once after BOTH
    # complete (no explicit join node).
    graph.add_edge("ensure_hld", "gate_hld")
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
        {_STATUS_AWAITING_APPROVAL: END, _STATUS_COMPLETE: "ensure_closure_report"},
    )
    graph.add_edge("ensure_closure_report", "gate_closure_report")
    graph.add_conditional_edges(
        "gate_closure_report",
        _route_after_gate_closure_report,
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
    closure_service: "ClosureReportService | None" = None,
    on_event: "_progress.OnEvent | None" = None,
) -> SDLCState:
    """Build the SDLC graph and run a single step. Returns the final SDLCState.

    `ba_service` / `sa_service` / `us_service` / `lld_service` / `tc_service` /
    `closure_service` are optional injection points (mirrors the Phase 8A pattern
    of passing the service explicitly); when omitted, real services are
    constructed for `project_id`, sharing one `BusinessAnalystService` (and
    `SolutionArchitectService`) as their upstream source.

    Phase 15B: `on_event` is an OPTIONAL structured-progress callback (see
    `app.observability.pipeline_progress`). When None (the default) this function
    behaves EXACTLY as before. When supplied it receives bounded `PipelineEvent`s
    — `run_started` / `stage_started` / `stage_completed` / `stage_failed` /
    `run_failed` / `run_completed` — carrying only `stage`, `run_id`,
    `elapsed_ms`, `outcome`, and the run-summary fields; NEVER a prompt, model
    output, secret, or exception message. A callback that raises is swallowed.
    Telemetry, retries, exceptions, persistence, and graph topology are
    unaffected.
    """
    service = ba_service or BusinessAnalystService(project_id=project_id)
    compiled = build_sdlc_graph(
        service,
        sa_service=sa_service,
        us_service=us_service,
        lld_service=lld_service,
        tc_service=tc_service,
        closure_service=closure_service,
        on_event=on_event,
    )

    initial: SDLCState = {
        "project_id": project_id,
        "request": request,
        "sow_path": sow_path,
        "metadata": metadata,
        "produced": {},
    }

    # Phase 13A: one UUID4 per run_step invocation. `run_context` binds it (and
    # the project id) for the whole graph run — LangGraph copies this context
    # into its sync-node executor, so every stage (incl. the concurrent HLD ∥
    # User-Story fan-out) and every LLM call inherits the same run_id. The
    # context is restored on exit, so sequential/concurrent runs never share it.
    run_id = new_run_id()
    logger.info(
        "run_step start run_id=%s project=%s request=%s", run_id, project_id, request
    )
    _emit(on_event, phase=_progress.PHASE_RUN_STARTED, run_id=run_id)
    started_at = time.perf_counter()
    with run_context(run_id=run_id, project_id=project_id):
        try:
            final_state: SDLCState = compiled.invoke(initial)
        except BaseException as exc:
            logger.error(
                "run_step failed run_id=%s project=%s request=%s stage=%s "
                "error_type=%s elapsed_ms=%s",
                run_id, project_id, request, _stage_from_exception(exc),
                type(exc).__name__, round((time.perf_counter() - started_at) * 1000),
            )
            _emit(
                on_event, phase=_progress.PHASE_RUN_FAILED, run_id=run_id,
                failure_stage=_stage_from_exception(exc),
                outcome=_progress.OUTCOME_ERROR,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000),
            )
            raise  # original exception, unchanged
    logger.info(
        "run_step complete run_id=%s project=%s status=%s awaiting=%s produced=%s "
        "elapsed_ms=%s",
        run_id, project_id, final_state.get("status"), final_state.get("awaiting"),
        final_state.get("produced"), round((time.perf_counter() - started_at) * 1000),
    )
    _emit(
        on_event, phase=_progress.PHASE_RUN_COMPLETED, run_id=run_id,
        pipeline_status=final_state.get("status"),
        awaiting=final_state.get("awaiting"),
        produced=dict(final_state.get("produced") or {}),
        elapsed_ms=round((time.perf_counter() - started_at) * 1000),
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
