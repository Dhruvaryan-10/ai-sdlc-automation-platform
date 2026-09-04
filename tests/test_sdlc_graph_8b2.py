"""
Phase 8B-2 — HLD + Initial User Stories fan-out (sequential) + HLD approval gate.

    gate_brd(complete) -> ensure_hld -> ensure_user_stories -> gate_hld -> END

Proves the orchestration graph delegates HLD generation to the EXISTING
SolutionArchitectService and user-story generation to the EXISTING
InitialUserStoryService, never finalizes, keeps VersionService the only writer,
enforces the "final BRD first" invariant, and stops at the HLD approval gate.

Deterministic: BA / SA / US LLMs are stubs injected via `agent=` / `*_service=`.
No Gemini calls.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.agent import InitialUserStoryAgentError
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.solution_architect.agent import SolutionArchitectAgentError
from app.agents.solution_architect.service import SolutionArchitectService
from app.orchestration.graph import (
    _gate_hld_node,
    _make_ensure_hld_node,
    _make_ensure_user_stories_node,
    _make_resolve_state_node,
    _route_after_gate_hld,
    build_sdlc_graph,
    run_step,
)
from app.orchestration.status import (
    NEXT_APPROVE_BRD,
    NEXT_APPROVE_HLD,
    NEXT_GENERATE_BRD,
    NEXT_GENERATE_HLD,
    NEXT_NONE,
    sdlc_status,
)
from app.services.version_service import BRDVersion, VersionService
from tests.conftest import StubBAAgent, StubSAAgent, StubUserStoryAgent

PID = "sdlc8b2"


def _svcs(pid=PID, *, ba_agent=None, sa_agent=None, us_agent=None):
    ba = BusinessAnalystService(project_id=pid, agent=ba_agent or StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=sa_agent or StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=us_agent or StubUserStoryAgent())
    return ba, sa, us


def _run(pid, ba, sa, us, sow_file, sample_metadata):
    return run_step(
        pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=ba, sa_service=sa, us_service=us,
    )


def _final_brd(ba, sow_file, sample_metadata):
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)


# --- A. exact topology ------------------------------------------------

def test_topology_is_the_8b2_shape(stub_ba_agent):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent)
    g = build_sdlc_graph(ba, sa_service=sa, us_service=us).get_graph()

    assert set(g.nodes) == {
        "__start__", "resolve_state", "ensure_brd", "gate_brd",
        "ensure_hld", "ensure_user_stories", "gate_hld", "__end__",
    }
    plain = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert plain == {
        ("__start__", "resolve_state"),
        ("resolve_state", "ensure_brd"),
        ("ensure_brd", "gate_brd"),
        ("ensure_hld", "ensure_user_stories"),
        ("ensure_user_stories", "gate_hld"),
    }
    cond = {(e.source, e.target) for e in g.edges if e.conditional}
    assert ("gate_brd", "__end__") in cond          # awaiting_approval
    assert ("gate_brd", "ensure_hld") in cond        # complete -> HLD hop
    assert ("gate_hld", "__end__") in cond           # both HLD-gate routes end 8B-2


def test_route_after_gate_hld_maps_both_outcomes():
    assert _route_after_gate_hld({"status": "complete"}) == "complete"
    assert _route_after_gate_hld({"status": "awaiting_approval"}) == "awaiting_approval"
    assert _route_after_gate_hld({}) == "awaiting_approval"  # safe default


def test_gate_hld_node_outcomes():
    assert _gate_hld_node({"hld_final_version": None}) == {
        "status": "awaiting_approval", "awaiting": "hld_final",
    }
    assert _gate_hld_node({"hld_final_version": 1}) == {
        "status": "complete", "awaiting": None,
    }


# --- resolve_state now also reads HLD / US -------------------------

def test_resolve_state_reads_brd_hld_us_pointers(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    sa.generate_initial_hld()
    us.generate_initial_stories()

    out = _make_resolve_state_node(ba, sa, us)({})
    assert out == {
        "brd_latest_version": 1, "brd_final_version": 1,
        "hld_latest_version": 1, "hld_final_version": None,
        "us_latest_version": 1,
    }


# --- B. final BRD prerequisite ------------------------------------

def test_no_final_brd_never_generates_hld_or_us(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    s = _run(PID, ba, sa, us, sow_file, sample_metadata)   # BRD generated but NOT final

    assert s["status"] == "awaiting_approval" and s["awaiting"] == "brd_final"
    assert s["produced"] == {"brd": 1}
    assert sa.get_all_versions() == [] and us.get_all_versions() == []
    assert stub_sa_agent.generate_calls == [] and stub_us_agent.generate_calls == []


# --- C. HLD generation --------------------------------------------

def test_hld_v1_generated_after_final_brd(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert s["produced"]["hld"] == 1
    assert s["hld_latest_version"] == 1
    hld_v1 = sa.get_version(1)
    assert hld_v1.source == "initial"
    assert hld_v1.source_ref == "brd_v1"
    assert hld_v1.note == "Generated from accepted BRD v1"
    assert len(stub_sa_agent.generate_calls) == 1
    assert stub_sa_agent.generate_calls[0][0]  # the final BRD content was passed


# --- D. initial user stories ------------------------------------

def test_user_stories_v1_generated_after_final_brd(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert s["produced"]["us"] == 1
    assert s["us_latest_version"] == 1
    us_v1 = us.get_version(1)
    assert us_v1.source == "initial"
    assert us_v1.source_ref == "brd_v1"
    assert us_v1.note == "Generated from accepted BRD v1"
    assert len(stub_us_agent.generate_calls) == 1


# --- E. combined run --------------------------------------------

def test_single_run_produces_both_hld_and_user_stories(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert s["produced"] == {"hld": 1, "us": 1}
    assert s["status"] == "awaiting_approval" and s["awaiting"] == "hld_final"
    assert [v.version for v in sa.get_all_versions()] == [1]
    assert [v.version for v in us.get_all_versions()] == [1]


# --- F. idempotency -------------------------------------------

def test_second_run_produces_nothing_and_files_are_byte_identical(
    stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)

    hld_file = isolated_output_dir / PID / "hld" / "versions.json"
    us_file = isolated_output_dir / PID / "user_stories" / "versions.json"
    brd_file = isolated_output_dir / PID / "versions.json"
    before = (hld_file.read_bytes(), us_file.read_bytes(), brd_file.read_bytes())

    s2 = _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert s2["produced"] == {}
    assert len(stub_sa_agent.generate_calls) == 1
    assert len(stub_us_agent.generate_calls) == 1
    assert [v.version for v in sa.get_all_versions()] == [1]
    assert [v.version for v in us.get_all_versions()] == [1]
    assert (hld_file.read_bytes(), us_file.read_bytes(), brd_file.read_bytes()) == before


# --- G. HLD approval invariant --------------------------------

def test_graph_never_finalizes_hld_on_generation(monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)  # BRD finalized by the test, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))
    monkeypatch.setattr(
        SolutionArchitectService, "choose_final_hld",
        lambda self, n: calls.append(("choose_final_hld", n)),
    )

    s = _run(PID, ba, sa, us, sow_file, sample_metadata)  # generates HLD v1 + US v1

    assert s["produced"] == {"hld": 1, "us": 1}
    assert s["awaiting"] == "hld_final"
    assert calls == []  # the graph finalized nothing


def test_graph_never_finalizes_when_hld_already_final(monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)
    sa.choose_final_hld(1)  # human finalization — before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))

    s = _run(PID, ba, sa, us, sow_file, sample_metadata)
    assert s["status"] == "complete" and s["awaiting"] is None
    assert s["produced"] == {}
    assert calls == []


# --- H. awaiting HLD approval --------------------------------

def test_final_brd_non_final_hld_awaits_hld_final(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert s["status"] == "awaiting_approval"
    assert s["awaiting"] == "hld_final"


# --- I. final HLD -> complete, no regeneration -----------------

def test_final_hld_outside_graph_then_run_is_complete(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)          # HLD v1 + US v1
    sa.choose_final_hld(1)                                    # OUTSIDE the graph

    s = _run(PID, ba, sa, us, sow_file, sample_metadata)
    assert s["status"] == "complete"
    assert s["awaiting"] is None
    assert s["produced"] == {}
    assert len(stub_sa_agent.generate_calls) == 1
    assert len(stub_us_agent.generate_calls) == 1


# --- J. persistence parity ----------------------------------

def test_graph_hld_matches_direct_service(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    d_ba = BusinessAnalystService(project_id="hld_parity_direct", agent=StubBAAgent())
    d_ba.generate_initial_brd(sow_file, sample_metadata); d_ba.choose_final_brd(1)
    d_sa = SolutionArchitectService(project_id="hld_parity_direct", ba_service=d_ba, agent=StubSAAgent())
    direct = d_sa.generate_initial_hld()

    g_ba, g_sa, g_us = _svcs("hld_parity_graph")
    g_ba.generate_initial_brd(sow_file, sample_metadata); g_ba.choose_final_brd(1)
    run_step("hld_parity_graph", "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
             ba_service=g_ba, sa_service=g_sa, us_service=g_us)
    graphed = g_sa.get_version(1)

    assert graphed.content == direct.content
    assert graphed.source == direct.source == "initial"
    assert graphed.source_ref == direct.source_ref == "brd_v1"
    assert graphed.note == direct.note == "Generated from accepted BRD v1"
    assert graphed.version == direct.version == 1


def test_graph_user_stories_match_direct_service(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    d_ba = BusinessAnalystService(project_id="us_parity_direct", agent=StubBAAgent())
    d_ba.generate_initial_brd(sow_file, sample_metadata); d_ba.choose_final_brd(1)
    d_us = InitialUserStoryService(project_id="us_parity_direct", ba_service=d_ba, agent=StubUserStoryAgent())
    direct = d_us.generate_initial_stories()

    g_ba, g_sa, g_us = _svcs("us_parity_graph")
    g_ba.generate_initial_brd(sow_file, sample_metadata); g_ba.choose_final_brd(1)
    run_step("us_parity_graph", "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
             ba_service=g_ba, sa_service=g_sa, us_service=g_us)
    graphed = g_us.get_version(1)

    assert graphed.content == direct.content
    assert graphed.source == direct.source == "initial"
    assert graphed.source_ref == direct.source_ref == "brd_v1"
    assert graphed.note == direct.note == "Generated from accepted BRD v1"
    assert graphed.version == direct.version == 1


# --- K. failure propagation --------------------------------

def test_hld_agent_failure_propagates_and_us_does_not_run(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    class _BoomSA:
        def generate_hld(self, brd_text, metadata):
            raise SolutionArchitectAgentError("HLD Gemini exploded")

    ba, _, us = _svcs(ba_agent=stub_ba_agent, us_agent=stub_us_agent)
    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=_BoomSA())
    _final_brd(ba, sow_file, sample_metadata)

    with pytest.raises(SolutionArchitectAgentError):
        _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert sa.get_all_versions() == []
    assert us.get_all_versions() == []                 # ensure_user_stories never executed
    assert stub_us_agent.generate_calls == []


def test_user_story_agent_failure_propagates(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    class _BoomUS:
        def generate_stories(self, brd_text, metadata):
            raise InitialUserStoryAgentError("US Gemini exploded")

    ba, sa, _ = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=_BoomUS())
    _final_brd(ba, sow_file, sample_metadata)

    with pytest.raises(InitialUserStoryAgentError):
        _run(PID, ba, sa, us, sow_file, sample_metadata)

    assert [v.version for v in sa.get_all_versions()] == [1]  # HLD succeeded first
    assert us.get_all_versions() == []


# --- persistence: only the expected streams are written ----------

def test_only_expected_streams_are_written(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata, isolated_output_dir):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)

    proj = isolated_output_dir / PID
    written = sorted(str(p.relative_to(proj)).replace("\\", "/") for p in proj.rglob("versions.json"))
    assert written == ["hld/versions.json", "user_stories/versions.json", "versions.json"]
    # no lld / test_cases / closure streams
    for rec_file in proj.rglob("versions.json"):
        for rec in json.loads(rec_file.read_text(encoding="utf-8")):
            assert isinstance(BRDVersion(**rec), BRDVersion)


# --- L. sdlc_status (extended, pure) ------------------------

def _status(pid, ba, sa, us):
    return sdlc_status(pid, ba_service=ba, sa_service=sa, us_service=us)


def test_status_empty_project(stub_ba_agent, stub_sa_agent, stub_us_agent):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    assert _status(PID, ba, sa, us) == {
        "project_id": PID,
        "brd_exists": False, "brd_latest_version": None, "brd_final_version": None,
        "awaiting_brd_approval": False,
        "hld_exists": False, "hld_latest_version": None, "hld_final_version": None,
        "awaiting_hld_approval": False,
        "us_exists": False, "us_latest_version": None,
        "next_step": NEXT_GENERATE_BRD,
    }


def test_status_final_brd_no_hld(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    st = _status(PID, ba, sa, us)
    assert st["brd_final_version"] == 1
    assert st["hld_exists"] is False and st["hld_latest_version"] is None
    assert st["us_exists"] is False
    assert st["awaiting_hld_approval"] is False
    assert st["next_step"] == NEXT_GENERATE_HLD


def test_status_hld_exists_not_final(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)  # HLD v1 + US v1
    st = _status(PID, ba, sa, us)
    assert st["hld_exists"] is True and st["hld_latest_version"] == 1 and st["hld_final_version"] is None
    assert st["us_exists"] is True and st["us_latest_version"] == 1
    assert st["awaiting_hld_approval"] is True
    assert st["next_step"] == NEXT_APPROVE_HLD


def test_status_hld_final(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)
    sa.choose_final_hld(1)
    st = _status(PID, ba, sa, us)
    assert st["hld_final_version"] == 1
    assert st["awaiting_hld_approval"] is False
    assert st["next_step"] is NEXT_NONE


def test_status_brd_not_final_next_step(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)  # not final
    st = _status(PID, ba, sa, us)
    assert st["awaiting_brd_approval"] is True
    assert st["next_step"] == NEXT_APPROVE_BRD


def test_status_is_pure_no_writes_no_generation(
    stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, sow_file, sample_metadata)

    proj = isolated_output_dir / PID
    before = {p: p.read_bytes() for p in proj.rglob("versions.json")}
    sa_calls, us_calls = len(stub_sa_agent.generate_calls), len(stub_us_agent.generate_calls)

    for _ in range(3):
        _status(PID, ba, sa, us)

    assert {p: p.read_bytes() for p in proj.rglob("versions.json")} == before
    assert len(stub_sa_agent.generate_calls) == sa_calls
    assert len(stub_us_agent.generate_calls) == us_calls
