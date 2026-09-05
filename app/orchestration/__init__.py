"""
Full-SDLC LangGraph orchestration (Phase 8B).

Phase 8B-1: the smallest skeleton — START -> resolve_state -> ensure_brd ->
gate_brd -> END. Phase 8B-2 extends it, after a final BRD, with a *sequential*
HLD + Initial-User-Story hop and an HLD approval gate. Phase 8B-3 extends it
again, after a final HLD, with a *sequential* LLD hop and an LLD approval gate.
Phase 8B-4 extends it again, after a final LLD, with a *sequential*
QA/Test-Case hop and a test-case approval gate:

    gate_brd(complete) -> ensure_hld -> ensure_user_stories -> gate_hld
    gate_hld(complete) -> ensure_lld -> gate_lld
    gate_lld(complete) -> ensure_test_cases -> gate_test_cases -> END

plus a pure `sdlc_status(project_id)` snapshot.

The QA/Test-Case hop (8B-4) delegates DIRECTLY to the EXISTING
`TestCaseService.generate()` — the same public method the UI calls. It does NOT
nest or invoke the Phase 8A QA LangGraph pilot
(`app/agents/test_case/graph.py::build_qa_graph` / `run_qa`), which remains an
unmodified, orchestration-agnostic internal implementation detail of
`TestCaseService` (its `use_graph` flag is untouched). `TestCaseService` itself
only hard-requires a final BRD; this orchestration graph intentionally imposes a
STRICTER gate (final LLD) before generating test cases, because test cases are
the last artifact in this modelled pipeline — that choice does not change what
`TestCaseService` allows when called directly, outside the graph.

Phase 8B-5 adds `refine_user_stories_step(project_id, *, us_service=None)` — an
EXPLICIT, human-requested User Story Refinement action that delegates DIRECTLY
to the EXISTING `UserStoryRefinementService.refine()`. Unlike every hop above,
it is DELIBERATELY NOT part of the compiled graph: no new node, no conditional
routing, no new `run_step(request=...)` value, and it does not reuse or change
the existing `request` field's meaning. `UserStoryRefinementService.refine()` is
itself non-idempotent by design (each call intentionally creates the next
version, v1 -> v2 -> v3 -> ...), which is incompatible with the `ensure_*` hops'
"generate exactly once, guarded by a `*_latest_version` check" pattern — so a
normal `run_step()` invocation can never trigger it, by construction, and no new
`SDLCState` fields or `sdlc_status()` fields were added for it (`us_latest_version`
already reflects the newest version, refined or not, since every User Story
version — initial, manually edited, or refined — shares the SAME
`VersionService(subdir="user_stories")` stream). The existing Streamlit Step 6
refinement UI is unaffected — it continues to call `UserStoryRefinementService.
refine()` directly; `refine_user_stories_step()` is an additional entry point,
not a replacement. Known, unchanged, out-of-scope limitation: `LowLevelDesignService`
and `TestCaseService` both consume the FINAL-OR-LATEST user-story version, so an
unapproved refinement can already become downstream context if LLD/QA are
subsequently generated — this pre-dates 8B-5 and is not altered by it.

Design rules (mirrors the Phase 8A QA pilot at app/agents/test_case/graph.py):
  * LangGraph orchestrates the EXISTING services; it does not reimplement them.
  * Nodes are thin delegators to BusinessAnalyst / SolutionArchitect / Initial
    User Story / Low-Level Design / Test Case service methods.
  * Graph state carries orchestration pointers only — never full BRDVersion
    objects or version histories.
  * Node functions do not catch exceptions; they propagate out of `.invoke()`.
  * The graph NEVER finalizes (never calls choose_final_brd / choose_final_hld /
    choose_final_lld / choose_final_stories / TestCaseService.choose_final /
    mark_final / unlock_final). Approval stays a human action.
  * `VersionService` (JSON) remains the only persistence authority. No
    checkpointer, no interrupt()/HITL, no new persistence.
  * The compiled graph is built per invocation, bound to one service instance;
    it is never cached at module scope.
  * Not every orchestration-level action needs to be a graph node —
    `refine_user_stories_step()` (8B-5) and `sdlc_status()` are both plain
    functions precisely because they need no multi-step choreography.
"""
