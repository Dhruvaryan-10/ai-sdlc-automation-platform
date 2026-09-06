"""
Phase 8B-7 — Closure Report hop (sequential) + closure approval gate.

    gate_test_cases(complete) -> ensure_closure_report -> gate_closure_report -> END

Proves the orchestration graph delegates closure-report generation DIRECTLY to
the EXISTING `ClosureReportService.generate()` — the same public method the UI
calls — never finalizes it, keeps `VersionService` (JSON) the only writer, is
idempotent (one closure report, not one per invocation), stops at the closure
approval gate, and continues only after a human finalizes.

Deterministic: BA / SA / US / LLD / TC / Closure LLMs are stubs injected via
`agent=` / `*_service=`. No Gemini calls.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.closure_report.service import ClosureReportService
from app.agents.closure_report.agent import ClosureReportAgentError
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.orchestration.graph import (
    _gate_closure_report_node,
    _make_ensure_closure_report_node,
    _make_resolve_state_node,
    _route_after_gate_closure_report,
    build_sdlc_graph,
    run_step,
)
from app.orchestration.state import SDLCState
from app.orchestration.status import (
    NEXT_APPROVE_CLOSURE_REPORT,
    NEXT_GENERATE_CLOSURE_REPORT,
    NEXT_NONE,
    sdlc_status,
)
from app.services.version_service import BRDVersion, VersionService
from tests.conftest import (
    StubBAAgent,
    StubClosureReportAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

PID = "sdlc8b7"


def _svcs(pid=PID, *, ba_agent=None, sa_agent=None, us_agent=None,
          lld_agent=None, tc_agent=None, closure_agent=None):
    ba = BusinessAnalystService(project_id=pid, agent=ba_agent or StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=sa_agent or StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=us_agent or StubUserStoryAgent())
    lld = LowLevelDesignService(
        project_id=pid, sa_service=sa, ba_service=ba, agent=lld_agent or StubLLDAgent()
    )
    tc = TestCaseService(project_id=pid, agent=tc_agent or StubTestCaseAgent())
    closure = ClosureReportService(project_id=pid, agent=closure_agent or StubClosureReportAgent())
    return ba, sa, us, lld, tc, closure


def _run(pid, ba, sa, us, lld, tc, closure, sow_file, sample_metadata):
    return run_step(
        pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=ba, sa_service=sa, us_service=us, lld_service=lld,
        tc_service=tc, closure_service=closure,
    )


def _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata):
    """Advance a fresh project to: final BRD + HLD + LLD + US + final Test Cases.
    Closure report absent. All finalization happens OUTSIDE the graph."""
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # HLD + US
    sa.choose_final_hld(1)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # LLD
    lld.choose_final_lld(1)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # test cases
    tc.choose_final(1)


# --- A. exact topology (full end-to-end shape, incl. the Closure hop) -----

def test_topology_is_the_8b7_shape(stub_ba_agent):
    ba, sa, us, lld, tc, closure = _svcs(ba_agent=stub_ba_agent)
    g = build_sdlc_graph(
        ba, sa_service=sa, us_service=us, lld_service=lld,
        tc_service=tc, closure_service=closure,
    ).get_graph()

    assert set(g.nodes) == {
        "__start__", "resolve_state", "ensure_brd", "gate_brd",
        "ensure_hld", "ensure_user_stories", "gate_hld",
        "ensure_lld", "gate_lld",
        "ensure_test_cases", "gate_test_cases",
        "ensure_closure_report", "gate_closure_report", "__end__",
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
        ("ensure_closure_report", "gate_closure_report"),
    }
    cond = {(e.source, e.target) for e in g.edges if e.conditional}
    assert ("gate_test_cases", "__end__") in cond              # tc awaiting_approval -> END
    assert ("gate_test_cases", "ensure_closure_report") in cond  # complete -> Closure hop
    assert ("gate_closure_report", "__end__") in cond          # both closure-gate routes end


def test_route_after_gate_closure_report_maps_both_outcomes():
    assert _route_after_gate_closure_report({"status": "complete"}) == "complete"
    assert _route_after_gate_closure_report({"status": "awaiting_approval"}) == "awaiting_approval"
    assert _route_after_gate_closure_report({}) == "awaiting_approval"  # safe default


def test_gate_closure_report_node_outcomes():
    assert _gate_closure_report_node({"closure_final_version": None}) == {
        "status": "awaiting_approval", "awaiting": "closure_final",
    }
    assert _gate_closure_report_node({"closure_final_version": 1}) == {
        "status": "complete", "awaiting": None,
    }


# --- B. SDLCState declares the closure_* fields -------------------------

def test_sdlc_state_declares_closure_fields():
    assert {"closure_latest_version", "closure_final_version"} <= set(SDLCState.__annotations__)


# --- C. resolve_state reads the closure pointers ----------------------

def test_resolve_state_reads_closure_pointers(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    closure.generate()  # closure v1, NOT final

    out = _make_resolve_state_node(ba, sa, us, lld, tc, closure)({})
    assert out["tc_final_version"] == 1
    assert out["closure_latest_version"] == 1
    assert out["closure_final_version"] is None


# --- D. final test-case prerequisite (graph never reaches closure early) --

def test_no_final_test_cases_never_generates_closure(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # HLD + US
    sa.choose_final_hld(1)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # LLD
    lld.choose_final_lld(1)
    s = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # test cases v1, NOT final

    assert s["status"] == "awaiting_approval" and s["awaiting"] == "tc_final"
    assert closure.get_all_versions() == []
    assert stub_closure_agent.calls == []


# --- E. closure generation after final test cases -------------------------

def test_closure_report_generated_after_final_test_cases(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    s = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)

    assert s["produced"] == {"closure": 1}
    assert s["closure_latest_version"] == 1
    assert s["status"] == "awaiting_approval" and s["awaiting"] == "closure_final"
    v1 = closure.get_version(1)
    assert v1.source == "initial"
    assert v1.source_ref == "brd_v1;hld_v1;lld_v1;us_v1;tc_v1"
    assert len(stub_closure_agent.calls) == 1


# --- F. end-to-end continuation after test-case approval -----------------

def test_run_from_final_brd_through_to_closure_gate(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)   # HLD + US
    sa.choose_final_hld(1)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)   # LLD
    lld.choose_final_lld(1)
    s3 = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # test cases
    assert s3["produced"] == {"tc": 1} and s3["awaiting"] == "tc_final"

    tc.choose_final(1)                                                    # OUTSIDE the graph
    s4 = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # closure report
    assert s4["produced"] == {"closure": 1}
    assert s4["status"] == "awaiting_approval" and s4["awaiting"] == "closure_final"
    assert [v.version for v in closure.get_all_versions()] == [1]


# --- G. idempotency: second run does not regenerate ---------------------

def test_second_run_does_not_regenerate_closure_report(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata, isolated_output_dir,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # closure v1

    cr_file = isolated_output_dir / PID / "closure_report" / "versions.json"
    before = cr_file.read_bytes()

    s2 = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    assert s2["produced"] == {}
    assert len(stub_closure_agent.calls) == 1
    assert [v.version for v in closure.get_all_versions()] == [1]
    assert cr_file.read_bytes() == before


def test_repeated_runs_after_closure_approval_are_complete_and_stable(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata, isolated_output_dir,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # closure v1
    closure.choose_final(1)                                             # human approval

    proj = isolated_output_dir / PID
    before = {p: p.read_bytes() for p in proj.rglob("versions.json")}

    for _ in range(3):
        s = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
        assert s["status"] == "complete" and s["awaiting"] is None
        assert s["produced"] == {}

    assert {p: p.read_bytes() for p in proj.rglob("versions.json")} == before
    assert len(stub_closure_agent.calls) == 1


# --- H. the graph never finalizes the closure report -------------------

def test_graph_never_finalizes_closure_on_generation(
    monkeypatch, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent,
    stub_tc_agent, stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # finalizes upstream, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))
    monkeypatch.setattr(
        ClosureReportService, "choose_final",
        lambda self, n: calls.append(("choose_final", n)),
    )

    s = _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    assert s["produced"] == {"closure": 1}
    assert s["awaiting"] == "closure_final"
    assert calls == []  # the graph finalized nothing


# --- I. failure propagation ------------------------------------------

def test_closure_agent_error_propagates(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    class _Boom:
        def synthesize_narrative(self, *a, **k):
            raise ClosureReportAgentError("Closure Gemini exploded")

    ba, sa, us, lld, tc, _ = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent,
    )
    closure = ClosureReportService(project_id=PID, agent=_Boom())
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)

    with pytest.raises(ClosureReportAgentError):
        _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    assert closure.get_all_versions() == []


# --- J. persistence parity: graph == direct ClosureReportService.generate() --

def test_graph_closure_matches_direct_service(sow_file, sample_metadata):
    # direct
    d_ba = BusinessAnalystService(project_id="clr_parity_direct", agent=StubBAAgent())
    d_ba.generate_initial_brd(sow_file, sample_metadata); d_ba.choose_final_brd(1)
    d_sa = SolutionArchitectService(project_id="clr_parity_direct", ba_service=d_ba, agent=StubSAAgent())
    d_sa.generate_initial_hld(); d_sa.choose_final_hld(1)
    d_us = InitialUserStoryService(project_id="clr_parity_direct", ba_service=d_ba, agent=StubUserStoryAgent())
    d_us.generate_initial_stories()
    d_lld = LowLevelDesignService(project_id="clr_parity_direct", sa_service=d_sa, ba_service=d_ba, agent=StubLLDAgent())
    d_lld.generate_initial_lld(); d_lld.choose_final_lld(1)
    d_tc = TestCaseService(project_id="clr_parity_direct", agent=StubTestCaseAgent())
    d_tc.generate(); d_tc.choose_final(1)
    d_cr = ClosureReportService(project_id="clr_parity_direct", agent=StubClosureReportAgent())
    direct = d_cr.generate()

    # graph
    g_ba, g_sa, g_us, g_lld, g_tc, g_cr = _svcs("clr_parity_graph")
    g_ba.generate_initial_brd(sow_file, sample_metadata); g_ba.choose_final_brd(1)
    _run("clr_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, g_cr, sow_file, sample_metadata)
    g_sa.choose_final_hld(1)
    _run("clr_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, g_cr, sow_file, sample_metadata)
    g_lld.choose_final_lld(1)
    _run("clr_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, g_cr, sow_file, sample_metadata)
    g_tc.choose_final(1)
    _run("clr_parity_graph", g_ba, g_sa, g_us, g_lld, g_tc, g_cr, sow_file, sample_metadata)
    graphed = g_cr.get_version(1)

    # content is identical once the (expected) differing project id is normalised
    assert (graphed.content.replace("clr_parity_graph", "PID")
            == direct.content.replace("clr_parity_direct", "PID"))
    assert graphed.version == direct.version == 1
    assert graphed.source == direct.source == "initial"
    assert graphed.source_ref == direct.source_ref == "brd_v1;hld_v1;lld_v1;us_v1;tc_v1"
    assert graphed.note == direct.note


# --- K. persistence streams ----------------------------------------

def test_only_expected_streams_written_through_closure(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata, isolated_output_dir,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # closure v1

    proj = isolated_output_dir / PID
    written = sorted(str(p.relative_to(proj)).replace("\\", "/") for p in proj.rglob("versions.json"))
    assert written == [
        "closure_report/versions.json", "hld/versions.json", "lld/versions.json",
        "test_cases/versions.json", "user_stories/versions.json", "versions.json",
    ]
    for rec_file in proj.rglob("versions.json"):
        for rec in json.loads(rec_file.read_text(encoding="utf-8")):
            assert isinstance(BRDVersion(**rec), BRDVersion)


# --- L. sdlc_status (extended) -----------------------------------------

def _status(pid, ba, sa, us, lld, tc, closure):
    return sdlc_status(
        pid, ba_service=ba, sa_service=sa, us_service=us, lld_service=lld,
        tc_service=tc, closure_service=closure,
    )


def test_status_empty_project_has_closure_keys(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    st = _status(PID, ba, sa, us, lld, tc, closure)
    assert st["closure_exists"] is False
    assert st["closure_latest_version"] is None
    assert st["closure_final_version"] is None
    assert st["awaiting_closure_approval"] is False


def test_status_final_tc_no_closure_next_step_generate_closure(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    st = _status(PID, ba, sa, us, lld, tc, closure)
    assert st["tc_final_version"] == 1
    assert st["closure_exists"] is False
    assert st["next_step"] == NEXT_GENERATE_CLOSURE_REPORT


def test_status_closure_exists_not_final(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # closure v1
    st = _status(PID, ba, sa, us, lld, tc, closure)
    assert st["closure_exists"] is True and st["closure_latest_version"] == 1
    assert st["closure_final_version"] is None
    assert st["awaiting_closure_approval"] is True
    assert st["next_step"] == NEXT_APPROVE_CLOSURE_REPORT


def test_status_closure_final_is_terminal(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    closure.choose_final(1)
    st = _status(PID, ba, sa, us, lld, tc, closure)
    assert st["closure_final_version"] == 1
    assert st["awaiting_closure_approval"] is False
    assert st["next_step"] is NEXT_NONE


def test_status_repeated_calls_are_side_effect_free(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata, isolated_output_dir,
):
    ba, sa, us, lld, tc, closure = _svcs(
        ba_agent=stub_ba_agent, sa_agent=stub_sa_agent, us_agent=stub_us_agent,
        lld_agent=stub_lld_agent, tc_agent=stub_tc_agent, closure_agent=stub_closure_agent,
    )
    _to_final_tc(ba, sa, us, lld, tc, closure, sow_file, sample_metadata)
    _run(PID, ba, sa, us, lld, tc, closure, sow_file, sample_metadata)  # closure v1

    proj = isolated_output_dir / PID
    before = {p: p.read_bytes() for p in proj.rglob("versions.json")}
    closure_calls = len(stub_closure_agent.calls)

    for _ in range(3):
        _status(PID, ba, sa, us, lld, tc, closure)

    assert {p: p.read_bytes() for p in proj.rglob("versions.json")} == before
    assert len(stub_closure_agent.calls) == closure_calls
