"""
Shared LangGraph orchestration state for the full-SDLC graph (Phase 8B-1/8B-2/8B-3/8B-4).

`SDLCState` holds ONLY orchestration pointers/status. It deliberately does not
carry `BRDVersion` objects, document content, or version histories — those stay
in `VersionService`. `resolve_state` re-derives the pointers from persistence on
every invocation, so the state is safe to rebuild from scratch each run.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from app.agents.business_analyst.agent import ProjectMetadata


def _merge_produced(a: dict, b: dict) -> dict:
    """Reducer for `SDLCState.produced` (Phase 11B).

    Shallow-merges the two branch updates. `ensure_hld` and `ensure_user_stories`
    now run as a concurrent fan-out from `gate_brd` and each writes a DISJOINT
    key ("hld" / "us"), so the merge is order-independent. None-safe.
    """
    return {**(a or {}), **(b or {})}


class SDLCState(TypedDict, total=False):
    """Orchestration state threaded through the SDLC graph.

    `total=False`: every key is optional and populated as the run progresses.
    Every key is written by exactly one node per super-step and uses default
    (last-value) channel semantics — EXCEPT `produced`. Since Phase 11B the HLD
    and Initial User Story hops fan out concurrently from `gate_brd`, so TWO
    nodes write `produced` in the same super-step; it is therefore the one
    reducer-enabled field (`_merge_produced`), a shallow merge of the two
    branches' disjoint "hld" / "us" keys. Without the reducer LangGraph 1.2.11
    raises InvalidUpdateError ("can receive only one value per step").
    """

    # --- identity / inputs ---
    project_id: str
    sow_path: str | None            # consumed only by `ensure_brd` for the first BRD
    metadata: ProjectMetadata | None  # ProjectMetadata for `generate_initial_brd`
    request: str                    # which step the caller asked for, e.g. "ensure_brd"

    # --- BRD pointers (populated by `resolve_state`; never full BRDVersion objects) ---
    brd_latest_version: int | None
    brd_final_version: int | None

    # --- HLD pointers (8B-2; populated by `resolve_state` / `ensure_hld`) ---
    hld_latest_version: int | None
    hld_final_version: int | None

    # --- Initial User Story pointer (8B-2; soft downstream context — no approval gate) ---
    us_latest_version: int | None

    # --- LLD pointers (8B-3; populated by `resolve_state` / `ensure_lld`) ---
    lld_latest_version: int | None
    lld_final_version: int | None

    # --- Test Case (QA) pointers (8B-4; populated by `resolve_state` / `ensure_test_cases`) ---
    tc_latest_version: int | None
    tc_final_version: int | None

    # --- Closure Report pointers (8B-7; populated by `resolve_state` / `ensure_closure_report`) ---
    closure_latest_version: int | None
    closure_final_version: int | None

    # --- results of THIS invocation ---
    # Reducer-enabled (Phase 11B): the concurrent HLD / Initial-US fan-out both
    # write this key in one super-step. `_merge_produced` shallow-merges them.
    produced: Annotated[dict[str, int], _merge_produced]  # e.g. {"hld": 1, "us": 1} — artifacts created this run
    status: str                     # "awaiting_approval" | "complete"
    awaiting: str | None            # blocking gate id, e.g. "brd_final" / "hld_final" / "lld_final" / "tc_final" / "closure_final", or None
