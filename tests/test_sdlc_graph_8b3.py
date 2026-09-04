"""
Phase 8B-3 — LLD hop (sequential) + LLD approval gate.

    gate_hld(complete) -> ensure_lld -> gate_lld -> END

Proves the orchestration graph delegates LLD generation to the EXISTING
LowLevelDesignService, never finalizes, keeps VersionService the only writer,
enforces the "final HLD first" invariant, is idempotent, and stops at the LLD
approval gate.

Deterministic: BA / SA / US / LLD LLMs are stubs injected via `agent=` /
`*_service=`. No Gemini calls.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.agent import LLDAgentError
from app.agents.low_level_design.service import LowLevelDesignService, NoFinalHLDError
from app.agents.solution_architect.service import SolutionArchitectService
from app.orchestration.graph import (
    _gate_lld_node,
    _make_ensure_lld_node,
    _make_resolve_state_node,
    _route_after_gate_lld,
    build_sdlc_graph,
    run_step,
)
from app.orchestration.status import (
    NEXT_APPROVE_LLD,
    NEXT_GENERATE_BRD,
    NEXT_GENERATE_LLD,
    NEXT_NONE,
    sdlc_status,
)
from app.services.version_service import BRDVersion, VersionService
from tests.conftest import StubBAAgent, StubLLDAgent, StubSAAgent, StubUserStoryAgent

PID = "sdlc8b3"


def _svcs(pid=PID, *, ba_agent=None, sa_agent=None, us_agent=None, lld_agent=None):
    ba = BusinessAnalystService(project_id=pid, agent=ba_agent or StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=sa_agent or StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=us_agent or StubUserStoryAgent())
    lld = LowLevelDesignService(
        project_id=pid, sa_service=sa, ba_service=ba, agent=lld_agent or StubLLDAgent()
    )
    return ba, sa, us, lld


def _run(pid, ba, sa, us, lld, sow_file, sample_metadata):
    return run_step(
        pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=ba, sa_service=sa, us_service=us, lld_service=lld,
    )


def _final_brd(ba, sow_file, sample_metadata):
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)


def _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata):
    """Advance a fresh project to: final BRD + final HLD (+ US v1). LLD absent."""
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # HLD v1 + US v1
    sa.choose_final_hld(1)                                 # human finalization, OUTSIDE the graph


# --- A. exact topology --------------------------------------------------

def test_topology_is_the_8b3_shape(stub_ba_agent):
    ba, sa, us, lld = _svcs(ba_agent=stub_ba_agent)
    g = build_sdlc_graph(ba, sa_service=sa, us_service=us, lld_service=lld).get_graph()

    assert set(g.nodes) == {
        "__start__", "resolve_state", "ensure_brd", "gate_brd",
        "ensure_hld", "ensure_user_stories", "gate_hld",
        "ensure_lld", "gate_lld", "__end__",
    }
    plain = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert plain == {
        ("__start__", "resolve_state"),
        ("resolve_state", "ensure_brd"),
        ("ensure_brd", "gate_brd"),
        ("ensure_hld", "ensure_user_stories"),
        ("ensure_user_stories", "gate_hld"),
        ("ensure_lld", "gate_lld"),
    }
    cond = {(e.source, e.target) for e in g.edges if e.conditional}
    assert ("gate_brd", "__end__") in cond
    assert ("gate_brd", "ensure_hld") in cond
    assert ("gate_hld", "__end__") in cond          # awaiting_approval -> END
    assert ("gate_hld", "ensure_lld") in cond        # complete -> LLD hop
    assert ("gate_lld", "__end__") in cond           # both LLD-gate routes end 8B-3
    assert type(build_sdlc_graph(ba, sa_service=sa, us_service=us, lld_service=lld)).__name__ == "CompiledStateGraph"


def test_route_after_gate_lld_maps_both_outcomes():
    assert _route_after_gate_lld({"status": "complete"}) == "complete"
    assert _route_after_gate_lld({"status": "awaiting_approval"}) == "awaiting_approval"
    assert _route_after_gate_lld({}) == "awaiting_approval"  # safe default


def test_gate_lld_node_outcomes():
    assert _gate_lld_node({"lld_final_version": None}) == {
        "status": "awaiting_approval", "awaiting": "lld_final",
    }
    assert _gate_lld_node({"lld_final_version": 1}) == {
        "status": "complete", "awaiting": None,
    }


# --- L. resolve_state reads LLD pointers -------------------------------

def test_resolve_state_reads_lld_pointers(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    lld.generate_initial_lld()  # LLD v1, NOT final

    out = _make_resolve_state_node(ba, sa, us, lld)({})
    assert out["hld_final_version"] == 1
    assert out["lld_latest_version"] == 1
    assert out["lld_final_version"] is None


# --- B. final HLD prerequisite ---------------------------------------

def test_no_final_hld_never_generates_lld(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _final_brd(ba, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # HLD v1 + US v1, HLD NOT final

    assert s["status"] == "awaiting_approval" and s["awaiting"] == "hld_final"
    assert lld.get_all_versions() == []
    assert stub_lld_agent.generate_calls == []


# --- C. LLD generation ---------------------------------------------

def test_lld_v1_generated_after_final_hld(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)

    assert s["produced"] == {"lld": 1}
    assert s["lld_latest_version"] == 1
    lld_v1 = lld.get_version(1)
    assert lld_v1.source == "initial"
    assert lld_v1.source_ref == "hld_v1"
    assert lld_v1.note.startswith("Generated from accepted HLD v1")
    assert "(with draft user stories as context)" in lld_v1.note  # ensure_user_stories ran earlier
    assert len(stub_lld_agent.generate_calls) == 1
    assert stub_lld_agent.generate_calls[0][0]  # the final HLD content was passed


# --- D. end-to-end continuation after HLD approval -----------------

def test_run_from_final_brd_then_hld_approval_reaches_lld_gate(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _final_brd(ba, sow_file, sample_metadata)

    s1 = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)   # HLD + US
    assert s1["produced"] == {"hld": 1, "us": 1} and s1["awaiting"] == "hld_final"

    sa.choose_final_hld(1)                                        # OUTSIDE the graph
    s2 = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)   # LLD
    assert s2["produced"] == {"lld": 1}
    assert s2["status"] == "awaiting_approval" and s2["awaiting"] == "lld_final"
    assert [v.version for v in lld.get_all_versions()] == [1]


# --- E. idempotency ----------------------------------------------

def test_second_run_does_not_regenerate_lld(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)        # LLD v1

    lld_file = isolated_output_dir / PID / "lld" / "versions.json"
    before = lld_file.read_bytes()

    s2 = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)
    assert s2["produced"] == {}
    assert len(stub_lld_agent.generate_calls) == 1
    assert [v.version for v in lld.get_all_versions()] == [1]
    assert lld_file.read_bytes() == before


# --- F. LLD approval invariant -------------------------------

def test_graph_never_finalizes_lld_on_generation(
    monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)  # finalizes BRD + HLD, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))
    monkeypatch.setattr(
        LowLevelDesignService, "choose_final_lld",
        lambda self, n: calls.append(("choose_final_lld", n)),
    )

    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # generates LLD v1
    assert s["produced"] == {"lld": 1}
    assert s["awaiting"] == "lld_final"
    assert calls == []  # the graph finalized nothing


def test_graph_never_finalizes_when_lld_already_final(
    monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)     # LLD v1
    lld.choose_final_lld(1)                                   # human finalization, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))

    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)
    assert s["status"] == "complete" and s["awaiting"] is None
    assert s["produced"] == {}
    assert calls == []


# --- G. awaiting LLD approval ------------------------------

def test_final_hld_non_final_lld_awaits_lld_final(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)

    assert s["status"] == "awaiting_approval"
    assert s["awaiting"] == "lld_final"


# --- H. final LLD -> complete, no regeneration ------------

def test_final_lld_outside_graph_then_run_is_complete(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)      # LLD v1
    lld.choose_final_lld(1)                                    # OUTSIDE the graph

    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)
    assert s["status"] == "complete"
    assert s["awaiting"] is None
    assert s["produced"] == {}
    assert len(stub_lld_agent.generate_calls) == 1


# --- I. persistence parity --------------------------------

def test_graph_lld_matches_direct_service(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    # direct: identical final BRD + final HLD + one US version, then generate LLD directly
    d_ba = BusinessAnalystService(project_id="lld_parity_direct", agent=StubBAAgent())
    d_ba.generate_initial_brd(sow_file, sample_metadata); d_ba.choose_final_brd(1)
    d_sa = SolutionArchitectService(project_id="lld_parity_direct", ba_service=d_ba, agent=StubSAAgent())
    d_sa.generate_initial_hld(); d_sa.choose_final_hld(1)
    d_us = InitialUserStoryService(project_id="lld_parity_direct", ba_service=d_ba, agent=StubUserStoryAgent())
    d_us.generate_initial_stories()
    d_lld = LowLevelDesignService(project_id="lld_parity_direct", sa_service=d_sa, ba_service=d_ba, agent=StubLLDAgent())
    direct = d_lld.generate_initial_lld()

    # graph: drive the same project shape through run_step
    g_ba, g_sa, g_us, g_lld = _svcs("lld_parity_graph")
    g_ba.generate_initial_brd(sow_file, sample_metadata); g_ba.choose_final_brd(1)
    _run("lld_parity_graph", g_ba, g_sa, g_us, g_lld, sow_file, sample_metadata)  # HLD + US
    g_sa.choose_final_hld(1)
    _run("lld_parity_graph", g_ba, g_sa, g_us, g_lld, sow_file, sample_metadata)  # LLD
    graphed = g_lld.get_version(1)

    assert graphed.content == direct.content
    assert graphed.source == direct.source == "initial"
    assert graphed.source_ref == direct.source_ref == "hld_v1"
    assert graphed.note == direct.note
    assert graphed.version == direct.version == 1


# --- J. failure propagation ------------------------------

def test_lld_agent_failure_propagates(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata):
    class _BoomLLD:
        def generate_lld(self, hld_text, brd_text, user_stories_text, metadata):
            raise LLDAgentError("LLD Gemini exploded")

    ba, sa, us, _ = _svcs(ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent)
    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=_BoomLLD())
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)

    with pytest.raises(LLDAgentError):
        _run(PID, ba, sa, us, lld, sow_file, sample_metadata)

    assert lld.get_all_versions() == []


def test_no_final_hld_path_does_not_raise_no_final_hld_error(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    """The graph stops at gate_hld before ensure_lld, so NoFinalHLDError never fires."""
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _final_brd(ba, sow_file, sample_metadata)  # HLD never finalized

    s = _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # must NOT raise NoFinalHLDError
    assert s["awaiting"] == "hld_final"
    assert lld.get_all_versions() == []


# --- K. persistence streams -----------------------------

def test_only_expected_streams_written_through_lld(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # LLD v1

    proj = isolated_output_dir / PID
    written = sorted(str(p.relative_to(proj)).replace("\\", "/") for p in proj.rglob("versions.json"))
    assert written == [
        "hld/versions.json", "lld/versions.json", "user_stories/versions.json", "versions.json",
    ]
    for rec_file in proj.rglob("versions.json"):
        for rec in json.loads(rec_file.read_text(encoding="utf-8")):
            assert isinstance(BRDVersion(**rec), BRDVersion)


# --- M. sdlc_status (extended) --------------------------

def _status(pid, ba, sa, us, lld):
    return sdlc_status(pid, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld)


def test_status_empty_project_has_lld_keys(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    st = _status(PID, ba, sa, us, lld)
    assert st["lld_exists"] is False
    assert st["lld_latest_version"] is None
    assert st["lld_final_version"] is None
    assert st["awaiting_lld_approval"] is False
    assert st["next_step"] == NEXT_GENERATE_BRD


def test_status_final_hld_no_lld(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    st = _status(PID, ba, sa, us, lld)
    assert st["hld_final_version"] == 1
    assert st["lld_exists"] is False
    assert st["awaiting_lld_approval"] is False
    assert st["next_step"] == NEXT_GENERATE_LLD


def test_status_lld_exists_not_final(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # LLD v1
    st = _status(PID, ba, sa, us, lld)
    assert st["lld_exists"] is True and st["lld_latest_version"] == 1 and st["lld_final_version"] is None
    assert st["awaiting_lld_approval"] is True
    assert st["next_step"] == NEXT_APPROVE_LLD


def test_status_lld_final(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)
    lld.choose_final_lld(1)
    st = _status(PID, ba, sa, us, lld)
    assert st["lld_final_version"] == 1
    assert st["awaiting_lld_approval"] is False
    assert st["next_step"] is NEXT_NONE


def test_status_repeated_calls_are_side_effect_free(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us, lld = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    _to_final_hld(ba, sa, us, lld, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, sow_file, sample_metadata)  # LLD v1

    proj = isolated_output_dir / PID
    before = {p: p.read_bytes() for p in proj.rglob("versions.json")}
    lld_calls = len(stub_lld_agent.generate_calls)

    for _ in range(3):
        _status(PID, ba, sa, us, lld)

    assert {p: p.read_bytes() for p in proj.rglob("versions.json")} == before
    assert len(stub_lld_agent.generate_calls) == lld_calls
