"""
Phase 8B-5 — explicit User Story Refinement orchestration action.

    refine_user_stories_step(project_id, *, us_service=None)

This is DELIBERATELY NOT a node in the compiled SDLC graph — no new node, no
conditional routing, no new `run_step(request=...)` value. It is a thin plain
function that delegates directly to the EXISTING
`UserStoryRefinementService.refine()` (the same method the Step 6 Streamlit UI
already calls). It must contain no refinement business logic of its own, must
NOT be idempotent (repeated explicit calls intentionally create v2, v3, v4, ...),
and must never be reachable from a normal `run_step()` pipeline invocation.

Deterministic: BA / SA / US / LLD / TC / USR LLMs are stubs injected via
`agent=` / `*_service=`. No Gemini calls.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.agents.user_story_refinement.service import (
    NoFinalBRDError,
    NoInitialUserStoriesError,
    RefinementLockedError,
    UserStoryRefinementService,
)
from app.orchestration.graph import build_sdlc_graph, refine_user_stories_step, run_step
from app.services.version_service import BRDVersion, VersionService
from tests.conftest import (
    StubBAAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
    StubUserStoryRefinementAgent,
)

PID = "sdlc8b5"


def _svcs(pid=PID):
    """The full 8B-4 service set, all stub-agent-backed (no Gemini)."""
    ba = BusinessAnalystService(project_id=pid, agent=StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=StubUserStoryAgent())
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=StubLLDAgent())
    tc = TestCaseService(project_id=pid, agent=StubTestCaseAgent())
    return ba, sa, us, lld, tc


def _usr(pid=PID):
    return UserStoryRefinementService(project_id=pid, agent=StubUserStoryRefinementAgent())


def _final_brd(ba, sow_file, sample_metadata):
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)


def _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata, pid=PID):
    """Advance a fresh project through the full normal pipeline to a final LLD
    (+ initial user stories v1). Test cases absent."""
    _final_brd(ba, sow_file, sample_metadata)
    run_step(pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
              ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc)  # HLD + US
    sa.choose_final_hld(1)
    run_step(pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
              ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc)  # LLD
    lld.choose_final_lld(1)


# --- A. delegates correctly -------------------------------------------------

def test_refine_user_stories_step_delegates_to_service_refine_exactly_once(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    version = refine_user_stories_step(PID, us_service=usr)

    assert len(stub_usr_agent.refine_calls) == 1
    assert version.version == 2
    assert version.source == "ai_refine"


# --- B. dependency injection -------------------------------------------------

def test_refine_user_stories_step_uses_the_injected_service(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    calls: list = []
    original_refine = usr.refine
    usr.refine = lambda: calls.append("called") or original_refine()

    refine_user_stories_step(PID, us_service=usr)
    assert calls == ["called"]  # the SUPPLIED instance was used, not a fresh one


# --- C. default service construction ----------------------------------------

def test_refine_user_stories_step_constructs_default_service_when_none_supplied(
    monkeypatch, stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    captured: dict = {}
    real_init = UserStoryRefinementService.__init__

    def _spy_init(self, project_id, agent=None):
        captured["project_id"] = project_id
        real_init(self, project_id, agent or stub_usr_agent)

    monkeypatch.setattr(UserStoryRefinementService, "__init__", _spy_init)

    version = refine_user_stories_step(PID)  # no us_service supplied
    assert captured["project_id"] == PID
    assert version.version == 2


# --- D. return value ----------------------------------------------------

def test_refine_user_stories_step_returns_refine_return_value_unchanged(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)

    captured: dict = {}
    original_refine = usr.refine

    def _spy_refine():
        result = original_refine()
        captured["result"] = result
        return result

    usr.refine = _spy_refine
    returned = refine_user_stories_step(PID, us_service=usr)
    assert returned is captured["result"]
    assert isinstance(returned, BRDVersion)


# --- E. repeated explicit refinement -----------------------------------

def test_repeated_explicit_refinement_creates_successive_versions(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()  # v1

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    v2 = refine_user_stories_step(PID, us_service=usr)
    v3 = refine_user_stories_step(PID, us_service=usr)

    assert v2.version == 2
    assert v3.version == 3
    assert [v.version for v in us.get_all_versions()] == [1, 2, 3]
    assert us.get_version(1).source == "initial"  # v1 untouched


# --- F. normal pipeline NEVER refines (critical regression) -----------------

def test_normal_pipeline_never_calls_refine(monkeypatch, stub_ba_agent, sow_file, sample_metadata):
    calls: list = []
    monkeypatch.setattr(
        UserStoryRefinementService, "refine", lambda self: calls.append("refine") or None
    )

    ba, sa, us, lld, tc = _svcs()
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)  # BRD -> HLD -> US -> LLD, all final
    s = run_step(
        PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )  # -> test cases v1

    assert s["produced"] == {"tc": 1}
    assert calls == []  # UserStoryRefinementService.refine() was NEVER called


# --- G. failure propagation ----------------------------------------------

def test_no_final_brd_propagates(stub_usr_agent):
    with pytest.raises(NoFinalBRDError):
        refine_user_stories_step(PID, us_service=_usr())


def test_no_initial_user_stories_propagates(stub_ba_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)  # BRD final, but no user stories yet

    with pytest.raises(NoInitialUserStoriesError):
        refine_user_stories_step(PID, us_service=_usr())


def test_locked_final_stories_reject_refinement(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    us.choose_final_stories(1)  # marks final + locks the WHOLE stream

    with pytest.raises(RefinementLockedError):
        refine_user_stories_step(PID, us_service=_usr())


# --- H. persistence isolation ---------------------------------------------

def test_refinement_touches_only_the_user_stories_stream(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    proj = isolated_output_dir / PID
    brd_before = (proj / "versions.json").read_bytes()

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    refine_user_stories_step(PID, us_service=usr)

    assert (proj / "versions.json").read_bytes() == brd_before  # BRD stream untouched
    other_streams = [p for p in proj.rglob("versions.json") if p.parent.name in ("hld", "lld", "test_cases")]
    assert other_streams == []  # no other stream file was even created
    us_records = json.loads((proj / "user_stories" / "versions.json").read_text(encoding="utf-8"))
    assert len(us_records) == 2  # v1 (initial) + v2 (refined)
    assert all(isinstance(BRDVersion(**rec), BRDVersion) for rec in us_records)


# --- I. direct-vs-orchestration parity --------------------------------------

def test_orchestration_refinement_matches_direct_service_call(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    d_ba = BusinessAnalystService(project_id="usr_parity_direct", agent=StubBAAgent())
    d_ba.generate_initial_brd(sow_file, sample_metadata); d_ba.choose_final_brd(1)
    d_us = InitialUserStoryService(project_id="usr_parity_direct", ba_service=d_ba, agent=StubUserStoryAgent())
    d_us.generate_initial_stories()
    d_usr = UserStoryRefinementService(project_id="usr_parity_direct", agent=StubUserStoryRefinementAgent())
    direct = d_usr.refine()

    g_ba = BusinessAnalystService(project_id="usr_parity_orch", agent=StubBAAgent())
    g_ba.generate_initial_brd(sow_file, sample_metadata); g_ba.choose_final_brd(1)
    g_us = InitialUserStoryService(project_id="usr_parity_orch", ba_service=g_ba, agent=StubUserStoryAgent())
    g_us.generate_initial_stories()
    g_usr = UserStoryRefinementService(project_id="usr_parity_orch", agent=StubUserStoryRefinementAgent())
    orchestrated = refine_user_stories_step("usr_parity_orch", us_service=g_usr)

    # Semantic persisted fields only — not incidental formatting.
    assert orchestrated.content == direct.content
    assert orchestrated.version == direct.version == 2
    assert orchestrated.source == direct.source == "ai_refine"
    assert orchestrated.source_ref == direct.source_ref == "brd_v1;us_v1;hld_vnone;lld_vnone"
    assert orchestrated.note == direct.note


# --- J. finalization invariant ----------------------------------------------

def test_refine_user_stories_step_never_finalizes(
    monkeypatch, stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    _final_brd(ba, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))
    monkeypatch.setattr(
        InitialUserStoryService, "choose_final_stories",
        lambda self, n: calls.append(("choose_final_stories", n)),
    )
    monkeypatch.setattr(
        InitialUserStoryService, "unlock_final_stories",
        lambda self: calls.append("unlock_final_stories"),
    )

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    version = refine_user_stories_step(PID, us_service=usr)

    assert version.version == 2
    assert calls == []  # nothing was finalized, locked, or unlocked


# --- K. existing `request` behavior is unchanged ----------------------------

def test_existing_request_semantics_unchanged_by_8b5(stub_ba_agent, sow_file, sample_metadata):
    """8B-5 must not alter BRD request semantics: `ensure_brd` still only
    generates when `request == "ensure_brd"`; any other value is still a no-op
    (regression proof — same behavior as before 8B-5 existed)."""
    svc = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)

    s_other = run_step(PID, "some_other_request", sow_path=str(sow_file),
                         metadata=sample_metadata, ba_service=svc)
    assert s_other["produced"] == {}
    assert svc.get_all_versions() == []  # BRD generation was skipped

    s_default = run_step(PID, "ensure_brd", sow_path=str(sow_file),
                           metadata=sample_metadata, ba_service=svc)
    assert s_default["produced"] == {"brd": 1}
    assert [v.version for v in svc.get_all_versions()] == [1]


# --- L. no graph topology change ------------------------------------------

def test_build_sdlc_graph_topology_is_unchanged_by_8b5(stub_ba_agent):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    g = build_sdlc_graph(ba).get_graph()

    assert set(g.nodes) == {
        "__start__", "resolve_state", "ensure_brd", "gate_brd",
        "ensure_hld", "ensure_user_stories", "gate_hld",
        "ensure_lld", "gate_lld",
        "ensure_test_cases", "gate_test_cases", "__end__",
    }
    # No refinement-related node was added to the compiled graph.
    assert not any("refine" in n for n in g.nodes)
