"""
Phase 8B-4 — QA/Test-Case hop (sequential) + test-case approval gate.

    gate_lld(complete) -> ensure_test_cases -> gate_test_cases -> END

Proves the orchestration graph delegates test-case generation DIRECTLY to the
EXISTING TestCaseService.generate() — the same public method the UI calls — and
NOT to the Phase 8A QA LangGraph pilot (app/agents/test_case/graph.py). Also
proves the graph never finalizes, keeps VersionService the only writer, enforces
the "final LLD first" invariant (a pipeline-level choice stricter than
TestCaseService's own "final BRD only" requirement), is idempotent, and stops at
the test-case approval gate.

Deterministic: BA / SA / US / LLD / TC LLMs are stubs injected via `agent=` /
`*_service=`. No Gemini calls.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.agent import TestCaseAgentError
from app.agents.test_case.service import InvalidTestCaseJSONError, TestCaseService
from app.orchestration.graph import (
    _gate_test_cases_node,
    _make_ensure_test_cases_node,
    _make_resolve_state_node,
    _route_after_gate_test_cases,
    build_sdlc_graph,
    run_step,
)
from app.orchestration.status import (
    NEXT_APPROVE_LLD,
    NEXT_APPROVE_TEST_CASES,
    NEXT_GENERATE_BRD,
    NEXT_GENERATE_LLD,
    NEXT_GENERATE_TEST_CASES,
    NEXT_NONE,
    sdlc_status,
)
from app.services.version_service import BRDVersion, VersionService
from tests.conftest import (
    StubBAAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

PID = "sdlc8b4"


def _svcs(pid=PID, *, ba_agent=None, sa_agent=None, us_agent=None, lld_agent=None, tc_agent=None):
    ba = BusinessAnalystService(project_id=pid, agent=ba_agent or StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=sa_agent or StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=us_agent or StubUserStoryAgent())
    lld = LowLevelDesignService(
        project_id=pid, sa_service=sa, ba_service=ba, agent=lld_agent or StubLLDAgent()
    )
    tc = TestCaseService(project_id=pid, agent=tc_agent or StubTestCaseAgent())
    return ba, sa, us, lld, tc


def _run(pid, ba, sa, us, lld, tc, sow_file, sample_metadata):
    return run_step(
        pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )


def _final_brd(ba, sow_file, sample_metadata):
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)


def _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata):
    """Advance a fresh project to: final BRD + final HLD + final LLD (+ US v1).
    Test cases absent. All finalization happens OUTSIDE the graph."""
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # HLD v1 + US v1
    sa.choose_final_hld(1)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # LLD v1
    lld.choose_final_lld(1)


# --- A. exact topology (full end-to-end shape, incl. the QA hop) ---------

def test_topology_is_the_8b4_shape(stub_ba_agent):
    ba, sa, us, lld, tc = _svcs(ba_agent=stub_ba_agent)
    g = build_sdlc_graph(
        ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc
    ).get_graph()

    assert set(g.nodes) == {
        "__start__", "resolve_state", "ensure_brd", "gate_brd",
        "ensure_hld", "ensure_user_stories", "gate_hld",
        "ensure_lld", "gate_lld",
        "ensure_test_cases", "gate_test_cases", "__end__",
    }
    plain = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert plain == {
        ("__start__", "resolve_state"),
        ("resolve_state", "ensure_brd"),
        ("ensure_brd", "gate_brd"),
        ("ensure_hld", "ensure_user_stories"),
        ("ensure_user_stories", "gate_hld"),
        ("ensure_lld", "gate_lld"),
        ("ensure_test_cases", "gate_test_cases"),
    }
    cond = {(e.source, e.target) for e in g.edges if e.conditional}
    assert ("gate_brd", "__end__") in cond
    assert ("gate_brd", "ensure_hld") in cond
    assert ("gate_hld", "__end__") in cond                # awaiting_approval -> END
    assert ("gate_hld", "ensure_lld") in cond              # complete -> LLD hop
    assert ("gate_lld", "__end__") in cond                 # awaiting_approval -> END
    assert ("gate_lld", "ensure_test_cases") in cond       # complete -> QA hop (8B-4)
    assert ("gate_test_cases", "__end__") in cond          # both QA-gate routes end 8B-4
    assert type(
        build_sdlc_graph(ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc)
    ).__name__ == "CompiledStateGraph"


def test_route_after_gate_test_cases_maps_both_outcomes():
    assert _route_after_gate_test_cases({"status": "complete"}) == "complete"
    assert _route_after_gate_test_cases({"status": "awaiting_approval"}) == "awaiting_approval"
    assert _route_after_gate_test_cases({}) == "awaiting_approval"  # safe default


def test_gate_test_cases_node_outcomes():
    assert _gate_test_cases_node({"tc_final_version": None}) == {
        "status": "awaiting_approval", "awaiting": "tc_final",
    }
    assert _gate_test_cases_node({"tc_final_version": 1}) == {
        "status": "complete", "awaiting": None,
    }


# --- B. SDLCState declares the tc_* fields --------------------------------

def test_sdlc_state_declares_tc_fields():
    from app.orchestration.state import SDLCState

    assert {"tc_latest_version", "tc_final_version"} <= set(SDLCState.__annotations__)


# --- C. resolve_state reads TC pointers -----------------------------------

def test_resolve_state_reads_tc_pointers(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    tc.generate()  # test-case v1, NOT final

    out = _make_resolve_state_node(ba, sa, us, lld, tc)({})
    assert out["lld_final_version"] == 1
    assert out["tc_latest_version"] == 1
    assert out["tc_final_version"] is None


# --- D. final LLD prerequisite (pipeline-level, stricter than the service) --

def test_no_final_lld_never_generates_test_cases(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # HLD v1 + US v1, HLD NOT final
    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert s["status"] == "awaiting_approval" and s["awaiting"] == "hld_final"
    assert tc.get_all_versions() == []
    assert stub_tc_agent.generate_calls == []


def test_final_hld_but_no_final_lld_never_generates_test_cases(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # HLD v1 + US v1
    sa.choose_final_hld(1)
    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # LLD v1, NOT final

    assert s["status"] == "awaiting_approval" and s["awaiting"] == "lld_final"
    assert tc.get_all_versions() == []
    assert stub_tc_agent.generate_calls == []


# --- E. test-case generation after final LLD ------------------------------

def test_test_cases_v1_generated_after_final_lld(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert s["produced"] == {"tc": 1}
    assert s["tc_latest_version"] == 1
    tc_v1 = tc.get_version(1)
    assert tc_v1.source == "initial"
    assert tc_v1.source_ref == "brd_v1;hld_v1;lld_v1;us_v1"
    assert tc_v1.note.startswith("Generated from BRD v1")
    assert len(stub_tc_agent.generate_calls) == 1
    brd_text, hld_text, lld_text, us_text, _metadata = stub_tc_agent.generate_calls[0]
    assert brd_text and hld_text and lld_text and us_text  # real artifact text, not sentinels


# --- F. end-to-end continuation after LLD approval ------------------------

def test_run_from_final_brd_then_lld_approval_reaches_test_case_gate(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _final_brd(ba, sow_file, sample_metadata)

    s1 = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # HLD + US
    assert s1["produced"] == {"hld": 1, "us": 1} and s1["awaiting"] == "hld_final"

    sa.choose_final_hld(1)                                          # OUTSIDE the graph
    s2 = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # LLD
    assert s2["produced"] == {"lld": 1} and s2["awaiting"] == "lld_final"

    lld.choose_final_lld(1)                                         # OUTSIDE the graph
    s3 = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # test cases
    assert s3["produced"] == {"tc": 1}
    assert s3["status"] == "awaiting_approval" and s3["awaiting"] == "tc_final"
    assert [v.version for v in tc.get_all_versions()] == [1]


# --- G. idempotency --------------------------------------------------------

def test_second_run_does_not_regenerate_test_cases(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # test cases v1

    tc_file = isolated_output_dir / PID / "test_cases" / "versions.json"
    before = tc_file.read_bytes()

    s2 = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    assert s2["produced"] == {}
    assert len(stub_tc_agent.generate_calls) == 1
    assert [v.version for v in tc.get_all_versions()] == [1]
    assert tc_file.read_bytes() == before


# --- H. QA approval invariant ----------------------------------------------

def test_graph_never_finalizes_qa_on_generation(
    monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)  # finalizes BRD+HLD+LLD, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))
    monkeypatch.setattr(
        TestCaseService, "choose_final",
        lambda self, n: calls.append(("choose_final", n)),
    )

    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # generates test cases v1
    assert s["produced"] == {"tc": 1}
    assert s["awaiting"] == "tc_final"
    assert calls == []  # the graph finalized nothing


def test_graph_never_finalizes_when_test_cases_already_final(
    monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)   # test cases v1
    tc.choose_final(1)                                           # human finalization, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))

    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    assert s["status"] == "complete" and s["awaiting"] is None
    assert s["produced"] == {}
    assert calls == []


# --- I. awaiting test-case approval -----------------------------------------

def test_final_lld_non_final_tc_awaits_tc_final(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert s["status"] == "awaiting_approval"
    assert s["awaiting"] == "tc_final"


# --- J. final test cases -> complete, no regeneration -----------------------

def test_final_test_cases_outside_graph_then_run_is_complete(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # test cases v1
    tc.choose_final(1)                                          # OUTSIDE the graph

    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    assert s["status"] == "complete"
    assert s["awaiting"] is None
    assert s["produced"] == {}
    assert len(stub_tc_agent.generate_calls) == 1


# --- K. persistence parity: graph == direct TestCaseService.generate() ----

def test_graph_test_cases_match_direct_service(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    # direct: identical final BRD + final HLD + final LLD + one US version, then generate() directly
    d_ba = BusinessAnalystService(project_id="tc_parity_direct", agent=StubBAAgent())
    d_ba.generate_initial_brd(sow_file, sample_metadata); d_ba.choose_final_brd(1)
    d_sa = SolutionArchitectService(project_id="tc_parity_direct", ba_service=d_ba, agent=StubSAAgent())
    d_sa.generate_initial_hld(); d_sa.choose_final_hld(1)
    d_us = InitialUserStoryService(project_id="tc_parity_direct", ba_service=d_ba, agent=StubUserStoryAgent())
    d_us.generate_initial_stories()
    d_lld = LowLevelDesignService(project_id="tc_parity_direct", sa_service=d_sa, ba_service=d_ba, agent=StubLLDAgent())
    d_lld.generate_initial_lld(); d_lld.choose_final_lld(1)
    d_tc = TestCaseService(project_id="tc_parity_direct", agent=StubTestCaseAgent())
    direct = d_tc.generate()

    # graph: drive the same project shape through run_step
    g_ba, g_sa, g_us, g_lld, g_tc = _svcs("tc_parity_graph")
    g_ba.generate_initial_brd(sow_file, sample_metadata); g_ba.choose_final_brd(1)
    _run("tc_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, sow_file, sample_metadata)  # HLD + US
    g_sa.choose_final_hld(1)
    _run("tc_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, sow_file, sample_metadata)  # LLD
    g_lld.choose_final_lld(1)
    _run("tc_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, sow_file, sample_metadata)  # test cases
    graphed = g_tc.get_version(1)

    # Semantic persisted fields only — not incidental formatting.
    assert graphed.content == direct.content
    assert graphed.version == direct.version == 1
    assert graphed.source == direct.source == "initial"
    assert graphed.source_ref == direct.source_ref == "brd_v1;hld_v1;lld_v1;us_v1"
    assert graphed.note == direct.note


# --- L. failure propagation --------------------------------------------

def test_test_case_agent_error_propagates(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    class _BoomTC:
        def generate_test_cases(self, brd_text, hld_text, lld_text, user_stories_text, metadata):
            raise TestCaseAgentError("QA Gemini exploded")

    ba, sa, us, lld, _ = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    tc = TestCaseService(project_id=PID, agent=_BoomTC())
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)

    with pytest.raises(TestCaseAgentError):
        _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert tc.get_all_versions() == []


def test_invalid_test_case_json_propagates(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    class _BadJSONTC:
        def generate_test_cases(self, brd_text, hld_text, lld_text, user_stories_text, metadata):
            return "not json"

    ba, sa, us, lld, _ = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent, lld_agent=stub_lld_agent
    )
    tc = TestCaseService(project_id=PID, agent=_BadJSONTC())
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)

    with pytest.raises(InvalidTestCaseJSONError):
        _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert tc.get_all_versions() == []


def test_no_final_lld_path_does_not_raise_no_final_brd_error(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    """The graph stops at gate_lld before ensure_test_cases, so TestCaseService's
    own NoFinalBRDError never fires (BRD IS final here; LLD is simply not yet)."""
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # HLD + US
    sa.choose_final_hld(1)

    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # LLD v1, NOT final
    assert s["awaiting"] == "lld_final"
    assert tc.get_all_versions() == []


# --- M. persistence streams -------------------------------------------

def test_only_expected_streams_written_through_test_cases(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # test cases v1

    proj = isolated_output_dir / PID
    written = sorted(str(p.relative_to(proj)).replace("\\", "/") for p in proj.rglob("versions.json"))
    assert written == [
        "hld/versions.json", "lld/versions.json", "test_cases/versions.json",
        "user_stories/versions.json", "versions.json",
    ]
    for rec_file in proj.rglob("versions.json"):
        for rec in json.loads(rec_file.read_text(encoding="utf-8")):
            assert isinstance(BRDVersion(**rec), BRDVersion)


# --- N. sdlc_status (extended) ------------------------------------------

def _status(pid, ba, sa, us, lld, tc):
    return sdlc_status(pid, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc)


def test_status_empty_project_has_tc_keys(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    st = _status(PID, ba, sa, us, lld, tc)
    assert st["tc_exists"] is False
    assert st["tc_latest_version"] is None
    assert st["tc_final_version"] is None
    assert st["awaiting_test_cases_approval"] is False
    assert st["next_step"] == NEXT_GENERATE_BRD


def test_status_final_lld_no_test_cases(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    st = _status(PID, ba, sa, us, lld, tc)
    assert st["lld_final_version"] == 1
    assert st["tc_exists"] is False
    assert st["awaiting_test_cases_approval"] is False
    assert st["next_step"] == NEXT_GENERATE_TEST_CASES


def test_status_test_cases_exist_not_final(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # test cases v1
    st = _status(PID, ba, sa, us, lld, tc)
    assert st["tc_exists"] is True and st["tc_latest_version"] == 1 and st["tc_final_version"] is None
    assert st["awaiting_test_cases_approval"] is True
    assert st["next_step"] == NEXT_APPROVE_TEST_CASES


def test_status_test_cases_final(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    tc.choose_final(1)
    st = _status(PID, ba, sa, us, lld, tc)
    assert st["tc_final_version"] == 1
    assert st["awaiting_test_cases_approval"] is False
    assert st["next_step"] is NEXT_NONE


def test_status_lld_not_final_next_step_is_approve_lld(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _final_brd(ba, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    sa.choose_final_hld(1)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # LLD v1, not final
    st = _status(PID, ba, sa, us, lld, tc)
    assert st["next_step"] == NEXT_APPROVE_LLD
    assert st["tc_exists"] is False


def test_status_repeated_calls_are_side_effect_free(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata, isolated_output_dir
):
    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)  # test cases v1

    proj = isolated_output_dir / PID
    before = {p: p.read_bytes() for p in proj.rglob("versions.json")}
    tc_calls = len(stub_tc_agent.generate_calls)

    for _ in range(3):
        _status(PID, ba, sa, us, lld, tc)

    assert {p: p.read_bytes() for p in proj.rglob("versions.json")} == before
    assert len(stub_tc_agent.generate_calls) == tc_calls


# --- O. the graph never nests / invokes the Phase 8A QA LangGraph pilot ----

def test_ensure_test_cases_never_touches_the_qa_graph_module(
    monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, sow_file, sample_metadata
):
    """`ensure_test_cases` must call TestCaseService.generate() directly, never
    app.agents.test_case.graph.run_qa / build_qa_graph (the 8A pilot)."""
    import app.agents.test_case.graph as qa_graph_mod

    calls: list = []
    monkeypatch.setattr(qa_graph_mod, "run_qa", lambda *a, **kw: calls.append("run_qa"))
    monkeypatch.setattr(qa_graph_mod, "build_qa_graph", lambda *a, **kw: calls.append("build_qa_graph"))

    ba, sa, us, lld, tc = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    assert tc._use_graph is False  # 8B-4 leaves use_graph exactly as-is (default False)
    _to_final_lld(ba, sa, us, lld, tc, sow_file, sample_metadata)

    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    assert s["produced"] == {"tc": 1}
    assert calls == []  # the QA pilot graph was never touched
