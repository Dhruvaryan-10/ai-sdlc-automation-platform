"""
Shared LangGraph orchestration state for the full-SDLC graph (Phase 8B-1).

`SDLCState` holds ONLY orchestration pointers/status. It deliberately does not
carry `BRDVersion` objects, document content, or version histories — those stay
in `VersionService`. `resolve_state` re-derives the pointers from persistence on
every invocation, so the state is safe to rebuild from scratch each run.
"""

from __future__ import annotations

from typing import TypedDict

from app.agents.business_analyst.agent import ProjectMetadata


class SDLCState(TypedDict, total=False):
    """Orchestration state threaded through the SDLC graph.

    Each key is written by exactly one node, so no reducers are needed.
    `total=False`: every key is optional and populated as the run progresses.
    """

    # --- identity / inputs ---
    project_id: str
    sow_path: str | None            # consumed only by `ensure_brd` for the first BRD
    metadata: ProjectMetadata | None  # ProjectMetadata for `generate_initial_brd`
    request: str                    # which step the caller asked for, e.g. "ensure_brd"

    # --- BRD pointers (populated by `resolve_state`; never full BRDVersion objects) ---
    brd_latest_version: int | None
    brd_final_version: int | None

    # --- results of THIS invocation ---
    produced: dict[str, int]        # {"brd": 1} — artifacts created this run
    status: str                     # "awaiting_approval" | "complete"
    awaiting: str | None            # blocking gate id, e.g. "brd_final", or None
