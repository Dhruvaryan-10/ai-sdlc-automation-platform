"""
Full-SDLC LangGraph orchestration (Phase 8B).

Phase 8B-1 scope: the smallest possible skeleton —

    START -> resolve_state -> ensure_brd -> gate_brd -> END

plus a pure `sdlc_status(project_id)` snapshot. Only the BRD hop exists so far.

Design rules (mirrors the Phase 8A QA pilot at app/agents/test_case/graph.py):
  * LangGraph orchestrates the EXISTING services; it does not reimplement them.
  * Nodes are thin delegators to `BusinessAnalystService` methods.
  * Graph state carries orchestration pointers only — never full BRDVersion
    objects or version histories.
  * Node functions do not catch exceptions; they propagate out of `.invoke()`.
  * The graph NEVER finalizes (never calls choose_final_brd / mark_final).
  * `VersionService` (JSON) remains the only persistence authority. No
    checkpointer, no interrupt()/HITL, no new persistence.
  * The compiled graph is built per invocation, bound to one service instance;
    it is never cached at module scope.
"""
