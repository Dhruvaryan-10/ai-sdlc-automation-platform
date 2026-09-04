"""
Phase 8B-1 — smallest full-SDLC LangGraph skeleton.

    START -> resolve_state -> ensure_brd -> gate_brd -> END

Proves the orchestration skeleton delegates to the EXISTING BusinessAnalystService,
never finalizes, keeps VersionService as the only writer, and that
`sdlc_status(project_id)` is a pure read.

Deterministic: the BA LLM is a stub injected via `agent=` / `ba_service=`. No
Gemini calls.
"""

import json

import pytest

from app.agents.business_analyst.agent import BusinessAnalystAgentError
from app.agents.business_analyst.service import (
    BusinessAnalystService,
    EmptyDocumentError,
    UnsupportedFileTypeError,
)
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.solution_architect.service import SolutionArchitectService
from app.orchestration.graph import (
    _gate_brd_node,
    _make_ensure_brd_node,
    _make_resolve_state_node,
    _route_after_gate_brd,
    build_sdlc_graph,
    run_step,
)
from app.orchestration.state import SDLCState
from app.orchestration.status import (
    NEXT_APPROVE_BRD,
    NEXT_GENERATE_BRD,
    NEXT_GENERATE_HLD,
    sdlc_status,
)
from app.services.version_service import BRDVersion, VersionService
from tests.conftest import STUB_BRD, StubBAAgent, StubSAAgent, StubUserStoryAgent

PID = "sdlc8b1"


def _ba(pid=PID, agent=None):
    return BusinessAnalystService(project_id=pid, agent=agent or StubBAAgent())


# --- 1. graph topology --------------------------------------------------

def test_graph_topology_brd_subpath_is_intact():
    """The BRD hop (8B-1 invariant) must survive graph extensions.

    Exact end-to-end topology is asserted in test_sdlc_graph_8b2.py; here we only
    pin the BRD sub-path: START -> resolve_state -> ensure_brd -> gate_brd, with
    gate_brd conditionally routing to END on awaiting_approval.
    """
    compiled = build_sdlc_graph(_ba())
    g = compiled.get_graph()

    assert {"__start__", "resolve_state", "ensure_brd", "gate_brd", "__end__"} <= set(g.nodes)
    plain = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert {
        ("__start__", "resolve_state"),
        ("resolve_state", "ensure_brd"),
        ("ensure_brd", "gate_brd"),
    } <= plain
    cond = {(e.source, e.target) for e in g.edges if e.conditional}
    assert ("gate_brd", "__end__") in cond   # awaiting_approval -> END
    assert type(compiled).__name__ == "CompiledStateGraph"


def test_route_after_gate_brd_maps_both_outcomes():
    assert _route_after_gate_brd({"status": "complete"}) == "complete"
    assert _route_after_gate_brd({"status": "awaiting_approval"}) == "awaiting_approval"
    assert _route_after_gate_brd({}) == "awaiting_approval"  # safe default


def test_sdlc_state_fields():
    assert set(SDLCState.__annotations__) == {
        "project_id", "sow_path", "metadata", "request",
        "brd_latest_version", "brd_final_version",
        "hld_latest_version", "hld_final_version", "us_latest_version",  # 8B-2
        "produced", "status", "awaiting",
    }


# --- 2. resolve_state (BRD pointers; 8B-2 adds hld_*/us_latest which are None here) ---

def _resolve_node(svc):
    """resolve_state bound with stub SA / US services (no Gemini)."""
    return _make_resolve_state_node(
        svc,
        SolutionArchitectService(project_id=svc.project_id, ba_service=svc, agent=StubSAAgent()),
        InitialUserStoryService(project_id=svc.project_id, ba_service=svc, agent=StubUserStoryAgent()),
    )


def test_resolve_state_empty_project(stub_ba_agent):
    svc = _ba(agent=stub_ba_agent)
    out = _resolve_node(svc)({})
    assert out == {
        "brd_latest_version": None, "brd_final_version": None,
        "hld_latest_version": None, "hld_final_version": None, "us_latest_version": None,
    }


def test_resolve_state_with_brd_but_no_final(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)          # v1, not final
    out = _resolve_node(svc)({})
    assert out["brd_latest_version"] == 1 and out["brd_final_version"] is None
    assert out["hld_latest_version"] is None and out["us_latest_version"] is None


def test_resolve_state_with_final_brd(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)
    svc.choose_final_brd(1)
    out = _resolve_node(svc)({})
    assert out["brd_latest_version"] == 1 and out["brd_final_version"] == 1


def test_resolve_state_is_read_only_and_repeatable(stub_ba_agent, sow_file, sample_metadata, isolated_output_dir):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)
    before = (isolated_output_dir / PID / "versions.json").read_bytes()
    node = _resolve_node(svc)
    node({}); node({}); node({})
    assert (isolated_output_dir / PID / "versions.json").read_bytes() == before


# --- 3. ensure_brd ---------------------------------------------------

def test_ensure_brd_generates_when_no_brd_exists(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    state = {"request": "ensure_brd", "sow_path": str(sow_file), "metadata": sample_metadata,
             "brd_latest_version": None, "produced": {}}
    out = _make_ensure_brd_node(svc)(state)

    assert out["produced"] == {"brd": 1}
    assert out["brd_latest_version"] == 1
    assert len(stub_ba_agent.generate_calls) == 1          # existing agent called exactly once
    assert svc.get_all_versions()[0].version == 1


def test_ensure_brd_is_noop_when_brd_already_exists(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)     # v1 exists (1 generate call)
    state = {"request": "ensure_brd", "sow_path": str(sow_file), "metadata": sample_metadata,
             "brd_latest_version": 1, "produced": {}}
    out = _make_ensure_brd_node(svc)(state)

    assert out == {}                                        # no state update
    assert len(stub_ba_agent.generate_calls) == 1          # NOT called again
    assert len(svc.get_all_versions()) == 1


def test_ensure_brd_requires_inputs_when_no_brd_exists(stub_ba_agent):
    svc = _ba(agent=stub_ba_agent)
    node = _make_ensure_brd_node(svc)
    with pytest.raises(ValueError, match="sow_path and metadata"):
        node({"request": "ensure_brd", "brd_latest_version": None, "produced": {}})


def test_run_step_records_produced_version(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    s = run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    assert s["produced"] == {"brd": 1}
    assert s["brd_latest_version"] == 1


def test_run_step_noop_second_call_does_not_regenerate(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    s2 = run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    assert s2["produced"] == {}
    assert len(stub_ba_agent.generate_calls) == 1
    assert [v.version for v in svc.get_all_versions()] == [1]


# --- 4. gate_brd ---------------------------------------------------

def test_gate_brd_no_final_is_awaiting_approval():
    assert _gate_brd_node({"brd_final_version": None}) == {
        "status": "awaiting_approval", "awaiting": "brd_final",
    }


def test_gate_brd_final_present_is_complete():
    assert _gate_brd_node({"brd_final_version": 1}) == {
        "status": "complete", "awaiting": None,
    }


def test_run_step_gate_brd_stops_at_end_when_brd_not_final(stub_ba_agent, sow_file, sample_metadata):
    """8B-1 BRD-gate invariant: with no final BRD the run stops at gate_brd -> END,
    awaiting brd_final. (After a final BRD the run continues into the 8B-2 HLD hop;
    that path is covered by test_sdlc_graph_8b2.py.)"""
    svc = _ba(agent=stub_ba_agent)
    s1 = run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    assert s1["status"] == "awaiting_approval" and s1["awaiting"] == "brd_final"

    svc.choose_final_brd(1)  # human finalization, via the service — NOT the graph
    # BRD is final -> gate_brd routes onward; stub SA/US injected so no Gemini.
    s2 = run_step(
        PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=svc,
        sa_service=SolutionArchitectService(project_id=PID, ba_service=svc, agent=StubSAAgent()),
        us_service=InitialUserStoryService(project_id=PID, ba_service=svc, agent=StubUserStoryAgent()),
    )
    assert s2["status"] == "awaiting_approval" and s2["awaiting"] == "hld_final"


# --- 5. approval invariant ---------------------------------------

def test_graph_never_finalizes_on_generation(monkeypatch, stub_ba_agent, sow_file, sample_metadata):
    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(
        BusinessAnalystService, "choose_final_brd",
        lambda self, n: calls.append(("choose_final_brd", n)),
    )
    svc = _ba(agent=stub_ba_agent)
    s = run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)

    assert s["produced"] == {"brd": 1}
    assert calls == []  # the graph finalized nothing


def test_graph_never_finalizes_even_when_final_brd_already_exists(monkeypatch, stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    svc.choose_final_brd(1)  # test finalizes, before patching

    calls: list = []
    monkeypatch.setattr(VersionService, "mark_final", lambda self, n: calls.append(("mark_final", n)))
    monkeypatch.setattr(VersionService, "unlock_final", lambda self: calls.append("unlock_final"))

    # BRD final -> the run continues into the 8B-2 HLD hop; stub SA/US -> no Gemini.
    s = run_step(
        PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=svc,
        sa_service=SolutionArchitectService(project_id=PID, ba_service=svc, agent=StubSAAgent()),
        us_service=InitialUserStoryService(project_id=PID, ba_service=svc, agent=StubUserStoryAgent()),
    )
    assert s["status"] == "awaiting_approval" and s["awaiting"] == "hld_final"
    assert calls == []  # the graph finalized nothing (BRD or HLD)


# --- 6. parity: graph BRD generation == direct BusinessAnalystService ----

def test_graph_generation_matches_direct_service(stub_ba_agent, sow_file, sample_metadata):
    direct_svc = BusinessAnalystService(project_id="parity_direct", agent=StubBAAgent())
    direct = direct_svc.generate_initial_brd(sow_file, sample_metadata)

    graph_svc = BusinessAnalystService(project_id="parity_graph", agent=StubBAAgent())
    run_step("parity_graph", "ensure_brd", sow_path=str(sow_file),
             metadata=sample_metadata, ba_service=graph_svc)
    graphed = graph_svc.get_version(1)

    assert graphed.content == direct.content
    assert graphed.source == direct.source == "initial"
    assert graphed.source_ref == direct.source_ref is None
    assert graphed.note == direct.note == "Generated from SOW"
    assert graphed.version == direct.version == 1


# --- 7. failure propagation (exceptions escape compiled.invoke()) ------

def test_unsupported_file_type_propagates(stub_ba_agent, tmp_path, sample_metadata):
    bad = tmp_path / "sow.rtf"
    bad.write_text("hello", encoding="utf-8")
    svc = _ba(agent=stub_ba_agent)
    with pytest.raises(UnsupportedFileTypeError):
        run_step(PID, "ensure_brd", sow_path=str(bad), metadata=sample_metadata, ba_service=svc)
    assert svc.get_all_versions() == []


def test_empty_document_propagates(stub_ba_agent, tmp_path, sample_metadata):
    empty = tmp_path / "sow.txt"
    empty.write_text("   \n\t\n", encoding="utf-8")
    svc = _ba(agent=stub_ba_agent)
    with pytest.raises(EmptyDocumentError):
        run_step(PID, "ensure_brd", sow_path=str(empty), metadata=sample_metadata, ba_service=svc)
    assert svc.get_all_versions() == []


def test_business_analyst_agent_error_propagates(sow_file, sample_metadata):
    class _BoomAgent:
        def generate_brd(self, clean_sow, metadata):
            raise BusinessAnalystAgentError("Gemini exploded")

    svc = BusinessAnalystService(project_id=PID, agent=_BoomAgent())
    with pytest.raises(BusinessAnalystAgentError):
        run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    assert svc.get_all_versions() == []


# --- 8. persistence: only BRD versions.json, VersionService is the writer --

def test_only_brd_versions_json_is_written(stub_ba_agent, sow_file, sample_metadata, isolated_output_dir):
    svc = _ba(agent=stub_ba_agent)
    run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)

    proj = isolated_output_dir / PID
    assert (proj / "versions.json").exists()
    other_streams = [p for p in proj.rglob("versions.json") if p != proj / "versions.json"]
    assert other_streams == []

    records = json.loads((proj / "versions.json").read_text(encoding="utf-8"))
    assert len(records) == 1
    assert set(records[0]) == {
        "version", "content", "source", "created_at", "note",
        "is_final", "is_locked", "source_ref",
    }
    assert records[0]["source"] == "initial" and records[0]["note"] == "Generated from SOW"
    assert isinstance(BRDVersion(**records[0]), BRDVersion)  # round-trips through the model


def test_noop_run_preserves_existing_records_byte_for_byte(stub_ba_agent, sow_file, sample_metadata, isolated_output_dir):
    svc = _ba(agent=stub_ba_agent)
    run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    vfile = isolated_output_dir / PID / "versions.json"
    before = vfile.read_bytes()

    run_step(PID, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata, ba_service=svc)
    assert vfile.read_bytes() == before


# --- 9. sdlc_status (pure read) --------------------------------

def test_sdlc_status_empty_project(stub_ba_agent):
    svc = _ba(agent=stub_ba_agent)
    st = sdlc_status(PID, ba_service=svc)
    assert st == {
        "project_id": PID,
        "brd_exists": False,
        "brd_latest_version": None,
        "brd_final_version": None,
        "awaiting_brd_approval": False,
        "hld_exists": False,
        "hld_latest_version": None,
        "hld_final_version": None,
        "awaiting_hld_approval": False,
        "us_exists": False,
        "us_latest_version": None,
        "next_step": NEXT_GENERATE_BRD,
    }


def test_sdlc_status_brd_not_final(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)
    st = sdlc_status(PID, ba_service=svc)
    assert st["brd_exists"] is True
    assert st["brd_latest_version"] == 1
    assert st["brd_final_version"] is None
    assert st["awaiting_brd_approval"] is True
    assert st["next_step"] == NEXT_APPROVE_BRD


def test_sdlc_status_brd_final(stub_ba_agent, sow_file, sample_metadata):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)
    svc.choose_final_brd(1)
    st = sdlc_status(PID, ba_service=svc)
    assert st["brd_final_version"] == 1
    assert st["awaiting_brd_approval"] is False
    assert st["hld_exists"] is False
    # BRD is final but no HLD yet -> the pipeline's next runnable step is the HLD hop
    assert st["next_step"] == NEXT_GENERATE_HLD


def test_sdlc_status_is_pure_no_writes(stub_ba_agent, sow_file, sample_metadata, isolated_output_dir):
    svc = _ba(agent=stub_ba_agent)
    svc.generate_initial_brd(sow_file, sample_metadata)
    vfile = isolated_output_dir / PID / "versions.json"
    before = vfile.read_bytes()

    for _ in range(3):
        sdlc_status(PID, ba_service=svc)

    assert vfile.read_bytes() == before
    assert len(stub_ba_agent.generate_calls) == 1  # never generated
