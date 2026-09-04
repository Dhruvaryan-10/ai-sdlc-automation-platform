"""
Full-SDLC LangGraph orchestration (Phase 8B).

Phase 8B-1: the smallest skeleton — START -> resolve_state -> ensure_brd ->
gate_brd -> END. Phase 8B-2 extends it, after a final BRD, with a *sequential*
HLD + Initial-User-Story hop and an HLD approval gate. Phase 8B-3 extends it
again, after a final HLD, with a *sequential* LLD hop and an LLD approval gate:

    gate_brd(complete) -> ensure_hld -> ensure_user_stories -> gate_hld
    gate_hld(complete) -> ensure_lld -> gate_lld -> END

plus a pure `sdlc_status(project_id)` snapshot.

Design rules (mirrors the Phase 8A QA pilot at app/agents/test_case/graph.py):
  * LangGraph orchestrates the EXISTING services; it does not reimplement them.
  * Nodes are thin delegators to BusinessAnalyst / SolutionArchitect / Initial
    User Story / Low-Level Design service methods.
  * Graph state carries orchestration pointers only — never full BRDVersion
    objects or version histories.
  * Node functions do not catch exceptions; they propagate out of `.invoke()`.
  * The graph NEVER finalizes (never calls choose_final_brd / choose_final_hld /
    choose_final_lld / mark_final / unlock_final). Approval stays a human action.
  * `VersionService` (JSON) remains the only persistence authority. No
    checkpointer, no interrupt()/HITL, no new persistence.
  * The compiled graph is built per invocation, bound to one service instance;
    it is never cached at module scope.
"""
