"""
QA / Test Case LangGraph orchestration pilot (Phase 8A).

WHAT THIS IS: a *thin* LangGraph workflow that orchestrates the EXISTING QA
generation / refinement flow — nothing more. It does not replace TestCaseAgent
or TestCaseService, does not touch persistence, structured output, retry/backoff,
prompts, or any other SDLC agent.

    START -> prepare -> invoke_agent -> persist -> END

Each node is a 2-4 line delegator to a method that already exists on the passed
`TestCaseService` instance, so behaviour is byte-for-byte identical to calling
`service.generate()` / `service.refine_with_ai()` directly. The graph is only
reached when `TestCaseService` is constructed with `use_graph=True`; the default
path is completely unchanged.

Exceptions raised by the underlying service/agent (`NoFinalBRDError`,
`TestCaseLockedError`, `ValueError`, `TestCaseAgentError`,
`InvalidTestCaseJSONError`) are NOT caught here — LangGraph re-raises them out of
`.invoke()`, so `run_qa()` surfaces exactly the same errors as the direct path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.test_case.service import (
    _NO_HLD_SENTINEL,
    _NO_LLD_SENTINEL,
    _NO_US_SENTINEL,
)
from app.services.version_service import BRDVersion
from app.utils.logger import get_logger

if TYPE_CHECKING:  # avoid an import cycle at module load time
    from app.agents.test_case.service import TestCaseService

logger = get_logger(__name__)

QAMode = Literal["generate", "refine"]

_GENERATE_SOURCE_LABEL = "Generated from artifacts"
_REFINE_SOURCE_LABEL = "Artifact-refined"
_REFINE_NOTE_PREFIX = "Refined"


class QAState(TypedDict, total=False):
    """State carried through the QA graph.

    Written once per key by exactly one node, so no reducers are needed. The
    `TestCaseService` itself is intentionally NOT stored here — it is bound into
    the node callables so the graph stays a pure orchestration layer.
    """

    # inputs
    mode: QAMode
    feedback: str | None
    # resolved by `prepare`
    brd: BRDVersion
    hld: BRDVersion | None
    lld: BRDVersion | None
    us: BRDVersion | None
    metadata: ProjectMetadata
    current_test_cases: str | None   # refine only
    current_version: int | None      # refine only
    # produced by `invoke_agent`
    raw_json: str
    # produced by `persist`
    version: BRDVersion


def _optional_text(version: BRDVersion | None, sentinel: str) -> str:
    """Same substitution `TestCaseService.generate/refine_with_ai` do inline:
    the artifact's content when present, otherwise the '(… available)' sentinel."""
    return version.content if version else sentinel


def _make_prepare_node(service: "TestCaseService"):
    def prepare(state: QAState) -> dict[str, Any]:
        mode = state["mode"]

        # Order matches the existing methods exactly:
        #   generate():        _guard_unlocked -> _require_final_brd
        #   refine_with_ai():  _guard_unlocked -> (latest is None -> ValueError)
        #                                      -> _require_final_brd
        service._guard_unlocked()

        current_test_cases: str | None = None
        current_version: int | None = None
        if mode == "refine":
            latest = service._tc.get_latest_version()
            if latest is None:
                raise ValueError(
                    "No existing test cases to refine. Generate them first."
                )
            current_test_cases = latest.content
            current_version = latest.version

        brd = service._require_final_brd()
        hld, lld, us = service._gather_optional()
        metadata = service._derive_metadata_from_brd(brd.content)

        return {
            "brd": brd,
            "hld": hld,
            "lld": lld,
            "us": us,
            "metadata": metadata,
            "current_test_cases": current_test_cases,
            "current_version": current_version,
        }

    return prepare


def _make_invoke_agent_node(service: "TestCaseService"):
    def invoke_agent(state: QAState) -> dict[str, Any]:
        mode = state["mode"]
        brd = state["brd"]
        metadata = state["metadata"]
        hld_text = _optional_text(state.get("hld"), _NO_HLD_SENTINEL)
        lld_text = _optional_text(state.get("lld"), _NO_LLD_SENTINEL)
        us_text = _optional_text(state.get("us"), _NO_US_SENTINEL)

        if mode == "refine":
            raw = service._agent.refine_test_cases(
                current_test_cases=state["current_test_cases"],
                user_feedback=state["feedback"],
                current_version=state["current_version"],
                brd_text=brd.content,
                hld_text=hld_text,
                lld_text=lld_text,
                user_stories_text=us_text,
                metadata=metadata,
            )
        else:
            raw = service._agent.generate_test_cases(
                brd_text=brd.content,
                hld_text=hld_text,
                lld_text=lld_text,
                user_stories_text=us_text,
                metadata=metadata,
            )
        return {"raw_json": raw}

    return invoke_agent


def _make_persist_node(service: "TestCaseService"):
    def persist(state: QAState) -> dict[str, Any]:
        if state["mode"] == "refine":
            version = service._commit(
                state["raw_json"],
                state["brd"], state.get("hld"), state.get("lld"), state.get("us"),
                state["metadata"],
                source_label=_REFINE_SOURCE_LABEL,
                note_prefix=_REFINE_NOTE_PREFIX,
            )
        else:
            version = service._commit(
                state["raw_json"],
                state["brd"], state.get("hld"), state.get("lld"), state.get("us"),
                state["metadata"],
                source_label=_GENERATE_SOURCE_LABEL,
            )
        return {"version": version}

    return persist


def build_qa_graph(service: "TestCaseService"):
    """Compile the QA orchestration graph bound to `service`.

    START -> prepare -> invoke_agent -> persist -> END  (linear; mode is branched
    on inside the nodes). Cheap to build; not cached.
    """
    graph = StateGraph(QAState)
    graph.add_node("prepare", _make_prepare_node(service))
    graph.add_node("invoke_agent", _make_invoke_agent_node(service))
    graph.add_node("persist", _make_persist_node(service))
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "invoke_agent")
    graph.add_edge("invoke_agent", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def run_qa(
    service: "TestCaseService",
    *,
    mode: QAMode,
    feedback: str | None = None,
) -> BRDVersion:
    """Execute the QA workflow through LangGraph and return the new BRDVersion.

    `mode="generate"` mirrors `TestCaseService.generate()`;
    `mode="refine"` mirrors `TestCaseService.refine_with_ai(feedback)`.
    """
    logger.info("QA LangGraph pilot: running mode=%s", mode)
    compiled = build_qa_graph(service)
    final_state = compiled.invoke({"mode": mode, "feedback": feedback})
    version = final_state["version"]
    logger.info(
        "QA LangGraph pilot: mode=%s produced version v%d", mode, version.version
    )
    return version
